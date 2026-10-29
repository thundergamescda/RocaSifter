"""Private Square reconciliation domain; no provider writes or HTTP routes.

Only a trusted adapter may supply snapshots retrieved with the tenant's Square
OAuth connection. A signed webhook is a notification to retrieve a resource,
not authoritative input to these operations. Live wiring additionally requires
immutable reservation/redemption terms and the release gates in the integration
note. All amounts are integer cents; ambiguous returns stay in review.
"""
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from psycopg.types.json import Jsonb
from .database import audit
from .security import ServiceError
from .credit_lots import reserve_lots, consume_lots, restore_lots, TAX_BASES


def _id(value, limit=255):
    # Square IDs are opaque. Do not silently trim them or impose the desktop
    # request-key alphabet on provider IDs. They are SQL parameters, never SQL.
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _cents(value, *, positive=False):
    return type(value) is int and (value > 0 if positive else value >= 0) and value <= 100_000_000


def _money(value, currency):
    if not isinstance(value, dict) or value.get('currency') != currency or not _cents(value.get('amount')):
        return None
    return value['amount']


def _objects(value):
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


@dataclass(frozen=True)
class ReservationTerms:
    order_id: str
    square_customer_id: str
    location_id: str
    discount_id: str
    reserved_cents: int
    policy_reference: str
    currency: str = 'USD'
    minimum_order_version: int = 1
    discount_uid: str | None = None
    discount_catalog_version: int | None = None
    discount_tax_basis: str | None = None

    def __post_init__(self):
        if not all(_id(value) for value in (self.order_id, self.square_customer_id, self.location_id, self.discount_id)):
            raise ValueError('Complete immutable Square reservation bindings are required.')
        if not _cents(self.reserved_cents, positive=True) or self.currency != 'USD':
            raise ValueError('A positive USD reservation amount is required.')
        if not isinstance(self.policy_reference, str) or not 1 <= len(self.policy_reference.strip()) <= 200:
            raise ValueError('An approved credit/tax policy reference is required.')
        if type(self.minimum_order_version) is not int or self.minimum_order_version < 1:
            raise ValueError('A positive minimum order version is required.')
        if self.discount_uid is not None and not _id(self.discount_uid, 60):
            raise ValueError('The bound order discount UID is invalid.')
        if self.discount_catalog_version is not None and (type(self.discount_catalog_version) is not int or not 1 <= self.discount_catalog_version < 2**63):
            raise ValueError('A valid discount catalog version is required.')
        if self.discount_tax_basis is not None and (not isinstance(self.discount_tax_basis,str) or self.discount_tax_basis not in TAX_BASES):
            raise ValueError('An explicit supported catalog tax basis is required.')


@dataclass(frozen=True)
class Decision:
    action: str
    code: str
    amount_cents: int = 0
    discount_uid: str | None = None
    order_version: int | None = None
    source_key: str | None = None


def evidence_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def record_exception(db, tenant_id, source_key, code, *, reservation_id=None, event_id=None, resource_id=None, details=None):
    """Persist a small machine-readable exception; never a raw provider payload."""
    if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code):
        raise ValueError('Exception code is invalid.')
    small = details or {}
    if not isinstance(small, dict) or len(json.dumps(small)) > 4096:
        raise ValueError('Exception details must be a small structured record.')
    ident = uuid.uuid4()
    row = db.execute('''INSERT INTO ss_reconciliation_exceptions(id,tenant_id,source_key,code,reservation_id,event_id,resource_id,details)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (tenant_id,source_key) DO UPDATE SET code=EXCLUDED.code,
                        event_id=COALESCE(EXCLUDED.event_id,ss_reconciliation_exceptions.event_id),
                        details=EXCLUDED.details,last_seen_at=now(),occurrences=ss_reconciliation_exceptions.occurrences+1,
                        status='open',resolved_at=NULL,resolved_by=NULL RETURNING id''',
                     (ident, tenant_id, source_key, code, reservation_id, event_id, resource_id, Jsonb(small))).fetchone()
    return str(row['id'])


def _decode_snapshot(row):
    if not row or row['terms_version'] != 1 or not isinstance(row['terms'], dict) or evidence_hash(row['terms']) != row['terms_hash']:
        raise ServiceError('Immutable reservation evidence is missing or inconsistent.', 409)
    value = row['terms']
    try:
        terms = ReservationTerms(value['order_id'], value['square_customer_id'], value['location_id'],
                                 value['discount_id'], value['reserved_cents'], value['policy_reference'],
                                 value['currency'], value['minimum_order_version'], value['discount_uid'],
                                 value['discount_catalog_version'], value['discount_tax_basis'])
        if terms.discount_catalog_version is None or terms.discount_tax_basis is None:
            raise ValueError()
        allocations = value['credit_origin_allocations']
        if not isinstance(allocations, list) or not allocations or sum(a['amount_cents'] for a in allocations) != terms.reserved_cents:
            raise ValueError()
        if len({a['lot_id'] for a in allocations}) != len(allocations) or any(not _cents(a['amount_cents'], positive=True) for a in allocations):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ServiceError('Immutable reservation evidence is invalid.', 409) from None
    return terms, value


