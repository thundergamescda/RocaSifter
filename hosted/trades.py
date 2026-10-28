"""Tenant-scoped accepted-trade receipts and exactly-once unapproved credit.

This records staff-reviewed card facts and an explicit customer agreement. It
does not make a cash payment, receive Square inventory, approve a tax policy or
enable credit checkout. Accepted receipt evidence is immutable.
"""
import base64
from datetime import datetime
import json
import uuid
from psycopg.types.json import Jsonb
from .credit_lots import record_credit_lot
from .database import audit
from .ledger import Ledger, require_role
from .security import ServiceError
from .settlement import evidence_hash

BUY_ROLES = ('owner', 'manager', 'buyer')
ACCEPT_ROLES = ('owner', 'manager')
MAX_CENTS = 100_000_000
IDENTITY_LIMITS = {'name': 200, 'game': 100, 'game_label': 100, 'set': 200, 'number': 100, 'language': 50,
                   'printing': 100, 'condition': 100, 'kind': 10, 'grading_company': 100,
                   'grade': 30, 'grade_qualifier': 100, 'certification': 100, 'year': 20, 'sport': 100,
                   'league': 100, 'team': 100, 'player': 150, 'serial_number': 100, 'manufacturer': 100}


def _text(value, label, limit, *, required=False):
    if not isinstance(value, str):raise ServiceError(label + ' must be text.')
    value = value.strip()
    if len(value) > limit or (required and not value) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ServiceError(label + ' is missing, too long or contains unsupported control characters.')
    return value


def _uuid(value, label):
    if isinstance(value, uuid.UUID):return value
    try:
        if not isinstance(value, str) or len(value) > 50:raise ValueError()
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):raise ServiceError(label + ' is invalid.') from None


def _integer(value, label, minimum=0, maximum=MAX_CENTS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ServiceError(label + ' must be a whole number in the supported range.')
    return value


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):raise ServiceError('Unsupported ' + label + ' fields.')


def normalize_lines(lines):
    if not isinstance(lines, list) or not 1 <= len(lines) <= 1000:
        raise ServiceError('A trade must have between 1 and 1,000 line items.')
    result = [];seen = set();certificates = set()
    for line in lines:
        _keys(line, ('id', 'identity', 'quantity', 'unit_intake_cents', 'included', 'reviewed'), 'trade line')
        identity = line.get('identity')
        _keys(identity, IDENTITY_LIMITS, 'card identity')
        normalized = {key: _text(identity.get(key, ''), key.replace('_', ' ').title(), limit,
                                  required=key in ('name', 'game')) for key, limit in IDENTITY_LIMITS.items()}
        normalized['kind'] = normalized['kind'] or 'card'
        if normalized['game'].startswith('other:') and not normalized['game_label']:
            raise ServiceError('An additional game key needs its readable game label.')
        if normalized['kind'] not in ('card', 'slab'):raise ServiceError('Select a raw card or graded slab.')
        if normalized['kind'] == 'slab' and not all(normalized[key] for key in ('grading_company', 'grade', 'certification')):
            raise ServiceError('A slab needs its grading company, grade and certification reference.')
        if normalized['kind'] == 'card' and any(normalized[key] for key in ('grading_company', 'grade', 'grade_qualifier', 'certification')):
            raise ServiceError('Use the slab item type for grading and certification details.')
        quantity = _integer(line.get('quantity'), 'Quantity', 1, 100000)
        if normalized['kind'] == 'slab':
            if quantity != 1:raise ServiceError('A certified slab must have quantity one for its individual certificate.')
            certificate = (normalized['grading_company'].casefold(), normalized['certification'].casefold())
            if certificate in certificates:raise ServiceError('The same certified slab cannot appear twice in a trade.')
            certificates.add(certificate)
        price = _integer(line.get('unit_intake_cents'), 'Intake value in cents')
        total = quantity * price
        if total > MAX_CENTS:raise ServiceError('A line intake value exceeds the supported trade limit.')
        included, reviewed = line.get('included', True), line.get('reviewed', False)
        if type(included) is not bool or type(reviewed) is not bool:raise ServiceError('Line inclusion and review must be explicit boolean values.')
        ident = str(_uuid(line['id'], 'Line ID')) if line.get('id') is not None else None
        if ident and ident in seen:raise ServiceError('A line ID cannot appear twice in one trade.')
        if ident:seen.add(ident)
        # Keep only facts supplied/reviewed by staff. Empty optional fields add
        # noise and can make an otherwise valid large draft impossible to save.
        normalized = {key: value for key, value in normalized.items() if value or key in ('name', 'game', 'kind')}
        result.append({'id': ident, 'identity': normalized, 'quantity': quantity,
                       'unit_intake_cents': price, 'included': included, 'reviewed': reviewed,
                       'line_total_cents': total})
    if sum(line['line_total_cents'] for line in result if line['included']) > MAX_CENTS:
        raise ServiceError('The included intake value exceeds the supported trade limit.')
    return result


