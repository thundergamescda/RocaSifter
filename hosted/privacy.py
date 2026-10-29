"""Owner-attested export of explicitly linked hosted customer data, never deletion.

The read snapshot is streamed, not saved to a server file/table. Audit metadata
records authorization and lifecycle only; it never contains the exported payload.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from psycopg.types.json import Jsonb
from .security import ServiceError

EXPORT_SCHEMA = 'stock-and-sleeve.hosted-customer-export'
EXPORT_VERSION = 1
SCOPE_LIMITATIONS = [
    'Only records structurally linked to this customer in this hosted store are selected, as of one database snapshot. Duplicate accounts and incidental free-text mentions are not searched.',
    'Local desktop databases, pending journals, scanner files, printer queues, generated files and previous downloads are not inspected.',
    'Backups, support copies, unlinked events and records held by Square or other providers are not searched. This export makes no provider calls.',
    'Staff credentials, passwords, sessions, MFA/recovery material and provider tokens are excluded. Customer-related audit details use an explicit field projection.',
    'Credential-like JSON keys are removed defensively and counted. Stored source hashes describe original evidence, not any redacted projection.',
    'The owner must review incidental third-party free text and deliver the copy privately. This is not a determination that a legal rights request is fulfilled.',
    'No deletion, anonymization, offboarding purge or record-retention decision is performed. Downloaded copies and backups need the approved separate handling policy.',
]


@dataclass(frozen=True)
class Dataset:
    name: str
    query: str


def dataset(name, columns, table, where, order):
    return Dataset(name, f'/* privacy:{name} */ SELECT {columns} FROM {table} WHERE {where} ORDER BY {order}')


CUSTOMER = 'tenant_id=%(tenant)s AND customer_id=%(customer)s'
OWN_ENTRIES = f'SELECT id FROM ss_credit_entries WHERE {CUSTOMER}'
OWN_LOTS = f'SELECT id FROM ss_credit_lots WHERE {CUSTOMER}'
OWN_RESERVATIONS = f'SELECT id FROM ss_reservations WHERE {CUSTOMER}'
OWN_TRADES = f'SELECT id FROM ss_trade_receipts WHERE {CUSTOMER}'
DATASETS = (
    dataset('customer', 'id,display_name,contact,square_customer_id,privacy_basis,balance_cents,version,created_at',
            'ss_customers', 'tenant_id=%(tenant)s AND id=%(customer)s', 'id'),
    dataset('credit_entries', 'id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason,staff_id,at',
            'ss_credit_entries', CUSTOMER, 'at,id'),
    dataset('credit_lots', 'id,customer_id,source_entry_id,lot_index,origin_code,policy_id,policy_version,issued_cents,remaining_cents,parent_lot_id,created_at',
            'ss_credit_lots', CUSTOMER, 'created_at,id'),
    dataset('lot_mutations', 'm.entry_id,m.lot_id,m.amount_cents,m.kind,m.created_at',
            'ss_credit_lot_mutations m JOIN ss_credit_lots l ON l.tenant_id=m.tenant_id AND l.id=m.lot_id JOIN ss_credit_entries e ON e.tenant_id=m.tenant_id AND e.id=m.entry_id',
            'm.tenant_id=%(tenant)s AND l.customer_id=%(customer)s AND e.customer_id=%(customer)s', 'm.created_at,m.entry_id,m.lot_id'),
    dataset('reservations', 'id,customer_id,amount_cents,state,order_id,request_key,staff_id,created_at,updated_at,last_order_version,last_order_state',
            'ss_reservations', CUSTOMER, 'created_at,id'),
    dataset('lot_reservations', 'a.reservation_id,a.lot_id,a.reserved_cents,a.consumed_cents,a.restored_cents',
            'ss_credit_lot_reservations a JOIN ss_reservations r ON r.tenant_id=a.tenant_id AND r.id=a.reservation_id JOIN ss_credit_lots l ON l.tenant_id=a.tenant_id AND l.id=a.lot_id',
            'a.tenant_id=%(tenant)s AND r.customer_id=%(customer)s AND l.customer_id=%(customer)s', 'a.reservation_id,a.lot_id'),
    dataset('reservation_terms', 't.reservation_id,t.terms_version,t.terms,t.terms_hash,t.created_at',
            'ss_reservation_terms t JOIN ss_reservations r ON r.tenant_id=t.tenant_id AND r.id=t.reservation_id',
            't.tenant_id=%(tenant)s AND r.customer_id=%(customer)s', 't.reservation_id'),
    dataset('square_settlements', 's.reservation_id,s.source_order_id,s.entry_id,s.applied_cents,s.currency,s.discount_uid,s.settled_order_version,s.terms_hash,s.consumed_allocations,s.created_at',
            'ss_square_settlements s JOIN ss_reservations r ON r.tenant_id=s.tenant_id AND r.id=s.reservation_id JOIN ss_credit_entries e ON e.tenant_id=s.tenant_id AND e.id=s.entry_id',
            's.tenant_id=%(tenant)s AND r.customer_id=%(customer)s AND e.customer_id=%(customer)s', 's.created_at,s.reservation_id'),
    dataset('credit_returns', 'k.return_order_id,k.source_order_id,k.entry_id,k.restored_cents,k.return_order_version,k.proof_hash,k.proof,k.created_at',
            'ss_credit_returns k JOIN ss_square_settlements s ON s.tenant_id=k.tenant_id AND s.source_order_id=k.source_order_id JOIN ss_reservations r ON r.tenant_id=s.tenant_id AND r.id=s.reservation_id JOIN ss_credit_entries e ON e.tenant_id=k.tenant_id AND e.id=k.entry_id',
            'k.tenant_id=%(tenant)s AND r.customer_id=%(customer)s AND e.customer_id=%(customer)s', 'k.created_at,k.return_order_id,k.source_order_id'),
    dataset('trade_receipts', 'id,customer_id,title,source_reference,creation_hash,state,version,currency,cash_bps,credit_bps,cash_override_cents,credit_override_cents,override_reason,intake_total_cents,cash_offer_cents,credit_offer_cents,lines_hash,created_by,updated_by,created_at,updated_at,selected_offer,accepted_amount_cents,accepted_at,accepted_by,ledger_entry_id,receipt_hash,accepted_receipt,acceptance_result,voided_at,voided_by,void_reason',
            'ss_trade_receipts', CUSTOMER, 'created_at,id'),
    dataset('trade_lines', 'l.id,l.trade_id,l.ordinal,l.identity,l.quantity,l.unit_intake_cents,l.included,l.reviewed,l.line_total_cents',
            'ss_trade_receipt_lines l JOIN ss_trade_receipts t ON t.tenant_id=l.tenant_id AND t.id=l.trade_id',
            'l.tenant_id=%(tenant)s AND t.customer_id=%(customer)s', 'l.trade_id,l.ordinal,l.id'),
    dataset('idempotency_records', 'q.key,q.operation,q.body_hash,q.result,q.at', 'ss_requests q',
            f'''q.tenant_id=%(tenant)s AND ((q.operation='ledger' AND
                (q.result->>'customer_id'=%(customer_text)s OR q.result->>'entry_id' IN (SELECT id::text FROM ss_credit_entries WHERE {CUSTOMER})
                 OR q.result->>'reservation_id' IN (SELECT id::text FROM ss_reservations WHERE {CUSTOMER})))
                OR (q.operation='trade' AND q.result->>'trade_id' IN (SELECT id::text FROM ss_trade_receipts WHERE {CUSTOMER})))''', 'q.at,q.key'),
    dataset('reconciliation_exceptions', 'x.id,x.source_key,x.code,x.reservation_id,x.event_id,x.resource_id,x.details,x.status,x.occurrences,x.first_seen_at,x.last_seen_at,x.resolved_at,x.resolved_by',
            'ss_reconciliation_exceptions x',
            f'x.tenant_id=%(tenant)s AND x.reservation_id IN ({OWN_RESERVATIONS})', 'x.first_seen_at,x.id'),
    dataset('origin_policies', 'p.policy_id,p.version,p.origin_code,p.policy_reference,p.currency,p.discount_id,p.discount_catalog_version,p.discount_tax_basis,p.approved,p.approved_at,p.approved_by,p.approval_reference,p.created_at',
            'ss_credit_origin_policies p',
            f'p.tenant_id=%(tenant)s AND EXISTS (SELECT 1 FROM ss_credit_lots l WHERE l.tenant_id=p.tenant_id AND l.customer_id=%(customer)s AND l.policy_id=p.policy_id AND l.policy_version=p.version)', 'p.policy_id,p.version'),
    dataset('policy_revocations', 'p.policy_id,p.policy_version,p.revoked_by,p.reason,p.revoked_at',
            'ss_credit_policy_revocations p',
            'p.tenant_id=%(tenant)s AND EXISTS (SELECT 1 FROM ss_credit_lots l WHERE l.tenant_id=p.tenant_id AND l.customer_id=%(customer)s AND l.policy_id=p.policy_id AND l.policy_version=p.policy_version)', 'p.revoked_at,p.policy_id,p.policy_version'),
    dataset('audit', 'a.id,a.staff_id,a.action,a.entity_id,a.details,a.at', 'ss_audit a',
            f'''a.tenant_id=%(tenant)s AND (a.entity_id=%(customer_text)s OR a.details->>'customer_id'=%(customer_text)s
                OR a.entity_id IN (SELECT id::text FROM ss_credit_entries WHERE {CUSTOMER})
                OR a.entity_id IN (SELECT id::text FROM ss_reservations WHERE {CUSTOMER})
                OR a.entity_id IN (SELECT id::text FROM ss_trade_receipts WHERE {CUSTOMER}))''', 'a.at,a.id'),
)
DATASET_NAMES = tuple(item.name for item in DATASETS)
AUDIT_DETAIL_FIELDS = frozenset(('customer_id','entry_id','credit_entry_id','reservation_id','amount_cents','restored_cents',
    'kind','offer_type','code','order_version','version','line_count','receipt_hash','terms_hash','proof_hash','discount_uid',
    'policy_reference','export_id','purpose','verification_method','case_reference_sha256','outcome','failure_code',
    'record_counts','content_chain_sha256','start_audit_id','redacted_fields'))
FORBIDDEN_KEY = re.compile(r'password|passwd|secret|token|credential|session|csrf|mfa|recovery|authorization', re.I)


def json_line(value):
    def encode(item):
        if isinstance(item, (uuid.UUID, date, datetime)):return str(item) if isinstance(item, uuid.UUID) else item.isoformat()
        if isinstance(item, Decimal):return str(item)
        raise TypeError('Unsupported export value.')
    return (json.dumps(value, default=encode, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def attestation(data, actor, customer):
    fields = {'expected_store','confirmed_customer_id','purpose','verification_method','case_reference',
              'identity_verified','authority_confirmed','scope_reviewed','copy_handling_confirmed','agent_authority_verified'}
    if not isinstance(data, dict) or set(data) != fields:
        raise ServiceError('Complete the export identity, purpose and copy-handling attestations.')
    if data['expected_store'] != actor.get('slug') or str(data['confirmed_customer_id']) != str(customer):
        raise ServiceError('The selected store or customer changed. Refresh before exporting.', 409)
    if data['purpose'] not in ('customer_access','customer_portability','correction_review'):
        raise ServiceError('Select the reason for this customer export.')
    if data['verification_method'] not in ('existing_verified_channel','in_person_known_customer','authorized_representative'):
        raise ServiceError('Select how the customer identity was verified.')
    if any(data[key] is not True for key in ('identity_verified','authority_confirmed','scope_reviewed','copy_handling_confirmed')):
        raise ServiceError('Verify identity, authority, scope and private copy handling before exporting.')
    if type(data['agent_authority_verified']) is not bool or (data['verification_method']=='authorized_representative' and data['agent_authority_verified'] is not True):
        raise ServiceError('Verify representative authority before exporting to an agent.')
    reference = data['case_reference']
    if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:#-]{2,99}', reference):
        raise ServiceError('Use an internal case reference of 3–100 letters/numbers or _ . / : # -; do not enter identity documents or contact details.')
    return {'purpose':data['purpose'],'verification_method':data['verification_method'],
            'case_reference_sha256':hashlib.sha256(reference.encode()).hexdigest(),
            'identity_verified':True,'authority_confirmed':True,'scope_reviewed':True,'copy_handling_confirmed':True,
            'agent_authority_verified':data['agent_authority_verified']}


@dataclass(frozen=True)
class Limits:
    page_rows: int = 50
    max_seconds: float = 120
    max_record_bytes: int = 1024 * 1024
    max_page_bytes: int = 2 * 1024 * 1024
    max_stream_bytes: int = 128 * 1024 * 1024
    browser_max_bytes: int = 32 * 1024 * 1024
    recent_sign_in_seconds: int = 15 * 60

    def __post_init__(self):
        if (type(self.page_rows) is not int or not 1 <= self.page_rows <= 100 or not 0 < self.max_seconds <= 300
                or not 1024 <= self.max_record_bytes <= 8*1024*1024 or not 1024 <= self.max_stream_bytes <= 512*1024*1024
                or not self.max_record_bytes+1024 <= self.max_page_bytes <= 16*1024*1024
                or not 1024 <= self.browser_max_bytes <= 64*1024*1024
                or type(self.recent_sign_in_seconds) is not int or not 60 <= self.recent_sign_in_seconds <= 1800):
            raise ValueError('Privacy export engineering limits are invalid.')


class Privacy:
    def __init__(self, database, *, authorize_session, proof_key, limits=None, clock=None, wall_clock=None):
        self.database,self.authorize_session=database,authorize_session
        self.proof_key=proof_key.encode() if isinstance(proof_key,str) else proof_key
        self.limits,self.clock=limits or Limits(),clock or time.monotonic
        self.wall_clock=wall_clock or (lambda:datetime.now(timezone.utc))
        self.active=threading.BoundedSemaphore(1)

    def authorize(self, actor, token):
        if actor.get('role')!='owner':raise ServiceError('Only the store owner can export customer data.',403)
        current=self.authorize_session(token)
        if (current.get('role')!='owner' or str(current.get('tenant_id'))!=str(actor.get('tenant_id'))
                or str(current.get('staff_id'))!=str(actor.get('staff_id')) or not current.get('session_hash')
                or not hmac.compare_digest(str(current['session_hash']),str(actor.get('session_hash','')))
                or current.get('mfa_enabled') and current.get('mfa_authenticated') is not True):
            raise ServiceError('The authorized owner session changed. Sign in again before exporting.',401)
        proof=current.get('mfa_authenticated_at') if current.get('mfa_enabled') else current.get('session_created_at')
        if not isinstance(proof,datetime) or proof.tzinfo is None:
            raise ServiceError('Customer export requires a recent sign-in. Return to the workspace, sign out and sign in again.',401)
        age=(self.wall_clock()-proof).total_seconds()
        if age < -5 or age>self.limits.recent_sign_in_seconds:
            raise ServiceError('Customer export requires sign-in within the last 15 minutes. Return to the workspace, sign out and sign in again.',401)
        return current

    def binding(self, actor, export_id):
        if not self.proof_key:raise ServiceError('Export audit security is not configured.',503)
        return hmac.new(self.proof_key,(str(export_id)+':'+str(actor['session_hash'])).encode(),hashlib.sha256).hexdigest()

    def audit(self, actor, export_id, action, details):
        with self.database.transaction() as db:
            return db.execute('''INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details)
                                 VALUES (%s,%s,%s,%s,%s) RETURNING id''',
                              (actor['tenant_id'],actor['staff_id'],action,str(export_id),Jsonb(details))).fetchone()['id']

    def begin(self, actor, customer_id, data, token):
        self.authorize(actor,token)
        try:customer=uuid.UUID(str(customer_id))
        except (ValueError,TypeError):raise ServiceError('Customer ID is invalid.') from None
        checked=attestation(data,actor,customer)
        capacity=getattr(self.database,'_max_size',5)
        if type(capacity) is int and capacity<3:
            raise ServiceError('Customer export needs reserved database capacity for authorization and audit.',503)
        if not self.active.acquire(blocking=False):
            raise ServiceError('An export is already active on this service worker. Wait for it to finish or cancel.',429)
        output=Export(self,actor,customer,checked,token)
        try:output.open()
        except Exception:
            output.close()
            raise
        return output

    def confirm(self, actor, export_id, data, token):
        self.authorize(actor,token)
        if (not isinstance(data,dict) or set(data)!={'start_audit_id','content_chain_sha256','record_counts'}
                or type(data['start_audit_id']) is not int or data['start_audit_id']<1
                or not isinstance(data['content_chain_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',data['content_chain_sha256'])
                or not isinstance(data['record_counts'],dict) or set(data['record_counts'])!=set(DATASET_NAMES)
                or any(type(count) is not int or count<0 for count in data['record_counts'].values())):
            raise ServiceError('A complete counted and hash-verified export footer is required.')
        with self.database.transaction() as db:
            # Serialize replayed acknowledgements without storing the export itself.
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(actor['tenant_id'])+':privacy-confirm:'+str(export_id),))
            staff=db.execute('SELECT role,enabled FROM ss_staff WHERE tenant_id=%s AND id=%s FOR SHARE',
                             (actor['tenant_id'],actor['staff_id'])).fetchone()
            session=db.execute('SELECT token_hash FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s AND token_hash=%s AND expires_at>now() FOR SHARE',
                               (actor['tenant_id'],actor['staff_id'],actor['session_hash'])).fetchone()
            if not staff or not staff['enabled'] or staff['role']!='owner' or not session:
                raise ServiceError('The authorized owner session changed.',401)
            start=db.execute("SELECT details FROM ss_audit WHERE id=%s AND tenant_id=%s AND staff_id=%s AND entity_id=%s AND action='customer-export-started'",
                             (data['start_audit_id'],actor['tenant_id'],actor['staff_id'],str(export_id))).fetchone()
            if not start or not hmac.compare_digest(start['details'].get('session_binding',''),self.binding(actor,export_id)):
                raise ServiceError('This export belongs to a different owner session.',409)
            end=db.execute("SELECT details FROM ss_audit WHERE id>%s AND tenant_id=%s AND staff_id=%s AND entity_id=%s AND action='customer-export-stream-ended' ORDER BY id DESC LIMIT 1",
                           (data['start_audit_id'],actor['tenant_id'],actor['staff_id'],str(export_id))).fetchone()
            if (not end or end['details'].get('outcome')!='footer_produced'
                    or end['details'].get('record_counts')!=data['record_counts']
                    or end['details'].get('content_chain_sha256')!=data['content_chain_sha256']):
                raise ServiceError('This export did not finish with a matching verified footer. No completed download is authorized.',409)
            previous=db.execute("SELECT id FROM ss_audit WHERE id>%s AND tenant_id=%s AND staff_id=%s AND entity_id=%s AND action='customer-export-client-verified' LIMIT 1",
                                (data['start_audit_id'],actor['tenant_id'],actor['staff_id'],str(export_id))).fetchone()
            if not previous:
                db.execute('INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details) VALUES (%s,%s,%s,%s,%s)',
                           (actor['tenant_id'],actor['staff_id'],'customer-export-client-verified',str(export_id),
                            Jsonb({'customer_id':start['details']['customer_id'],'start_audit_id':data['start_audit_id'],
                                   'record_counts':data['record_counts'],'content_chain_sha256':data['content_chain_sha256'],
                                   'meaning':'Client reported count/hash verification; delivery to a customer is not confirmed.'})))
        return {'verified':True,'already_verified':bool(previous),'delivery_confirmed':False}


class Export:
    def __init__(self, service, actor, customer, checked, token):
        self.service,self.actor,self.customer,self.checked,self.token=service,dict(actor),customer,checked,token
        self.id=uuid.uuid4();self.started=service.clock();self.context=None;self.db=None;self.closed=False
        self.start_audit_id=None;self.footer_produced=False;self.failure_code=None
        self.counts={name:0 for name in DATASET_NAMES};self.expected={};self.chain=bytes(32)
        self.bytes_generated=0;self.pages=0;self.redacted=0;self.square_customer_id=None
        self.params={'tenant':actor['tenant_id'],'customer':customer,'customer_text':str(customer)}

    def check(self):
        if self.service.clock()-self.started>self.service.limits.max_seconds:
            raise ServiceError('Export exceeded its processing limit. No complete export was generated.',413)
        self.service.authorize(self.actor,self.token)

    def open(self):
        self.context=self.service.database.transaction();self.db=self.context.__enter__()
        self.db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        self.db.execute("SET LOCAL statement_timeout='15000ms'")
        self.db.execute("SET LOCAL idle_in_transaction_session_timeout='30000ms'")
        snapshot=self.db.execute('SELECT transaction_timestamp() AS snapshot_at,(SELECT max(version) FROM ss_schema_versions) AS schema_version').fetchone()
        self.snapshot_at,self.database_version=snapshot['snapshot_at'],snapshot['schema_version']
        customer=self.db.execute('SELECT id,square_customer_id FROM ss_customers WHERE tenant_id=%s AND id=%s',
                                 (self.actor['tenant_id'],self.customer)).fetchone()
        if not customer:raise ServiceError('Customer not found in this store.',404)
        self.square_customer_id=customer['square_customer_id']
        # Foreign keys prove tenant links, not that a corrupted association belongs
        # to the same customer. Fail rather than exporting a cross-customer edge.
        checks=(
            'SELECT 1 FROM ss_credit_lots l JOIN ss_credit_entries e ON e.tenant_id=l.tenant_id AND e.id=l.source_entry_id WHERE l.tenant_id=%(tenant)s AND (l.customer_id=%(customer)s OR e.customer_id=%(customer)s) AND l.customer_id<>e.customer_id LIMIT 1',
            'SELECT 1 FROM ss_credit_lot_mutations m JOIN ss_credit_lots l ON l.tenant_id=m.tenant_id AND l.id=m.lot_id JOIN ss_credit_entries e ON e.tenant_id=m.tenant_id AND e.id=m.entry_id WHERE m.tenant_id=%(tenant)s AND (l.customer_id=%(customer)s OR e.customer_id=%(customer)s) AND l.customer_id<>e.customer_id LIMIT 1',
            'SELECT 1 FROM ss_credit_lot_reservations a JOIN ss_credit_lots l ON l.tenant_id=a.tenant_id AND l.id=a.lot_id JOIN ss_reservations r ON r.tenant_id=a.tenant_id AND r.id=a.reservation_id WHERE a.tenant_id=%(tenant)s AND (l.customer_id=%(customer)s OR r.customer_id=%(customer)s) AND l.customer_id<>r.customer_id LIMIT 1',
            'SELECT 1 FROM ss_square_settlements s JOIN ss_reservations r ON r.tenant_id=s.tenant_id AND r.id=s.reservation_id JOIN ss_credit_entries e ON e.tenant_id=s.tenant_id AND e.id=s.entry_id WHERE s.tenant_id=%(tenant)s AND (r.customer_id=%(customer)s OR e.customer_id=%(customer)s) AND r.customer_id<>e.customer_id LIMIT 1',
            'SELECT 1 FROM ss_credit_returns k JOIN ss_square_settlements s ON s.tenant_id=k.tenant_id AND s.source_order_id=k.source_order_id JOIN ss_reservations r ON r.tenant_id=s.tenant_id AND r.id=s.reservation_id JOIN ss_credit_entries e ON e.tenant_id=k.tenant_id AND e.id=k.entry_id WHERE k.tenant_id=%(tenant)s AND (r.customer_id=%(customer)s OR e.customer_id=%(customer)s) AND r.customer_id<>e.customer_id LIMIT 1',
            'SELECT 1 FROM ss_trade_receipts t JOIN ss_credit_entries e ON e.tenant_id=t.tenant_id AND e.id=t.ledger_entry_id WHERE t.tenant_id=%(tenant)s AND (t.customer_id=%(customer)s OR e.customer_id=%(customer)s) AND t.customer_id<>e.customer_id LIMIT 1',
        )
        for query in checks:
            self.check()
            if self.db.execute('/* privacy-integrity */ '+query,self.params).fetchone():
                raise ServiceError('Linked customer evidence is inconsistent. Owner review is required before export.',409)
        for item in DATASETS:
            self.check()
            self.expected[item.name]=int(self.db.execute(f'/* privacy-count:{item.name} */ SELECT count(*) AS count FROM ({item.query}) AS selected',self.params).fetchone()['count'])
        self.start_audit_id=self.service.audit(self.actor,self.id,'customer-export-started',
            self.checked|{'customer_id':str(self.customer),'session_binding':self.service.binding(self.actor,self.id),
                          'schema_version':EXPORT_VERSION,'snapshot_at':self.snapshot_at.isoformat()})

    def clean(self, value, depth=0):
        if depth>30:raise ServiceError('Linked export evidence exceeds the supported nesting limit.',413)
        if isinstance(value,dict):
            result={}
            for key,item in value.items():
                if FORBIDDEN_KEY.search(str(key)):
                    self.redacted+=1;continue
                if key=='customer_id' and str(item) not in (str(self.customer),str(self.square_customer_id)):
                    raise ServiceError('Linked customer evidence is inconsistent. No complete export was generated.',409)
                if key=='tenant_id' and str(item)!=str(self.actor['tenant_id']):
                    raise ServiceError('Linked store evidence is inconsistent. No complete export was generated.',409)
                if key=='customer' and isinstance(item,dict) and 'id' in item and str(item['id'])!=str(self.customer):
                    raise ServiceError('Accepted receipt customer evidence is inconsistent. No complete export was generated.',409)
                if key=='square_customer_id' and item not in (None,self.square_customer_id):
                    raise ServiceError('Square customer evidence needs owner review before export.',409)
                result[key]=self.clean(item,depth+1)
            return result
        if isinstance(value,(list,tuple)):return [self.clean(item,depth+1) for item in value]
        return value

    def line(self, value, *, chained=True):
        content=json_line(value)
        if self.bytes_generated+len(content)>self.service.limits.max_stream_bytes:
            raise ServiceError('Export exceeded its byte limit. No complete export was generated.',413)
        self.bytes_generated+=len(content)
        if chained:self.chain=hashlib.sha256(self.chain+content).digest()
        return content

    def iter_bytes(self):
        try:
            self.check()
            yield self.line({'kind':'manifest','schema':EXPORT_SCHEMA,'version':EXPORT_VERSION,'export_id':str(self.id),
                'start_audit_id':self.start_audit_id,'store':self.actor['slug'],'customer_id':str(self.customer),
                'snapshot_at':self.snapshot_at,'database_schema_version':self.database_version,'record_counts':self.expected,
                'attestation':self.checked,'scope_limitations':SCOPE_LIMITATIONS,
                'hash_algorithm':'SHA256 chain: start with 32 zero bytes; SHA256(previous digest || each UTF-8 manifest/page line including LF). Footer is excluded.',
                'engineering_limits':{'page_rows':self.service.limits.page_rows,'max_seconds':self.service.limits.max_seconds,
                                      'max_record_bytes':self.service.limits.max_record_bytes,'max_stream_bytes':self.service.limits.max_stream_bytes,
                                      'max_page_bytes':self.service.limits.max_page_bytes,'cursor_fetch_rows':1,
                                      'browser_max_bytes':self.service.limits.browser_max_bytes,
                                      'recent_sign_in_seconds':self.service.limits.recent_sign_in_seconds}})
            for item in DATASETS:
                self.check()
                with self.db.cursor(name='privacy_'+uuid.uuid4().hex) as cursor:
                    cursor.itersize=1
                    cursor.execute(item.query,self.params)
                    values=[];page_bytes=0
                    while True:
                        if self.service.clock()-self.started>self.service.limits.max_seconds:
                            raise ServiceError('Export exceeded its processing limit. No complete export was generated.',413)
                        rows=cursor.fetchmany(1)
                        if not rows:break
                        for row in rows:
                            value=dict(row)
                            if item.name=='audit':
                                details=value.get('details',{})
                                if not isinstance(details,dict):raise ServiceError('Customer audit evidence is inconsistent.',409)
                                self.redacted+=len(set(details)-AUDIT_DETAIL_FIELDS)
                                value['details']={key:entry for key,entry in details.items() if key in AUDIT_DETAIL_FIELDS}
                            value=self.clean(value)
                            record_bytes=len(json_line(value))
                            if record_bytes>self.service.limits.max_record_bytes:
                                raise ServiceError('A linked record exceeds the export limit. No complete export was generated.',413)
                            # Reserve ample space for the fixed page envelope.
                            # Fetching1 avoids materializing50 large JSON rows
                            # before applying this byte bound.
                            if values and (page_bytes+record_bytes>self.service.limits.max_page_bytes-1024
                                           or len(values)>=self.service.limits.page_rows):
                                self.counts[item.name]+=len(values);self.pages+=1;self.check()
                                yield self.line({'kind':'page','dataset':item.name,'page':self.pages,'records':values,'count':len(values)})
                                values=[];page_bytes=0
                            values.append(value)
                            page_bytes+=record_bytes
                    if values:
                        self.counts[item.name]+=len(values);self.pages+=1
                        self.check()
                        yield self.line({'kind':'page','dataset':item.name,'page':self.pages,'records':values,'count':len(values)})
            self.check()
            if self.counts!=self.expected:raise ServiceError('Export counts did not match the snapshot. No complete export was generated.',409)
            yield self.line({'kind':'complete','schema':EXPORT_SCHEMA,'version':EXPORT_VERSION,'export_id':str(self.id),
                'start_audit_id':self.start_audit_id,'record_counts':self.counts,'pages':self.pages,
                'content_chain_sha256':self.chain.hex(),'bytes_before_footer':self.bytes_generated,'redacted_fields':self.redacted},chained=False)
            self.footer_produced=True
        except GeneratorExit:
            self.failure_code='client_interrupted';raise
        except Exception as error:
            self.failure_code=('authorization_changed' if isinstance(error,ServiceError) and error.status in (401,403)
                               else 'engineering_limit' if isinstance(error,ServiceError) and error.status==413 else 'stream_failed')
            yield json_line({'kind':'error','export_id':str(self.id),'code':self.failure_code,
                             'error':'No complete export was generated. Refresh your owner session or contact support with this export ID.'})
        finally:
            self.close()

    def close(self):
        if self.closed:return
        self.closed=True
        cleanup_ok=True
        try:
            if self.context is not None:self.context.__exit__(None,None,None)
        except Exception:
            cleanup_ok=False
        finally:
            self.db=None;self.context=None;self.token=''
            self.service.active.release()
        if self.start_audit_id is not None:
            outcome='footer_produced' if self.footer_produced and cleanup_ok else 'interrupted'
            try:
                self.service.audit(self.actor,self.id,'customer-export-stream-ended',
                    {'customer_id':str(self.customer),'start_audit_id':self.start_audit_id,'outcome':outcome,
                     'failure_code':self.failure_code or (None if cleanup_ok else 'snapshot_cleanup_failed'),
                     'record_counts':dict(self.counts),'content_chain_sha256':self.chain.hex(),'redacted_fields':self.redacted})
            except Exception:
                # The browser must receive normal EOF and confirm the persisted
                # end audit before it can call any download complete.
                if self.footer_produced:raise ServiceError('Export audit did not finish. No completed download is authorized.',503) from None