def record_reservation_terms(db, tenant_id, reservation_id, checkout_binding, *, reserved_cents, customer_id):
    """Called inside reserve's transaction with trusted, retrieved checkout data.

    Approval and credit-origin allocations come from persisted server policies;
    no browser-supplied approval or allocation object is accepted.
    """
    binding = checkout_binding
    if not isinstance(binding, dict) or 'credit_origin_allocations' in binding:
        raise ServiceError('A trusted server checkout binding is required.', 409)
    required = ('order_id', 'merchant_id', 'location_id', 'square_customer_id', 'discount_id', 'discount_uid')
    if not all(_id(binding.get(key), 60 if key == 'discount_uid' else 255) for key in required):
        raise ServiceError('The checkout binding is incomplete.', 409)
    if (binding.get('environment') not in ('sandbox', 'production') or binding.get('currency') != 'USD' or
            type(binding.get('order_version')) is not int or binding['order_version'] < 1 or
            type(binding.get('discount_catalog_version')) is not int or binding['discount_catalog_version'] < 1 or
            binding.get('discount_tax_basis') not in TAX_BASES or not _cents(reserved_cents, positive=True)):
        raise ServiceError('The checkout binding has an unsupported currency, version or tax basis.', 409)
    existing = db.execute('SELECT * FROM ss_reservation_terms WHERE tenant_id=%s AND reservation_id=%s',
                          (tenant_id, reservation_id)).fetchone()
    if existing:
        _, previous = _decode_snapshot(existing)
        compare = dict(binding); compare['minimum_order_version'] = compare.pop('order_version')
        if any(previous.get(key) != value for key, value in compare.items()) or previous['reserved_cents'] != reserved_cents:
            raise ServiceError('Checkout evidence was already bound with different details.', 409)
        return previous
    customer = db.execute('SELECT * FROM ss_customers WHERE tenant_id=%s AND id=%s FOR UPDATE', (tenant_id, customer_id)).fetchone()
    reservation = db.execute('SELECT * FROM ss_reservations WHERE tenant_id=%s AND id=%s FOR UPDATE', (tenant_id, reservation_id)).fetchone()
    store = db.execute('SELECT * FROM ss_tenants WHERE id=%s FOR SHARE', (tenant_id,)).fetchone()
    connection = db.execute('SELECT merchant_id,environment,revoked_at FROM ss_square_connections WHERE tenant_id=%s FOR SHARE', (tenant_id,)).fetchone()
    if (not customer or not reservation or reservation['customer_id'] != customer['id'] or reservation['state'] != 'reserved' or
            reservation['order_id'] != binding['order_id'] or reservation['amount_cents'] != reserved_cents or
            customer['square_customer_id'] != binding['square_customer_id']):
        raise ServiceError('Checkout evidence does not match the reserved customer/order.', 409)
    if not connection or connection['revoked_at'] or connection['merchant_id'] != binding['merchant_id'] or connection['environment'] != binding['environment']:
        raise ServiceError('Checkout evidence does not match the active Square connection.', 409)
    if (not store or not store['credit_checkout_enabled'] or not isinstance(store['tax_policy_reference'], str) or not store['tax_policy_reference'].strip() or store['tax_policy_version'] < 1 or
            store['discount_id'] != binding['discount_id'] or store['square_location_id'] != binding['location_id'] or
            store['discount_catalog_version'] != binding['discount_catalog_version'] or store['discount_tax_basis'] != binding['discount_tax_basis']):
        raise ServiceError('The store checkout policy/catalog configuration is not approved for this binding.', 409)
    allocations = reserve_lots(db, tenant_id, customer_id, reservation_id, reserved_cents, binding)
    value = {key: binding[key] for key in required}
    value.update(environment=binding['environment'], currency='USD', minimum_order_version=binding['order_version'],
                 discount_catalog_version=binding['discount_catalog_version'], discount_tax_basis=binding['discount_tax_basis'],
                 reserved_cents=reserved_cents, policy_reference=store['tax_policy_reference'],
                 policy_version=store['tax_policy_version'], credit_origin_allocations=allocations)
    db.execute('INSERT INTO ss_reservation_terms(tenant_id,reservation_id,terms_version,terms,terms_hash) VALUES (%s,%s,1,%s,%s)',
               (tenant_id, reservation_id, Jsonb(value), evidence_hash(value)))
    return value


def catalog_evidence(catalog):
    """Minimal non-PII verification facts, used only after the planner validates."""
    data=catalog['discount_data']
    return {'id':catalog['id'],'version':catalog['version'],'type':catalog['type'],
            'discount_type':data['discount_type'],'modify_tax_basis':data['modify_tax_basis'],
            **({'amount_money':{key:data['amount_money'][key] for key in ('amount','currency')}} if data.get('amount_money') is not None else {})}


