"""Private bounded shared-input snapshots and preparation; no writes or grants.

Caller owns schema9 attestation, fresh source/actor locks and the SAME active
transaction through any later registry suffix. Absent-row locks do not reserve
keys. No input/job/policy/worker/lifecycle or publication authority exists here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import json
import re

from psycopg.pq import TransactionStatus
from intake_label_contract import MAX_INPUT_BYTES, canonical, input_hash, validate_input
from .intake_label_context import CapturedLabelContext
from .receiving import ExpectedLabelSource
from .security import ServiceError


FIELDS = ('tenant_id', 'id', 'receiving_id', 'server_revision', 'plan_hash', 'input_hash',
    'terminal_basis_hash', 'confirmed_rows_hash', 'row_count', 'total_copies', 'input_body',
    'content_state', 'revision', 'created_at', 'policy_hash', 'retention_until',
    'restricted_at', 'review_due_at', 'disposed_at', 'suppression_until')
_DATES = ('created_at', 'retention_until', 'restricted_at', 'review_due_at',
          'disposed_at', 'suppression_until')
_FLAGS = ('_scalar_valid', '_body_valid', '_body_too_large', '_dates_valid')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_HASH = re.compile(r'[0-9a-f]{64}')
_DATE = re.compile(r'[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{6}Z')
_SCHEMA = re.compile(r'(?:public|ss_test_[0-9a-f]{32})')
_UTF8_SQL = "(pg_catalog.current_setting('server_encoding')='UTF8' AND pg_catalog.current_setting('client_encoding')='UTF8')"
_MESSAGES = {
    'INVALID': 'The shared label input is unavailable or inconsistent. Preserve its reference for review.',
    'CHANGED': 'The shared label input changed. Reload its verified context before continuing.',
    'UNAVAILABLE': 'The shared label input is restricted, disposed or outside its approved retention period.',
    'LIMIT': 'The shared label input exceeds the supported content budget.',
    'RELOAD_REQUIRED': 'Another shared label input now owns these keys. Reload before continuing.',
    'TRANSACTION_REQUIRED': 'A caller-owned active database transaction is required for shared label input checks.',
}


class LabelInputError(ServiceError):
    def __init__(self, kind='INVALID'):
        if type(kind) is not str or kind not in _MESSAGES:
            kind = 'INVALID'
        self.code = 'INTAKE_LABEL_INPUT_' + kind
        super().__init__(_MESSAGES[kind], 409)


def _error(kind='INVALID'):
    raise LabelInputError(kind)


def _safe(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LabelInputError:
            raise
        except Exception:
            pass
        # Fixed replacement outside owned handlers: no driver/decoder context.
        _error()
    return invoke


def _uuid(value):
    return type(value) is str and _UUID.fullmatch(value) is not None


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _date(value, nullable=False):
    # SQL attests the calendar/domain before transfer. Final CAS never parses it.
    return value is None and nullable or (type(value) is str and len(value) == 27
        and not value.startswith('0000-') and _DATE.fullmatch(value) is not None)


def _raw_shape(values):
    if type(values) is not tuple or len(values) != 20:
        return False
    tenant, ident, receiving, server, plan, hashed, basis, rows, count, copies, body, state, revision, created, policy, retention, restricted, review, disposed, suppression = values
    if (not all(_uuid(value) for value in (tenant, ident, receiving))
        or not all(_hash(value) for value in (plan, hashed, basis, rows, policy))
        or type(server) is not int or not 0 <= server <= 2**63-1
        or type(count) is not int or not 1 <= count <= 1000
        or type(copies) is not int or not count <= copies <= 1000000
        or type(revision) is not int or not 1 <= revision <= 2**63-1
        or type(state) is not str or state not in ('active', 'restricted', 'disposal_pending', 'disposed')
        or not _date(created) or not _date(retention) or retention <= created
        or not all(_date(value, True) for value in (restricted, review, disposed, suppression))):
        return False
    present = type(body) is str and 1 <= len(body) <= MAX_INPUT_BYTES and '\0' not in body
    if state == 'active':
        return present and all(value is None for value in (restricted, review, disposed, suppression))
    if state in ('restricted', 'disposal_pending'):
        return present and restricted is not None and review is not None and disposed is None and suppression is None
    return (body is None and restricted is not None and review is None and disposed is not None
            and suppression is not None and suppression > disposed)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedLabelInput:
    tenant_id: str
    id: str
    receiving_id: str
    server_revision: int
    plan_hash: str
    input_hash: str
    terminal_basis_hash: str
    confirmed_rows_hash: str
    row_count: int
    total_copies: int
    input_body: str | None
    content_state: str
    revision: int
    created_at: str
    policy_hash: str
    retention_until: str
    restricted_at: str | None
    review_due_at: str | None
    disposed_at: str | None
    suppression_until: str | None

    def __post_init__(self):
        if type(self) is not ExpectedLabelInput or not _raw_shape(tuple(getattr(self, name) for name in FIELDS)):
            _error()

    def __repr__(self):
        return '<ExpectedLabelInput>'

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class PreparedLabelInput:
    expected: ExpectedLabelInput | None
    tenant_id: str
    input_id: str
    input_hash: str
    terminal_basis_hash: str
    insert_values: tuple | None

    def __repr__(self):
        return '<PreparedLabelInput>'

    __str__ = __repr__


def _transaction(db):
    valid = False
    try:
        autocommit, status = db.autocommit, db.info.transaction_status
        valid = (type(autocommit) is bool and autocommit is False
                 and type(status) is TransactionStatus and status is TransactionStatus.INTRANS)
    except Exception:
        pass
    if not valid:
        _error('TRANSACTION_REQUIRED')


def _table(schema):
    if type(schema) is not str or _SCHEMA.fullmatch(schema) is None:
        _error()
    return '"' + schema + '"."ss_intake_label_inputs"'


def _sql_date(name, nullable):
    value = 'i.' + name
    domain = ("pg_catalog.pg_typeof(" + value + ")='pg_catalog.timestamptz'::pg_catalog.regtype"
        + ' AND pg_catalog.isfinite(' + value + ')'
        + " AND " + value + ">='0001-01-01 00:00:00+00'::pg_catalog.timestamptz"
        + " AND " + value + "<='9999-12-31 23:59:59.999999+00'::pg_catalog.timestamptz")
    return '(' + value + ' IS NULL OR (' + domain + '))' if nullable else '(' + domain + ')'


# All expression identifiers are source-owned constants. Only the separately
# validated, quoted application schema and one of two literal lookup columns vary.
_SCALAR_SQL = ' AND '.join(
    ["pg_catalog.pg_typeof(i." + name + ")='pg_catalog.uuid'::pg_catalog.regtype AND i." + name + ' IS NOT NULL'
     for name in ('tenant_id', 'id', 'receiving_id')]
    + ["pg_catalog.pg_typeof(i." + name + ")='pg_catalog.text'::pg_catalog.regtype"
       + ' AND pg_catalog.octet_length(i.' + name + ")=64 AND i." + name + " ~ '^[0-9a-f]{64}$'"
       for name in ('plan_hash', 'input_hash', 'terminal_basis_hash', 'confirmed_rows_hash', 'policy_hash')]
    + ["pg_catalog.pg_typeof(i.server_revision)='pg_catalog.int8'::pg_catalog.regtype AND i.server_revision BETWEEN 0 AND 9223372036854775807",
       "pg_catalog.pg_typeof(i.revision)='pg_catalog.int8'::pg_catalog.regtype AND i.revision BETWEEN 1 AND 9223372036854775807",
       "pg_catalog.pg_typeof(i.row_count)='pg_catalog.int4'::pg_catalog.regtype AND i.row_count BETWEEN 1 AND 1000",
       "pg_catalog.pg_typeof(i.total_copies)='pg_catalog.int4'::pg_catalog.regtype AND i.total_copies BETWEEN i.row_count AND 1000000",
       "pg_catalog.pg_typeof(i.content_state)='pg_catalog.text'::pg_catalog.regtype AND pg_catalog.octet_length(i.content_state)<=16 AND i.content_state IN ('active','restricted','disposal_pending','disposed')"])
_DATES_SQL = ' AND '.join(_sql_date(name, name not in ('created_at', 'retention_until')) for name in _DATES)
_PROJECTION = []
for _name in FIELDS:
    if _name in _DATES:
        _value = ("CASE WHEN v.dates_valid THEN pg_catalog.to_char(i." + _name
                  + " AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') ELSE NULL END")
    elif _name == 'input_body':
        _value = 'CASE WHEN v.body_valid THEN i.input_body ELSE NULL END'
    else:
        _raw = 'i.' + _name + ('::pg_catalog.text' if _name in ('tenant_id', 'id', 'receiving_id') else '')
        _value = 'CASE WHEN v.scalar_valid THEN ' + _raw + ' ELSE NULL END'
    _PROJECTION.append(_value + ' AS ' + _name)
_SELECT = ('SELECT ' + ','.join(_PROJECTION)
    + ',v.scalar_valid AS _scalar_valid,v.body_valid AS _body_valid,'
    + 'v.body_too_large AS _body_too_large,v.dates_valid AS _dates_valid'
    + ' FROM __SS_INPUT_TABLE__ AS i CROSS JOIN LATERAL (SELECT'
    + ' COALESCE((' + _UTF8_SQL + ' AND ' + _SCALAR_SQL + '),FALSE) AS scalar_valid,'
    + ' COALESCE((' + _UTF8_SQL + " AND pg_catalog.pg_typeof(i.input_body)='pg_catalog.text'::pg_catalog.regtype AND"
    + ' (i.input_body IS NULL OR pg_catalog.octet_length(i.input_body) BETWEEN 1 AND 8388608)),FALSE) AS body_valid,'
    + ' COALESCE((i.input_body IS NOT NULL AND pg_catalog.octet_length(i.input_body)>8388608),FALSE) AS body_too_large,'
    + ' COALESCE((' + _DATES_SQL + '),FALSE) AS dates_valid) AS v'
    + ' WHERE i.tenant_id=%s AND i.__SS_LOOKUP_FIELD__=%s ORDER BY i.id LIMIT 2 FOR SHARE OF i')
del _PROJECTION, _name, _value, _raw


def _query(db, table, field, tenant, key):
    statement = _SELECT.replace('__SS_INPUT_TABLE__', table).replace('__SS_LOOKUP_FIELD__', field)
    _transaction(db)
    rows = db.execute(statement, (tenant, key)).fetchall()
    _transaction(db)
    if type(rows) is not list or len(rows) > 1:
        _error('CHANGED')
    return rows


def _row(value):
    if (type(value) is not dict or any(type(key) is not str for key in value)
        or set(value) != set(FIELDS) | set(_FLAGS) or any(type(value[name]) is not bool for name in _FLAGS)):
        _error()
    if value['_body_too_large']:
        _error('LIMIT')
    if not all(value[name] for name in ('_scalar_valid', '_body_valid', '_dates_valid')):
        _error()
    return ExpectedLabelInput(*(value[name] for name in FIELDS))


def _values(expected):
    if type(expected) is not ExpectedLabelInput:
        _error()
    values = tuple(getattr(expected, name) for name in FIELDS)
    if not _raw_shape(values):
        _error()
    return values


def _capture(db, tenant, hashed, basis, schema):
    table = _table(schema)
    if not _uuid(tenant) or not _hash(hashed) or not _hash(basis):
        _error()
    _transaction(db)
    left = _query(db, table, 'input_hash', tenant, hashed)
    right = _query(db, table, 'terminal_basis_hash', tenant, basis)
    _transaction(db)
    if not left and not right:
        return None
    if not left or not right:
        _error('CHANGED')
    first, second = _row(left[0]), _row(right[0])
    if (_values(first) != _values(second) or first.tenant_id != tenant
        or first.input_hash != hashed or first.terminal_basis_hash != basis):
        _error('CHANGED')
    _transaction(db)
    return first


@_safe
def capture_by_hashes(db, tenant_id, input_hash, terminal_basis_hash, *, schema):
    """Raw comparison data only; caller already owns source/schema authority."""
    return _capture(db, tenant_id, input_hash, terminal_basis_hash, schema)


@_safe
def assert_input_current(db, expected, *, tenant_id, input_hash, terminal_basis_hash, schema):
    """Final raw input CAS only, after actual source CAS on this transaction."""
    before = None if expected is None else _values(expected)
    current = _capture(db, tenant_id, input_hash, terminal_basis_hash, schema)
    if before is None:
        if current is not None:
            _error('RELOAD_REQUIRED')
    elif current is None or before != _values(current):
        _error('CHANGED')
    _transaction(db)


def _timestamp(value):
    if not _date(value):
        _error()
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)


def _context(context):
    if (type(context) is not CapturedLabelContext or type(context.input_body) is not str
        or not 1 <= len(context.input_body) <= MAX_INPUT_BYTES or not _hash(context.input_hash)):
        _error()
    if len(context.input_body.encode('utf-8')) > MAX_INPUT_BYTES:
        _error('LIMIT')
    value = validate_input(json.loads(context.input_body))
    if canonical(value) != context.input_body or input_hash(value) != context.input_hash:
        _error()
    pairs = (('tenant_id', 'tenantId'), ('receiving_id', 'receivingId'), ('server_revision', 'serverRevision'),
        ('plan_hash', 'planHash'), ('physical_intake_id', 'physicalIntakeId'),
        ('physical_manifest_hash', 'physicalManifestHash'), ('source_snapshot_hash', 'sourceSnapshotHash'),
        ('terminal_receipt_hash', 'terminalReceiptHash'), ('provider_hash', 'providerHash'),
        ('confirmed_rows_hash', 'confirmedRowsHash'), ('terminal_basis_hash', 'terminalBasisHash'))
    if any(type(getattr(context, field)) is not type(value[key]) or getattr(context, field) != value[key]
           for field, key in pairs):
        _error()
    profile = value['labelProfile']
    wanted = (profile['stock'], profile['layout'], profile['dpi'])
    if (type(context.label_profile) is not tuple or len(context.label_profile) != 3
        or any(type(left) is not type(right) or left != right for left, right in zip(context.label_profile, wanted))
        or type(context.row_count) is not int or context.row_count != len(value['confirmedRows'])
        or type(context.total_copies) is not int or context.total_copies != sum(row['quantity'] for row in value['confirmedRows'])):
        _error()
    source = context.expected_source
    if type(source) is not ExpectedLabelSource:
        _error()
    ExpectedLabelSource(source.tenant_id, source.claim, source.operation, source.steps)
    if (source.tenant_id != context.tenant_id or source.claim[0] != context.physical_intake_id
        or source.claim[3] != context.physical_manifest_hash or source.operation[0] != context.receiving_id
        or source.operation[1] != source.claim[0] or source.operation[3] != context.server_revision
        or source.operation[4] != context.plan_hash):
        _error()
    return context


@_safe
def prepare_input(context, expected, *, input_id, observed_at, retention_until, policy_hash):
    """Outside final guards only; prepare immutable values without writing."""
    context = _context(context)
    observed = _timestamp(observed_at)
    if not _hash(policy_hash):
        _error()
    if expected is None:
        if not _uuid(input_id) or _timestamp(retention_until) <= observed:
            _error()
        values = (context.tenant_id, input_id, context.receiving_id, context.server_revision,
            context.plan_hash, context.input_hash, context.terminal_basis_hash, context.confirmed_rows_hash,
            context.row_count, context.total_copies, context.input_body, 'active', 1, observed_at,
            policy_hash, retention_until, None, None, None, None)
        ExpectedLabelInput(*values)
        return PreparedLabelInput(None, context.tenant_id, input_id, context.input_hash,
                                  context.terminal_basis_hash, values)
    if input_id is not None or retention_until is not None:
        _error()
    _values(expected)
    for name in _DATES:
        if getattr(expected, name) is not None:
            _timestamp(getattr(expected, name))
    if expected.content_state != 'active' or observed >= _timestamp(expected.retention_until):
        _error('UNAVAILABLE')
    if (expected.tenant_id != context.tenant_id or expected.receiving_id != context.receiving_id
        or expected.server_revision != context.server_revision or expected.plan_hash != context.plan_hash
        or expected.input_hash != context.input_hash or expected.terminal_basis_hash != context.terminal_basis_hash
        or expected.confirmed_rows_hash != context.confirmed_rows_hash or expected.row_count != context.row_count
        or expected.total_copies != context.total_copies or expected.input_body != context.input_body
        or expected.policy_hash != policy_hash):
        _error('CHANGED')
    return PreparedLabelInput(expected, context.tenant_id, expected.id, context.input_hash,
                              context.terminal_basis_hash, None)
