"""Private policy/control observations on one caller-owned active transaction.

Fixed records and locks are comparison data, not policy or actor authority.
No schema installation, mutation, clock, codec, compatibility or lifecycle work.
"""
from dataclasses import dataclass
from functools import wraps
import re
from types import MappingProxyType

from psycopg import Error as PostgreSQLError
from psycopg.pq import TransactionStatus


__all__ = (
    'capture_policy_records_for_read', 'capture_policy_records_for_writer',
    'assert_policy_records_current_for_read',
    'assert_policy_records_current_for_writer',
)

_MESSAGE = 'Shared label policy records require a fresh review.'
_KINDS = ('INVALID', 'CHANGED', 'TRANSACTION_REQUIRED', 'DATABASE_ERROR')
_PREFIX = 'INTAKE_LABEL_POLICY_RECORDS_'
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_HASH = re.compile(r'[0-9a-f]{64}')
_DATE = re.compile(r'[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{6}Z')
_SCHEMA = re.compile(r'(?:public|ss_test_[0-9a-f]{32})')
_SCHEMA_TOKEN = '__SS_POLICY_RECORDS_SCHEMA__'
_MAX_INT8 = 9223372036854775807
_TEXT_CAP = 65536
_FLAGS = ('_scalar_valid', '_body_valid', '_body_too_large', '_dates_valid')
_REFERENCE_FIELDS = ('tenant_id', 'revision', 'policy_hash')
_UTF8 = "(pg_catalog.current_setting('server_encoding')='UTF8' AND pg_catalog.current_setting('client_encoding')='UTF8')"


class LabelPolicyRecordsError(ValueError):
    """Owned fixed error only; no rejected values or driver payload."""
    __slots__ = ('_code',)

    def __init__(self, kind='INVALID'):
        chosen = kind if type(kind) is str and kind in _KINDS else 'INVALID'
        self._code = _PREFIX + chosen
        super().__init__(_MESSAGE)

    @property
    def code(self):
        return self._code

    def __str__(self):
        return _MESSAGE

    def __repr__(self):
        return 'LabelPolicyRecordsError()'


def _fail(kind='INVALID'):
    raise LabelPolicyRecordsError(kind)


def _require(condition, kind='INVALID'):
    if not condition:
        _fail(kind)