def normalize_offer_changes(value):
    if value is None:return None
    _keys(value, ('cash_bps', 'credit_bps', 'cash_override_cents', 'credit_override_cents', 'override_reason'), 'offer')
    result = {}
    for key in ('cash_bps', 'credit_bps'):
        if key in value:result[key] = _integer(value[key], 'Offer basis points', 0, 10000)
    for key in ('cash_override_cents', 'credit_override_cents'):
        if key in value:result[key] = None if value[key] is None else _integer(value[key], 'Negotiated offer in cents')
    if 'override_reason' in value:result['override_reason'] = _text(value['override_reason'], 'Offer change reason', 500, required=True)
    if result and not result.get('override_reason'):raise ServiceError('A manager reason is required for custom offer terms.')
    return result


def calculate_offer(lines, cash_bps, credit_bps, *, cash_override_cents=None, credit_override_cents=None):
    _integer(cash_bps, 'Cash basis points', 0, 10000);_integer(credit_bps, 'Credit basis points', 0, 10000)
    included = [line for line in lines if line['included']]
    total = sum(line['line_total_cents'] for line in included)
    _integer(total, 'Intake total')
    cash = (total * cash_bps + 5000) // 10000
    credit = (total * credit_bps + 5000) // 10000
    if cash_override_cents is not None:cash = _integer(cash_override_cents, 'Cash offer in cents')
    if credit_override_cents is not None:credit = _integer(credit_override_cents, 'Credit offer in cents')
    return {'intake_total_cents': total, 'cash_offer_cents': cash, 'credit_offer_cents': credit,
            'included_count': len(included), 'included_quantity': sum(line['quantity'] for line in included),
            'all_included_reviewed': bool(included) and all(line['reviewed'] for line in included)}


