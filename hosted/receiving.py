"""Durable hosted receiving. Global and per-tenant writes default OFF.

First vertical slice reuses approved, same-price tracked variations. It never
reprices/deletes catalog objects or calls the desktop Square vault. Unknown
provider outcomes retain the exact encrypted step and seller occupancy.
"""
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
import math
import re
import uuid
import unicodedata
from psycopg.types.json import Jsonb
from psycopg.pq import TransactionStatus
from psycopg import Error as PostgreSQLError
from intake_manifest import (source_lines, source_line_id, normalize_identity, ManifestError,
    manifest_digest, semantic_fingerprint)
from catalog_physical_ownership import (CatalogOwnershipError, ownership_from_card,
    require_identity)
from scanner_adapters import GAME_LABELS, normalize_game
from .database import audit
from .intake import current_authority, _claim_result
from .receiving_plan import (API_VERSION, READ_SCOPES, WRITE_SCOPES, ReceivingError, normalize_plan,
    canonical, fingerprint, uuid_text, text_hash, integer, keys, bind_targets,
    effective_variation, inventory_body, history_proof, public_response_bytes)
from .security import ServiceError, provider_id
from .settlement import evidence_hash


RESTORATION_SOURCE_BYTES = 2_000_000
RESTORATION_STATE_DOMAIN = 'stock-and-sleeve:receiving-restoration-state:v1'
RESTORATION_SOURCE_DOMAIN = 'stock-and-sleeve:receiving-restoration-source:v1'
_RESTORATION_PLAN_FIELDS = ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind',
    'accepted_trade_id','accepted_receipt_hash','location_id','new_inventory_confirmed',
    'excluded_not_received_ack','external_ownership_confirmed','client_label_profile','lines',
    'physical_ownership_version')

# Internal label-source capture only. Legacy restoration reads retain their caps.
_LABEL_MANIFEST_BYTES = 1024 * 1024
_LABEL_PLAN_BYTES = _LABEL_RECEIPT_BYTES = 8 * 1024 * 1024
_LABEL_PROOF_BYTES = 64 * 1024
_LABEL_RESOLUTIONS_BYTES = 2 * 1024 * 1024
_LABEL_OPERATION_BYTES = 15 * 1024 * 1024
_LABEL_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_LABEL_HASH = re.compile(r'[0-9a-f]{64}')
_LABEL_DATE = re.compile(r'[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{6}Z')
_LABEL_SOURCE_MESSAGES = {
    'INTAKE_LABEL_SOURCE_INVALID': 'The original inventory-only receiving source is unavailable or inconsistent. Preserve its reference for review.',
    'INTAKE_LABEL_SOURCE_LIMIT': 'The original receiving source exceeds the shared-label capture budget. Preserve its reference for review.',
    'INTAKE_LABEL_SOURCE_CHANGED': 'The original receiving source changed. Reload its verified context before continuing.',
    'INTAKE_LABEL_SOURCE_POLICY_UNSUPPORTED': 'This receiving source policy requires a separate shared-label review.',
    'INTAKE_LABEL_SOURCE_TRANSACTION_REQUIRED': 'Shared-label source checks require the caller\'s active transaction.',
}


def _label_source_error(code='INTAKE_LABEL_SOURCE_INVALID'):
    raise ReceivingError(_LABEL_SOURCE_MESSAGES[code], code=code)


def _label_uuid(value):
    return type(value) is str and _LABEL_UUID.fullmatch(value) is not None


def _label_hash(value):
    return type(value) is str and _LABEL_HASH.fullmatch(value) is not None


def _label_date(value, *, nullable=False):
    return value is None and nullable or (type(value) is str and len(value) == 27
        and not value.startswith('0000-') and _LABEL_DATE.fullmatch(value) is not None)


def _label_raw(value, cap, *, nullable=False):
    # SQL enforces UTF8 bytes before transfer; this cheap shape check never encodes.
    return value is None and nullable or (type(value) is str and 1 <= len(value) <= cap and '\x00' not in value)


def _label_expected_shape(tenant, claim, operation, steps):
    if (not _label_uuid(tenant) or type(claim) is not tuple or len(claim) != 7
        or type(operation) is not tuple or len(operation) != 14 or type(steps) is not tuple
        or not 1 <= len(steps) <= 2):
        return False
    ci, version, state, manifest, semantic, trade, manifest_raw = claim
    oi, linked, workflow, revision, plan_hash, op_state, first, generation, until, plan, proof, receipt, catalog, resolutions = operation
    if (not _label_uuid(ci) or type(version) is not int or version != 0
        or type(state) is not str or state != 'open' or trade is not None
        or not _label_hash(manifest) or not _label_hash(semantic)
        or not _label_raw(manifest_raw, _LABEL_MANIFEST_BYTES)
        or not _label_uuid(oi) or not _label_uuid(linked) or linked != ci
        or type(workflow) is not str or workflow != 'inventory_only'
        or type(op_state) is not str or op_state != 'received'
        or type(revision) is not int or not 0 <= revision <= 2**63-1
        or type(generation) is not int or not 0 <= generation <= 2**63-1
        or not _label_hash(plan_hash) or not _label_date(first) or not _label_date(until, nullable=True)
        or not _label_raw(plan, _LABEL_PLAN_BYTES) or not _label_raw(proof, _LABEL_PROOF_BYTES)
        or not _label_raw(receipt, _LABEL_RECEIPT_BYTES)
        or not _label_raw(catalog, _LABEL_PROOF_BYTES, nullable=True)
        or not _label_raw(resolutions, _LABEL_RESOLUTIONS_BYTES, nullable=True)
        or sum(len(value) for value in (plan, proof, receipt, catalog, resolutions) if value is not None) > _LABEL_OPERATION_BYTES):
        return False
    previous = -1
    for step in steps:
        if type(step) is not tuple or len(step) != 3:
            return False
        ordinal, endpoint, digest = step
        if (type(ordinal) is not int or ordinal <= previous
            or type(endpoint) is not str or (ordinal, endpoint) not in ((0, 'catalog_batch'), (1, 'inventory_batch'))
            or not _label_hash(digest)):
            return False
        previous = ordinal
    return steps[-1][0] == 1


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedLabelSource:
    """Private raw CAS facts, never authorization or a serializable DTO."""
    tenant_id: str
    claim: tuple
    operation: tuple
    steps: tuple

    def __post_init__(self):
        if type(self) is not ExpectedLabelSource or not _label_expected_shape(self.tenant_id, self.claim, self.operation, self.steps):
            _label_source_error()

    def __repr__(self):
        return '<ExpectedLabelSource>'

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class _LabelSourceSnapshot:
    expected: ExpectedLabelSource
    claim_metadata: tuple
    observed_at: str
    lease_active: bool

    def __repr__(self):
        return '<LabelSourceSnapshot>'

    __str__ = __repr__


def _label_date_sql(column, *, nullable=False):
    # Used only to construct fixed SQL constants, never from a request or guard.
    if column not in ('created_at', 'consumed_at', 'first_admitted_at', 'lease_until', 'observed_at'):
        raise ValueError('Unsupported fixed timestamp column.')
    bounded = (f"(isfinite({column}) AND ({column} AT TIME ZONE 'UTC') >= TIMESTAMP '0001-01-01' "
               f"AND ({column} AT TIME ZONE 'UTC') < TIMESTAMP '10000-01-01')")
    return f'({column} IS NULL OR {bounded})' if nullable else f'({column} IS NOT NULL AND {bounded})'


_LABEL_DATE_FORMAT = '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\''
_LABEL_REFERENCE_SQL = 'SELECT claim_id::text AS claim_id FROM ss_receiving_operations WHERE tenant_id=%s::uuid AND id=%s::uuid'
_LABEL_CLAIM_SQL = f'''WITH locked AS MATERIALIZED (
 SELECT id,version,state,manifest_hash,semantic_hash,bound_trade_id,source_manifest,
        physical_reference,additional_delivery_of,created_at,consumed_at
 FROM ss_intake_claims WHERE tenant_id=%s::uuid AND id=%s::uuid FOR SHARE
), checked AS (
 SELECT id,version,state,manifest_hash,semantic_hash,bound_trade_id,source_manifest,
        physical_reference,additional_delivery_of,created_at,consumed_at,
        (jsonb_typeof(source_manifest)='object') IS TRUE AS source_shape_valid,
        (octet_length(source_manifest::text) BETWEEN 1 AND {_LABEL_MANIFEST_BYTES}) IS TRUE AS source_size_valid,
        (version=0 AND state='open' AND bound_trade_id IS NULL AND consumed_at IS NULL
         AND octet_length(manifest_hash)=64 AND manifest_hash ~ '^[a-f0-9]{{64}}$'
         AND octet_length(semantic_hash)=64 AND semantic_hash ~ '^[a-f0-9]{{64}}$'
         AND length(physical_reference) BETWEEN 3 AND 100 AND octet_length(physical_reference)<=400) IS TRUE AS scalars_valid,
        {_label_date_sql('created_at')} AS created_at_valid,
        {_label_date_sql('consumed_at', nullable=True)} AS consumed_at_valid
 FROM locked
)
SELECT id::text AS id,version,
 CASE WHEN scalars_valid THEN state END AS state,
 CASE WHEN scalars_valid THEN manifest_hash END AS manifest_hash,
 CASE WHEN scalars_valid THEN semantic_hash END AS semantic_hash,
 bound_trade_id::text AS bound_trade_id,
 CASE WHEN source_shape_valid AND source_size_valid THEN source_manifest::text END AS source_manifest_raw,
 CASE WHEN scalars_valid THEN physical_reference END AS physical_reference,
 additional_delivery_of::text AS additional_delivery_of,
 CASE WHEN created_at_valid THEN to_char(created_at AT TIME ZONE 'UTC',{_LABEL_DATE_FORMAT}) END AS created_at_raw,
 CASE WHEN consumed_at_valid THEN to_char(consumed_at AT TIME ZONE 'UTC',{_LABEL_DATE_FORMAT}) END AS consumed_at_raw,
 source_shape_valid,source_size_valid,scalars_valid,created_at_valid,consumed_at_valid FROM checked'''