def plan_order(terms, order, *, catalog_discount=None):
    """Plan consumption/release without mutating data or trusting discount names."""
    if not isinstance(terms, ReservationTerms):
        raise TypeError('Validated reservation terms are required.')
    if not isinstance(order, dict):
        return Decision('exception', 'invalid_order')
    if order.get('id') != terms.order_id:
        return Decision('exception', 'order_mismatch')
    if order.get('location_id') != terms.location_id:
        return Decision('exception', 'location_mismatch')
    if order.get('customer_id') != terms.square_customer_id:
        return Decision('exception', 'customer_mismatch')
    version = order.get('version')
    if type(version) is not int or version < 1:
        return Decision('exception', 'invalid_order_version')
    if version < terms.minimum_order_version:
        return Decision('pending', 'stale_order_version', order_version=version)
    state = order.get('state')
    if state in ('OPEN', 'DRAFT'):
        return Decision('pending', 'order_not_terminal', order_version=version)
    if state == 'CANCELED':
        return Decision('release', 'order_canceled', order_version=version)
    if state != 'COMPLETED':
        return Decision('exception', 'unknown_order_state', order_version=version)
    if _money(order.get('total_money'), terms.currency) is None:
        return Decision('exception', 'invalid_order_currency_or_total', order_version=version)
    discounts = order.get('discounts', [])
    if not _objects(discounts):
        return Decision('exception', 'invalid_discounts', order_version=version)
    matches = [item for item in discounts if item.get('catalog_object_id') == terms.discount_id]
    if len(matches) != 1:
        return Decision('exception', 'tracked_discount_not_unique', order_version=version)
    discount = matches[0]
    uid = discount.get('uid')
    if not _id(uid, 60) or sum(item.get('uid') == uid for item in discounts) != 1:
        return Decision('exception', 'invalid_discount_uid', order_version=version)
    if terms.discount_uid is not None and terms.discount_uid != uid:
        return Decision('exception', 'discount_uid_mismatch', order_version=version)
    if discount.get('type') not in ('FIXED_AMOUNT', 'VARIABLE_AMOUNT') or discount.get('percentage') not in (None, ''):
        return Decision('exception', 'unapproved_discount_type', order_version=version)
    if terms.discount_catalog_version is None or terms.discount_tax_basis is None:
        return Decision('exception', 'catalog_terms_missing', order_version=version)
    actual_version=discount.get('catalog_version')
    if type(actual_version) is not int or not 1 <= actual_version < 2**63:
        return Decision('exception', 'discount_catalog_version_missing', order_version=version)
    if actual_version != terms.discount_catalog_version:
        return Decision('exception', 'discount_catalog_version_mismatch', order_version=version)
    applied = _money(discount.get('applied_money'), terms.currency)
    if applied is None or applied > terms.reserved_cents:
        return Decision('exception', 'invalid_applied_credit', order_version=version)
    if catalog_discount is None:
        return Decision('exception', 'catalog_discount_unverified', order_version=version)
    if (not isinstance(catalog_discount,dict) or catalog_discount.get('id') != terms.discount_id
            or catalog_discount.get('type') != 'DISCOUNT' or type(catalog_discount.get('version')) is not int
            or catalog_discount['version'] != actual_version):
        return Decision('exception', 'catalog_discount_mismatch', order_version=version)
    if catalog_discount.get('is_deleted') is not None and catalog_discount.get('is_deleted') is not False:
        return Decision('exception', 'catalog_discount_deleted', order_version=version)
    data=catalog_discount.get('discount_data')
    if (not isinstance(data,dict) or data.get('discount_type') != discount['type']
            or data.get('percentage') not in (None,'')):
        return Decision('exception', 'catalog_discount_type_mismatch', order_version=version)
    if data.get('modify_tax_basis') != terms.discount_tax_basis:
        return Decision('exception', 'catalog_tax_basis_mismatch', order_version=version)
    if ((data['discount_type']=='FIXED_AMOUNT' and _money(data.get('amount_money'),terms.currency) is None)
            or (data.get('amount_money') is not None and _money(data['amount_money'],terms.currency) is None)):
        return Decision('exception', 'catalog_discount_currency_mismatch', order_version=version)
    if applied == 0:
        return Decision('release', 'completed_without_credit_use', discount_uid=uid, order_version=version)
    return Decision('consume', 'verified_completed_order', applied, uid, version, 'order:' + terms.order_id)


@dataclass(frozen=True)
class RedemptionTerms:
    order_id: str
    square_customer_id: str
    location_id: str
    discount_id: str
    discount_uid: str
    redeemed_cents: int
    policy_reference: str
    currency: str = 'USD'

    def __post_init__(self):
        ReservationTerms(self.order_id, self.square_customer_id, self.location_id,
                         self.discount_id, self.redeemed_cents, self.policy_reference,
                         self.currency, discount_uid=self.discount_uid)


