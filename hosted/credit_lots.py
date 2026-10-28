"""Origin-preserving credit lots and strict, server-owned reservation allocation.

Unclassified and unapproved balances remain visible but are ineligible for
automatic discount checkout. These functions share the caller's PostgreSQL
transaction; customer locking serializes every balance/lot/hold mutation.
"""
import uuid
from .security import ServiceError

UNKNOWN_ORIGINS = frozenset(('unknown', 'legacy_unknown', 'opening_unknown', 'adjustment_unknown',
                            'trade_unreviewed', 'accepted_trade_unreviewed', 'trade_unknown'))
TAX_BASES = frozenset(('MODIFY_TAX_BASIS', 'DO_NOT_MODIFY_TAX_BASIS'))


def _positive(value):
    return type(value) is int and 0 < value <= 100_000_000


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 255


def eligible(lot, binding):
    """Evaluate only server-retrieved policy rows, never browser approval flags."""
    return (lot.get('policy_approved') is True and not lot.get('policy_revoked') and
            lot.get('approver_role') == 'owner' and lot.get('approved_at') is not None and
            _text(lot.get('approval_reference')) and _text(lot.get('policy_reference')) and
            _text(lot.get('origin_code')) and lot['origin_code'].strip().casefold() not in UNKNOWN_ORIGINS and
            _text(lot.get('policy_id')) and type(lot.get('policy_version')) is int and lot['policy_version'] > 0 and
            lot.get('origin_code') == lot.get('policy_origin_code') and
            lot.get('policy_currency') == binding.get('currency') == 'USD' and
            lot.get('policy_discount_id') == binding.get('discount_id') and
            lot.get('policy_catalog_version') == binding.get('discount_catalog_version') and
            lot.get('policy_tax_basis') == binding.get('discount_tax_basis') and
            binding.get('discount_tax_basis') in TAX_BASES)


def choose_allocations(lots, amount_cents, binding):
    """Choose FIFO from already ordered rows with approved compatible policies."""
    if not _positive(amount_cents):
        raise ServiceError('A positive integer credit amount is required.')
    if (not isinstance(binding, dict) or binding.get('currency') != 'USD' or not _text(binding.get('discount_id')) or
            type(binding.get('discount_catalog_version')) is not int or binding['discount_catalog_version'] < 1 or
            binding.get('discount_tax_basis') not in TAX_BASES):
        raise ServiceError('A verified compatible checkout catalog binding is required.', 409)
    remaining = amount_cents; result = []
    for lot in lots:
        available = lot.get('available_cents')
        if type(available) is not int or available < 0:
            raise ServiceError('Credit lot availability is inconsistent. Manager review is required.', 409)
        if not eligible(lot, binding) or not available:
            continue
        used = min(available, remaining)
        result.append({'lot_id': str(lot['id']), 'source_entry_id': str(lot['source_entry_id']),
                       'origin_code': lot['origin_code'], 'policy_id': lot['policy_id'],
                       'policy_version': lot['policy_version'], 'policy_reference': lot['policy_reference'],
                       'amount_cents': used})
        remaining -= used
        if not remaining:
            return result
    raise ServiceError('Verified credit with an approved compatible origin policy is insufficient. Unknown-origin credit requires review.', 409)


def _customer(db, tenant, customer_id):
    row = db.execute('SELECT * FROM ss_customers WHERE tenant_id=%s AND id=%s FOR UPDATE',
                     (tenant, customer_id)).fetchone()
    if not row:
        raise ServiceError('Customer not found.', 404)
    return row


def _entry(db, tenant, customer_id, entry_id):
    row = db.execute('SELECT * FROM ss_credit_entries WHERE tenant_id=%s AND customer_id=%s AND id=%s',
                     (tenant, customer_id, entry_id)).fetchone()
    if not row:
        raise ServiceError('Credit source entry not found.', 404)
    return row


