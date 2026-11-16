"""Private typed label records on one caller-owned active transaction.

Values and locks are comparison data, not source/actor/policy authority. One ID
lock anchors each mutable family; secondary aliases never lock another row.
No mutation, codec/hash/calendar parser, clock, provider, worker or delivery.
"""
from dataclasses import dataclass
from functools import wraps
import re
from types import MappingProxyType

from psycopg import Error as PostgreSQLError
from psycopg.pq import TransactionStatus
from .intake_label_inputs import ExpectedLabelInput, FIELDS as _INPUT_FIELDS


__all__ = ('capture_for_read', 'capture_for_reservation', 'assert_current')
_MESSAGE = 'Shared label records require a fresh review.'
_KINDS = ('INVALID', 'CHANGED', 'TRANSACTION_REQUIRED', 'DATABASE_ERROR')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_HASH = re.compile(r'[0-9a-f]{64}')
_KEY = re.compile(r'[A-Za-z0-9_-]{1,150}')
_ERROR = re.compile(r'[A-Z][A-Z0-9_]{0,99}')
_DATE = re.compile(r'[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{6}Z')
_SCHEMA = re.compile(r'(?:public|ss_test_[0-9a-f]{32})')
_FLAGS = ('_scalar_valid', '_body_valid', '_body_too_large', '_dates_valid')
_SCHEMA_TOKEN = '__SS_REGISTRY_SCHEMA__'
_UTF8_SQL = "(pg_catalog.current_setting('server_encoding')='UTF8' AND pg_catalog.current_setting('client_encoding')='UTF8')"


class LabelRegistryError(ValueError):
    """Fixed owned code/message only; no rejected value or driver cause."""
    __slots__ = ('_code',)

    def __init__(self, kind='INVALID'):
        chosen = kind if type(kind) is str and kind in _KINDS else 'INVALID'
        self._code = 'INTAKE_LABEL_REGISTRY_' + chosen
        super().__init__(_MESSAGE)

    @property
    def code(self):
        return self._code

    def __str__(self):
        return _MESSAGE


def _fail(kind='INVALID'):
    raise LabelRegistryError(kind)


def _require(condition, kind='INVALID'):
    if not condition:
        _fail(kind)