def plan_return(terms, return_order, payment_refunds, *, already_restored_cents=0):
    """Restore only explicit returned tracked discount, never refund dollar totals.

The refund list must contain retrieved PaymentRefund resources corresponding to
the return order's documented legacy Refund references (tender_id + '_' + id).
Unproven references, unlinked/custom returns, exchanges and zero-payment returns
remain exceptions until their separate provider/device policy has been proven.
"""
    if not isinstance(terms, RedemptionTerms):
        raise TypeError('Validated immutable redemption terms are required.')
    if not _cents(already_restored_cents) or already_restored_cents > terms.redeemed_cents:
        raise ValueError('The already-restored amount is inconsistent with the redemption.')
    if not isinstance(return_order, dict) or not _id(return_order.get('id')):
        return Decision('exception', 'invalid_return_order')
    if return_order['id'] == terms.order_id:
        return Decision('exception', 'return_is_original_order')
    if return_order.get('location_id') != terms.location_id:
        return Decision('exception', 'return_location_mismatch')
    if return_order.get('customer_id') not in (None, terms.square_customer_id):
        return Decision('exception', 'return_customer_mismatch')
    version = return_order.get('version')
    if type(version) is not int or version < 1:
        return Decision('exception', 'invalid_return_version')
    if return_order.get('state') in ('OPEN', 'DRAFT'):
        return Decision('pending', 'return_not_completed', order_version=version)
    if return_order.get('state') != 'COMPLETED':
        return Decision('exception', 'return_not_completed', order_version=version)
    sale_items = return_order.get('line_items', [])
    if not _objects(sale_items):
        return Decision('exception', 'invalid_return_sale_items', order_version=version)
    if sale_items:
        return Decision('exception', 'exchange_requires_review', order_version=version)
    returns = return_order.get('returns')
    if not _objects(returns):
        return Decision('exception', 'invalid_return_entries', order_version=version)
    relevant = [item for item in returns if item.get('source_order_id') == terms.order_id]
    if not relevant:
        return Decision('exception', 'return_source_mismatch', order_version=version)
    seen_returns = set(); seen_discounts = set(); restored = 0
    for returned in relevant:
        uid = returned.get('uid')
        if not _id(uid, 60) or uid in seen_returns:
            return Decision('exception', 'invalid_return_uid', order_version=version)
        seen_returns.add(uid)
        items = returned.get('return_line_items')
        if not _objects(items) or not items:
            return Decision('exception', 'return_items_unproven', order_version=version)
        for item in items:
            raw_quantity = item.get('quantity')
            try:
                quantity = Decimal(raw_quantity) if isinstance(raw_quantity, str) and 1 <= len(raw_quantity) <= 12 else Decimal('NaN')
            except (InvalidOperation, TypeError, ValueError):
                quantity = Decimal('NaN')
            if item.get('item_type') != 'ITEM' or not _id(item.get('source_line_item_uid'), 60) or not quantity.is_finite() or quantity <= 0:
                return Decision('exception', 'custom_or_unlinked_return_requires_review', order_version=version)
        discounts = returned.get('return_discounts', [])
        if not _objects(discounts):
            return Decision('exception', 'invalid_return_discounts', order_version=version)
        for discount in discounts:
            matches_uid = discount.get('source_discount_uid') == terms.discount_uid
            matches_catalog = discount.get('catalog_object_id') == terms.discount_id
            if not matches_uid:
                if matches_catalog:
                    return Decision('exception', 'return_discount_source_mismatch', order_version=version)
                continue
            if discount.get('catalog_object_id') not in (None, terms.discount_id):
                return Decision('exception', 'return_discount_catalog_mismatch', order_version=version)
            uid = discount.get('uid')
            value = _money(discount.get('applied_money'), terms.currency)
            if not _id(uid, 60) or uid in seen_discounts or value is None:
                return Decision('exception', 'invalid_return_discount_amount', order_version=version)
            seen_discounts.add(uid); restored += value
    if restored == 0:
        return Decision('none', 'no_tracked_credit_returned', order_version=version)
    if restored + already_restored_cents > terms.redeemed_cents:
        return Decision('exception', 'return_exceeds_redemption', order_version=version)
    references = return_order.get('refunds')
    if not _objects(references) or not references or not _objects(payment_refunds):
        return Decision('exception', 'refund_completion_unproven', order_version=version)
    by_id = {}
    for refund in payment_refunds:
        refund_id = refund.get('id')
        if not _id(refund_id) or refund_id in by_id:
            return Decision('exception', 'invalid_payment_refund_proof', order_version=version)
        by_id[refund_id] = refund
    seen_references = set()
    for reference in references:
        if not _id(reference.get('id')) or not _id(reference.get('tender_id')):
            return Decision('exception', 'refund_reference_unproven', order_version=version)
        reference_id = reference['tender_id'] + '_' + reference['id']
        if reference_id in seen_references:
            return Decision('exception', 'refund_reference_unproven', order_version=version)
        seen_references.add(reference_id)
        refund = by_id.get(reference_id)
        if not refund or refund.get('unlinked') is True or refund.get('location_id') != terms.location_id or _money(refund.get('amount_money'), terms.currency) in (None, 0):
            return Decision('exception', 'refund_completion_unproven', order_version=version)
        if refund.get('status') == 'PENDING':
            return Decision('pending', 'payment_refund_pending', order_version=version)
        if refund.get('status') != 'COMPLETED':
            return Decision('exception', 'payment_refund_not_completed', order_version=version)
    return Decision('restore', 'verified_itemized_credit_return', restored, terms.discount_uid,
                    version, 'return:' + return_order['id'] + ':order:' + terms.order_id)