def _lots(db, tenant, customer_id):
    rows = db.execute('''SELECT l.*,p.approved AS policy_approved,p.origin_code AS policy_origin_code,
                         p.policy_reference,p.currency AS policy_currency,p.discount_id AS policy_discount_id,
                         p.discount_catalog_version AS policy_catalog_version,p.discount_tax_basis AS policy_tax_basis,
                         p.approved_at,p.approval_reference,s.role AS approver_role,
                         (v.policy_id IS NOT NULL) AS policy_revoked
                         FROM ss_credit_lots l LEFT JOIN ss_credit_origin_policies p
                         ON p.tenant_id=l.tenant_id AND p.policy_id=l.policy_id AND p.version=l.policy_version
                         LEFT JOIN ss_credit_policy_revocations v ON v.tenant_id=p.tenant_id
                         AND v.policy_id=p.policy_id AND v.policy_version=p.version
                         LEFT JOIN ss_staff s ON s.tenant_id=p.tenant_id AND s.id=p.approved_by
                         WHERE l.tenant_id=%s AND l.customer_id=%s ORDER BY l.created_at,l.id FOR UPDATE OF l''',
                      (tenant, customer_id)).fetchall()
    held = db.execute('''SELECT a.lot_id,SUM(a.reserved_cents) AS held FROM ss_credit_lot_reservations a
                         JOIN ss_reservations r ON r.tenant_id=a.tenant_id AND r.id=a.reservation_id
                         WHERE r.tenant_id=%s AND r.customer_id=%s AND r.state IN ('reserved','exception')
                         GROUP BY a.lot_id''', (tenant, customer_id)).fetchall()
    holds = {row['lot_id']: int(row['held']) for row in held}
    return [dict(row) | {'available_cents': row['remaining_cents'] - holds.get(row['id'], 0)} for row in rows]


def _coverage(lots, balance):
    if sum(lot['remaining_cents'] for lot in lots) != balance:
        raise ServiceError('The balance has incomplete credit-origin history. Manager-reviewed migration is required.', 409)