def _safe(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        failure = 'INVALID'
        try:
            return function(*args, **kwargs)
        except LabelRegistryError as error:
            code = getattr(error, '_code', None) if type(error) is LabelRegistryError else None
            if type(code) is str:
                failure = next((kind for kind in _KINDS
                                if code == 'INTAKE_LABEL_REGISTRY_' + kind), 'INVALID')
        except PostgreSQLError:
            failure = 'DATABASE_ERROR'
        except Exception:
            pass
        # Fresh replacement after leaving handlers drops any owned caught chain.
        _fail(failure)
    return invoke


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _date_shape(value):
    # Native SQL attests calendar/finite/year/interval domains before transfer.
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
        for kind, label in _OPAQUE_NAMES.items():
            if type(self) is kind:
                return label
        return '<LabelRegistryValue>'

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedLabelJob(_Opaque):
    tenant_id: str
    id: str
    receiving_id: str
    input_id: str
    input_hash: str
    terminal_basis_hash: str
    job_basis_hash: str
    renderer_build_hash: str
    purpose: str
    reprint_of_job_id: str | None
    copy_offset: int
    copy_count: int
    part_size: int
    part_count: int
    render_plan_hash: str
    created_by: str
    created_at: str
    policy_hash: str
    retention_until: str

    @_safe
    def __post_init__(self):
        _values("job", self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedLabelPublication(_Opaque):
    tenant_id: str
    id: str
    job_id: str
    part_index: int
    part_hash: str
    copy_offset: int
    copy_count: int
    generation_id: str
    state: str
    revision: int
    lease_generation: int
    lease_token: str | None
    lease_until: str | None
    authorization_request_key: str | None
    render_input_hash: str | None
    render_basis_hash: str | None
    output_metadata: str | None
    pair_sealed_at: str | None
    pdf_sha256: str | None
    pdf_bytes: int | None
    zpl_sha256: str | None
    zpl_bytes: int | None
    published_at: str | None
    retention_until: str
    tombstoned_at: str | None
    policy_hash: str
    error_code: str | None

    @_safe
    def __post_init__(self):
        _values("publication", self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedLabelPreview(_Opaque):
    tenant_id: str
    id: str
    job_id: str
    publication_id: str
    created_by: str
    session_hash: str
    role: str
    request_key: str
    selector_body: str
    selector_hash: str
    input_hash: str
    part_hash: str
    build_hash: str
    state: str
    revision: int
    lease_generation: int
    lease_token: str | None
    lease_until: str | None
    render_basis_hash: str | None
    overflows_body: str | None
    clipping_required: bool | None
    created_at: str
    ready_at: str | None
    expires_at: str
    error_code: str | None
    expected_publication_revision: int

    @_safe
    def __post_init__(self):
        _values("preview", self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class ExpectedLabelRequest(_Opaque):
    tenant_id: str
    request_key: str
    kind: str
    intent_body: str
    intent_hash: str
    preview_id: str | None
    job_id: str
    publication_id: str
    reprint_of_publication_id: str | None
    created_by: str
    session_hash: str
    created_at: str
    expected_publication_revision: int

    @_safe
    def __post_init__(self):
        _values("request", self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class LookupSlot(_Opaque):
    tag: str
    key: tuple | None

    @_safe
    def __post_init__(self):
        _validate_slot(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class LookupObservation(_Opaque):
    state: str
    row: object | None

    @_safe
    def __post_init__(self):
        _validate_observation(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class LabelLookupSelector(_Opaque):
    tenant_id: str
    input_by_hash: LookupSlot
    input_by_terminal: LookupSlot
    input_by_id: LookupSlot
    job_by_basis: LookupSlot
    job_by_id: LookupSlot
    parent_job_by_id: LookupSlot
    publication_by_part: LookupSlot
    publication_by_id: LookupSlot
    publication_by_generation: LookupSlot
    preview_by_id: LookupSlot
    preview_by_request_key: LookupSlot
    request_by_key: LookupSlot

    @_safe
    def __post_init__(self):
        _validate_selector(self)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class CapturedLabelRows(_Opaque):
    lookup_selector: LabelLookupSelector
    capture_kind: str
    input_by_hash: LookupObservation
    input_by_terminal: LookupObservation
    input_by_id: LookupObservation
    job_by_basis: LookupObservation
    job_by_id: LookupObservation
    parent_job_by_id: LookupObservation
    publication_by_part: LookupObservation
    publication_by_id: LookupObservation
    publication_by_generation: LookupObservation
    preview_by_id: LookupObservation
    preview_by_request_key: LookupObservation
    request_by_key: LookupObservation

    @_safe
    def __post_init__(self):
        _validate_capture(self)


SLOT_FIELDS = ("input_by_hash", "input_by_terminal", "input_by_id", "job_by_basis", "job_by_id", "parent_job_by_id", "publication_by_part", "publication_by_id", "publication_by_generation", "preview_by_id", "preview_by_request_key", "request_by_key")
SELECTOR_FIELDS = ('tenant_id',) + SLOT_FIELDS
CAPTURE_FIELDS = ('lookup_selector', 'capture_kind') + SLOT_FIELDS
ROW_FIELDS = MappingProxyType({
    "input": _INPUT_FIELDS,
    "job": ("tenant_id", "id", "receiving_id", "input_id", "input_hash", "terminal_basis_hash", "job_basis_hash", "renderer_build_hash", "purpose", "reprint_of_job_id", "copy_offset", "copy_count", "part_size", "part_count", "render_plan_hash", "created_by", "created_at", "policy_hash", "retention_until"),
    "publication": ("tenant_id", "id", "job_id", "part_index", "part_hash", "copy_offset", "copy_count", "generation_id", "state", "revision", "lease_generation", "lease_token", "lease_until", "authorization_request_key", "render_input_hash", "render_basis_hash", "output_metadata", "pair_sealed_at", "pdf_sha256", "pdf_bytes", "zpl_sha256", "zpl_bytes", "published_at", "retention_until", "tombstoned_at", "policy_hash", "error_code"),
    "preview": ("tenant_id", "id", "job_id", "publication_id", "created_by", "session_hash", "role", "request_key", "selector_body", "selector_hash", "input_hash", "part_hash", "build_hash", "state", "revision", "lease_generation", "lease_token", "lease_until", "render_basis_hash", "overflows_body", "clipping_required", "created_at", "ready_at", "expires_at", "error_code", "expected_publication_revision"),
    "request": ("tenant_id", "request_key", "kind", "intent_body", "intent_hash", "preview_id", "job_id", "publication_id", "reprint_of_publication_id", "created_by", "session_hash", "created_at", "expected_publication_revision"),
})
_RECORD_TYPES = MappingProxyType({
    "input": ExpectedLabelInput,
    "job": ExpectedLabelJob,
    "publication": ExpectedLabelPublication,
    "preview": ExpectedLabelPreview,
    "request": ExpectedLabelRequest,
})
_OPAQUE_NAMES = MappingProxyType({kind: '<' + kind.__name__ + '>' for kind in (
    LookupSlot, LookupObservation, LabelLookupSelector, CapturedLabelRows,
    ExpectedLabelJob, ExpectedLabelPublication, ExpectedLabelPreview, ExpectedLabelRequest)})
_FIELD_SPECS = MappingProxyType({
    "input": (
        ("tenant_id", "uuid", False, None),
        ("id", "uuid", False, None),
        ("receiving_id", "uuid", False, None),
        ("server_revision", "int8", False, (0, 9223372036854775807)),
        ("plan_hash", "hash", False, None),
        ("input_hash", "hash", False, None),
        ("terminal_basis_hash", "hash", False, None),
        ("confirmed_rows_hash", "hash", False, None),
        ("row_count", "int4", False, (1, 1000)),
        ("total_copies", "int4", False, (1, 1000000)),
        ("input_body", "body", True, 8388608),
        ("content_state", "enum", False, ("active", "restricted", "disposal_pending", "disposed")),
        ("revision", "int8", False, (1, 9223372036854775807)),
        ("created_at", "date", False, None),
        ("policy_hash", "hash", False, None),
        ("retention_until", "date", False, None),
        ("restricted_at", "date", True, None),
        ("review_due_at", "date", True, None),
        ("disposed_at", "date", True, None),
        ("suppression_until", "date", True, None),
    ),
    "job": (
        ("tenant_id", "uuid", False, None),
        ("id", "uuid", False, None),
        ("receiving_id", "uuid", False, None),
        ("input_id", "uuid", False, None),
        ("input_hash", "hash", False, None),
        ("terminal_basis_hash", "hash", False, None),
        ("job_basis_hash", "hash", False, None),
        ("renderer_build_hash", "hash", False, None),
        ("purpose", "enum", False, ("initial", "range_reprint")),
        ("reprint_of_job_id", "uuid", True, None),
        ("copy_offset", "int4", False, (0, 999999)),
        ("copy_count", "int4", False, (1, 1000000)),
        ("part_size", "int4", False, (1, 1000)),
        ("part_count", "int4", False, (1, 2147483647)),
        ("render_plan_hash", "hash", False, None),
        ("created_by", "uuid", False, None),
        ("created_at", "date", False, None),
        ("policy_hash", "hash", False, None),
        ("retention_until", "date", False, None),
    ),
    "publication": (
        ("tenant_id", "uuid", False, None),
        ("id", "uuid", False, None),
        ("job_id", "uuid", False, None),
        ("part_index", "int4", False, (0, 2147483647)),
        ("part_hash", "hash", False, None),
        ("copy_offset", "int4", False, (0, 999999)),
        ("copy_count", "int4", False, (1, 1000)),
        ("generation_id", "uuid", False, None),
        ("state", "enum", False, ("planned", "queued", "rendering", "bytes_prepared", "publishing", "published", "authorization_required", "recovery_required")),
        ("revision", "int8", False, (1, 9223372036854775807)),
        ("lease_generation", "int8", False, (0, 9223372036854775807)),
        ("lease_token", "uuid", True, None),
        ("lease_until", "date", True, None),
        ("authorization_request_key", "key", True, None),
        ("render_input_hash", "hash", True, None),
        ("render_basis_hash", "hash", True, None),
        ("output_metadata", "body", True, 2097152),
        ("pair_sealed_at", "date", True, None),
        ("pdf_sha256", "hash", True, None),
        ("pdf_bytes", "int4", True, (1, 33554432)),
        ("zpl_sha256", "hash", True, None),
        ("zpl_bytes", "int4", True, (1, 33554432)),
        ("published_at", "date", True, None),
        ("retention_until", "date", False, None),
        ("tombstoned_at", "date", True, None),
        ("policy_hash", "hash", False, None),
        ("error_code", "error", True, None),
    ),
    "preview": (
        ("tenant_id", "uuid", False, None),
        ("id", "uuid", False, None),
        ("job_id", "uuid", False, None),
        ("publication_id", "uuid", False, None),
        ("created_by", "uuid", False, None),
        ("session_hash", "hash", False, None),
        ("role", "enum", False, ("owner", "manager")),
        ("request_key", "key", False, None),
        ("selector_body", "body", False, 16384),
        ("selector_hash", "hash", False, None),
        ("input_hash", "hash", False, None),
        ("part_hash", "hash", False, None),
        ("build_hash", "hash", False, None),
        ("state", "enum", False, ("queued", "checking", "ready", "failed", "expired")),
        ("revision", "int8", False, (1, 9223372036854775807)),
        ("lease_generation", "int8", False, (0, 9223372036854775807)),
        ("lease_token", "uuid", True, None),
        ("lease_until", "date", True, None),
        ("render_basis_hash", "hash", True, None),
        ("overflows_body", "body", True, 2097152),
        ("clipping_required", "bool", True, None),
        ("created_at", "date", False, None),
        ("ready_at", "date", True, None),
        ("expires_at", "date", False, None),
        ("error_code", "error", True, None),
        ("expected_publication_revision", "int8", False, (1, 9223372036854775807)),
    ),
    "request": (
        ("tenant_id", "uuid", False, None),
        ("request_key", "key", False, None),
        ("kind", "enum", False, ("confirm_part", "resume_part", "reprint_exact")),
        ("intent_body", "body", False, 16384),
        ("intent_hash", "hash", False, None),
        ("preview_id", "uuid", True, None),
        ("job_id", "uuid", False, None),
        ("publication_id", "uuid", False, None),
        ("reprint_of_publication_id", "uuid", True, None),
        ("created_by", "uuid", False, None),
        ("session_hash", "hash", False, None),
        ("created_at", "date", False, None),
        ("expected_publication_revision", "int8", False, (1, 9223372036854775807)),
    ),
})
_SLOT_INFO = MappingProxyType({
    "input_by_hash": ("input", ("tenant_id", "input_hash")),
    "input_by_terminal": ("input", ("tenant_id", "terminal_basis_hash")),
    "input_by_id": ("input", ("tenant_id", "id")),
    "job_by_basis": ("job", ("tenant_id", "job_basis_hash")),
    "job_by_id": ("job", ("tenant_id", "id")),
    "parent_job_by_id": ("job", ("tenant_id", "id")),
    "publication_by_part": ("publication", ("tenant_id", "job_id", "part_index")),
    "publication_by_id": ("publication", ("tenant_id", "id")),
    "publication_by_generation": ("publication", ("tenant_id", "generation_id")),
    "preview_by_id": ("preview", ("tenant_id", "id")),
    "preview_by_request_key": ("preview", ("tenant_id", "request_key")),
    "request_by_key": ("request", ("tenant_id", "request_key")),
})


def _field_valid(spec, value):
    name, kind, nullable, detail = spec
    if value is None:
        return nullable
    if kind == 'uuid':
        return _matches(_UUID, value)
    if kind == 'hash':
        return _matches(_HASH, value)
    if kind == 'key':
        return _matches(_KEY, value)
    if kind == 'error':
        return _matches(_ERROR, value)
    if kind in ('int4', 'int8'):
        return _integer(value, *detail)
    if kind == 'enum':
        return type(value) is str and value in detail
    if kind == 'bool':
        return type(value) is bool
    if kind == 'date':
        return _date_shape(value)
    if kind == 'body':
        # SQL attests octet bounds; no UTF8/body codec here.
        return type(value) is str and 1 <= len(value) <= detail and '\0' not in value
    return False


def _all_or_none(values):
    return all(value is None for value in values) or all(value is not None for value in values)


def _semantics(family, row):
    if family == 'input':
        _require(row.total_copies >= row.row_count and row.retention_until > row.created_at)
        if row.content_state == 'active':
            _require(row.input_body is not None and all(value is None for value in (
                row.restricted_at, row.review_due_at, row.disposed_at, row.suppression_until)))
        elif row.content_state in ('restricted', 'disposal_pending'):
            _require(row.input_body is not None and row.restricted_at is not None
                     and row.review_due_at is not None and row.disposed_at is None
                     and row.suppression_until is None)
        else:
            _require(row.input_body is None and row.restricted_at is not None
                     and row.review_due_at is None and row.disposed_at is not None
                     and row.suppression_until is not None and row.suppression_until > row.disposed_at)
    elif family == 'job':
        _require(row.copy_offset + row.copy_count <= 1000000
                 and row.part_count == (row.copy_count + row.part_size - 1) // row.part_size
                 and row.retention_until > row.created_at)
        _require((row.purpose == 'initial' and row.reprint_of_job_id is None)
                 or (row.purpose == 'range_reprint' and row.reprint_of_job_id is not None
                     and row.reprint_of_job_id != row.id))
    elif family == 'publication':
        _require(row.copy_offset + row.copy_count <= 1000000)
        _require((row.state == 'planned') == (row.authorization_request_key is None))
        _require((row.lease_token is None) == (row.lease_until is None))
        _require((row.state in ('rendering', 'bytes_prepared', 'publishing'))
                 == (row.lease_token is not None))
        if row.lease_until is not None:
            _require(row.lease_until <= row.retention_until)
        _require(_all_or_none((row.pair_sealed_at, row.pdf_sha256, row.pdf_bytes,
                               row.zpl_sha256, row.zpl_bytes)))
        _require(_all_or_none((row.render_input_hash, row.render_basis_hash, row.output_metadata)))
        if row.pair_sealed_at is not None:
            _require(row.render_input_hash is not None)
        if row.state in ('bytes_prepared', 'publishing', 'published'):
            _require(row.pair_sealed_at is not None)
        _require((row.state == 'published') == (row.published_at is not None))
        if row.published_at is not None:
            _require(row.tombstoned_at is None and row.published_at < row.retention_until
                     and row.pair_sealed_at is not None and row.pair_sealed_at <= row.published_at)
    elif family == 'preview':
        _require(_all_or_none((row.ready_at, row.render_basis_hash,
                               row.overflows_body, row.clipping_required)))
        if row.state == 'ready':
            _require(row.ready_at is not None)
        _require((row.lease_token is None) == (row.lease_until is None))
        _require((row.state == 'checking') == (row.lease_token is not None))
        _require(row.expires_at > row.created_at)
        if row.lease_until is not None:
            _require(row.lease_until <= row.expires_at)
        if row.ready_at is not None:
            _require(row.ready_at >= row.created_at and row.expires_at > row.ready_at)
        # Native SQL flags attest 10m/5m intervals; no Python calendar parser.
    else:
        _require((row.kind == 'reprint_exact' and row.reprint_of_publication_id is not None
                  and row.reprint_of_publication_id == row.publication_id)
                 or (row.kind != 'reprint_exact' and row.reprint_of_publication_id is None))


def _values(family, row):
    _require(type(row) is _RECORD_TYPES[family])
    values = tuple(getattr(row, name) for name in ROW_FIELDS[family])
    _require(all(_field_valid(spec, value) for spec, value in zip(_FIELD_SPECS[family], values)))
    _semantics(family, row)
    return values


def _validate_slot(slot):
    _require(type(slot) is LookupSlot and type(slot.tag) is str)
    if slot.tag == 'omitted':
        _require(slot.key is None)
    else:
        _require(slot.tag == 'selected' and type(slot.key) is tuple and len(slot.key) in (2, 3))
        _require(all(type(value) is str or _integer(value, 0, 2147483647) for value in slot.key))


def _validate_observation(observation):
    _require(type(observation) is LookupObservation and type(observation.state) is str)
    if observation.state in ('not_selected', 'absent'):
        _require(observation.row is None)
    else:
        _require(observation.state == 'present')
        family = next((name for name, kind in _RECORD_TYPES.items()
                       if type(observation.row) is kind), None)
        _require(family is not None)
        _values(family, observation.row)


def _selected(selector, name):
    return getattr(selector, name).tag == 'selected'


def _validate_selector(selector):
    _require(type(selector) is LabelLookupSelector and _matches(_UUID, selector.tenant_id))
    for name in SLOT_FIELDS:
        slot = getattr(selector, name)
        _validate_slot(slot)
        if slot.tag == 'omitted':
            continue
        family, columns = _SLOT_INFO[name]
        _require(len(slot.key) == len(columns))
        specs = {spec[0]: spec for spec in _FIELD_SPECS[family]}
        _require(all(_field_valid(specs[column], value) for column, value in zip(columns, slot.key)))
        _require(slot.key[0] == selector.tenant_id, 'CHANGED')
    selected = {name: _selected(selector, name) for name in SLOT_FIELDS}
    _require(any(selected.values()))
    _require(selected['input_by_hash'] == selected['input_by_terminal'])
    _require(not selected['input_by_id'] or selected['input_by_hash'])
    _require(selected['job_by_basis'] == selected['job_by_id'])
    _require(not selected['job_by_id'] or selected['input_by_id'])
    _require(not selected['parent_job_by_id'] or selected['job_by_id'])
    _require(selected['publication_by_part'] == selected['publication_by_id']
             == selected['publication_by_generation'])
    _require(not selected['publication_by_id'] or selected['job_by_id'])
    _require(selected['preview_by_id'] == selected['preview_by_request_key'])
    _require(not selected['preview_by_id'] or selected['publication_by_id'])
    _require(not selected['request_by_key'] or selected['preview_by_id'])
    if selected['publication_by_id']:
        _require(selector.publication_by_part.key[1] == selector.job_by_id.key[1], 'CHANGED')
    if selected['request_by_key']:
        _require(selector.request_by_key.key[1] == selector.preview_by_request_key.key[1], 'CHANGED')
    if selected['parent_job_by_id']:
        _require(selector.parent_job_by_id.key[1] != selector.job_by_id.key[1], 'CHANGED')


def _key_matches(name, slot, row):
    family, columns = _SLOT_INFO[name]
    _values(family, row)
    _require(tuple(getattr(row, column) for column in columns) == slot.key, 'CHANGED')


def _same_observation(family, left, right):
    return (left.state == right.state and (left.state != 'present'
            or _values(family, left.row) == _values(family, right.row)))


def _same_group(captured, family, names):
    observations = [getattr(captured, name) for name in names
                    if _selected(captured.lookup_selector, name)]
    if observations:
        _require(all(_same_observation(family, observations[0], value)
                     for value in observations[1:]), 'CHANGED')


def _row(captured, name):
    observation = getattr(captured, name)
    return observation.row if observation.state == 'present' else None


def _validate_capture(captured):
    _require(type(captured) is CapturedLabelRows)
    selector = captured.lookup_selector
    _validate_selector(selector)
    _require(type(captured.capture_kind) is str and captured.capture_kind in ('read', 'reservation'))
    for name in SLOT_FIELDS:
        slot, observation = getattr(selector, name), getattr(captured, name)
        _validate_observation(observation)
        if slot.tag == 'omitted':
            _require(observation.state == 'not_selected')
        else:
            _require(observation.state in ('absent', 'present'))
            if observation.state == 'present':
                _require(type(observation.row) is _RECORD_TYPES[_SLOT_INFO[name][0]])
                _key_matches(name, slot, observation.row)
    _same_group(captured, 'input', ('input_by_hash', 'input_by_terminal', 'input_by_id'))
    _same_group(captured, 'job', ('job_by_id', 'job_by_basis'))
    _same_group(captured, 'publication', ('publication_by_id', 'publication_by_part', 'publication_by_generation'))
    _same_group(captured, 'preview', ('preview_by_id', 'preview_by_request_key'))
    original = _row(captured, 'input_by_hash')
    job = _row(captured, 'job_by_id')
    parent = _row(captured, 'parent_job_by_id')
    publication = _row(captured, 'publication_by_id')
    preview = _row(captured, 'preview_by_id')
    request = _row(captured, 'request_by_key')
    if parent is not None:
        _require(original is not None and parent.input_id == original.id
                 and parent.receiving_id == original.receiving_id
                 and parent.input_hash == original.input_hash
                 and parent.terminal_basis_hash == original.terminal_basis_hash, 'CHANGED')
    if job is not None:
        _require(original is not None and job.input_id == original.id
                 and job.receiving_id == original.receiving_id
                 and job.input_hash == original.input_hash
                 and job.terminal_basis_hash == original.terminal_basis_hash, 'CHANGED')
        if job.reprint_of_job_id is None:
            _require(not _selected(selector, 'parent_job_by_id'), 'CHANGED')
        else:
            _require(parent is not None and job.reprint_of_job_id == parent.id
                     and job.input_id == parent.input_id, 'CHANGED')
    if publication is not None:
        _require(job is not None and publication.job_id == job.id, 'CHANGED')
    if preview is not None:
        _require(publication is not None and job is not None
                 and preview.publication_id == publication.id and preview.job_id == job.id, 'CHANGED')
    if request is not None:
        _require(preview is not None and publication is not None and job is not None
                 and request.preview_id == preview.id and request.request_key == preview.request_key
                 and request.publication_id == publication.id and request.job_id == job.id
                 and request.created_by == preview.created_by
                 and request.session_hash == preview.session_hash
                 and request.expected_publication_revision == preview.expected_publication_revision, 'CHANGED')
        # Historical quote/request revision need NOT equal current publication revision.
        # An old selected key need NOT be the publication's latest authorization key.


# SQL flags attest static CHECK domains, never current-time action eligibility.
_SCALAR_EXTRA = MappingProxyType({
    'input': "(r.total_copies >= r.row_count AND ((r.content_state='active' AND r.input_body IS NOT NULL AND r.restricted_at IS NULL AND r.review_due_at IS NULL AND r.disposed_at IS NULL AND r.suppression_until IS NULL) OR (r.content_state IN ('restricted','disposal_pending') AND r.input_body IS NOT NULL AND r.restricted_at IS NOT NULL AND r.review_due_at IS NOT NULL AND r.disposed_at IS NULL AND r.suppression_until IS NULL) OR (r.content_state='disposed' AND r.input_body IS NULL AND r.restricted_at IS NOT NULL AND r.review_due_at IS NULL AND r.disposed_at IS NOT NULL AND r.suppression_until IS NOT NULL)))",
    'job': "(r.copy_offset::pg_catalog.int8+r.copy_count::pg_catalog.int8<=1000000 AND CASE WHEN r.part_size BETWEEN 1 AND 1000 THEN r.part_count::pg_catalog.int8=(r.copy_count::pg_catalog.int8+r.part_size::pg_catalog.int8-1)/r.part_size::pg_catalog.int8 ELSE FALSE END AND ((r.purpose='initial' AND r.reprint_of_job_id IS NULL) OR (r.purpose='range_reprint' AND r.reprint_of_job_id IS NOT NULL AND r.reprint_of_job_id<>r.id)))",
    'publication': "(r.copy_offset::pg_catalog.int8+r.copy_count::pg_catalog.int8<=1000000 AND ((r.state='planned')=(r.authorization_request_key IS NULL)) AND ((r.lease_token IS NULL)=(r.lease_until IS NULL)) AND ((r.state IN ('rendering','bytes_prepared','publishing'))=(r.lease_token IS NOT NULL)) AND ((r.pair_sealed_at IS NULL AND r.pdf_sha256 IS NULL AND r.pdf_bytes IS NULL AND r.zpl_sha256 IS NULL AND r.zpl_bytes IS NULL) OR (r.pair_sealed_at IS NOT NULL AND r.pdf_sha256 IS NOT NULL AND r.pdf_bytes IS NOT NULL AND r.zpl_sha256 IS NOT NULL AND r.zpl_bytes IS NOT NULL)) AND ((r.render_input_hash IS NULL AND r.render_basis_hash IS NULL AND r.output_metadata IS NULL) OR (r.render_input_hash IS NOT NULL AND r.render_basis_hash IS NOT NULL AND r.output_metadata IS NOT NULL)) AND (r.pair_sealed_at IS NULL OR r.render_input_hash IS NOT NULL) AND (r.state NOT IN ('bytes_prepared','publishing','published') OR r.pair_sealed_at IS NOT NULL) AND ((r.state='published')=(r.published_at IS NOT NULL)) AND (r.state<>'published' OR r.tombstoned_at IS NULL))",
    'preview': "(((r.ready_at IS NULL AND r.render_basis_hash IS NULL AND r.overflows_body IS NULL AND r.clipping_required IS NULL) OR (r.ready_at IS NOT NULL AND r.render_basis_hash IS NOT NULL AND r.overflows_body IS NOT NULL AND r.clipping_required IS NOT NULL)) AND (r.state<>'ready' OR r.ready_at IS NOT NULL) AND ((r.lease_token IS NULL)=(r.lease_until IS NULL)) AND ((r.state='checking')=(r.lease_token IS NOT NULL)))",
    'request': "((r.kind='reprint_exact' AND r.reprint_of_publication_id IS NOT NULL AND r.reprint_of_publication_id=r.publication_id) OR (r.kind<>'reprint_exact' AND r.reprint_of_publication_id IS NULL))",
})
_DATE_EXTRA = MappingProxyType({
    'input': "(r.retention_until>r.created_at AND (r.content_state<>'disposed' OR r.suppression_until>r.disposed_at))",
    'job': "(r.retention_until>r.created_at)",
    'publication': "((r.lease_until IS NULL OR r.lease_until<=r.retention_until) AND (r.published_at IS NULL OR (r.published_at<r.retention_until AND r.pair_sealed_at<=r.published_at)))",
    'preview': "(r.expires_at>r.created_at AND r.expires_at<=r.created_at+'00:10:00'::pg_catalog.interval AND (r.lease_until IS NULL OR r.lease_until<=r.expires_at) AND (r.ready_at IS NULL OR (r.ready_at>=r.created_at AND r.expires_at>r.ready_at AND r.expires_at<=r.ready_at+'00:05:00'::pg_catalog.interval)))",
    'request': "TRUE",
})
_TYPE_NAMES = MappingProxyType({
    'uuid': 'uuid', 'hash': 'text', 'key': 'text', 'error': 'text', 'enum': 'text',
    'body': 'text', 'date': 'timestamptz', 'bool': 'bool', 'int4': 'int4', 'int8': 'int8',
})


def _native_condition(spec):
    name, kind, nullable, detail = spec
    value = 'r.' + name
    native = "pg_catalog.pg_typeof(" + value + ")='pg_catalog." + _TYPE_NAMES[kind] + "'::pg_catalog.regtype"
    condition = native
    if kind == 'hash':
        condition += " AND pg_catalog.octet_length(" + value + ")=64 AND " + value + " ~ '^[0-9a-f]{64}$'"
    elif kind == 'key':
        condition += " AND pg_catalog.octet_length(" + value + ") BETWEEN 1 AND 150 AND " + value + " ~ '^[A-Za-z0-9_-]{1,150}$'"
    elif kind == 'error':
        condition += " AND pg_catalog.octet_length(" + value + ") BETWEEN 1 AND 100 AND " + value + " ~ '^[A-Z][A-Z0-9_]{0,99}$'"
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
    # All arguments come only from fixed source-owned QUERY_ROLES below.
    specs = _FIELD_SPECS[family]
    scalar = ' AND '.join(_native_condition(spec) for spec in specs if spec[1] not in ('body', 'date'))
    body = ' AND '.join(_native_condition(spec) for spec in specs if spec[1] == 'body') or 'TRUE'
    dates = ' AND '.join(_native_condition(spec) for spec in specs if spec[1] == 'date') or 'TRUE'
    too_large = ' OR '.join('(r.' + spec[0] + ' IS NOT NULL AND pg_catalog.octet_length(r.' + spec[0] + ')>' + str(spec[3]) + ')'
                            for spec in specs if spec[1] == 'body') or 'FALSE'
    projection = []
    for name, kind, nullable, detail in specs:
        if kind == 'date':
            value = "CASE WHEN v.dates_valid THEN pg_catalog.to_char(r." + name + " AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') ELSE NULL END"
        elif kind == 'body':
            value = 'CASE WHEN v.body_valid THEN r.' + name + ' ELSE NULL END'
        else:
            raw = 'r.' + name + ('::pg_catalog.text' if kind == 'uuid' else '')
            value = 'CASE WHEN v.scalar_valid THEN ' + raw + ' ELSE NULL END'
        projection.append(value + ' AS ' + name)
    return ('/* ss_label_registry:' + role + ' */ SELECT ' + ','.join(projection)
        + ',v.scalar_valid AS _scalar_valid,v.body_valid AS _body_valid,'
        + 'v.body_too_large AS _body_too_large,v.dates_valid AS _dates_valid'
        + ' FROM ' + _SCHEMA_TOKEN + '.ss_intake_label_' + {
            'input': 'inputs', 'job': 'jobs', 'publication': 'publications',
            'preview': 'previews', 'request': 'requests'}[family] + ' AS r'
        + ' CROSS JOIN LATERAL (SELECT COALESCE((' + _UTF8_SQL + ' AND ' + scalar + ' AND ' + _SCALAR_EXTRA[family] + '),FALSE) AS scalar_valid,'
        + 'COALESCE((' + _UTF8_SQL + ' AND (' + body + ')),FALSE) AS body_valid,COALESCE((' + too_large + '),FALSE) AS body_too_large,'
        + 'CASE WHEN COALESCE((' + dates + '),FALSE) THEN COALESCE((' + _DATE_EXTRA[family] + '),FALSE) ELSE FALSE END AS dates_valid) AS v'
        + ' WHERE ' + ' AND '.join('r.' + name + '=%s' for name in columns)
        + ' ORDER BY r.' + ('request_key' if family == 'request' else 'id')
        + ' LIMIT 2' + (' ' + lock if lock else ''))


QUERY_ROLES = MappingProxyType({
    "input.hash": ("input", ("tenant_id", "input_hash"), "FOR SHARE OF r"),
    "input.terminal": ("input", ("tenant_id", "terminal_basis_hash"), "FOR SHARE OF r"),
    "input.id": ("input", ("tenant_id", "id"), "FOR SHARE OF r"),
    "job.id": ("job", ("tenant_id", "id"), "FOR SHARE OF r"),
    "job.basis": ("job", ("tenant_id", "job_basis_hash"), ""),
    "publication.anchor.read": ("publication", ("tenant_id", "id"), "FOR SHARE OF r"),
    "publication.anchor.reservation": ("publication", ("tenant_id", "id"), "FOR UPDATE OF r"),
    "publication.part": ("publication", ("tenant_id", "job_id", "part_index"), ""),
    "publication.generation": ("publication", ("tenant_id", "generation_id"), ""),
    "preview.anchor.read": ("preview", ("tenant_id", "id"), "FOR SHARE OF r"),
    "preview.anchor.reservation": ("preview", ("tenant_id", "id"), "FOR UPDATE OF r"),
    "preview.key": ("preview", ("tenant_id", "request_key"), ""),
    "request.key": ("request", ("tenant_id", "request_key"), "FOR SHARE OF r"),
})
_QUERIES = MappingProxyType({role: _make_query(role, *info) for role, info in QUERY_ROLES.items()})


def _fetch(db, role, key, schema):
    _transaction(db)
    family = QUERY_ROLES[role][0]
    statement = _QUERIES[role].replace(_SCHEMA_TOKEN, _schema(schema))
    rows = db.execute(statement, key).fetchall()
    _transaction(db)
    _require(type(rows) is list and len(rows) <= 1)
    if not rows:
        return LookupObservation('absent', None)
    row = rows[0]
    _require(type(row) is dict and all(type(name) is str for name in row)
             and set(row) == set(ROW_FIELDS[family]) | set(_FLAGS))
    _require(all(type(row[name]) is bool for name in _FLAGS))
    _require(row['_scalar_valid'] and row['_body_valid'] and row['_dates_valid']
             and not row['_body_too_large'])
    result = _RECORD_TYPES[family](*(row[name] for name in ROW_FIELDS[family]))
    _values(family, result)
    return LookupObservation('present', result)


def _capture(db, lookup_selector, schema, kind):
    _transaction(db)
    _schema(schema)
    _validate_selector(lookup_selector)
    result = {name: LookupObservation('not_selected', None) for name in SLOT_FIELDS}

    def take(name, role):
        slot = getattr(lookup_selector, name)
        observation = _fetch(db, role, slot.key, schema)
        if observation.state == 'present':
            _key_matches(name, slot, observation.row)
        result[name] = observation
        return observation

    for name, role in (('input_by_hash', 'input.hash'), ('input_by_terminal', 'input.terminal'),
                       ('input_by_id', 'input.id')):
        if _selected(lookup_selector, name):
            take(name, role)
    if _selected(lookup_selector, 'input_by_hash'):
        for name in ('input_by_terminal', 'input_by_id'):
            if _selected(lookup_selector, name):
                _require(_same_observation('input', result['input_by_hash'], result[name]), 'CHANGED')

    job_slots = [name for name in ('job_by_id', 'parent_job_by_id')
                 if _selected(lookup_selector, name)]
    for name in sorted(job_slots, key=lambda item: getattr(lookup_selector, item).key[1]):
        take(name, 'job.id')
    if _selected(lookup_selector, 'job_by_basis'):
        take('job_by_basis', 'job.basis')
        _require(_same_observation('job', result['job_by_id'], result['job_by_basis']), 'CHANGED')

    if _selected(lookup_selector, 'publication_by_id'):
        anchor = take('publication_by_id', 'publication.anchor.' + kind)
        for name, role in (('publication_by_part', 'publication.part'),
                           ('publication_by_generation', 'publication.generation')):
            alias = take(name, role)
            _require(_same_observation('publication', anchor, alias), 'CHANGED')

    if _selected(lookup_selector, 'preview_by_id'):
        anchor = take('preview_by_id', 'preview.anchor.' + kind)
        alias = take('preview_by_request_key', 'preview.key')
        _require(_same_observation('preview', anchor, alias), 'CHANGED')

    if _selected(lookup_selector, 'request_by_key'):
        take('request_by_key', 'request.key')
    _transaction(db)
    captured = CapturedLabelRows(lookup_selector, kind, *(result[name] for name in SLOT_FIELDS))
    _transaction(db)
    return captured


@_safe
def capture_for_read(db, lookup_selector, *, schema):
    """Fixed shared anchors; comparison data only, no authority/reservation."""
    return _capture(db, lookup_selector, schema, 'read')


@_safe
def capture_for_reservation(db, lookup_selector, *, schema):
    """Fixed single update anchors; no mutation, policy or permission."""
    return _capture(db, lookup_selector, schema, 'reservation')


@_safe
def assert_current(db, expected_rows, *, schema):
    """Recapture identical selected keys/kind; no rebind or read upgrade."""
    _transaction(db)
    _validate_capture(expected_rows)
    current = _capture(db, expected_rows.lookup_selector, schema, expected_rows.capture_kind)
    for name in SLOT_FIELDS:
        family = _SLOT_INFO[name][0]
        _require(_same_observation(family, getattr(expected_rows, name), getattr(current, name)), 'CHANGED')
    _transaction(db)