class Settlement:
    """Atomic adapter loading immutable server evidence; never expose to browsers."""
    def __init__(self, database):
        self.database = database

    def reconcile_order(self, tenant_id, reservation_id, order, *, terms=None,catalog_discount=None,catalog_error=None):
        if terms is not None and not isinstance(terms, ReservationTerms):
            raise TypeError('Immutable reservation terms are required.')
        with self.database.transaction() as db:
            reservation = db.execute('SELECT * FROM ss_reservations WHERE tenant_id=%s AND id=%s',
                                     (tenant_id, reservation_id)).fetchone()
            if not reservation:
                raise ServiceError('Reservation not found.', 404)
            # All credit mutations lock the customer first. Keep reservation locks
            # after it so concurrent reserves/adjustments cannot spend this hold.
            customer = db.execute('SELECT * FROM ss_customers WHERE tenant_id=%s AND id=%s FOR UPDATE',
                                  (tenant_id, reservation['customer_id'])).fetchone()
            reservation = db.execute('SELECT * FROM ss_reservations WHERE tenant_id=%s AND id=%s FOR UPDATE',
                                     (tenant_id, reservation_id)).fetchone()
            if not customer or not reservation or reservation['customer_id'] != customer['id']:
                raise ServiceError('Reservation binding changed. Reconcile again.', 409)
            snapshot = db.execute('SELECT * FROM ss_reservation_terms WHERE tenant_id=%s AND reservation_id=%s',
                                  (tenant_id, reservation_id)).fetchone()
            try:
                stored_terms, evidence = _decode_snapshot(snapshot)
            except ServiceError:
                return self._exception(db, tenant_id, reservation, Decision('exception', 'reservation_terms_missing'))
            if terms is not None and terms != stored_terms:
                raise ServiceError('Caller-supplied terms do not match immutable server evidence.', 409)
            terms = stored_terms
            connection = db.execute('SELECT merchant_id,environment,revoked_at FROM ss_square_connections WHERE tenant_id=%s FOR SHARE', (tenant_id,)).fetchone()
            if (not connection or connection['revoked_at'] or connection['merchant_id'] != evidence.get('merchant_id') or
                    connection['environment'] != evidence.get('environment')):
                return self._exception(db, tenant_id, reservation, Decision('exception', 'square_connection_binding_changed'))
            if reservation['order_id'] != terms.order_id or reservation['amount_cents'] != terms.reserved_cents or customer['square_customer_id'] != terms.square_customer_id:
                raise ServiceError('Immutable reservation terms do not match the ledger.', 409)
            previous_version = reservation.get('last_order_version') or 0
            if previous_version > terms.minimum_order_version:
                terms = replace(terms, minimum_order_version=previous_version)
            decision = plan_order(terms, order,catalog_discount=catalog_discount)
            if catalog_error is not None:
                if catalog_error != 'catalog_discount_unavailable':raise ValueError('Invalid catalog verification error.')
                if decision.code=='catalog_discount_unverified' or decision.action in ('consume','release'):
                    decision=replace(decision,action='exception',code=catalog_error,amount_cents=0,source_key=None)
            source = 'order:' + terms.order_id
            existing = db.execute('SELECT * FROM ss_credit_entries WHERE tenant_id=%s AND source_key=%s',
                                  (tenant_id, source)).fetchone()
            state = reservation['state']
            if state == 'consumed':
                if not existing or existing['customer_id'] != customer['id'] or existing['kind'] != 'discount_redeem' or existing['reference'] != terms.order_id or not 0 < -existing['amount_cents'] <= terms.reserved_cents:
                    raise ServiceError('Consumed reservation has inconsistent ledger evidence.', 409)
                settlement = db.execute('SELECT * FROM ss_square_settlements WHERE tenant_id=%s AND reservation_id=%s', (tenant_id, reservation_id)).fetchone()
                if not settlement or settlement['terms_hash'] != snapshot['terms_hash'] or settlement['entry_id'] != existing['id']:
                    return self._exception(db, tenant_id, reservation, Decision('exception', 'settlement_evidence_missing'))
                conflict = decision.action in ('release', 'exception') or (decision.action == 'consume' and -existing['amount_cents'] != decision.amount_cents)
                if conflict:
                    audit(db, tenant_id, None, 'credit-terminal-reconciliation-exception', reservation_id,
                          {'code': 'terminal_order_conflict', 'order_version': decision.order_version})
                    record_exception(db, tenant_id, 'reservation:' + str(reservation_id) + ':terminal_order_conflict',
                                     'terminal_order_conflict', reservation_id=reservation_id,
                                     resource_id=terms.order_id, details={'order_version': decision.order_version})
                return self._result(reservation, decision, duplicate=True,
                                    code='terminal_order_conflict' if conflict else 'already_consumed',
                                    exception=conflict)
            if state == 'released':
                if existing:
                    raise ServiceError('Released reservation has inconsistent ledger evidence.', 409)
                completed = isinstance(order, dict) and order.get('state') == 'COMPLETED'
                conflict = completed or decision.action == 'exception'
                code = 'released_order_completed' if completed else 'terminal_order_conflict' if conflict else 'already_released'
                if conflict:
                    audit(db, tenant_id, None, 'credit-terminal-reconciliation-exception', reservation_id,
                          {'code': code, 'order_version': decision.order_version})
                    record_exception(db, tenant_id, 'reservation:' + str(reservation_id) + ':' + code,
                                     code, reservation_id=reservation_id, resource_id=terms.order_id,
                                     details={'order_version': decision.order_version})
                return self._result(reservation, decision, duplicate=True,
                                    code=code, exception=conflict)
            if state not in ('reserved', 'exception'):
                raise ServiceError('Reservation state is invalid.', 409)
            if existing:
                raise ServiceError('Active reservation already has a redemption entry.', 409)
            if decision.action == 'pending':
                self._observe(db, tenant_id, reservation_id, order, decision)
                return self._result(reservation, decision)
            if decision.action == 'exception':
                return self._exception(db, tenant_id, reservation, decision)
            if decision.action == 'release':
                db.execute("UPDATE ss_reservations SET state='released',updated_at=now() WHERE tenant_id=%s AND id=%s", (tenant_id, reservation_id))
                self._observe(db, tenant_id, reservation_id, order, decision)
                self._resolve(db, tenant_id, reservation_id)
                facts={'code':decision.code}
                if decision.code=='completed_without_credit_use':facts['catalog_proof_hash']=evidence_hash(catalog_evidence(catalog_discount))
                audit(db, tenant_id, None, 'credit-reservation-released', reservation_id, facts)
                return self._result(dict(reservation) | {'state': 'released'}, decision)
            others = db.execute("SELECT COALESCE(SUM(amount_cents),0) AS amount FROM ss_reservations WHERE tenant_id=%s AND customer_id=%s AND id<>%s AND state IN ('reserved','exception')",
                                (tenant_id, customer['id'], reservation_id)).fetchone()['amount']
            if customer['balance_cents'] - int(others) < decision.amount_cents:
                raise ServiceError('Reserved credit is inconsistent with the available balance.', 409)
            balance = customer['balance_cents'] - decision.amount_cents
            entry = str(uuid.uuid4())
            db.execute('UPDATE ss_customers SET balance_cents=%s,version=version+1 WHERE tenant_id=%s AND id=%s',
                       (balance, tenant_id, customer['id']))
            db.execute('''INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason)
                          VALUES (%s,%s,%s,%s,%s,'discount_redeem',%s,%s,'Verified completed Square order')''',
                       (entry, tenant_id, customer['id'], -decision.amount_cents, balance, source, terms.order_id))
            consumed = consume_lots(db, tenant_id, customer['id'], reservation_id, entry, evidence['credit_origin_allocations'])
            db.execute('''INSERT INTO ss_square_settlements(tenant_id,reservation_id,source_order_id,entry_id,applied_cents,currency,discount_uid,settled_order_version,terms_hash,consumed_allocations)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (tenant_id, reservation_id, terms.order_id, entry, decision.amount_cents, terms.currency,
                        decision.discount_uid, decision.order_version, snapshot['terms_hash'], Jsonb(consumed)))
            db.execute("UPDATE ss_reservations SET state='consumed',updated_at=now() WHERE tenant_id=%s AND id=%s", (tenant_id, reservation_id))
            self._observe(db, tenant_id, reservation_id, order, decision)
            self._resolve(db, tenant_id, reservation_id)
            audit(db, tenant_id, None, 'credit-reservation-consumed', reservation_id,
                  {'entry_id': entry, 'amount_cents': decision.amount_cents, 'discount_uid': decision.discount_uid,
                   'catalog_version':terms.discount_catalog_version,'discount_tax_basis':terms.discount_tax_basis,
                   'catalog_proof_hash':evidence_hash(catalog_evidence(catalog_discount)),
                   'order_version': decision.order_version, 'policy_reference': terms.policy_reference})
            return self._result(dict(reservation) | {'state': 'consumed'}, decision) | {'entry_id': entry, 'balance_cents': balance}

    def reconcile_return(self, tenant_id, source_order_id, return_order, payment_refunds):
        """Restore only explicit itemized, fully proven returned credit allocations."""
        with self.database.transaction() as db:
            settled = db.execute('''SELECT s.*,e.customer_id,e.amount_cents FROM ss_square_settlements s
                                    JOIN ss_credit_entries e ON e.tenant_id=s.tenant_id AND e.id=s.entry_id
                                    WHERE s.tenant_id=%s AND s.source_order_id=%s''', (tenant_id, source_order_id)).fetchone()
            if not settled:
                raise ServiceError('Original immutable settlement is not available for this return.', 409)
            customer = db.execute('SELECT * FROM ss_customers WHERE tenant_id=%s AND id=%s FOR UPDATE',
                                  (tenant_id, settled['customer_id'])).fetchone()
            reservation = db.execute('SELECT * FROM ss_reservations WHERE tenant_id=%s AND id=%s FOR UPDATE',
                                     (tenant_id, settled['reservation_id'])).fetchone()
            snapshot = db.execute('SELECT * FROM ss_reservation_terms WHERE tenant_id=%s AND reservation_id=%s',
                                  (tenant_id, settled['reservation_id'])).fetchone()
            try:
                original, evidence = _decode_snapshot(snapshot)
            except ServiceError:
                return self._return_exception(db, tenant_id, reservation, return_order, 'return_original_evidence_missing')
            connection = db.execute('SELECT merchant_id,environment,revoked_at FROM ss_square_connections WHERE tenant_id=%s FOR SHARE', (tenant_id,)).fetchone()
            if (not connection or connection['revoked_at'] or connection['merchant_id'] != evidence.get('merchant_id') or
                    connection['environment'] != evidence.get('environment')):
                return self._return_exception(db, tenant_id, reservation, return_order, 'square_connection_binding_changed')
            if (not customer or not reservation or reservation['state'] != 'consumed' or
                    settled['terms_hash'] != snapshot['terms_hash'] or settled['amount_cents'] != -settled['applied_cents'] or
                    customer['square_customer_id'] != original.square_customer_id or source_order_id != original.order_id):
                raise ServiceError('Original settlement/return bindings are inconsistent.', 409)
            terms = RedemptionTerms(original.order_id, original.square_customer_id, original.location_id,
                                    original.discount_id, settled['discount_uid'], settled['applied_cents'],
                                    original.policy_reference, original.currency)
            return_id = return_order.get('id') if isinstance(return_order, dict) else None
            existing = None
            if _id(return_id):
                existing = db.execute('SELECT * FROM ss_credit_returns WHERE tenant_id=%s AND return_order_id=%s AND source_order_id=%s',
                                      (tenant_id, return_id, source_order_id)).fetchone()
            restored = int(db.execute('SELECT COALESCE(SUM(restored_cents),0) AS amount FROM ss_credit_returns WHERE tenant_id=%s AND source_order_id=%s',
                                      (tenant_id, source_order_id)).fetchone()['amount'])
            if existing:
                observed_version = return_order.get('version')
                if type(observed_version) is int and observed_version < existing['return_order_version']:
                    return {'state': 'restored', 'action': 'none', 'code': 'stale_return_ignored',
                            'duplicate': True, 'amount_cents': existing['restored_cents'], 'needs_review': False}
                restored -= existing['restored_cents']
            decision = plan_return(terms, return_order, payment_refunds, already_restored_cents=restored)
            if decision.action == 'restore':
                proof = self._return_proof(terms, return_order, payment_refunds, decision)
                hashed = evidence_hash(proof)
                if existing:
                    if existing['restored_cents'] == decision.amount_cents and existing['proof_hash'] == hashed:
                        return {'state': 'restored', 'action': 'none', 'code': 'already_restored', 'duplicate': True,
                                'amount_cents': existing['restored_cents'], 'needs_review': False}
                    return self._return_exception(db, tenant_id, reservation, return_order, 'terminal_return_conflict')
            elif existing:
                return self._return_exception(db, tenant_id, reservation, return_order, 'terminal_return_conflict')
            if decision.action == 'exception':
                return self._return_exception(db, tenant_id, reservation, return_order, decision.code)
            if decision.action in ('pending', 'none'):
                return {'state': 'pending' if decision.action == 'pending' else 'ignored', 'action': decision.action,
                        'code': decision.code, 'amount_cents': 0, 'duplicate': False, 'needs_review': False}
            if db.execute('SELECT 1 FROM ss_credit_entries WHERE tenant_id=%s AND source_key=%s', (tenant_id, decision.source_key)).fetchone():
                return self._return_exception(db, tenant_id, reservation, return_order, 'return_ledger_evidence_inconsistent')
            balance = customer['balance_cents'] + decision.amount_cents
            entry = uuid.uuid4()
            db.execute('UPDATE ss_customers SET balance_cents=%s,version=version+1 WHERE tenant_id=%s AND id=%s',
                       (balance, tenant_id, customer['id']))
            db.execute('''INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason)
                          VALUES (%s,%s,%s,%s,%s,'discount_return',%s,%s,'Verified itemized Square credit return')''',
                       (entry, tenant_id, customer['id'], decision.amount_cents, balance, decision.source_key, source_order_id))
            restore_lots(db, tenant_id, customer['id'], reservation['id'], entry, settled['consumed_allocations'])
            db.execute('''INSERT INTO ss_credit_returns(tenant_id,return_order_id,source_order_id,entry_id,restored_cents,return_order_version,proof_hash,proof)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (tenant_id, return_id, source_order_id, entry, decision.amount_cents, decision.order_version, hashed, Jsonb(proof)))
            prefix = 'return:' + return_id + ':order:' + source_order_id + ':'
            db.execute("""UPDATE ss_reconciliation_exceptions SET status='resolved',resolved_at=now()
                          WHERE tenant_id=%s AND left(source_key,length(%s))=%s AND status='open'""",
                       (tenant_id, prefix, prefix))
            audit(db, tenant_id, None, 'credit-return-restored', return_id,
                  {'entry_id': str(entry), 'source_order_id': source_order_id, 'amount_cents': decision.amount_cents,
                   'proof_hash': hashed, 'return_order_version': decision.order_version})
            return {'state': 'restored', 'action': 'restore', 'code': decision.code, 'duplicate': False,
                    'amount_cents': decision.amount_cents, 'entry_id': str(entry), 'balance_cents': balance, 'needs_review': False}

    @staticmethod
    def _return_proof(terms, return_order, payment_refunds, decision):
        references = sorted(reference['tender_id'] + '_' + reference['id'] for reference in return_order['refunds'])
        allocations = []
        for returned in return_order['returns']:
            if returned.get('source_order_id') != terms.order_id:
                continue
            for discount in returned.get('return_discounts', []):
                if discount.get('source_discount_uid') == terms.discount_uid:
                    allocations.append({'return_uid': returned['uid'], 'discount_uid': discount['uid'],
                                        'amount_cents': discount['applied_money']['amount']})
        return {'return_order_id': return_order['id'], 'source_order_id': terms.order_id,
                'source_discount_uid': terms.discount_uid, 'currency': terms.currency,
                'restored_cents': decision.amount_cents, 'completed_refund_ids': references,
                'allocations': sorted(allocations, key=lambda item: (item['return_uid'], item['discount_uid']))}

    @staticmethod
    def _return_exception(db, tenant_id, reservation, return_order, code):
        return_id = return_order.get('id') if isinstance(return_order, dict) else 'invalid'
        if not _id(return_id):return_id = 'invalid'
        source = 'return:' + return_id + ':order:' + reservation['order_id'] + ':' + code
        ident = record_exception(db, tenant_id, source, code, reservation_id=reservation['id'], resource_id=return_id)
        audit(db, tenant_id, None, 'credit-return-reconciliation-exception', return_id, {'code': code})
        return {'state': 'review', 'action': 'exception', 'code': code, 'amount_cents': 0,
                'duplicate': False, 'needs_review': True, 'exception_id': ident}

    def _exception(self, db, tenant_id, reservation, decision):
        state = reservation['state']
        if state in ('reserved', 'exception'):
            state = 'exception'
            db.execute("UPDATE ss_reservations SET state='exception',updated_at=now() WHERE tenant_id=%s AND id=%s", (tenant_id, reservation['id']))
        ident = record_exception(db, tenant_id, 'reservation:' + str(reservation['id']) + ':' + decision.code,
                                 decision.code, reservation_id=reservation['id'], resource_id=reservation['order_id'],
                                 details={'order_version': decision.order_version})
        audit(db, tenant_id, None, 'credit-reconciliation-exception', reservation['id'], {'code': decision.code})
        return self._result(dict(reservation) | {'state': state}, decision, exception=True) | {'exception_id': ident}

    @staticmethod
    def _observe(db, tenant_id, reservation_id, order, decision):
        if decision.order_version is not None and decision.code != 'stale_order_version':
            db.execute('''UPDATE ss_reservations SET last_order_version=%s,last_order_state=%s
                          WHERE tenant_id=%s AND id=%s AND COALESCE(last_order_version,0)<=%s''',
                       (decision.order_version, order['state'], tenant_id, reservation_id, decision.order_version))

    @staticmethod
    def _resolve(db, tenant_id, reservation_id):
        db.execute("""UPDATE ss_reconciliation_exceptions SET status='resolved',resolved_at=now()
                      WHERE tenant_id=%s AND source_key LIKE %s AND status='open'""",
                   (tenant_id, 'reservation:' + str(reservation_id) + ':%'))

    @staticmethod
    def _result(reservation, decision, *, duplicate=False, code=None, exception=False):
        return {'reservation_id': str(reservation['id']), 'state': reservation['state'],
                'action': decision.action, 'code': code or decision.code,
                'amount_cents': decision.amount_cents, 'duplicate': duplicate,
                'needs_review': exception}