def record_credit_lot(db, tenant_id, customer_id, entry_id, *, origin_code='unknown', policy_id=None, policy_version=None):
    """After a positive ledger entry, create its lot without inferring approval."""
    customer = _customer(db, tenant_id, customer_id)
    entry = _entry(db, tenant_id, customer_id, entry_id)
    if not _positive(entry['amount_cents']):
        raise ServiceError('Only a positive credit entry can issue a credit lot.')
    existing = db.execute('SELECT * FROM ss_credit_lots WHERE tenant_id=%s AND source_entry_id=%s ORDER BY lot_index',
                          (tenant_id, entry_id)).fetchall()
    if existing:
        if sum(row['issued_cents'] for row in existing) != entry['amount_cents']:
            raise ServiceError('Existing source lots do not match the credit entry.', 409)
        return [str(row['id']) for row in existing]
    if not _text(origin_code) or ((policy_id is None) != (policy_version is None)):
        raise ServiceError('Complete credit-origin policy bindings are required.')
    if policy_id is not None:
        policy = db.execute('SELECT * FROM ss_credit_origin_policies WHERE tenant_id=%s AND policy_id=%s AND version=%s',
                            (tenant_id, policy_id, policy_version)).fetchone()
        if not policy or policy['origin_code'] != origin_code:
            raise ServiceError('Credit origin does not match the selected policy.', 409)
    lots = _lots(db, tenant_id, customer_id)
    _coverage(lots, customer['balance_cents'] - entry['amount_cents'])
    ident = uuid.uuid4()
    db.execute('''INSERT INTO ss_credit_lots(id,tenant_id,customer_id,source_entry_id,origin_code,policy_id,policy_version,issued_cents,remaining_cents)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
               (ident, tenant_id, customer_id, entry_id, origin_code, policy_id, policy_version, entry['amount_cents'], entry['amount_cents']))
    db.execute("INSERT INTO ss_credit_lot_mutations(tenant_id,entry_id,lot_id,amount_cents,kind) VALUES (%s,%s,%s,%s,'issue')",
               (tenant_id, entry_id, ident, entry['amount_cents']))
    return [str(ident)]


def debit_available_lots(db, tenant_id, customer_id, entry_id):
    """After a negative manual adjustment, burn FIFO available lots, retaining origin history."""
    customer = _customer(db, tenant_id, customer_id)
    entry = _entry(db, tenant_id, customer_id, entry_id)
    value = -entry['amount_cents']
    if not _positive(value):
        raise ServiceError('A negative adjustment entry is required.')
    if db.execute('SELECT 1 FROM ss_credit_lot_mutations WHERE tenant_id=%s AND entry_id=%s', (tenant_id, entry_id)).fetchone():
        return
    lots = _lots(db, tenant_id, customer_id)
    _coverage(lots, customer['balance_cents'] + value)
    remaining = value
    for lot in lots:
        if lot['available_cents'] < 0:
            raise ServiceError('Credit lot availability is inconsistent.', 409)
        used = min(remaining, lot['available_cents'])
        if used:
            db.execute('UPDATE ss_credit_lots SET remaining_cents=remaining_cents-%s WHERE tenant_id=%s AND id=%s',
                       (used, tenant_id, lot['id']))
            db.execute("INSERT INTO ss_credit_lot_mutations(tenant_id,entry_id,lot_id,amount_cents,kind) VALUES (%s,%s,%s,%s,'manual_adjustment')",
                       (tenant_id, entry_id, lot['id'], -used))
            remaining -= used
        if not remaining:
            return
    raise ServiceError('The adjustment would consume reserved or untracked credit.', 409)


def reserve_lots(db, tenant_id, customer_id, reservation_id, amount_cents, binding):
    customer = _customer(db, tenant_id, customer_id)
    if db.execute('SELECT 1 FROM ss_credit_lot_reservations WHERE tenant_id=%s AND reservation_id=%s',
                  (tenant_id, reservation_id)).fetchone():
        raise ServiceError('This reservation already has credit-origin allocations.', 409)
    lots = _lots(db, tenant_id, customer_id)
    _coverage(lots, customer['balance_cents'])
    allocations = choose_allocations(lots, amount_cents, binding)
    for allocated in allocations:
        db.execute('INSERT INTO ss_credit_lot_reservations(tenant_id,reservation_id,lot_id,reserved_cents) VALUES (%s,%s,%s,%s)',
                   (tenant_id, reservation_id, allocated['lot_id'], allocated['amount_cents']))
    return allocations


def consume_lots(db, tenant_id, customer_id, reservation_id, entry_id, allocations):
    customer = _customer(db, tenant_id, customer_id)
    entry = _entry(db, tenant_id, customer_id, entry_id)
    value = -entry['amount_cents']
    if not _positive(value):
        raise ServiceError('A negative redemption entry is required.')
    lots = _lots(db, tenant_id, customer_id)
    _coverage(lots, customer['balance_cents'] + value)
    by_id = {str(lot['id']): lot for lot in lots}
    rows = db.execute('SELECT * FROM ss_credit_lot_reservations WHERE tenant_id=%s AND reservation_id=%s FOR UPDATE',
                      (tenant_id, reservation_id)).fetchall()
    held = {str(row['lot_id']): row for row in rows}
    if len(rows) != len(allocations) or sum(row['reserved_cents'] for row in rows) < value:
        raise ServiceError('Reserved lot evidence is incomplete.', 409)
    remaining = value; consumed = []
    for allocation in allocations:
        ident = allocation['lot_id']; lot = by_id.get(ident); reserved = held.get(ident)
        if not lot or not reserved or reserved['reserved_cents'] != allocation['amount_cents'] or reserved['consumed_cents'] or reserved['restored_cents']:
            raise ServiceError('Reserved lot evidence is inconsistent.', 409)
        if (str(lot['source_entry_id']) != allocation['source_entry_id'] or lot['origin_code'] != allocation['origin_code'] or
                lot['policy_id'] != allocation['policy_id'] or lot['policy_version'] != allocation['policy_version'] or
                lot['policy_reference'] != allocation['policy_reference']):
            raise ServiceError('Reserved lot origin changed after authorization.', 409)
        used = min(remaining, allocation['amount_cents'])
        # available_cents excludes this reservation too; add only its own hold.
        if used > lot['available_cents'] + reserved['reserved_cents']:
            raise ServiceError('Reserved lot was consumed outside its reservation.', 409)
        db.execute('UPDATE ss_credit_lot_reservations SET consumed_cents=%s WHERE tenant_id=%s AND reservation_id=%s AND lot_id=%s',
                   (used, tenant_id, reservation_id, lot['id']))
        if used:
            db.execute('UPDATE ss_credit_lots SET remaining_cents=remaining_cents-%s WHERE tenant_id=%s AND id=%s',
                       (used, tenant_id, lot['id']))
            db.execute("INSERT INTO ss_credit_lot_mutations(tenant_id,entry_id,lot_id,amount_cents,kind) VALUES (%s,%s,%s,%s,'discount_redeem')",
                       (tenant_id, entry_id, lot['id'], -used))
            consumed.append(dict(allocation) | {'amount_cents': used})
            remaining -= used
    if remaining:
        raise ServiceError('Reserved credit origins cannot cover this redemption.', 409)
    return consumed


def restore_lots(db, tenant_id, customer_id, reservation_id, entry_id, consumed_allocations):
    customer = _customer(db, tenant_id, customer_id)
    entry = _entry(db, tenant_id, customer_id, entry_id)
    value = entry['amount_cents']
    if not _positive(value):
        raise ServiceError('A positive restoration entry is required.')
    lots = _lots(db, tenant_id, customer_id)
    _coverage(lots, customer['balance_cents'] - value)
    originals = {str(lot['id']): lot for lot in lots}
    rows = db.execute('SELECT * FROM ss_credit_lot_reservations WHERE tenant_id=%s AND reservation_id=%s FOR UPDATE',
                      (tenant_id, reservation_id)).fetchall()
    held = {str(row['lot_id']): row for row in rows}
    remaining = value; result = []
    for original in consumed_allocations:
        lot = originals.get(original['lot_id']); row = held.get(original['lot_id'])
        if not lot or not row or row['consumed_cents'] != original['amount_cents']:
            raise ServiceError('Original consumed credit origins are inconsistent.', 409)
        used = min(remaining, row['consumed_cents'] - row['restored_cents'])
        if used:
            ident = uuid.uuid4(); index = len(result)
            db.execute('''INSERT INTO ss_credit_lots(id,tenant_id,customer_id,source_entry_id,lot_index,origin_code,policy_id,policy_version,issued_cents,remaining_cents,parent_lot_id)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (ident, tenant_id, customer_id, entry_id, index, lot['origin_code'], lot['policy_id'], lot['policy_version'], used, used, lot['id']))
            db.execute('UPDATE ss_credit_lot_reservations SET restored_cents=restored_cents+%s WHERE tenant_id=%s AND reservation_id=%s AND lot_id=%s',
                       (used, tenant_id, reservation_id, lot['id']))
            db.execute("INSERT INTO ss_credit_lot_mutations(tenant_id,entry_id,lot_id,amount_cents,kind) VALUES (%s,%s,%s,%s,'discount_return')",
                       (tenant_id, entry_id, ident, used))
            result.append({'lot_id': str(ident), 'parent_lot_id': str(lot['id']), 'amount_cents': used})
            remaining -= used
        if not remaining:
            return result
    raise ServiceError('Restoration exceeds the remaining consumed credit origins.', 409)