_LABEL_OPERATION_SQL = f'''WITH locked AS MATERIALIZED (
 SELECT id,claim_id,workflow_kind,revision,plan_hash,state,first_admitted_at,lease_generation,lease_until,
        plan,inventory_proof,receipt,catalog_proof,catalog_resolutions,clock_timestamp() AS observed_at
 FROM ss_receiving_operations WHERE tenant_id=%s::uuid AND id=%s::uuid FOR SHARE
), checked AS (
 SELECT id,claim_id,workflow_kind,revision,plan_hash,state,first_admitted_at,lease_generation,lease_until,
        plan,inventory_proof,receipt,catalog_proof,catalog_resolutions,observed_at,
        (jsonb_typeof(plan)='object' AND jsonb_typeof(inventory_proof)='object' AND jsonb_typeof(receipt)='object'
         AND (catalog_proof IS NULL OR jsonb_typeof(catalog_proof)='object')
         AND (catalog_resolutions IS NULL OR jsonb_typeof(catalog_resolutions)='array')) IS TRUE AS source_shape_valid,
        (octet_length(plan::text) BETWEEN 1 AND {_LABEL_PLAN_BYTES}
         AND octet_length(inventory_proof::text) BETWEEN 1 AND {_LABEL_PROOF_BYTES}
         AND octet_length(receipt::text) BETWEEN 1 AND {_LABEL_RECEIPT_BYTES}
         AND coalesce(octet_length(catalog_proof::text),0)<={_LABEL_PROOF_BYTES}
         AND coalesce(octet_length(catalog_resolutions::text),0)<={_LABEL_RESOLUTIONS_BYTES}
         AND coalesce(octet_length(plan::text),0)+coalesce(octet_length(inventory_proof::text),0)
             +coalesce(octet_length(receipt::text),0)+coalesce(octet_length(catalog_proof::text),0)
             +coalesce(octet_length(catalog_resolutions::text),0)<={_LABEL_OPERATION_BYTES}) IS TRUE AS source_size_valid,
        (workflow_kind='inventory_only' AND state='received' AND revision>=0 AND lease_generation>=0
         AND octet_length(plan_hash)=64 AND plan_hash ~ '^[a-f0-9]{{64}}$') IS TRUE AS scalars_valid,
        ({_label_date_sql('first_admitted_at')} AND {_label_date_sql('lease_until', nullable=True)}
         AND {_label_date_sql('observed_at')}) AS dates_valid
 FROM locked
)
SELECT id::text AS id,claim_id::text AS claim_id,
 CASE WHEN scalars_valid THEN workflow_kind END AS workflow_kind,revision,
 CASE WHEN scalars_valid THEN plan_hash END AS plan_hash,
 CASE WHEN scalars_valid THEN state END AS state,
 CASE WHEN dates_valid THEN to_char(first_admitted_at AT TIME ZONE 'UTC',{_LABEL_DATE_FORMAT}) END AS first_admitted_at_raw,
 lease_generation,
 CASE WHEN dates_valid THEN to_char(lease_until AT TIME ZONE 'UTC',{_LABEL_DATE_FORMAT}) END AS lease_until_raw,
 CASE WHEN source_shape_valid AND source_size_valid THEN plan::text END AS plan_raw,
 CASE WHEN source_shape_valid AND source_size_valid THEN inventory_proof::text END AS inventory_proof_raw,
 CASE WHEN source_shape_valid AND source_size_valid THEN receipt::text END AS receipt_raw,
 CASE WHEN source_shape_valid AND source_size_valid THEN catalog_proof::text END AS catalog_proof_raw,
 CASE WHEN source_shape_valid AND source_size_valid THEN catalog_resolutions::text END AS catalog_resolutions_raw,
 CASE WHEN dates_valid THEN to_char(observed_at AT TIME ZONE 'UTC',{_LABEL_DATE_FORMAT}) END AS observed_at_raw,
 (lease_until IS NOT NULL AND lease_until>observed_at) AS lease_active,
 source_shape_valid,source_size_valid,scalars_valid,dates_valid FROM checked'''
_LABEL_STEPS_SQL = '''SELECT ordinal,
 CASE WHEN endpoint IN ('catalog_batch','inventory_batch') THEN endpoint END AS endpoint,
 CASE WHEN octet_length(body_hash)=64 THEN body_hash END AS body_hash,
 ((ordinal=0 AND endpoint='catalog_batch' OR ordinal=1 AND endpoint='inventory_batch')
  AND octet_length(body_hash)=64 AND body_hash ~ '^[a-f0-9]{64}$') IS TRUE AS step_valid
 FROM ss_receiving_steps WHERE tenant_id=%s::uuid AND operation_id=%s::uuid
 ORDER BY ordinal LIMIT 3 FOR SHARE'''


def _label_tenant(actor):
    if type(actor) is not dict:
        _label_source_error()
    tenant = actor.get('tenant_id')
    if type(tenant) is uuid.UUID:
        tenant = str(tenant)
    if not _label_uuid(tenant):
        _label_source_error()
    return tenant


def _require_label_transaction(db):
    """Local connection status only, never BEGIN/commit or an SQL probe."""
    autocommit, status = None, None
    try:
        autocommit, status = db.autocommit, db.info.transaction_status
    except Exception:
        pass
    if autocommit is not False or type(status) is not TransactionStatus or status != TransactionStatus.INTRANS:
        _label_source_error('INTAKE_LABEL_SOURCE_TRANSACTION_REQUIRED')


def _label_row_flags(row, *, dates):
    if type(row) is not dict:
        _label_source_error('INTAKE_LABEL_SOURCE_CHANGED')
    if any(type(row.get(key)) is not bool for key in ('source_shape_valid','source_size_valid','scalars_valid',*dates)):
        _label_source_error()
    if (row.get('source_shape_valid') is not True or row.get('scalars_valid') is not True
        or any(row.get(key) is not True for key in dates)):
        _label_source_error()
    if row['source_size_valid'] is not True:
        _label_source_error('INTAKE_LABEL_SOURCE_LIMIT')


def _label_sql_read(db, sql, parameters, *, many=False):
    failed = False
    try:
        cursor = db.execute(sql, parameters)
        result = cursor.fetchall() if many else cursor.fetchone()
    except PostgreSQLError:
        failed = True
    if failed:
        _label_source_error()
    return result


def _label_live_authority(db, actor):
    failed = False
    try:
        current_authority(db, actor)
    except PostgreSQLError:
        failed = True
    if failed:
        _label_source_error()


def _read_label_source_snapshot(db, tenant, ident):
    """Fixed SQL only. No JSON/date parsing, encoding, hashing or dispatch."""
    reference = _label_sql_read(db, _LABEL_REFERENCE_SQL, (tenant, ident))
    if type(reference) is not dict or set(reference) != {'claim_id'} or not _label_uuid(reference['claim_id']):
        _label_source_error('INTAKE_LABEL_SOURCE_CHANGED')
    claim = _label_sql_read(db, _LABEL_CLAIM_SQL, (tenant, reference['claim_id']))
    _label_row_flags(claim, dates=('created_at_valid', 'consumed_at_valid'))
    operation = _label_sql_read(db, _LABEL_OPERATION_SQL, (tenant, ident))
    _label_row_flags(operation, dates=('dates_valid',))
    invalid = False
    try:
        claim_values = tuple(claim[key] for key in ('id','version','state','manifest_hash','semantic_hash','bound_trade_id','source_manifest_raw'))
        operation_values = tuple(operation[key] for key in ('id','claim_id','workflow_kind','revision','plan_hash','state',
            'first_admitted_at_raw','lease_generation','lease_until_raw','plan_raw','inventory_proof_raw','receipt_raw','catalog_proof_raw','catalog_resolutions_raw'))
        metadata = tuple(claim[key] for key in ('physical_reference','additional_delivery_of','created_at_raw','consumed_at_raw'))
        observed_at, active = operation['observed_at_raw'], operation['lease_active']
    except (KeyError, TypeError, AttributeError):
        invalid = True
    if invalid:
        _label_source_error()
    if (claim_values[0] != reference['claim_id'] or operation_values[0] != ident or operation_values[1] != reference['claim_id']
        or type(active) is not bool or active is not False or not _label_date(observed_at)
        or type(metadata[0]) is not str or not 3 <= len(metadata[0]) <= 100
        or metadata[1] is not None and not _label_uuid(metadata[1])
        or not _label_date(metadata[2]) or metadata[3] is not None):
        _label_source_error('INTAKE_LABEL_SOURCE_CHANGED')
    rows = _label_sql_read(db, _LABEL_STEPS_SQL, (tenant, ident), many=True)
    if type(rows) is not list or not 1 <= len(rows) <= 2:
        _label_source_error()
    steps = []
    for row in rows:
        if type(row) is not dict or set(row) != {'ordinal','endpoint','body_hash','step_valid'} or row['step_valid'] is not True:
            _label_source_error()
        steps.append((row['ordinal'], row['endpoint'], row['body_hash']))
    expected = ExpectedLabelSource(tenant, claim_values, operation_values, tuple(steps))
    return _LabelSourceSnapshot(expected, metadata, observed_at, active)


def _label_json_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _label_source_error()
        result[key] = value
    return result


def _label_bad_number(value):
    _label_source_error()


def _label_finite_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        _label_source_error()
    return value


def _materialize_label_snapshot(snapshot):
    """Preparation only; final assertions never call this decoder."""
    c, o = snapshot.expected.claim, snapshot.expected.operation
    failed = False
    try:
        def decode(raw):
            return None if raw is None else json.loads(raw, object_pairs_hook=_label_json_pairs,
                parse_constant=_label_bad_number, parse_float=_label_finite_float)
        def date(raw):
            return None if raw is None else datetime.fromisoformat(raw[:-1]+'+00:00')
        claim = dict(zip(('id','version','state','manifest_hash','semantic_hash','bound_trade_id'), c[:6]))
        claim.update(source_manifest=decode(c[6]), physical_reference=snapshot.claim_metadata[0],
            additional_delivery_of=snapshot.claim_metadata[1],created_at=date(snapshot.claim_metadata[2]),consumed_at=None)
        row = dict(zip(('id','claim_id','workflow_kind','revision','plan_hash','state'), o[:6]))
        row.update(first_admitted_at=date(o[6]),lease_generation=o[7],lease_until=date(o[8]),
            plan=decode(o[9]),inventory_proof=decode(o[10]),receipt=decode(o[11]),
            catalog_proof=decode(o[12]),catalog_resolutions=decode(o[13]))
        observed = date(snapshot.observed_at)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, ServiceError):
        failed = True
    if failed:
        _label_source_error()
    if (type(claim['source_manifest']) is not dict or type(row['plan']) is not dict
        or type(row['inventory_proof']) is not dict or type(row['receipt']) is not dict
        or row['catalog_proof'] is not None and type(row['catalog_proof']) is not dict
        or row['catalog_resolutions'] is not None and type(row['catalog_resolutions']) is not list):
        _label_source_error()
    return claim, row, observed


def restoration_source_bytes(value):
    """The compact source read keeps the ordinary desktop response cap."""
    result=canonical(value)
    if len(result)>RESTORATION_SOURCE_BYTES:
        raise ReceivingError('The complete original receiving source exceeds the supported read budget. Keep its original reference for review.',
                             code='RESTORATION_SOURCE_LIMIT')
    return result


def _key(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_-]{1,150}',value):raise ServiceError('Use a stable receiving request key.')
    return value


def _reason(value):
    if not isinstance(value,str) or not 10<=len(value.strip())<=200 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ServiceError('A 10 to 200 character review reason is required.')
    return value.strip()


def principal(access, location):
    return {key:access.proof[key] for key in ('merchant_id','environment','application_id')}|{'location_id':location}