class Trades:
    def __init__(self, database):
        self.database = database
        self.ledger = Ledger(database)

    @staticmethod
    def _staff(db, actor, roles):
        require_role(actor, *roles)
        staff = db.execute('SELECT role,enabled FROM ss_staff WHERE tenant_id=%s AND id=%s FOR SHARE',
                           (actor['tenant_id'], actor['staff_id'])).fetchone()
        if not staff or not staff['enabled'] or staff['role'] not in roles or staff['role'] != actor['role']:
            raise ServiceError('Your current store role cannot perform this trade action.', 403)

    @staticmethod
    def _lines(db, tenant, ident):
        rows = db.execute('SELECT * FROM ss_trade_receipt_lines WHERE tenant_id=%s AND trade_id=%s ORDER BY ordinal,id', (tenant, ident)).fetchall()
        return [{'id': str(row['id']), 'identity': row['identity'], 'quantity': row['quantity'],
                 'unit_intake_cents': row['unit_intake_cents'], 'included': row['included'], 'reviewed': row['reviewed'],
                 'line_total_cents': row['line_total_cents']} for row in rows]

    @staticmethod
    def _write_lines(db, tenant, ident, lines):
        db.execute('DELETE FROM ss_trade_receipt_lines WHERE tenant_id=%s AND trade_id=%s', (tenant, ident))
        for index, line in enumerate(lines):
            db.execute('''INSERT INTO ss_trade_receipt_lines(id,tenant_id,trade_id,ordinal,identity,quantity,unit_intake_cents,included,reviewed,line_total_cents)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (line['id'], tenant, ident, index, Jsonb(line['identity']), line['quantity'], line['unit_intake_cents'], line['included'], line['reviewed'], line['line_total_cents']))

    @staticmethod
    def _materialize(lines):
        return [dict(line) | {'id': line['id'] or str(uuid.uuid4())} for line in lines]

    @staticmethod
    def _offer_fields(row):
        return {key: row[key] for key in ('cash_bps', 'credit_bps', 'cash_override_cents', 'credit_override_cents', 'override_reason')}

    @staticmethod
    def _calculate(lines, values):
        return calculate_offer(lines, values['cash_bps'], values['credit_bps'],
                               cash_override_cents=values['cash_override_cents'], credit_override_cents=values['credit_override_cents'])

    def create(self, actor, data):
        require_role(actor, *BUY_ROLES)
        _keys(data, ('request_key', 'customer_id', 'title', 'source_reference', 'replace_voided_source', 'lines', 'offer_terms'), 'trade')
        customer = _uuid(data.get('customer_id'), 'Customer ID')
        title = _text(data.get('title', 'Customer trade'), 'Trade title', 150, required=True)
        source = _text(data.get('source_reference', ''), 'Source receipt reference', 200)
        replacement = data.get('replace_voided_source', False)
        if type(replacement) is not bool or (replacement and not source):raise ServiceError('A voided-source replacement needs an explicit boolean choice and stable source reference.')
        lines = normalize_lines(data.get('lines'));change = normalize_offer_changes(data.get('offer_terms'))
        if change:require_role(actor, *ACCEPT_ROLES)
        intent = {'op': 'trade_create', 'customer_id': str(customer), 'title': title, 'source_reference': source,
                  'lines': lines, 'offer_terms': change, 'replace_voided_source': replacement}
        creation_hash = evidence_hash({key: value for key, value in intent.items() if key != 'replace_voided_source'})
        tenant = actor['tenant_id'];key = data.get('request_key')
        with self.database.transaction() as db:
            self._staff(db, actor, BUY_ROLES)
            hashed, old = self.ledger._begin_request(db, tenant, key, intent)
            if old:return old
            if source:
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (str(tenant) + ':trade-source:' + source,))
                previous = db.execute("SELECT id,version,state,creation_hash FROM ss_trade_receipts WHERE tenant_id=%s AND source_reference=%s AND state<>'voided'", (tenant, source)).fetchone()
                if previous:
                    if previous['creation_hash'] != creation_hash:raise ServiceError('This source receipt was already imported with different details.', 409)
                    return self.ledger._finish(db, tenant, key, hashed, {'trade_id': str(previous['id']), 'version': previous['version'],
                                'state': previous['state'], 'source_deduplicated': True}, operation='trade')
                voided = db.execute("SELECT 1 FROM ss_trade_receipts WHERE tenant_id=%s AND source_reference=%s AND state='voided' LIMIT 1", (tenant, source)).fetchone()
                if voided and not replacement:raise ServiceError('This source receipt was voided. Explicitly confirm a new draft after reviewing the prior void.', 409)
            if not db.execute('SELECT 1 FROM ss_customers WHERE tenant_id=%s AND id=%s FOR SHARE', (tenant, customer)).fetchone():
                raise ServiceError('Customer not found.', 404)
            store = db.execute('SELECT trade_cash_bps,trade_credit_bps FROM ss_tenants WHERE id=%s', (tenant,)).fetchone()
            values = {'cash_bps': store['trade_cash_bps'], 'credit_bps': store['trade_credit_bps'],
                      'cash_override_cents': None, 'credit_override_cents': None, 'override_reason': ''}
            if change:values.update(change)
            lines = self._materialize(lines);totals = self._calculate(lines, values);ident = uuid.uuid4()
            db.execute('''INSERT INTO ss_trade_receipts(id,tenant_id,customer_id,title,source_reference,creation_hash,
                          cash_bps,credit_bps,cash_override_cents,credit_override_cents,override_reason,
                          intake_total_cents,cash_offer_cents,credit_offer_cents,lines_hash,created_by,updated_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (ident, tenant, customer, title, source, creation_hash, values['cash_bps'], values['credit_bps'],
                        values['cash_override_cents'], values['credit_override_cents'], values['override_reason'],
                        totals['intake_total_cents'], totals['cash_offer_cents'], totals['credit_offer_cents'],
                        evidence_hash(lines), actor['staff_id'], actor['staff_id']))
            self._write_lines(db, tenant, ident, lines)
            audit(db, tenant, actor['staff_id'], 'trade-draft-created', ident, {'line_count': len(lines)})
            return self.ledger._finish(db, tenant, key, hashed, {'trade_id': str(ident), 'version': 0, 'state': 'draft', 'source_deduplicated': False}, operation='trade')

    def get(self, actor, trade_id):
        require_role(actor, *BUY_ROLES);ident = _uuid(trade_id, 'Trade ID');tenant = actor['tenant_id']
        with self.database.transaction() as db:
            self._staff(db, actor, BUY_ROLES)
            row = db.execute('SELECT * FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s', (tenant, ident)).fetchone()
            if not row:raise ServiceError('Trade receipt not found.', 404)
            lines = self._lines(db, tenant, ident)
            result = dict(row)
            for key, value in list(result.items()):
                if isinstance(value, uuid.UUID):result[key] = str(value)
                elif isinstance(value, datetime):result[key] = value.isoformat()
            result.pop('tenant_id');result.pop('creation_hash')
            result.update(lines=lines, scope='hosted', inventory_received=False, cash_payment_executed=False)
            if row['state'].startswith('accepted_'):
                if not isinstance(row['accepted_receipt'], dict) or evidence_hash(row['accepted_receipt']) != row['receipt_hash']:
                    raise ServiceError('Accepted receipt evidence is inconsistent. Manager review is required.', 409)
            return result

    def list(self, actor, query='', *, limit=50, cursor=None):
        require_role(actor, *BUY_ROLES);query = _text(query, 'Trade search', 200);_integer(limit, 'Page size', 1, 100)
        before = None
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 512:raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
                moment = datetime.fromisoformat(value['updated_at'])
                if moment.tzinfo is None:raise ValueError()
                before = (moment, _uuid(value['id'], 'Cursor ID'))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):raise ServiceError('Trade page cursor is invalid.') from None
        with self.database.transaction() as db:
            self._staff(db, actor, BUY_ROLES)
            where = ' AND (t.updated_at,t.id)<(%s,%s)' if before else ''
            params = [actor['tenant_id'], query] + (list(before) if before else []) + [limit + 1]
            rows = db.execute('''SELECT t.id,t.customer_id,c.display_name AS customer_name,t.title,t.state,t.version,t.updated_at,
                                 t.intake_total_cents,t.cash_offer_cents,t.credit_offer_cents,t.selected_offer,t.accepted_amount_cents
                                 FROM ss_trade_receipts t JOIN ss_customers c ON c.tenant_id=t.tenant_id AND c.id=t.customer_id
                                 WHERE t.tenant_id=%s AND strpos(lower(t.title||' '||t.source_reference||' '||c.display_name),lower(%s))>0''' +
                              where + ' ORDER BY t.updated_at DESC,t.id DESC LIMIT %s', params).fetchall()
            more = len(rows) > limit;rows = rows[:limit]
            result = [{key: str(value) if isinstance(value, uuid.UUID) else value.isoformat() if isinstance(value, datetime) else value
                       for key, value in row.items()} for row in rows]
            next_cursor = None
            if more and result:
                next_cursor = base64.urlsafe_b64encode(json.dumps({'updated_at': result[-1]['updated_at'], 'id': result[-1]['id']}).encode()).decode().rstrip('=')
            return {'trades': result, 'next_cursor': next_cursor, 'scope': 'hosted'}

    def update(self, actor, trade_id, data):
        require_role(actor, *BUY_ROLES);ident = _uuid(trade_id, 'Trade ID');tenant = actor['tenant_id']
        _keys(data, ('request_key', 'version', 'customer_id', 'title', 'lines', 'offer_terms'), 'trade update')
        version = _integer(data.get('version'), 'Trade version', 0, 2**63-1)
        customer = _uuid(data['customer_id'], 'Customer ID') if 'customer_id' in data else None
        title = _text(data['title'], 'Trade title', 150, required=True) if 'title' in data else None
        lines = normalize_lines(data['lines']) if 'lines' in data else None
        change = normalize_offer_changes(data.get('offer_terms'))
        if change:require_role(actor, *ACCEPT_ROLES)
        intent = {'op': 'trade_update', 'trade_id': str(ident), 'version': version, 'customer_id': str(customer) if customer else None,
                  'title': title, 'lines': lines, 'offer_terms': change};key = data.get('request_key')
        with self.database.transaction() as db:
            self._staff(db, actor, BUY_ROLES)
            hashed, old = self.ledger._begin_request(db, tenant, key, intent)
            if old:return old
            initial = db.execute('SELECT customer_id FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s', (tenant, ident)).fetchone()
            if not initial:raise ServiceError('Trade receipt not found.', 404)
            target = customer or initial['customer_id']
            for locked in sorted({initial['customer_id'], target}, key=str):
                if not db.execute('SELECT 1 FROM ss_customers WHERE tenant_id=%s AND id=%s FOR SHARE', (tenant, locked)).fetchone():
                    raise ServiceError('Customer not found.', 404)
            row = db.execute('SELECT * FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s FOR UPDATE', (tenant, ident)).fetchone()
            if row['state'] != 'draft':raise ServiceError('Accepted or voided trade receipts cannot be edited.', 409)
            if row['version'] != version or row['customer_id'] != initial['customer_id']:
                raise ServiceError('The trade changed. Refresh it before saving.', 409)
            values = self._offer_fields(row)
            if change:values.update(change)
            lines = self._materialize(lines) if lines is not None else self._lines(db, tenant, ident)
            totals = self._calculate(lines, values)
            db.execute('''UPDATE ss_trade_receipts SET customer_id=%s,title=%s,cash_bps=%s,credit_bps=%s,
                          cash_override_cents=%s,credit_override_cents=%s,override_reason=%s,intake_total_cents=%s,
                          cash_offer_cents=%s,credit_offer_cents=%s,lines_hash=%s,version=version+1,updated_at=now(),updated_by=%s
                          WHERE tenant_id=%s AND id=%s''',
                       (target, title or row['title'], values['cash_bps'], values['credit_bps'], values['cash_override_cents'],
                        values['credit_override_cents'], values['override_reason'], totals['intake_total_cents'],
                        totals['cash_offer_cents'], totals['credit_offer_cents'], evidence_hash(lines), actor['staff_id'], tenant, ident))
            self._write_lines(db, tenant, ident, lines)
            audit(db, tenant, actor['staff_id'], 'trade-draft-updated', ident, {'version': version + 1, 'line_count': len(lines)})
            return self.ledger._finish(db, tenant, key, hashed, {'trade_id': str(ident), 'state': 'draft', 'version': version + 1}, operation='trade')

    def accept(self, actor, trade_id, data):
        require_role(actor, *ACCEPT_ROLES);ident = _uuid(trade_id, 'Trade ID');tenant = actor['tenant_id']
        _keys(data, ('request_key', 'version', 'customer_id', 'offer_type', 'expected_offer_cents', 'customer_agreed', 'items_received'), 'trade acceptance')
        version = _integer(data.get('version'), 'Trade version', 0, 2**63-1)
        customer_id = _uuid(data.get('customer_id'), 'Customer ID')
        offer = data.get('offer_type')
        if offer not in ('credit', 'cash'):raise ServiceError('Select the agreed cash or store-credit offer.')
        expected = _integer(data.get('expected_offer_cents'), 'Expected agreed offer in cents', 1)
        if data.get('customer_agreed') is not True or data.get('items_received') is not True:
            raise ServiceError('Confirm the customer agreed and the store received the included items before acceptance.')
        intent = {'op': 'trade_accept', 'trade_id': str(ident), 'version': version, 'customer_id': str(customer_id),
                  'offer_type': offer, 'expected_offer_cents': expected, 'customer_agreed': True, 'items_received': True}
        key = data.get('request_key')
        with self.database.transaction() as db:
            self._staff(db, actor, ACCEPT_ROLES)
            hashed, old = self.ledger._begin_request(db, tenant, key, intent)
            if old:return old
            customer = self.ledger._customer(db, tenant, customer_id, lock=True)
            row = db.execute('SELECT * FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s FOR UPDATE', (tenant, ident)).fetchone()
            if not row:raise ServiceError('Trade receipt not found.', 404)
            if row['customer_id'] != customer['id']:raise ServiceError('The trade customer changed. Refresh before accepting.', 409)
            if row['state'].startswith('accepted_'):
                previous = row['accepted_receipt']
                if (not isinstance(previous, dict) or evidence_hash(previous) != row['receipt_hash'] or
                        row['selected_offer'] != offer or row['accepted_amount_cents'] != expected or
                        version not in (previous['version_before_acceptance'], row['version'])):
                    raise ServiceError('This trade was already accepted with different terms.', 409)
                return self.ledger._finish(db, tenant, key, hashed, row['acceptance_result'], operation='trade')
            if row['state'] != 'draft':raise ServiceError('A voided trade cannot be accepted.', 409)
            if row['version'] != version:raise ServiceError('The trade changed. Refresh before accepting.', 409)
            lines = self._lines(db, tenant, ident)
            # Revalidate stored line facts and derive money from the locked receipt.
            checked = normalize_lines([{key: value for key, value in line.items() if key != 'line_total_cents'} for line in lines])
            if checked != lines or evidence_hash(lines) != row['lines_hash']:
                raise ServiceError('Trade line evidence is inconsistent. Manager review is required.', 409)
            totals = self._calculate(lines, self._offer_fields(row))
            if not totals['all_included_reviewed']:raise ServiceError('Review every included card before accepting the trade.', 409)
            if any(totals[key] != row[key] for key in ('intake_total_cents', 'cash_offer_cents', 'credit_offer_cents')):
                raise ServiceError('Stored trade totals are inconsistent.', 409)
            value = totals[offer + '_offer_cents']
            if value <= 0 or value != expected:raise ServiceError('The agreed offer changed. Refresh the trade before accepting.', 409)
            at = db.execute('SELECT now() AS at').fetchone()['at']
            entry = None;balance = customer['balance_cents']
            if offer == 'credit':
                source = 'trade:' + str(ident)
                if db.execute('SELECT 1 FROM ss_credit_entries WHERE tenant_id=%s AND source_key=%s', (tenant, source)).fetchone():
                    raise ServiceError('This trade already has credit evidence requiring review.', 409)
                entry = uuid.uuid4();balance += value
                db.execute('UPDATE ss_customers SET balance_cents=%s,version=version+1 WHERE tenant_id=%s AND id=%s', (balance, tenant, customer['id']))
                db.execute('''INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason,staff_id)
                              VALUES (%s,%s,%s,%s,%s,'trade_credit',%s,%s,'Accepted hosted trade receipt',%s)''',
                           (entry, tenant, customer['id'], value, balance, source, str(ident), actor['staff_id']))
                record_credit_lot(db, tenant, customer['id'], entry, origin_code='trade_unreviewed')
            receipt = {'trade_schema_version': 1, 'trade_id': str(ident), 'version_before_acceptance': version, 'receipt_version': version + 1,
                       'customer': {'id': str(customer['id']), 'display_name': customer['display_name']},
                       'title': row['title'], 'source_reference': row['source_reference'], 'currency': 'USD',
                       'lines': [line for line in lines if line['included']], 'lines_hash': row['lines_hash'],
                       'intake_total_cents': totals['intake_total_cents'], 'cash_offer_cents': totals['cash_offer_cents'],
                       'credit_offer_cents': totals['credit_offer_cents'], 'offer_terms': self._offer_fields(row),
                       'selected_offer': offer, 'accepted_amount_cents': value, 'accepted_at': at.isoformat(),
                       'accepted_by': str(actor['staff_id']), 'accepted_by_role': actor['role'],
                       'customer_agreed': True, 'items_received': True,
                       'inventory_received': False, 'cash_payment_executed': False,
                       'credit_policy_state': 'unreviewed' if entry else 'not_applicable',
                       'credit_entry_id': str(entry) if entry else None}
            hashed_receipt = evidence_hash(receipt)
            result = {'trade_id': str(ident), 'state': 'accepted_' + offer, 'version': version + 1,
                      'accepted_amount_cents': value, 'offer_type': offer, 'receipt_hash': hashed_receipt,
                      'credit_entry_id': str(entry) if entry else None, 'cash_payment_executed': False,
                      'inventory_received': False, 'credit_policy_state': 'unreviewed' if entry else 'not_applicable',
                      'checkout_eligible': False, 'balance_after_cents': balance}
            db.execute('''UPDATE ss_trade_receipts SET state=%s,version=version+1,selected_offer=%s,accepted_amount_cents=%s,
                          accepted_at=%s,accepted_by=%s,ledger_entry_id=%s,receipt_hash=%s,accepted_receipt=%s,
                          acceptance_result=%s,updated_at=now(),updated_by=%s WHERE tenant_id=%s AND id=%s''',
                       ('accepted_' + offer, offer, value, at, actor['staff_id'], entry, hashed_receipt, Jsonb(receipt), Jsonb(result), actor['staff_id'], tenant, ident))
            audit(db, tenant, actor['staff_id'], 'trade-accepted', ident, {'offer_type': offer, 'amount_cents': value,
                  'credit_entry_id': str(entry) if entry else None, 'receipt_hash': hashed_receipt})
            return self.ledger._finish(db, tenant, key, hashed, result, operation='trade')

    def void(self, actor, trade_id, data):
        require_role(actor, *BUY_ROLES);ident = _uuid(trade_id, 'Trade ID');tenant = actor['tenant_id']
        _keys(data, ('request_key', 'version', 'reason'), 'trade void')
        version = _integer(data.get('version'), 'Trade version', 0, 2**63-1)
        reason = _text(data.get('reason', ''), 'Void reason', 500, required=True);key = data.get('request_key')
        intent = {'op': 'trade_void', 'trade_id': str(ident), 'version': version, 'reason': reason}
        with self.database.transaction() as db:
            self._staff(db, actor, BUY_ROLES)
            hashed, old = self.ledger._begin_request(db, tenant, key, intent)
            if old:return old
            row = db.execute('SELECT * FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s FOR UPDATE', (tenant, ident)).fetchone()
            if not row:raise ServiceError('Trade receipt not found.', 404)
            if row['state'] != 'draft' or row['version'] != version:raise ServiceError('Only the current draft can be voided.', 409)
            db.execute("UPDATE ss_trade_receipts SET state='voided',version=version+1,voided_at=now(),voided_by=%s,void_reason=%s,updated_at=now(),updated_by=%s WHERE tenant_id=%s AND id=%s",
                       (actor['staff_id'], reason, actor['staff_id'], tenant, ident))
            audit(db, tenant, actor['staff_id'], 'trade-draft-voided', ident)
            return self.ledger._finish(db, tenant, key, hashed, {'trade_id': str(ident), 'state': 'voided', 'version': version + 1}, operation='trade')