def _safe(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        failure = 'INVALID'
        try:
            return function(*args, **kwargs)
        except LabelPolicyRecordsError as error:
            code = getattr(error, '_code', None) if type(error) is LabelPolicyRecordsError else None
            if type(code) is str:
                failure = next((kind for kind in _KINDS
                                if code == _PREFIX + kind), 'INVALID')
        except PostgreSQLError:
            failure = 'DATABASE_ERROR'
        except Exception:
            pass
        # Leaving our handlers drops the owned caught chain before replacement.
        # Externally active caller exceptions/traceback locals are not erased.
        _fail(failure)
    return invoke


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _date_shape(value):
    # Native flags establish calendar/finite/range before transfer, not this RE.
    return (type(value) is str and len(value) == 27
            and not value.startswith('0000-') and _DATE.fullmatch(value) is not None)


def _transaction(db):
    valid = False
    try:
        valid = (type(db.autocommit) is bool and db.autocommit is False
                 and type(db.info.transaction_status) is TransactionStatus
                 and db.info.transaction_status is TransactionStatus.INTRANS)
    except Exception:
        pass
    if not valid:
        _fail('TRANSACTION_REQUIRED')


def _schema(value):
    _require(_matches(_SCHEMA, value))
    return '"' + value + '"'


class _Opaque:
    __slots__ = ()

    def __repr__(self):
        return _OPAQUE_NAMES.get(type(self), '<PolicyRecordValue>')

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedPolicyHead(_Opaque):
    tenant_id: str
    policy_revision: int
    policy_state: str
    current_version_revision: int | None
    current_policy_hash: str | None
    clock_floor_at: str | None
    clock_revision: int
    clock_state: str

    @_safe
    def __post_init__(self):
        _validate_head(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedPolicyVersion(_Opaque):
    tenant_id: str
    revision: int
    policy_hash: str
    envelope_version: int
    document_format: str
    document_body: str
    document_hash: str
    notice_body: str
    notice_hash: str
    accepted_enabled: bool
    accepted_by: str
    accepted_at: str
    evidence_use_expires_at: str

    @_safe
    def __post_init__(self):
        _validate_version(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class PolicyRecordSelector(_Opaque):
    tenant_id: str
    original_policy_hash: str | None

    @_safe
    def __post_init__(self):
        _validate_selector(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class PolicyRecordObservation(_Opaque):
    state: str
    row: ExpectedPolicyHead | ExpectedPolicyVersion | None

    @_safe
    def __post_init__(self):
        _validate_observation(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class CapturedPolicyRecords(_Opaque):
    selector: PolicyRecordSelector
    capture_kind: str
    head: PolicyRecordObservation
    current_version: PolicyRecordObservation
    original_version: PolicyRecordObservation

    @_safe
    def __post_init__(self):
        _validate_capture(self)


_OPAQUE_NAMES = MappingProxyType({
    ExpectedPolicyHead: '<ExpectedPolicyHead>',
    ExpectedPolicyVersion: '<ExpectedPolicyVersion>',
    PolicyRecordSelector: '<PolicyRecordSelector>',
    PolicyRecordObservation: '<PolicyRecordObservation>',
    CapturedPolicyRecords: '<CapturedPolicyRecords>',
})

_HEAD_SPECS = (
    ('tenant_id', 'uuid', False, None),
    ('policy_revision', 'int8', False, (1, _MAX_INT8)),
    ('policy_state', 'enum', False,
     ('unaccepted', 'enabled', 'disabled', 'restricted', 'restore_review')),
    ('current_version_revision', 'int8', True, (1, _MAX_INT8)),
    ('current_policy_hash', 'hash', True, None),
    ('clock_floor_at', 'date', True, None),
    ('clock_revision', 'int8', False, (0, _MAX_INT8)),
    ('clock_state', 'enum', False,
     ('uninitialized', 'healthy', 'blocked_regression', 'restore_review')),
)
_VERSION_SPECS = (
    ('tenant_id', 'uuid', False, None),
    ('revision', 'int8', False, (1, _MAX_INT8)),
    ('policy_hash', 'hash', False, None),
    ('envelope_version', 'int4', False, (1, 1)),
    ('document_format', 'enum', False, ('ss_label_policy_document_utf8_v1',)),
    ('document_body', 'body', False, _TEXT_CAP),
    ('document_hash', 'hash', False, None),
    ('notice_body', 'body', False, _TEXT_CAP),
    ('notice_hash', 'hash', False, None),
    ('accepted_enabled', 'bool', False, None),
    ('accepted_by', 'uuid', False, None),
    ('accepted_at', 'date', False, None),
    ('evidence_use_expires_at', 'date', False, None),
)
_REFERENCE_SPECS = tuple(spec for spec in _VERSION_SPECS
                         if spec[0] in _REFERENCE_FIELDS)
HEAD_FIELDS = tuple(spec[0] for spec in _HEAD_SPECS)
VERSION_FIELDS = tuple(spec[0] for spec in _VERSION_SPECS)
_FIELD_SPECS = MappingProxyType({
    'head': _HEAD_SPECS, 'version': _VERSION_SPECS,
    'reference': _REFERENCE_SPECS,
})
_TYPE_NAMES = MappingProxyType({
    'uuid': 'uuid', 'hash': 'text', 'enum': 'text', 'body': 'text',
    'int8': 'int8', 'int4': 'int4', 'bool': 'bool', 'date': 'timestamptz',
})


def _field_valid(spec, value):
    name, kind, nullable, detail = spec
    if value is None:
        return nullable
    if kind == 'uuid':
        return _matches(_UUID, value)
    if kind == 'hash':
        return _matches(_HASH, value)
    if kind in ('int4', 'int8'):
        return _integer(value, *detail)
    if kind == 'enum':
        return type(value) is str and value in detail
    if kind == 'bool':
        return type(value) is bool
    if kind == 'date':
        return _date_shape(value)
    if kind == 'body':
        # No codec here; native UTF8/octet flags guard the transferred bodies.
        return type(value) is str and 1 <= len(value) <= detail and '\0' not in value
    return False


def _validate_fields(row, record_type, specs):
    _require(type(row) is record_type)
    _require(all(_field_valid(spec, getattr(row, spec[0])) for spec in specs))


def _validate_head(row):
    _validate_fields(row, ExpectedPolicyHead, _HEAD_SPECS)
    selection = (row.current_version_revision, row.current_policy_hash)
    _require(all(value is None for value in selection)
             or all(value is not None for value in selection))
    if row.policy_state == 'unaccepted':
        _require(row.current_version_revision is None)
    if row.policy_state == 'enabled':
        _require(row.current_version_revision is not None)
    if row.clock_state == 'uninitialized':
        _require(row.clock_floor_at is None and row.clock_revision == 0)
    else:
        _require(row.clock_floor_at is not None and row.clock_revision >= 1)


def _validate_version(row):
    _validate_fields(row, ExpectedPolicyVersion, _VERSION_SPECS)
    _require(row.accepted_at < row.evidence_use_expires_at)


def _validate_selector(selector):
    _require(type(selector) is PolicyRecordSelector)
    _require(_matches(_UUID, selector.tenant_id))
    _require(selector.original_policy_hash is None
             or _matches(_HASH, selector.original_policy_hash))


def _validate_observation(observation):
    _require(type(observation) is PolicyRecordObservation)
    _require(type(observation.state) is str
             and observation.state in ('not_selected', 'absent', 'present'))
    if observation.state == 'present':
        if type(observation.row) is ExpectedPolicyHead:
            _validate_head(observation.row)
        elif type(observation.row) is ExpectedPolicyVersion:
            _validate_version(observation.row)
        else:
            _fail()
    else:
        _require(observation.row is None)


def _head_values(row):
    return tuple(getattr(row, name) for name in HEAD_FIELDS)


def _version_values(row):
    return tuple(getattr(row, name) for name in VERSION_FIELDS)


def _same_version(left, right):
    return _version_values(left) == _version_values(right)


def _validate_capture(captured):
    _require(type(captured) is CapturedPolicyRecords)
    _validate_selector(captured.selector)
    _require(type(captured.capture_kind) is str
             and captured.capture_kind in ('read', 'writer'))
    for observation in (captured.head, captured.current_version,
                        captured.original_version):
        _validate_observation(observation)
    _require(captured.head.state in ('absent', 'present'))
    if captured.head.state == 'present':
        _require(type(captured.head.row) is ExpectedPolicyHead)
    for observation in (captured.current_version, captured.original_version):
        if observation.state == 'present':
            _require(type(observation.row) is ExpectedPolicyVersion)

    head = captured.head.row if captured.head.state == 'present' else None
    has_current = head is not None and head.current_version_revision is not None
    if has_current:
        _require(captured.current_version.state != 'not_selected')
        _require(captured.current_version.state == 'present', 'CHANGED')
    else:
        _require(captured.current_version.state == 'not_selected')
    original_hash = captured.selector.original_policy_hash
    if original_hash is None:
        _require(captured.original_version.state == 'not_selected')
    else:
        _require(captured.original_version.state in ('absent', 'present'))

    tenant = captured.selector.tenant_id
    for observation in (captured.head, captured.current_version,
                        captured.original_version):
        if observation.state == 'present':
            _require(observation.row.tenant_id == tenant, 'CHANGED')
    current = captured.current_version.row if has_current else None
    if current is not None:
        _require(current.revision == head.current_version_revision
                 and current.policy_hash == head.current_policy_hash, 'CHANGED')
        if head.policy_state == 'enabled':
            _require(current.accepted_enabled is True, 'CHANGED')
    original = (captured.original_version.row
                if captured.original_version.state == 'present' else None)
    if original is not None:
        _require(original.policy_hash == original_hash, 'CHANGED')
    if current is not None and original_hash is not None:
        if original is None:
            _require(current.policy_hash != original_hash, 'CHANGED')
        elif (current.revision == original.revision
              or current.policy_hash == original.policy_hash):
            _require(_same_version(current, original), 'CHANGED')


_SCALAR_EXTRA = MappingProxyType({
    'head': (
        "((r.current_version_revision IS NULL)=(r.current_policy_hash IS NULL)"
        " AND (r.policy_state<>'unaccepted' OR r.current_version_revision IS NULL)"
        " AND (r.policy_state<>'enabled' OR r.current_version_revision IS NOT NULL)"
        " AND ((r.clock_state='uninitialized' AND r.clock_revision=0)"
        " OR (r.clock_state<>'uninitialized' AND r.clock_revision>=1)))"
    ),
    'version': 'TRUE',
    'reference': 'TRUE',
})
_DATE_EXTRA = MappingProxyType({
    'head': "((r.clock_state='uninitialized')=(r.clock_floor_at IS NULL))",
    'version': '(r.accepted_at<r.evidence_use_expires_at)',
})


def _native_condition(spec):
    name, kind, nullable, detail = spec
    value = 'r.' + name
    native = ("pg_catalog.pg_typeof(" + value + ")='pg_catalog."
              + _TYPE_NAMES[kind] + "'::pg_catalog.regtype")
    condition = native
    if kind == 'hash':
        condition += (" AND pg_catalog.octet_length(" + value + ")=64 AND "
                      + value + " ~ '^[0-9a-f]{64}$'")
    elif kind in ('int4', 'int8'):
        condition += ' AND ' + value + ' BETWEEN ' + str(detail[0]) + ' AND ' + str(detail[1])
    elif kind == 'enum':
        condition += " AND " + value + " IN (" + ','.join("'" + item + "'" for item in detail) + ")"
    elif kind == 'body':
        condition += ' AND pg_catalog.octet_length(' + value + ') BETWEEN 1 AND ' + str(detail)
    elif kind == 'date':
        condition += (" AND pg_catalog.isfinite(" + value + ") AND "
                      + value + ">='0001-01-01 00:00:00+00'::pg_catalog.timestamptz AND "
                      + value + "<='9999-12-31 23:59:59.999999+00'::pg_catalog.timestamptz")
    if nullable:
        return '(' + native + ' AND (' + value + ' IS NULL OR (' + condition + ')))'
    return '(' + value + ' IS NOT NULL AND ' + condition + ')'


def _make_query(role, family, columns, lock):
    # Every argument comes from the fixed private QUERY_ROLES map below.
    specs = _FIELD_SPECS[family]
    scalar = ' AND '.join(_native_condition(spec) for spec in specs
                          if spec[1] not in ('body', 'date'))
    scalar_flag = ('COALESCE((' + _UTF8 + ' AND ' + scalar + ' AND '
                   + _SCALAR_EXTRA[family] + '),FALSE) AS scalar_valid')
    if family == 'reference':
        flags = scalar_flag
    else:
        body_conditions = [_native_condition(spec) for spec in specs if spec[1] == 'body']
        body = _UTF8 + ' AND ' + ' AND '.join(body_conditions) if body_conditions else 'TRUE'
        too_large = ' OR '.join(
            '(r.' + spec[0] + ' IS NOT NULL AND pg_catalog.octet_length(r.'
            + spec[0] + ')>' + str(spec[3]) + ')'
            for spec in specs if spec[1] == 'body') or 'FALSE'
        dates = ' AND '.join(_native_condition(spec) for spec in specs if spec[1] == 'date')
        flags = (scalar_flag + ',COALESCE((' + body + '),FALSE) AS body_valid,'
                 + 'COALESCE((' + too_large + '),FALSE) AS body_too_large,'
                 + 'CASE WHEN COALESCE((' + dates + '),FALSE) THEN COALESCE(('
                 + _DATE_EXTRA[family] + '),FALSE) ELSE FALSE END AS dates_valid')
    projection = []
    for name, kind, nullable, detail in specs:
        if kind == 'date':
            value = ("CASE WHEN v.dates_valid THEN pg_catalog.to_char(r." + name
                     + " AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') ELSE NULL END")
        elif kind == 'body':
            value = 'CASE WHEN v.body_valid THEN r.' + name + ' ELSE NULL END'
        else:
            raw = 'r.' + name + ('::pg_catalog.text' if kind == 'uuid' else '')
            value = 'CASE WHEN v.scalar_valid THEN ' + raw + ' ELSE NULL END'
        projection.append(value + ' AS ' + name)
    projection.append('v.scalar_valid AS _scalar_valid')
    if family != 'reference':
        projection.extend((
            'v.body_valid AS _body_valid', 'v.body_too_large AS _body_too_large',
            'v.dates_valid AS _dates_valid',
        ))
    table = ('ss_store_label_policy_heads' if family == 'head'
             else 'ss_store_label_policy_versions')
    order = 'tenant_id' if family == 'head' else 'revision'
    return ('/* ss_label_policy_records:' + role + ' */ SELECT '
            + ','.join(projection) + ' FROM ' + _SCHEMA_TOKEN + '.' + table
            + ' AS r CROSS JOIN LATERAL (SELECT ' + flags + ') AS v WHERE '
            + ' AND '.join('r.' + name + '=%s' for name in columns)
            + ' ORDER BY r.' + order + ' LIMIT 2' + (' ' + lock if lock else ''))


QUERY_ROLES = MappingProxyType({
    'head.read': ('head', ('tenant_id',), 'FOR SHARE OF r'),
    'head.writer': ('head', ('tenant_id',), 'FOR UPDATE OF r'),
    'version.hash.discovery': ('reference', ('tenant_id', 'policy_hash'), ''),
    'version.revision': ('version', ('tenant_id', 'revision'), 'FOR SHARE OF r'),
    'version.hash.alias': ('reference', ('tenant_id', 'policy_hash'), ''),
})
_QUERIES = MappingProxyType({
    role: _make_query(role, *details) for role, details in QUERY_ROLES.items()
})


def _fetch(db, role, key, schema):
    _transaction(db)
    family = QUERY_ROLES[role][0]
    statement = _QUERIES[role].replace(_SCHEMA_TOKEN, _schema(schema))
    cursor = db.execute(statement, key)
    _transaction(db)
    rows = cursor.fetchall()
    _transaction(db)
    _require(type(rows) is list and len(rows) <= 1)
    if not rows:
        # No returned flag means no context/encoding attestation, only absence.
        return None
    row = rows[0]
    fields = tuple(spec[0] for spec in _FIELD_SPECS[family])
    flags = ('_scalar_valid',) if family == 'reference' else _FLAGS
    _require(type(row) is dict and all(type(name) is str for name in row)
             and set(row) == set(fields) | set(flags))
    _require(all(type(row[name]) is bool for name in flags))
    _require(row['_scalar_valid'])
    if family != 'reference':
        _require(row['_body_valid'] and row['_dates_valid']
                 and not row['_body_too_large'])
    if family == 'head':
        return ExpectedPolicyHead(*(row[name] for name in HEAD_FIELDS))
    if family == 'version':
        return ExpectedPolicyVersion(*(row[name] for name in VERSION_FIELDS))
    _require(all(_field_valid(spec, row[spec[0]]) for spec in _REFERENCE_SPECS))
    return tuple(row[name] for name in _REFERENCE_FIELDS)


def _reference_matches(reference, tenant, policy_hash):
    if reference is not None:
        _require(reference[0] == tenant and reference[2] == policy_hash, 'CHANGED')


def _version_reference(row):
    return (row.tenant_id, row.revision, row.policy_hash)


def _capture(db, selector, schema, kind):
    _transaction(db)
    _schema(schema)
    _validate_selector(selector)
    head = _fetch(db, 'head.' + kind, (selector.tenant_id,), schema)
    if head is not None:
        _require(head.tenant_id == selector.tenant_id, 'CHANGED')
    current_revision = head.current_version_revision if head is not None else None
    original_hash = selector.original_policy_hash
    discovered = None
    if original_hash is not None:
        discovered = _fetch(db, 'version.hash.discovery',
                            (selector.tenant_id, original_hash), schema)
        _reference_matches(discovered, selector.tenant_id, original_hash)

    revisions = []
    if current_revision is not None:
        revisions.append(current_revision)
    if discovered is not None:
        revisions.append(discovered[1])
    anchors = {}
    for revision in sorted(set(revisions)):
        version = _fetch(db, 'version.revision', (selector.tenant_id, revision), schema)
        _require(version is not None, 'CHANGED')
        _require(version.tenant_id == selector.tenant_id
                 and version.revision == revision, 'CHANGED')
        anchors[revision] = version

    if original_hash is not None:
        alias = _fetch(db, 'version.hash.alias',
                       (selector.tenant_id, original_hash), schema)
        _reference_matches(alias, selector.tenant_id, original_hash)
        _require(alias == discovered, 'CHANGED')
        if discovered is not None:
            _require(_version_reference(anchors[discovered[1]]) == discovered, 'CHANGED')

    head_observation = PolicyRecordObservation('present', head) if head is not None else PolicyRecordObservation('absent', None)
    current_observation = (PolicyRecordObservation('present', anchors[current_revision])
                           if current_revision is not None
                           else PolicyRecordObservation('not_selected', None))
    if original_hash is None:
        original_observation = PolicyRecordObservation('not_selected', None)
    elif discovered is None:
        original_observation = PolicyRecordObservation('absent', None)
    else:
        original_observation = PolicyRecordObservation('present', anchors[discovered[1]])
    _transaction(db)
    captured = CapturedPolicyRecords(selector, kind, head_observation,
                                      current_observation, original_observation)
    _transaction(db)
    return captured


def _same_observation(left, right, family):
    if left.state != right.state:
        return False
    if left.state != 'present':
        return True
    if family == 'head':
        return _head_values(left.row) == _head_values(right.row)
    return _same_version(left.row, right.row)


def _assert_current(db, expected, schema, kind):
    _transaction(db)
    _schema(schema)
    _validate_capture(expected)
    _require(expected.capture_kind == kind)
    current = _capture(db, expected.selector, schema, kind)
    for name, family in (('head', 'head'), ('current_version', 'version'),
                         ('original_version', 'version')):
        _require(_same_observation(getattr(expected, name), getattr(current, name), family),
                 'CHANGED')
    _transaction(db)


@_safe
def capture_policy_records_for_read(db, selector, *, schema):
    """Fixed shared HEAD; private raw observations only, no product authority."""
    return _capture(db, selector, schema, 'read')


@_safe
def capture_policy_records_for_writer(db, selector, *, schema):
    """Fixed update HEAD from this entrypoint; no prior-lock-history proof."""
    return _capture(db, selector, schema, 'writer')


@_safe
def assert_policy_records_current_for_read(db, expected, *, schema):
    """Recapture the exact read selection; no mutation or lock upgrade."""
    _assert_current(db, expected, schema, 'read')


@_safe
def assert_policy_records_current_for_writer(db, expected, *, schema):
    """Recapture the exact writer selection; comparison, not a committed CAS."""
    _assert_current(db, expected, schema, 'writer')