class Receiving:
    def __init__(self, database, provider, cipher, *, writes_enabled=False):
        self.database,self.provider,self.cipher=database,provider,cipher
        self.writes_enabled=writes_enabled is True
        from .receiving_templates import PriceTemplates
        self.templates=PriceTemplates(database,provider,self)

    def _old(self, db, tenant, key, action, intent):
        key=_key(key);hashed=fingerprint({'action':action,'intent':intent})
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':receiving-request:'+key,))
        row=db.execute('''SELECT q.*,COALESCE(o.result,q.result) AS result FROM ss_receiving_requests q LEFT JOIN ss_receiving_request_outcomes o
                          ON o.tenant_id=q.tenant_id AND o.request_key=q.request_key WHERE q.tenant_id=%s AND q.request_key=%s''',(tenant,key)).fetchone()
        if row and action=='plan' and row['action']=='plan' and row['result'].get('retired') is True:
            raise ReceivingError('This original plan request was explicitly retired before creation. Preserve its retirement proof and start a reviewed correction with a new key.',code='PLAN_REQUEST_RETIRED')
        if row and (row['intent_hash']!=hashed or row['action']!=action):raise ReceivingError('This receiving request key has different immutable terms.',code='PLAN_CHANGED')
        return hashed,row['result'] if row else None

    @staticmethod
    def _finish(db, tenant, key, action, hashed, result, operation=None):
        db.execute('INSERT INTO ss_receiving_requests(tenant_id,request_key,action,intent_hash,operation_id,result) VALUES (%s,%s,%s,%s,%s,%s)',
                   (tenant,key,action,hashed,operation,Jsonb(result)))
        return result

    @staticmethod
    def _row(db, tenant, ident, *, lock=False):
        row=db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s'+(' FOR UPDATE' if lock else ''),(tenant,uuid_text(ident,'Receiving ID'))).fetchone()
        if not row:raise ServiceError('Receiving operation not found.',404)
        if fingerprint(row['plan'])!=row['plan_hash']:raise ReceivingError('Stored receiving plan evidence is inconsistent.')
        return row

    @staticmethod
    def _view(row):
        plan=row['plan'];result={key:plan.get(key) for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','client_label_profile','workflow_kind',
            'accepted_trade_id','accepted_receipt_hash','provider','selected_quantities','excluded_quantities')}
        fields=('review_line_id','source_line_key','accepted_line_id','identity_hash','binding_id','binding_version','binding_evidence_hash',
                'source_sku','target_sku','variation_id','parent_id','selling_cents','currency','quantity','action','binding_role',
                'template_id','template_version','template_evidence_hash','physical_ownership')
        if 'physical_ownership_version' in plan:result['physical_ownership_version']=plan['physical_ownership_version']
        result.update(receiving_id=str(row['id']),revision=row['revision'],state=row['state'],plan_hash=row['plan_hash'],
            targets=[{key:target[key] for key in fields if key in target} for target in plan['targets']],
            inventory_confirmed=bool(row['inventory_proof']),receipt=row['receipt'],blocked_reasons=[],requires_fresh_mfa=True)
        result['catalog_confirmed']=bool(row.get('catalog_proof'))
        result['catalog_resolutions']=row.get('catalog_resolutions') or []
        result['catalog_proof']=row.get('catalog_proof')
        result['catalog_proof_hash']=fingerprint(row['catalog_proof']) if row.get('catalog_proof') else None
        result['catalog_resolutions_hash']=fingerprint(result['catalog_resolutions']) if row.get('catalog_proof') else None
        if row['state'] in ('outcome_unknown','needs_review'):result['blocked_reasons']=['PROVIDER_OUTCOME_UNKNOWN' if row['state']=='outcome_unknown' else 'RECEIVING_REVIEW_REQUIRED']
        return result

    @staticmethod
    def _receipt(row,targets,proof,*,received_at=None):
        """One public receipt builder, shared by admission budgeting and outcome."""
        receipt={key:row['plan'].get(key) for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','client_label_profile','workflow_kind','accepted_trade_id','accepted_receipt_hash','provider','selected_quantities','excluded_quantities')}
        receipt.update(receipt_schema_version=1,receiving_id=str(row['id']),revision=row['revision'],plan_hash=row['plan_hash'],
            confirmed_targets=[dict(target,confirmed_quantity=target['quantity']) for target in targets],inventory_confirmed=True,
            catalog_write_performed=bool(row.get('catalog_proof')),received_at=received_at or datetime.now(timezone.utc).isoformat(),provider_proof_hash=fingerprint(proof))
        if 'physical_ownership_version' in row['plan']:receipt['physical_ownership_version']=row['plan']['physical_ownership_version']
        if row.get('catalog_proof'):
            receipt.update(catalog_proof_hash=fingerprint(row['catalog_proof']),catalog_resolutions_hash=fingerprint(row['catalog_resolutions']),catalog_resolutions=row['catalog_resolutions'])
        receipt['receipt_hash']=fingerprint(receipt)
        return receipt

    @staticmethod
    def _intake_history_view(ident,operations):
        return {'version':1,'physical_intake_id':str(ident),'operations':operations,'lookup_complete':True}

    def _projection_budget(self,plan,ident,revision=0):
        """Bound the largest terminal DTO before persisting/admitting a plan.

        A catalog result may introduce an opaque provider ID up to 255 Unicode
        characters. Reserve its largest UTF-8 representation, new binding facts,
        coalesced review aliases, and both repeated receipt/resolution copies.
        This contains no provider body or customer identity/free text.
        """
        proof={'kind':'batch_create_ack','request_hash':'f'*64,'response_hash':'f'*64}
        row={'id':str(ident),'revision':max(revision,2**63-1),'state':'received','plan':plan,'plan_hash':fingerprint(plan),
             'inventory_proof':proof,'catalog_proof':None,'catalog_resolutions':[],'receipt':None}
        if plan.get('catalog_required'):
            row['catalog_proof']={'kind':'catalog_state_match','request_hash':'f'*64,'evidence_hash':'f'*64}
            for addition in plan['catalog_additions']:
                value={key:addition[key] for key in ('review_line_id','identity_hash','parent_id','target_sku','selling_cents','template_id','template_version')}
                if 'physical_ownership' in addition:value['physical_ownership']=addition['physical_ownership']
                reserved_version=2**63-1 if type(plan.get('physical_ownership_version')) is int and plan['physical_ownership_version']==2 else 1
                value.update(variation_id='\U0010ffff'*255,currency='USD',binding_id='f'*36,binding_version=reserved_version,binding_evidence_hash='f'*64,
                             review_line_ids=sorted(review for review,representative in plan['catalog_aliases'].items() if representative==addition['review_line_id']))
                row['catalog_resolutions'].append(value)
        row['receipt']=self._receipt(row,self._final_targets(row),proof,received_at='9999-12-31T23:59:59.999999+00:00')
        # Both supported recovery envelopes must fit before new admission. Keep
        # the existing returned request-envelope measurement for callers/tests.
        # Already-admitted older plans may fail history closed rather than gain
        # a larger transport budget or lose terminal proof/target rows.
        view=self._view(row)
        wire=public_response_bytes({'found':True,'result':view})
        public_response_bytes(self._intake_history_view(plan['physical_intake_id'],[view]))
        return len(wire)

    def get(self, actor, ident):
        with self.database.transaction() as db:
            current_authority(db,actor)
            return self._view(self._row(db,actor['tenant_id'],ident))

    def restoration_source(self, actor, ident):
        """Project original public input facts; never admit or recover work.

        A shared operation lock keeps plan/state/step facts coherent with a
        concurrent provider admission. No provider body/key or local PC request
        key belongs in this DTO. Authentication is repeated after projection.
        """
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        with self.database.transaction() as db:
            current_authority(db,actor)
            reference=db.execute('SELECT claim_id FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',(tenant,ident)).fetchone()
            if not reference:raise ServiceError('Receiving operation not found.',404)
            # Match admission's security -> claim -> operation lock order.
            claim=db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND id=%s FOR SHARE',(tenant,reference['claim_id'])).fetchone()
            row=db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s FOR SHARE',(tenant,ident)).fetchone()
            if not row:raise ServiceError('Receiving operation not found.',404)
            inconsistent=False
            try:
                normalized=self._verify_restoration_plan_locked(db,tenant,ident,claim,row)
                steps=db.execute('SELECT ordinal,endpoint,body_hash FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s ORDER BY ordinal FOR SHARE',
                                 (tenant,ident)).fetchall()
                stages=self._restoration_stages_locked(steps)
                now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
                result=self._finish_restoration_source_locked(ident,row,normalized,stages,now)
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError,ServiceError):
                inconsistent=True
            if inconsistent:
                raise ReceivingError('Stored original receiving source evidence is inconsistent. Preserve its original operation for review.',
                                     code='RESTORATION_SOURCE_INCONSISTENT',receiving_id=ident)
            restoration_source_bytes(result)
            current_authority(db,actor)
            return result

    def _verify_restoration_plan_locked(self,db,tenant,ident,claim,row,*,bounded=False):
        plan=row['plan']
        if not isinstance(plan,dict) or fingerprint(plan)!=row['plan_hash']:raise ValueError()
        original={key:plan[key] for key in _RESTORATION_PLAN_FIELDS if key in plan}
        normalized=normalize_plan(original)
        if canonical(normalized)!=canonical(original):raise ValueError()
        integer(row['revision'],'Server revision',0,2**63-1)
        if (str(row['claim_id'])!=normalized['physical_intake_id'] or
                row['workflow_kind']!=normalized['workflow_kind']):raise ValueError()
        if (not claim or manifest_digest(claim['source_manifest'])!=claim['manifest_hash'] or
                str(claim['id'])!=normalized['physical_intake_id'] or
                semantic_fingerprint(claim['source_manifest'])!=claim['semantic_hash'] or
                claim['manifest_hash']!=normalized['physical_manifest_hash']):raise ValueError()
        historical=(row['state']=='cancelled_before_write' and row['first_admitted_at'] is None and
                    normalized['workflow_kind']=='inventory_only')
        if historical:
            budget={line['source_line_key']:line['quantity'] for line in source_lines(claim['source_manifest'])};accepted=[]
        elif bounded:_,budget,accepted=self._source_from_locked_claim(db,tenant,normalized,claim)
        else:_,budget,accepted=self._source(db,tenant,normalized)
        accepted={str(line['id']):line for line in accepted};used={};by_accepted={}
        targets=plan.get('targets')
        if not isinstance(targets,list) or len(targets)!=len(normalized['lines']):raise ValueError()
        by_review={target['review_line_id']:target for target in targets}
        if len(by_review)!=len(targets):raise ValueError()
        for line in normalized['lines']:
            target=by_review.get(line['review_line_id'])
            if not isinstance(target,dict) or any(canonical(target.get(key))!=canonical(value) for key,value in line.items()):raise ValueError()
            key=line['source_line_key'];used[key]=used.get(key,0)+line['quantity']
            if key not in budget or used[key]>budget[key]:raise ValueError()
            identity=line.get('identity')
            if normalized['workflow_kind']=='accepted_trade':
                accepted_line=accepted.get(line['accepted_line_id'])
                if (not accepted_line or accepted_line.get('included') is not True or
                        accepted_line.get('source_line_key')!=key):raise ValueError()
                by_accepted[line['accepted_line_id']]=by_accepted.get(line['accepted_line_id'],0)+line['quantity']
                if by_accepted[line['accepted_line_id']]>integer(accepted_line['quantity'],'Accepted quantity',1,100000):raise ValueError()
                identity=accepted_line['identity']
            if normalized.get('physical_ownership_version')==2:
                try:require_identity(target.get('physical_ownership'),identity)
                except CatalogOwnershipError:raise ValueError() from None
            if (source_line_id(identity)!=target.get('identity_hash') or
                    source_line_id(target.get('identity'))!=target.get('identity_hash')):raise ValueError()
        if (canonical(plan.get('selected_quantities'))!=canonical(used) or
                canonical(plan.get('excluded_quantities'))!=canonical({key:qty-used.get(key,0) for key,qty in budget.items()})):raise ValueError()
        return normalized

    def _restoration_stages_locked(self,steps):
        stages={step['ordinal']:step for step in steps}
        if len(stages)!=len(steps) or any((step['ordinal'],step['endpoint']) not in ((0,'catalog_batch'),(1,'inventory_batch')) for step in steps):raise ValueError()
        return stages

    def _finish_restoration_source_locked(self,ident,row,normalized,stages,observed_at):
        plan=row['plan']
        admitted=row['first_admitted_at'] is not None
        active=row['lease_until'] is not None and row['lease_until']>observed_at
        state=row['state']
        if (admitted!=bool(stages) or active and not admitted or
                state in ('ready','cancelled_before_write') and admitted or
                state in ('outcome_unknown','needs_review') and not admitted or
                state in ('catalog_pending','catalog_confirmed') and 0 not in stages or
                state in ('inventory_pending','received') and 1 not in stages or
                0 in stages and plan.get('catalog_required') is not True or
                1 in stages and plan.get('catalog_required') is True and not row.get('catalog_proof')):raise ValueError()
        for name,ordinal,kinds in (('catalog_proof',0,('catalog_batch_ack','catalog_state_match')),
                                   ('inventory_proof',1,('batch_create_ack','history_adjustments'))):
            proof=row.get(name)
            if proof is not None and (not isinstance(proof,dict) or ordinal not in stages or proof.get('kind') not in kinds or
                          proof.get('request_hash')!=stages[ordinal]['body_hash']):raise ValueError()
        if state=='catalog_confirmed' and not row.get('catalog_proof'):raise ValueError()
        if row.get('catalog_proof'):self._final_targets(row)
        if state=='received':
            receipt=row['receipt']
            if not isinstance(receipt,dict) or not row.get('inventory_proof') or not receipt.get('received_at'):raise ValueError()
            expected=self._receipt(row,self._final_targets(row),row['inventory_proof'],received_at=receipt['received_at'])
            if canonical(receipt)!=canonical(expected):raise ValueError()
        elif row['receipt'] is not None:raise ValueError()
        admission={'everAdmitted':admitted,'catalogStepPresent':0 in stages,
                   'inventoryStepPresent':1 in stages,'leaseActive':bool(active)}
        result={'version':1,'receivingId':ident,'serverRevision':row['revision'],'planHash':row['plan_hash'],
            'sourceSnapshotHash':normalized['source_snapshot_hash'],'physicalIntakeId':normalized['physical_intake_id'],
            'physicalManifestHash':normalized['physical_manifest_hash'],'workflowKind':normalized['workflow_kind'],
            'acceptedTradeId':normalized.get('accepted_trade_id'),'acceptedReceiptHash':normalized.get('accepted_receipt_hash'),
            'normalizedPlan':normalized,'admission':admission}
        result['stateBasisHash']=fingerprint({'domain':RESTORATION_STATE_DOMAIN,'operation':self._view(row),'admission':admission})
        result['restorationSourceHash']=fingerprint({'domain':RESTORATION_SOURCE_DOMAIN,'source':result})
        return result

    def _assert_label_source_policy(self):
        if (getattr(type(self),'_source',None) is not Receiving._source
            or getattr(type(self),'_source_from_locked_claim',None) is not Receiving._source_from_locked_claim
            or '_source' in self.__dict__ or '_source_from_locked_claim' in self.__dict__):
            _label_source_error('INTAKE_LABEL_SOURCE_POLICY_UNSUPPORTED')

    def capture_restoration_bundle(self,db,actor,receiving_id):
        """Coherent bounded preparation on the caller's transaction, no writes."""
        _require_label_transaction(db)
        tenant=_label_tenant(actor)
        if not _label_uuid(receiving_id):_label_source_error()
        _label_live_authority(db,actor)
        _require_label_transaction(db)
        Receiving._assert_label_source_policy(self)
        snapshot=_read_label_source_snapshot(db,tenant,receiving_id)
        claim,row,observed_at=_materialize_label_snapshot(snapshot)
        inconsistent=False
        try:
            normalized=Receiving._verify_restoration_plan_locked(self,db,tenant,receiving_id,claim,row,bounded=True)
            steps=[dict(zip(('ordinal','endpoint','body_hash'),step)) for step in snapshot.expected.steps]
            stages=Receiving._restoration_stages_locked(self,steps)
            source=Receiving._finish_restoration_source_locked(self,receiving_id,row,normalized,stages,observed_at)
            operation=self._view(row)
            public_response_bytes(operation)
            claim_view=_claim_result(claim)
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,ServiceError):
            inconsistent=True
        if inconsistent:_label_source_error()
        restoration_source_bytes(source)
        _label_live_authority(db,actor)
        _require_label_transaction(db)
        return {'restorationSource':source,'operation':operation,'claim':claim_view,'expectedSource':snapshot.expected}

    def assert_label_source_current(self,db,expected_source,actor):
        """Final source CAS only; no JSON/hash/projector or arbitrary callback."""
        _require_label_transaction(db)
        tenant=_label_tenant(actor)
        values=None
        if type(expected_source) is ExpectedLabelSource:
            try:values=(expected_source.tenant_id,expected_source.claim,expected_source.operation,expected_source.steps)
            except AttributeError:pass
        if values is None or not _label_expected_shape(*values):_label_source_error()
        if values[0]!=tenant:_label_source_error('INTAKE_LABEL_SOURCE_CHANGED')
        _label_live_authority(db,actor)
        _require_label_transaction(db)
        Receiving._assert_label_source_policy(self)
        current=_read_label_source_snapshot(db,tenant,values[2][0]).expected
        if (current.tenant_id!=values[0] or current.claim!=values[1]
            or current.operation!=values[2] or current.steps!=values[3]):
            _label_source_error('INTAKE_LABEL_SOURCE_CHANGED')
        _label_live_authority(db,actor)
        _require_label_transaction(db)

    def intake_history(self, actor, intake_id):
        """Authoritative bounded lookup, never a new receiving authorization.

        The schema permanently permits one operation per tenant/physical claim,
        including cancelled plans. Fetch a sentinel rather than silently losing
        evidence if an unsupported older schema ever violates that invariant.
        """
        ident=uuid_text(intake_id,'Physical intake ID');tenant=actor['tenant_id']
        with self.database.transaction() as db:
            current_authority(db,actor)
            claim=db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND id=%s FOR SHARE',(tenant,ident)).fetchone()
            if not claim:raise ServiceError('Physical intake claim not found.',404)
            try:
                if (manifest_digest(claim['source_manifest'])!=claim['manifest_hash'] or
                        semantic_fingerprint(claim['source_manifest'])!=claim['semantic_hash']):
                    raise ManifestError('Stored claim binding mismatch.')
            except ManifestError:
                raise ReceivingError('Stored original intake evidence is inconsistent and requires review.',code='RECEIVING_HISTORY_INCONSISTENT') from None
            rows=db.execute('''SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND claim_id=%s
                               ORDER BY created_at,id LIMIT 2 FOR SHARE''',(tenant,ident)).fetchall()
            if len(rows)>1:
                raise ReceivingError('Multiple receiving operations exist for this physical intake. Preserve all evidence for review.',code='RECEIVING_HISTORY_AMBIGUOUS')
            operations=[]
            for row in rows:
                plan=row['plan']
                if (not isinstance(plan,dict) or str(row['claim_id'])!=ident or plan.get('physical_intake_id')!=ident or
                        plan.get('physical_manifest_hash')!=claim['manifest_hash'] or
                        plan.get('workflow_kind')!=row['workflow_kind'] or
                        plan.get('workflow_kind') not in ('inventory_only','accepted_trade') or
                        fingerprint(plan)!=row['plan_hash'] or
                        (row['state']=='cancelled_before_write' and row['first_admitted_at'] is not None)):
                    raise ReceivingError('Stored receiving source evidence is inconsistent and requires review.',code='RECEIVING_HISTORY_INCONSISTENT')
                # A never-admitted cancelled inventory plan remains history after
                # a legitimate later trade consumes the same physical claim.
                # It grants no authority to revive or admit that old plan.
                historical_cancel=(row['state']=='cancelled_before_write' and row['first_admitted_at'] is None and
                                   plan['workflow_kind']=='inventory_only')
                if not historical_cancel:self._source(db,tenant,plan)
                operations.append(self._view(row))
            result=self._intake_history_view(ident,operations)
            public_response_bytes(result)
            current_authority(db,actor)
            return result

    def request_status(self, actor, key):
        key=_key(key)
        with self.database.transaction() as db:
            current_authority(db,actor)
            row=db.execute('''SELECT COALESCE(o.result,q.result) AS result FROM ss_receiving_requests q LEFT JOIN ss_receiving_request_outcomes o
                              ON o.tenant_id=q.tenant_id AND o.request_key=q.request_key WHERE q.tenant_id=%s AND q.request_key=%s''',(actor['tenant_id'],key)).fetchone()
            return {'found':bool(row),'result':row['result'] if row else None}

    def readiness(self, actor):
        with self.database.transaction() as db:
            current_authority(db,actor)
            row=db.execute('SELECT enabled FROM ss_receiving_settings WHERE tenant_id=%s',(actor['tenant_id'],)).fetchone()
        tenant=bool(row and row['enabled'])
        return {'protocol_version':1,'physical_ownership_versions':[1,2],
                'write_enabled':self.writes_enabled and tenant,'server_write_enabled':self.writes_enabled,
                'tenant_write_enabled':tenant,'same_price_existing_supported':True,'new_price_supported':False,'new_parent_enabled':False,
                'fresh_mfa_required':True,'max_targets':100,'location_selection_required':True,'grants_verified':False,
                'required_scopes':list(WRITE_SCOPES)}

    def locations(self, actor):
        with self.database.transaction() as db:current_authority(db,actor)
        access=self.provider.access(actor['tenant_id'])
        result=self.provider.locations(access)
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,actor['tenant_id'],access)
        return {'locations':result}

    def catalog_games(self, actor, name=None):
        """Owner-only intake game choices; no provider call or saved mapping."""
        with self.database.transaction() as db:current_authority(db,actor,allowed_roles=('owner',))
        result={'catalog_games_version':1,'games':[{'id':key,'label':label} for key,label in GAME_LABELS.items()],
                'supports_additional_games':True}
        if name is not None:
            if (type(name) is not str or not 1<=len(name)<=500 or not 1<=len(name.strip())<=100
                or any(unicodedata.category(char) in ('Cc','Cs') for char in name)):
                raise ServiceError('Enter a game name of at most 100 characters, without control characters.')
            game,label=normalize_game(name)
            label=unicodedata.normalize('NFC',label)
            if not game or len(game)>100 or not label or len(label)>100:
                raise ServiceError('Use a shorter game name for catalog review.')
            result['resolved']={'game':game,'game_label':label}
        return result

    def catalog_lookup(self, actor, sku, location_id):
        """Owner-only discovery. A result cannot approve a mapping or stock."""
        if (type(sku) is not str or not 1<=len(sku)<=100 or not sku.strip()
            or any(unicodedata.category(char) in ('Cc','Cf','Cs') for char in sku)):
            raise ServiceError('Enter an exact Square SKU of 1 to 100 supported characters.')
        location=provider_id(location_id,'Location ID');tenant=actor['tenant_id']
        with self.database.transaction() as db:current_authority(db,actor,allowed_roles=('owner',))
        access=self.provider.access(tenant)
        bound=principal(access,location)
        def live():
            with self.database.transaction() as db:
                current_authority(db,actor,allowed_roles=('owner',));self.provider.assert_current(db,tenant,access)
                if principal(access,location)!=bound:raise ReceivingError('Square authorization changed. Repeat this lookup.',code='OAUTH_RECONSENT_REQUIRED')
        live()
        # Location validation is outside the lookup failure conversion: an
        # unavailable/foreign location is not an authoritative empty SKU result.
        self.provider.location(access,location)
        live()
        base={'lookup_version':1,'requested_sku':sku,'location_id':location,
              'state':'incomplete','lookup_complete':False,'exact_match_count':0,
              'case_variant_count':0,'candidates':[]}
        result=dict(base)
        try:
            rows=self.provider.lookup_sku(access,sku)
            live()
            if type(rows) is not list or len(rows)>20:raise ValueError()
            exact=[row for row in rows if row['item_variation_data']['sku']==sku]
            if len({row['id'] for row in rows})!=len(rows):raise ValueError()
            variants=len(rows)-len(exact)
            result.update(lookup_complete=True,exact_match_count=len(exact),case_variant_count=variants,
                          state='no_match' if not exact else 'ambiguous' if len(rows)>1 else 'unavailable')
            if len(exact)==1 and len(rows)==1:
                searched=exact[0];ident=provider_id(searched['id'],'Variation ID')
                parent_id=provider_id(searched['item_variation_data']['item_id'],'Parent ID')
                objects=self.provider.catalog(access,[ident,parent_id])
                live()
                if objects[ident]!=searched:raise ValueError()
                variation,parent=objects[ident],objects[parent_id]
                try:effective=effective_variation(variation,parent,location)
                except ReceivingError:effective=None
                if effective is not None:
                    if effective['sku']!=sku:raise ValueError()
                    item_name=parent['item_data'].get('name','');variation_name=variation['item_variation_data'].get('name','')
                    for value in (item_name,variation_name):
                        if (type(value) is not str or len(value)>512
                            or any(unicodedata.category(char) in ('Cc','Cf','Cs') for char in value)):raise ValueError()
                    result.update(state='match',candidates=[{'variation_id':ident,'parent_id':parent_id,'sku':sku,
                        'item_name':item_name,'variation_name':variation_name,
                        'selling_cents':effective['selling_cents'],'currency':'USD'}])
            self.provider.location(access,location)
        except (ServiceError,ValueError,TypeError,KeyError,AttributeError):
            result=dict(base)  # Never publish a partial candidate or raw provider error.
        live()
        return result

    def binding_preview(self, actor, data):
        keys(data,('variation_id','location_id','identity','physical_ownership_version','physical_facts'))
        with self.database.transaction() as db:current_authority(db,actor,allowed_roles=('owner',))
        try:identity=normalize_identity(data.get('identity'))
        except ManifestError:raise ServiceError('A complete reviewed identity is required.') from None
        ownership=None
        if 'physical_ownership_version' in data or 'physical_facts' in data:
            try:
                if type(data.get('physical_ownership_version')) is not int or data['physical_ownership_version']!=2:
                    raise CatalogOwnershipError()
                ownership=ownership_from_card(data.get('physical_facts'))
                if set(data['physical_facts'])!=set(ownership['facts']):raise CatalogOwnershipError()
                ownership=require_identity(ownership,identity)
            except (CatalogOwnershipError,TypeError,KeyError):
                raise ReceivingError('Review the complete physical card details using a supported mapping version.',
                                     code='CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED') from None
        variation_id=provider_id(data.get('variation_id'),'Variation ID');location=provider_id(data.get('location_id'),'Location ID')
        access=self.provider.access(actor['tenant_id']);self.provider.location(access,location)
        objects=self.provider.catalog(access,[variation_id]);variation=objects[variation_id]
        parent_id=(variation.get('item_variation_data') or {}).get('item_id')
        if parent_id not in objects:objects.update(self.provider.catalog(access,[provider_id(parent_id,'Parent ID')]))
        parent=objects[parent_id];effective=effective_variation(variation,parent,location)
        self.provider.assert_skus(access,[effective])
        facts={'identity':identity,'identity_hash':source_line_id(identity),'provider':principal(access,location),
               'snapshot_hash':fingerprint({'variation':variation,'parent':parent}),'location_id':location}|effective
        if ownership is not None:facts['physical_ownership']=ownership
        ident=uuid.uuid4();hashed=fingerprint(facts)
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));self.provider.assert_current(db,actor['tenant_id'],access)
            db.execute("INSERT INTO ss_receiving_binding_previews(tenant_id,id,facts,preview_hash,created_by,expires_at) VALUES (%s,%s,%s,%s,%s,clock_timestamp()+interval '15 minutes')",
                       (actor['tenant_id'],ident,Jsonb(facts),hashed,actor['staff_id']))
        return {'preview_id':str(ident),'preview_hash':hashed,'expires_in':900,'facts':facts,'approval_required':True}

    def binding_review(self, actor, preview_id, *, limit=25, cursor=None):
        """Read current conflicting mappings; never authorize an approval."""
        ident=uuid_text(preview_id,'Preview ID');integer(limit,'Page size',1,50)
        after=uuid_text(cursor,'Cursor') if cursor else None
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',))
            preview=db.execute('''SELECT facts,preview_hash FROM ss_receiving_binding_previews
                WHERE tenant_id=%s AND id=%s AND expires_at>clock_timestamp() FOR SHARE''',
                (actor['tenant_id'],ident)).fetchone()
            if not preview:
                raise ReceivingError('This mapping review is unavailable or expired. Read the current Square target again.',
                                     code='CATALOG_REVIEW_UNAVAILABLE')
            facts=preview['facts']
            if not isinstance(facts,dict) or fingerprint(facts)!=preview['preview_hash']:
                raise ReceivingError('The saved mapping preview is inconsistent. Read the current Square target again.',
                                     code='CATALOG_REVIEW_INVALID')
            ownership=self._binding_ownership(facts)
            # One statement keeps the global predicate independent of the page,
            # including a cursor beyond the final matching binding. Stored
            # preview scope is authoritative only for this read, never approval.
            result=db.execute('''WITH conflicts AS (
                SELECT id,version,identity,identity_hash,sku,location_id,selling_cents,physical_ownership
                FROM ss_receiving_bindings
                WHERE tenant_id=%s AND variation_id=%s AND revoked_at IS NULL
                  AND provider->>'environment'=%s AND provider->>'merchant_id'=%s
                  AND (identity_hash<>%s OR physical_ownership IS DISTINCT FROM %s::jsonb)
                ), page AS (
                    SELECT * FROM conflicts WHERE (%s::uuid IS NULL OR id>%s::uuid)
                    ORDER BY id LIMIT %s
                )
                SELECT EXISTS(SELECT 1 FROM conflicts) AS conflict_exists,
                    COALESCE((SELECT jsonb_agg(to_jsonb(page) ORDER BY id) FROM page),'[]'::jsonb) AS conflicts''',
                (actor['tenant_id'],facts['variation_id'],facts['provider']['environment'],
                 facts['provider']['merchant_id'],facts['identity_hash'],
                 Jsonb(ownership) if ownership is not None else None,after,after,limit+1)).fetchone()
            rows=result['conflicts'];more=len(rows)>limit;rows=rows[:limit]
            conflicts=[{'binding_id':row['id'],'version':row['version'],'identity':row['identity'],
                        'identity_hash':row['identity_hash'],'retained_sku':row['sku'],
                        'location_id':row['location_id'],'selling_cents':row['selling_cents']} for row in rows]
            for conflict,row in zip(conflicts,rows):
                if row['physical_ownership'] is not None:conflict['physical_ownership']=row['physical_ownership']
            return {'review_version':1,'preview_id':ident,'preview_hash':preview['preview_hash'],
                    'conflict_exists':result['conflict_exists'],'conflicts':conflicts,
                    'next_cursor':rows[-1]['id'] if more else None}

    @staticmethod
    def _binding_ownership(facts):
        if 'physical_ownership' not in facts:return None
        try:return require_identity(facts['physical_ownership'],facts['identity'])
        except (CatalogOwnershipError,KeyError):
            raise ReceivingError('The saved physical ownership evidence needs a new owner review.',
                                 code='CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED') from None

    @staticmethod
    def _lock_binding_variations(db,tenant,facts):
        """Owner and delegated allocations share one ordered physical-owner lane."""
        lanes={fingerprint({'tenant':str(tenant),'environment':row['provider']['environment'],
                           'merchant_id':row['provider']['merchant_id'],'variation_id':row['variation_id']}) for row in facts}
        for lane in sorted(lanes):
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('receiving-variation-identity:'+lane,))

    @staticmethod
    def _lock_binding_prices(db,tenant,facts):
        lanes={str(tenant)+':receiving-binding:'+row['identity_hash']+':'+row['location_id']+':'+str(row['selling_cents']) for row in facts}
        for lane in sorted(lanes):db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(lane,))

    @staticmethod
    def _assert_binding_ownership(db,tenant,facts,ownership):
        conflict=db.execute('''SELECT 1 FROM ss_receiving_bindings
            WHERE tenant_id=%s AND variation_id=%s AND revoked_at IS NULL
              AND provider->>'environment'=%s AND provider->>'merchant_id'=%s
              AND (identity_hash<>%s OR physical_ownership IS DISTINCT FROM %s::jsonb) LIMIT 1''',
            (tenant,facts['variation_id'],facts['provider']['environment'],facts['provider']['merchant_id'],
             facts['identity_hash'],Jsonb(ownership) if ownership is not None else None)).fetchone()
        if conflict:
            raise ReceivingError('This Square variation already has a different approved card identity. Review its existing mappings before approving another location.',
                                 code='CATALOG_IDENTITY_CONFLICT')

    def approve_binding(self, actor, preview_id, data):
        keys(data,('request_key','preview_hash','identity_confirmed','reason'))
        if data.get('identity_confirmed') is not True:raise ServiceError('Explicitly confirm this physical identity maps to the selected Square variation.')
        key=_key(data.get('request_key'));reason=_reason(data.get('reason'));ident=uuid_text(preview_id,'Preview ID')
        intent={'preview_id':ident,'preview_hash':text_hash(data.get('preview_hash')),'identity_confirmed':True,'reason':reason}
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',))
            _,old=self._old(db,actor['tenant_id'],key,'binding_approve',intent)
            if old:return old
            preview=db.execute('SELECT * FROM ss_receiving_binding_previews WHERE tenant_id=%s AND id=%s AND expires_at>clock_timestamp()', (actor['tenant_id'],ident)).fetchone()
            if not preview or preview['preview_hash']!=intent['preview_hash']:raise ReceivingError('The mapping preview changed or expired. Review it again.')
        facts=preview['facts'];ownership=self._binding_ownership(facts)
        if fingerprint(facts)!=preview['preview_hash']:
            raise ReceivingError('The saved physical mapping preview is inconsistent. Read the current Square target again.',
                                 code='CATALOG_REVIEW_INVALID')
        access=self.provider.access(actor['tenant_id'])
        objects=self.provider.catalog(access,[facts['variation_id'],facts['parent_id']]);self.provider.location(access,facts['location_id'])
        if principal(access,facts['location_id'])!=facts['provider'] or fingerprint({'variation':objects[facts['variation_id']],'parent':objects[facts['parent_id']]})!=facts['snapshot_hash']:
            raise ReceivingError('The provider catalog changed. Review a fresh mapping preview.',code='CATALOG_VERSION_CHANGED')
        self.provider.assert_skus(access,[facts])
        binding=uuid.uuid4()
        with self.database.transaction() as db:
            current_authority(db,actor,fresh_mfa=True,allowed_roles=('owner',));self.provider.assert_current(db,actor['tenant_id'],access)
            hashed,old=self._old(db,actor['tenant_id'],key,'binding_approve',intent)
            if old:return old
            if not db.execute('SELECT 1 FROM ss_receiving_binding_previews WHERE tenant_id=%s AND id=%s AND expires_at>clock_timestamp() FOR SHARE',(actor['tenant_id'],ident)).fetchone():
                raise ReceivingError('The mapping preview expired.')
            # Fresh owner mappings share one physical identity per seller
            # variation across locations. Exact saved outcomes return above;
            # original admitted receiving/recovery paths are not reclassified.
            self._lock_binding_variations(db,actor['tenant_id'],[facts])
            self._assert_binding_ownership(db,actor['tenant_id'],facts,ownership)
            self._lock_binding_prices(db,actor['tenant_id'],[facts])
            existing=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND identity_hash=%s AND location_id=%s AND selling_cents=%s AND revoked_at IS NULL',
                                (actor['tenant_id'],facts['identity_hash'],facts['location_id'],facts['selling_cents'])).fetchone()
            if existing:
                if existing.get('physical_ownership')!=ownership:
                    raise ReceivingError('This mapping needs explicit retirement and a new reviewed physical ownership binding.',
                                         code='CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED')
                if any(existing[field]!=facts[field] for field in ('variation_id','parent_id','sku','provider','identity_anchor_hash')):
                    raise ReceivingError('Another approved mapping conflicts with this identity/price. Revoke/review explicitly.')
                return self._finish(db,actor['tenant_id'],key,'binding_approve',hashed,{'binding_id':str(existing['id']),'version':existing['version'],'approved':True,'provider_write_performed':False})
            db.execute('''INSERT INTO ss_receiving_bindings(tenant_id,id,identity,identity_hash,provider,variation_id,parent_id,sku,location_id,selling_cents,identity_anchor_hash,evidence_hash,approved_by,approval_reason,physical_ownership)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (actor['tenant_id'],binding,Jsonb(facts['identity']),facts['identity_hash'],Jsonb(facts['provider']),facts['variation_id'],facts['parent_id'],facts['sku'],facts['location_id'],facts['selling_cents'],facts['identity_anchor_hash'],preview['preview_hash'],actor['staff_id'],reason,
                        Jsonb(ownership) if ownership is not None else None))
            audit(db,actor['tenant_id'],actor['staff_id'],'receiving-binding-approved',binding,{'identity_hash':facts['identity_hash']})
            return self._finish(db,actor['tenant_id'],key,'binding_approve',hashed,{'binding_id':str(binding),'version':1,'approved':True,'provider_write_performed':False})

    def revoke_binding(self, actor, binding_id, data):
        keys(data,('request_key','version','reason'));key=_key(data.get('request_key'));version=integer(data.get('version'),'Binding version',1)
        ident=uuid_text(binding_id,'Binding ID');reason=_reason(data.get('reason'))
        intent={'binding_id':ident,'version':version,'reason':reason}
        with self.database.transaction() as db:
            current_authority(db,actor,allowed_roles=('owner',));hashed,old=self._old(db,actor['tenant_id'],key,'binding_revoke',intent)
            if old:return old
            current_authority(db,actor,fresh_mfa=True,allowed_roles=('owner',))
            from .receiving_templates import authority_lane,revoke_delegated_authority
            authority_lane(db,actor['tenant_id'])
            row=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s FOR UPDATE',(actor['tenant_id'],ident)).fetchone()
            if not row:raise ServiceError('Catalog binding not found.',404)
            if row['version']!=version:raise ReceivingError('The catalog binding changed.')
            if row['revoked_at'] is None:
                revoke_delegated_authority(db,actor['tenant_id'],actor['staff_id'],reason,binding_id=row['id'])
                audit(db,actor['tenant_id'],actor['staff_id'],'receiving-binding-revoked',ident)
            return self._finish(db,actor['tenant_id'],key,'binding_revoke',hashed,{'binding_id':ident,'version':version,'revoked':True})

    def _source(self, db, tenant, plan, *, lock=False):
        claim=db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND id=%s'+(' FOR UPDATE' if lock else ' FOR SHARE'),(tenant,plan['physical_intake_id'])).fetchone()
        return self._source_from_locked_claim(db,tenant,plan,claim)

    def _source_from_locked_claim(self,db,tenant,plan,claim):
        if not claim or claim['manifest_hash']!=plan['physical_manifest_hash']:raise ReceivingError('The central physical claim binding is unavailable or changed.')
        if plan['workflow_kind']=='accepted_trade':
            if claim['state']!='consumed' or str(claim['bound_trade_id'])!=plan['accepted_trade_id']:raise ReceivingError('Receiving must use this claim\'s exact accepted trade.')
            trade=db.execute('SELECT * FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s',(tenant,plan['accepted_trade_id'])).fetchone()
            if (not trade or not trade['state'].startswith('accepted_') or trade['receipt_hash']!=plan['accepted_receipt_hash'] or
                    evidence_hash(trade['accepted_receipt'])!=trade['receipt_hash']):raise ReceivingError('The immutable accepted receipt evidence changed.')
            lines=trade['accepted_receipt']['lines'];budget={}
            for line in lines:
                key=line.get('source_line_key')
                if not key:raise ReceivingError('A legacy trade without original source bindings needs migration review.',code='UPGRADE_REIMPORT_REQUIRED')
                budget[key]=budget.get(key,0)+line['quantity']
        else:
            if claim['state']!='open':raise ReceivingError('An accepted trade claim cannot be received as inventory-only.')
            lines=[];budget={row['source_line_key']:row['quantity'] for row in source_lines(claim['source_manifest'])}
        return claim,budget,lines

    def _prepare(self, actor, plan):
        tenant=actor['tenant_id']
        with self.database.transaction() as db:
            current_authority(db,actor);_,budget,lines=self._source(db,tenant,plan)
            accepted={str(row['id']):row for row in lines};needed=set()
            for row in plan['lines']:
                if plan['workflow_kind']=='accepted_trade':
                    original=accepted.get(row['accepted_line_id'])
                    if not original:raise ReceivingError('The accepted line is not in the immutable receipt.')
                    identity=original['identity']
                else:identity=row['identity']
                needed.add((source_line_id(identity),row['selling_cents']))
            if len(needed)>100:raise ReceivingError('This version supports at most 100 identity/price targets.')
            bindings=db.execute('''SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND location_id=%s AND revoked_at IS NULL
                                   AND identity_hash=ANY(%s::text[]) AND selling_cents=ANY(%s::bigint[]) LIMIT 201''',
                                (tenant,plan['location_id'],list({value[0] for value in needed}),list({value[1] for value in needed}))).fetchall()
            bindings=[row for row in bindings if (row['identity_hash'],row['selling_cents']) in needed]
        if not bindings:raise ReceivingError('The owner must approve exact catalog identity/price mappings before receiving.',code='CATALOG_MAPPING_REVIEW_REQUIRED')
        if len(bindings)>100:raise ReceivingError('Use a bounded mapping query before receiving.')
        access=self.provider.access(tenant);self.provider.location(access,plan['location_id'])
        provider=principal(access,plan['location_id'])
        bindings=[row for row in bindings if row['provider']==provider]
        if not bindings:raise ReceivingError('Catalog mappings belong to a different Square connection.')
        objects=self.provider.catalog(access,[value for row in bindings for value in (row['variation_id'],row['parent_id'])])
        targets,selected,excluded=bind_targets(plan,budget,lines,bindings,objects)
        self.provider.assert_skus(access,targets)
        return dict(plan,provider=provider,targets=targets,selected_quantities=selected,excluded_quantities=excluded),access

    def create_plan(self, actor, data):
        key=_key(data.get('request_key'));tenant=actor['tenant_id']
        try:plan=normalize_plan(data)
        except ServiceError:
            with self.database.transaction() as db:
                current_authority(db,actor)
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':receiving-request:'+key,))
                retained=db.execute("SELECT result FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s AND action='plan'",(tenant,key)).fetchone()
                if retained and retained['result'].get('retired') is True:raise ReceivingError('This original plan request was retired before creation.',code='PLAN_REQUEST_RETIRED') from None
            raise
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'plan',plan)
            if old:return old
            pending=db.execute('SELECT * FROM ss_receiving_plan_intents WHERE tenant_id=%s AND request_key=%s',(tenant,key)).fetchone()
            if pending:
                if pending['wire_hash']!=fingerprint(data) or pending['intent_hash']!=fingerprint(plan):raise ReceivingError('The original plan request wire body changed.',code='PLAN_CHANGED')
            else:
                db.execute('''INSERT INTO ss_receiving_plan_intents(tenant_id,request_key,intent_hash,wire_hash,claim_id,created_by,workflow_kind,accepted_trade_id,accepted_receipt_hash)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                           (tenant,key,fingerprint(plan),fingerprint(data),plan['physical_intake_id'],actor['staff_id'],plan['workflow_kind'],plan.get('accepted_trade_id'),plan.get('accepted_receipt_hash')))
        prepared,access=self._prepare(actor,plan);ident=uuid.uuid4();hashed_plan=fingerprint(prepared)
        self._projection_budget(prepared,ident)
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'plan',plan)
            if old:return old
            self._source(db,tenant,plan,lock=True)
            existing=db.execute('SELECT id FROM ss_receiving_operations WHERE tenant_id=%s AND claim_id=%s',(tenant,plan['physical_intake_id'])).fetchone()
            if existing:raise ReceivingError('This physical claim already has a receiving operation. Recover it instead.',code='CLAIM_WORKFLOW_CONFLICT',receiving_id=existing['id'])
            db.execute('''INSERT INTO ss_receiving_operations(tenant_id,id,claim_id,workflow_kind,plan_hash,plan,created_by,updated_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',(tenant,ident,plan['physical_intake_id'],plan['workflow_kind'],hashed_plan,Jsonb(prepared),actor['staff_id'],actor['staff_id']))
            db.execute('INSERT INTO ss_receiving_plan_revisions(tenant_id,operation_id,revision,plan_hash,plan,created_by) VALUES (%s,%s,0,%s,%s,%s)',(tenant,ident,hashed_plan,Jsonb(prepared),actor['staff_id']))
            row=self._row(db,tenant,ident)
            audit(db,tenant,actor['staff_id'],'receiving-plan-created',ident,{'plan_hash':hashed_plan})
            return self._finish(db,tenant,key,'plan',hashed,self._view(row),ident)

    def retire_plan_request(self,actor,request_key,data):
        keys(data,('original_plan','retire_uncreated_request'))
        key=_key(request_key);original=data.get('original_plan')
        if data.get('retire_uncreated_request') is not True or not isinstance(original,dict) or original.get('request_key')!=key:
            raise ServiceError('Confirm retirement of the exact original uncreated plan request.')
        wire=fingerprint(original);tenant=actor['tenant_id']
        try:normalized=normalize_plan(original)
        except ServiceError:normalized={'invalid_original_plan_wire_sha256':wire}
        try:claim_id=uuid_text(original.get('physical_intake_id'),'Physical intake ID')
        except ServiceError:claim_id=None
        with self.database.transaction() as db:
            current_authority(db,actor)
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':receiving-request:'+key,))
            pending=db.execute('SELECT * FROM ss_receiving_plan_intents WHERE tenant_id=%s AND request_key=%s',(tenant,key)).fetchone()
            if pending and pending['wire_hash']!=wire:raise ReceivingError('Retirement must match the registered original wire body.',code='PLAN_CHANGED')
            existing=db.execute('SELECT * FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s',(tenant,key)).fetchone()
            if existing:
                if existing['action']!='plan':raise ReceivingError('This key belongs to another receiving action.',code='PLAN_CHANGED')
                if existing['result'].get('retired') is True:
                    if existing['result'].get('original_plan_sha256')!=wire:raise ReceivingError('The original retirement request body changed.',code='PLAN_CHANGED')
                    return existing['result']
                if existing['operation_id']:raise ReceivingError('This original request already created an operation. Recover it.',code='CLAIM_WORKFLOW_CONFLICT',receiving_id=existing['operation_id'])
                raise ReceivingError('This request has conflicting retained evidence.',code='PLAN_CHANGED')
            if pending:claim_id=str(pending['claim_id']) if pending['claim_id'] else None
            if claim_id:
                db.execute('SELECT id FROM ss_intake_claims WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,claim_id)).fetchone()
                operation=db.execute('SELECT id FROM ss_receiving_operations WHERE tenant_id=%s AND claim_id=%s',(tenant,claim_id)).fetchone()
                if operation:raise ReceivingError('This physical claim already has a receiving operation. Recover it rather than retiring the local intent.',code='CLAIM_WORKFLOW_CONFLICT',receiving_id=operation['id'])
            result={'request_key':key,'retired':True,'state':'retired_before_creation','receiving_id':None,'original_plan_sha256':wire,
                    'retirement_basis':'registered_original_wire' if pending else 'unregistered_key_no_operation'}
            hashed=fingerprint({'action':'plan','intent':normalized})
            self._finish(db,tenant,key,'plan',hashed,result)
            audit(db,tenant,actor['staff_id'],'receiving-plan-request-retired',key,{'original_plan_sha256':wire})
            return result

    @staticmethod
    def _terms(data, allowed=()):
        keys(data,('request_key','revision','plan_hash')+tuple(allowed))
        return _key(data.get('request_key')),integer(data.get('revision'),'Plan revision',0,2**63-1),text_hash(data.get('plan_hash'),'Plan hash')

    @staticmethod
    def _match(row, revision, hashed):
        if row['revision']!=revision or row['plan_hash']!=hashed:raise ReceivingError('The receiving plan changed. Refresh it before continuing.',code='PLAN_CHANGED',receiving_id=row['id'])

    def _seal(self, tenant, operation, body, *, ordinal=1, endpoint='inventory_batch'):
        if self.cipher is None:raise ServiceError('Server encryption is required for receiving.',503)
        if (ordinal,endpoint) not in ((0,'catalog_batch'),(1,'inventory_batch')):raise ServiceError('Unsupported receiving step.')
        context={'domain':'stock-sleeve-receiving-step-v1','tenant':str(tenant),'operation':str(operation),'ordinal':ordinal,'endpoint':endpoint,'api_version':API_VERSION}
        return self.cipher.encrypt(canonical({'context':context,'body':body})).decode()

    def _open(self, tenant, operation, step):
        try:
            value=__import__('json').loads(self.cipher.decrypt(step['encrypted_body'].encode()))
            ordinal=step.get('ordinal',1);endpoint=step['endpoint']
            if (ordinal,endpoint) not in ((0,'catalog_batch'),(1,'inventory_batch')):raise ValueError()
            expected={'domain':'stock-sleeve-receiving-step-v1','tenant':str(tenant),'operation':str(operation),'ordinal':ordinal,'endpoint':endpoint,'api_version':API_VERSION}
            if value['context']!=expected or step['api_version']!=API_VERSION or fingerprint(value['body'])!=step['body_hash'] or value['body']['idempotency_key']!=str(step['provider_key']):raise ValueError()
            return value['body']
        except Exception:raise ReceivingError('The encrypted receiving journal is unavailable or inconsistent. Do not create a replacement operation.') from None

    def _current_targets(self, db, tenant, plan, objects):
        claim,budget,lines=self._source(db,tenant,plan,lock=True)
        rows=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=ANY(%s::uuid[]) AND revoked_at IS NULL FOR SHARE',
                        (tenant,list({row['binding_id'] for row in plan['targets']}))).fetchall()
        targets,selected,excluded=bind_targets(plan,budget,lines,rows,objects)
        if targets!=plan['targets'] or selected!=plan['selected_quantities'] or excluded!=plan['excluded_quantities']:
            raise ReceivingError('The exact receiving targets changed. Review before any new admission.',code='CATALOG_VERSION_CHANGED')
        if any(row['provider']!=plan['provider'] for row in rows):raise ReceivingError('The catalog provider binding changed.')
        return claim

    def commit(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data,('confirmed','excluded_not_received_ack'))
        if data.get('confirmed') is not True or data.get('excluded_not_received_ack') is not True:raise ServiceError('Explicitly confirm the exact receiving plan and excluded quantities.')
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'confirmed':True,'excluded_not_received_ack':True}
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'commit',intent)
            if old:return old
            row=self._row(db,tenant,ident);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:
                hashed,_=self._old(db,tenant,key,'commit',intent)
                return self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
            settings=db.execute('SELECT enabled FROM ss_receiving_settings WHERE tenant_id=%s',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled']:
                raise ReceivingError('Hosted receiving writes are disabled for this server or store.',403,code='RECEIVING_DISABLED')
            if row['state']!='ready':raise ReceivingError('Only a ready reviewed plan can be committed.')
            self._projection_budget(row['plan'],ident,row['revision'])
            current_authority(db,actor,fresh_mfa=True)
        plan=row['plan'];access=self.provider.access(tenant,write=True)
        if principal(access,plan['location_id'])!=plan['provider']:raise ReceivingError('This operation belongs to a different Square principal.')
        self.provider.location(access,plan['location_id'])
        objects=self.provider.catalog(access,[value for target in plan['targets'] for value in (target['variation_id'],target['parent_id'])])
        self.provider.assert_skus(access,plan['targets'])
        with self.database.transaction() as db:
            current_authority(db,actor,fresh_mfa=True);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'commit',intent)
            if old:return old
            # Global lock order: staff/security/session -> claim -> operation -> seller.
            self._source(db,tenant,plan,lock=True)
            row=self._row(db,tenant,ident,lock=True);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:return self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
            settings=db.execute('SELECT enabled FROM ss_receiving_settings WHERE tenant_id=%s FOR SHARE',(tenant,)).fetchone()
            if not self.writes_enabled or not settings or not settings['enabled'] or row['state']!='ready':raise ReceivingError('Receiving admission is disabled or this plan is no longer ready.',403,code='RECEIVING_DISABLED')
            self._current_targets(db,tenant,plan,objects)
            provider=plan['provider']
            db.execute('INSERT INTO ss_receiving_seller_lanes(environment,merchant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',(provider['environment'],provider['merchant_id']))
            lane=db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE environment=%s AND merchant_id=%s FOR UPDATE',(provider['environment'],provider['merchant_id'])).fetchone()
            if lane['operation_id'] is not None and (lane['tenant_id']!=tenant or str(lane['operation_id'])!=ident):
                raise ReceivingError('Another operation has an occupied or uncertain seller lane. Recover it before more stock.',code='SELLER_OPERATION_PENDING')
            at=db.execute('SELECT clock_timestamp() AS at').fetchone()['at'];provider_key=uuid.uuid4()
            body=inventory_body(ident,plan['targets'],plan['location_id'],at.isoformat(),provider_key)
            sealed=self._seal(tenant,ident,body)
            db.execute('''INSERT INTO ss_receiving_steps(tenant_id,operation_id,ordinal,endpoint,api_version,body_hash,encrypted_body,provider_key,occurred_at,authorized_by,session_hash,authority_hash)
                          VALUES (%s,%s,1,'inventory_batch',%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (tenant,ident,API_VERSION,fingerprint(body),sealed,provider_key,at,actor['staff_id'],actor['session_hash'],fingerprint(access.proof)))
            db.execute("UPDATE ss_receiving_operations SET state='inventory_pending',first_admitted_at=%s,lease_generation=lease_generation+1,lease_until=clock_timestamp()+interval '90 seconds',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(at,actor['staff_id'],tenant,ident))
            db.execute("UPDATE ss_receiving_seller_lanes SET state='occupied',tenant_id=%s,operation_id=%s WHERE environment=%s AND merchant_id=%s",(tenant,ident,provider['environment'],provider['merchant_id']))
            row=self._row(db,tenant,ident)
            audit(db,tenant,actor['staff_id'],'receiving-inventory-admitted',ident,{'plan_hash':hashed_plan,'body_hash':fingerprint(body)})
            self._finish(db,tenant,key,'commit',hashed,self._view(row),ident)
        # Crash here leaves the durable body/key/occupancy before any HTTP.
        try:
            proof=self.provider.dispatch_inventory(access,canonical(body))
        except Exception:
            return self._save_outcome(actor,ident,row['lease_generation'],None,'PROVIDER_OUTCOME_UNKNOWN',key)
        error=None
        try:self._verify_catalog(actor,plan,access)
        except Exception:error='CATALOG_VERSION_CHANGED'
        return self._save_outcome(actor,ident,row['lease_generation'],proof,error,key)

    def _verify_catalog(self, actor, plan, access):
        with self.database.transaction() as db:current_authority(db,actor)
        objects=self.provider.catalog(access,[value for target in plan['targets'] for value in (target['variation_id'],target['parent_id'])])
        self.provider.assert_skus(access,plan['targets'])
        self.provider.location(access,plan['location_id'])
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,actor['tenant_id'],access)
            self._current_targets(db,actor['tenant_id'],plan,objects)

    def _save_outcome(self, actor, ident, generation, proof, error_code, request_key):
        tenant=actor['tenant_id']
        with self.database.transaction() as db:
            # Outcome recording is not another mutation admission. A logout or
            # staff disable after dispatch must not discard provider evidence.
            row=self._row(db,tenant,ident,lock=True)
            if row['state']=='received':return self._view(row)
            if proof:
                step=db.execute('SELECT body_hash FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(tenant,ident)).fetchone()
                try:
                    if (not isinstance(proof,dict) or set(proof)-{'kind','request_hash','response_hash','adjustment_ids','evidence_hash'} or
                            not step or proof.get('request_hash')!=step['body_hash'] or proof.get('kind') not in ('batch_create_ack','history_adjustments')):raise ValueError()
                    if proof['kind']=='batch_create_ack':text_hash(proof.get('response_hash'))
                    else:
                        text_hash(proof.get('evidence_hash'))
                        if not isinstance(proof.get('adjustment_ids'),list) or not 1<=len(proof['adjustment_ids'])<=100:raise ValueError()
                        for value in proof['adjustment_ids']:provider_id(value,'Adjustment ID')
                except (ValueError,ServiceError):proof=None;error_code='PROVIDER_OUTCOME_UNKNOWN'
            if row['lease_generation']!=generation:
                db.execute('INSERT INTO ss_receiving_attempts(id,tenant_id,operation_id,generation,action,evidence) VALUES (%s,%s,%s,%s,%s,%s)',
                           (uuid.uuid4(),tenant,ident,generation,'stale-outcome',Jsonb({'proof':proof,'code':error_code})))
                return self._view(row)
            provider=row['plan']['provider']
            if proof and not error_code:
                receipt=self._receipt(row,self._final_targets(row),proof)
                db.execute("UPDATE ss_receiving_operations SET state='received',inventory_proof=%s,receipt=%s,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(Jsonb(proof),Jsonb(receipt),tenant,ident))
                db.execute("UPDATE ss_receiving_seller_lanes SET state='idle',tenant_id=NULL,operation_id=NULL WHERE environment=%s AND merchant_id=%s AND tenant_id=%s AND operation_id=%s",(provider['environment'],provider['merchant_id'],tenant,ident))
                audit(db,tenant,actor['staff_id'],'receiving-completed',ident,{'receipt_hash':receipt['receipt_hash']})
            else:
                code=error_code if error_code in ('PROVIDER_OUTCOME_UNKNOWN','CATALOG_VERSION_CHANGED','RECEIVING_REVIEW_REQUIRED') else 'PROVIDER_OUTCOME_UNKNOWN'
                db.execute('''UPDATE ss_receiving_operations SET state=%s,inventory_proof=COALESCE(%s,inventory_proof),lease_until=NULL,updated_at=clock_timestamp()
                              WHERE tenant_id=%s AND id=%s''',('needs_review' if proof else 'outcome_unknown',Jsonb(proof) if proof else None,tenant,ident))
                db.execute("UPDATE ss_receiving_seller_lanes SET state='unknown' WHERE environment=%s AND merchant_id=%s AND tenant_id=%s AND operation_id=%s",(provider['environment'],provider['merchant_id'],tenant,ident))
                db.execute('''INSERT INTO ss_receiving_exceptions(tenant_id,operation_id,code) VALUES (%s,%s,%s) ON CONFLICT(tenant_id,operation_id,code)
                              DO UPDATE SET occurrences=ss_receiving_exceptions.occurrences+1,last_seen_at=clock_timestamp()''',(tenant,ident,code))
            db.execute('INSERT INTO ss_receiving_attempts(id,tenant_id,operation_id,generation,action,evidence) VALUES (%s,%s,%s,%s,%s,%s)',
                       (uuid.uuid4(),tenant,ident,generation,'outcome',Jsonb({'proof':proof,'code':error_code})))
            result=self._view(self._row(db,tenant,ident))
            db.execute('INSERT INTO ss_receiving_request_outcomes(tenant_id,request_key,result) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,request_key,Jsonb(result)))
            return result

    def _final_targets(self, row):return self._view(row)['targets']

    def recover(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data);ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan}
        with self.database.transaction() as db:
            current_authority(db,actor);hashed,old=self._old(db,tenant,key,'recover',intent)
            if old:return old
            initial=self._row(db,tenant,ident);self._match(initial,revision,hashed_plan)
            self._source(db,tenant,initial['plan'],lock=True)
            row=self._row(db,tenant,ident,lock=True)
            if row['state']=='received' or row['first_admitted_at'] is None:
                return self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
            if row['lease_until'] and row['lease_until']>db.execute('SELECT clock_timestamp() AS at').fetchone()['at']:
                return self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(tenant,ident)).fetchone()
            if not step:raise ReceivingError('An admitted receiving operation is missing its durable step journal.')
            body=self._open(tenant,ident,step)
            db.execute("UPDATE ss_receiving_operations SET lease_generation=lease_generation+1,lease_until=clock_timestamp()+interval '90 seconds' WHERE tenant_id=%s AND id=%s",(tenant,ident))
            row=self._row(db,tenant,ident)
            self._finish(db,tenant,key,'recover',hashed,self._view(row),ident)
        proof=row['inventory_proof'];error=None
        try:
            access=self.provider.access(tenant)
            if principal(access,row['plan']['location_id'])!=row['plan']['provider']:raise ReceivingError('Provider identity changed.')
            if not proof:
                history=self.provider.history(access,body,row['first_admitted_at'])
                proof=history_proof(body,history,row['plan']['provider']['application_id'])
            self._verify_catalog(actor,row['plan'],access)
        except Exception:error='CATALOG_VERSION_CHANGED' if proof else 'PROVIDER_OUTCOME_UNKNOWN'
        return self._save_outcome(actor,ident,row['lease_generation'],proof,error,key)

    def authorize(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data,('resume_same_operation',))
        if data.get('resume_same_operation') is not True:raise ServiceError('Explicitly authorize the same immutable operation.')
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'resume_same_operation':True}
        with self.database.transaction() as db:
            current_authority(db,actor);hashed,old=self._old(db,tenant,key,'authorize',intent)
            if old:return old
            row=self._row(db,tenant,ident);self._match(row,revision,hashed_plan)
            if row['state']!='received':current_authority(db,actor,fresh_mfa=True)
            # This first slice never redispatches an uncertain inventory call.
            audit(db,tenant,actor['staff_id'],'receiving-same-operation-authorized',ident,{'plan_hash':hashed_plan})
            return self._finish(db,tenant,key,'authorize',hashed,self._view(row),ident)

    def cancel(self, actor, ident, data):
        key,revision,hashed_plan=self._terms(data,('reason',));reason=_reason(data.get('reason'))
        ident=uuid_text(ident,'Receiving ID');tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan_hash':hashed_plan,'reason':reason}
        with self.database.transaction() as db:
            current_authority(db,actor);hashed,old=self._old(db,tenant,key,'cancel',intent)
            if old:return old
            initial=self._row(db,tenant,ident);self._source(db,tenant,initial['plan'],lock=True)
            row=self._row(db,tenant,ident,lock=True);self._match(row,revision,hashed_plan)
            if row['first_admitted_at'] is not None:raise ReceivingError('An admitted or uncertain inventory request cannot be cancelled or replaced. Recover it.',code='PROVIDER_OUTCOME_UNKNOWN',receiving_id=ident)
            db.execute("UPDATE ss_receiving_operations SET state='cancelled_before_write',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(actor['staff_id'],tenant,ident))
            audit(db,tenant,actor['staff_id'],'receiving-cancelled-before-write',ident,{'plan_hash':hashed_plan})
            return self._finish(db,tenant,key,'cancel',hashed,self._view(self._row(db,tenant,ident)),ident)

    @staticmethod
    def _require_same_ownership_protocol(original, revised):
        versions=[]
        for plan in (original,revised):
            if 'physical_ownership_version' not in plan:versions.append(None)
            elif type(plan['physical_ownership_version']) is int and plan['physical_ownership_version']==2:versions.append(2)
            else:
                raise ReceivingError('The original physical ownership protocol needs review.',
                                     code='CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED')
        if versions[0]!=versions[1]:
            raise ReceivingError('A revision must keep the original physical ownership protocol. Review a separate explicit migration instead.',
                                 code='CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED')

    def revise(self, actor, ident, data):
        values=dict(data);revision=integer(values.pop('revision',None),'Plan revision',0,2**63-1)
        ident=uuid_text(ident,'Receiving ID');key=_key(values.get('request_key'));plan=normalize_plan(values);tenant=actor['tenant_id']
        intent={'receiving_id':ident,'revision':revision,'plan':plan}
        with self.database.transaction() as db:
            current_authority(db,actor);_,old=self._old(db,tenant,key,'revise',intent)
            if old:return old
            row=self._row(db,tenant,ident)
            if row['revision']!=revision or row['first_admitted_at'] is not None or str(row['claim_id'])!=plan['physical_intake_id']:
                raise ReceivingError('Only the current unadmitted operation can be revised on its same physical claim.',code='PLAN_CHANGED')
            self._require_same_ownership_protocol(row['plan'],plan)
        prepared,access=self._prepare(actor,plan);new_hash=fingerprint(prepared)
        self._projection_budget(prepared,ident,revision+1)
        with self.database.transaction() as db:
            current_authority(db,actor);self.provider.assert_current(db,tenant,access)
            hashed,old=self._old(db,tenant,key,'revise',intent)
            if old:return old
            self._source(db,tenant,plan,lock=True);row=self._row(db,tenant,ident,lock=True)
            if row['revision']!=revision or row['first_admitted_at'] is not None:raise ReceivingError('The plan changed or was admitted.',code='PLAN_CHANGED')
            self._require_same_ownership_protocol(row['plan'],plan)
            db.execute("UPDATE ss_receiving_operations SET revision=revision+1,workflow_kind=%s,plan_hash=%s,plan=%s,state='ready',updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",(plan['workflow_kind'],new_hash,Jsonb(prepared),actor['staff_id'],tenant,ident))
            db.execute('INSERT INTO ss_receiving_plan_revisions(tenant_id,operation_id,revision,plan_hash,plan,created_by) VALUES (%s,%s,%s,%s,%s,%s)',(tenant,ident,revision+1,new_hash,Jsonb(prepared),actor['staff_id']))
            return self._finish(db,tenant,key,'revise',hashed,self._view(self._row(db,tenant,ident)),ident)
