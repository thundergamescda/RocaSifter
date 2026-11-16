"""Private original-label preparation on a caller-owned receiving transaction.

Receiving supplies fresh authority and verified original evidence. These frozen
values never grant policy, storage, render, disposal, publication or print rights.
No source capture or context object is a public/asdict/log payload.
"""
from dataclasses import dataclass
import json
import re
import uuid

from intake_label_contract import (LabelContractError, MAX_INPUT_BYTES,
    SERVICE_ORIGIN, canonical, confirmed_input, input_hash)
from .receiving import ExpectedLabelSource, Receiving
from .security import ServiceError


SELECTOR_FIELDS = frozenset(('receivingId', 'serverRevision', 'planHash',
                            'expectedTerminalBasisHash'))
CONTEXT_FIELDS = ('expected_source', 'input_body', 'input_hash', 'tenant_id',
    'receiving_id', 'server_revision', 'plan_hash', 'physical_intake_id',
    'physical_manifest_hash', 'source_snapshot_hash', 'terminal_receipt_hash',
    'provider_hash', 'confirmed_rows_hash', 'terminal_basis_hash', 'label_profile',
    'row_count', 'total_copies')
_BUNDLE_FIELDS = frozenset(('restorationSource', 'operation', 'claim', 'expectedSource'))
_ADMISSION_FIELDS = frozenset(('everAdmitted', 'catalogStepPresent',
                              'inventoryStepPresent', 'leaseActive'))
_HASH = re.compile(r'[0-9a-f]{64}')


class LabelContextError(ServiceError):
    """Fixed adapter error; no rejected object or underlying exception retained."""
    def __init__(self, *, changed=False):
        self.code = 'INTAKE_LABEL_CONTEXT_CHANGED' if changed else 'INTAKE_LABEL_CONTEXT_INVALID'
        message = ('The original confirmed label context changed. Reload before continuing.'
                   if changed else 'The original confirmed label context is unavailable or inconsistent.')
        super().__init__(message, 409)


def _invalid():
    raise LabelContextError()


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        _invalid()
    return value


def _hash(value):
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _invalid()
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        _invalid()
    return value


def _tenant(actor):
    if type(actor) is not dict:
        _invalid()
    value = actor['tenant_id']
    # Auth.resolve retains the database's native UUID. This is a deliberate
    # exact-type conversion, never arbitrary str()/mapping coercion.
    return str(value) if type(value) is uuid.UUID else _uuid(value)


def _selector(value):
    if (type(value) is not dict or any(type(key) is not str for key in value)
        or set(value) != SELECTOR_FIELDS):
        _invalid()
    return (_uuid(value['receivingId']), _revision(value['serverRevision']),
            _hash(value['planHash']), _hash(value['expectedTerminalBasisHash']))


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class CapturedLabelContext:
    """Internal prepared data, not a process capability or authorization lease."""
    expected_source: ExpectedLabelSource
    input_body: str
    input_hash: str
    tenant_id: str
    receiving_id: str
    server_revision: int
    plan_hash: str
    physical_intake_id: str
    physical_manifest_hash: str
    source_snapshot_hash: str
    terminal_receipt_hash: str
    provider_hash: str
    confirmed_rows_hash: str
    terminal_basis_hash: str
    label_profile: tuple
    row_count: int
    total_copies: int

    def __repr__(self):
        return '<CapturedLabelContext>'

    __str__ = __repr__

    def materialize_input(self):
        """Read/prepare only: a new detached graph; forbidden in final guards."""
        try:
            if (type(self) is CapturedLabelContext and type(self.input_body) is str
                and 1 <= len(self.input_body) <= MAX_INPUT_BYTES):
                value = json.loads(self.input_body)
                if type(value) is dict:
                    return value
        except (ValueError, TypeError, AttributeError, RecursionError):
            pass
        # Do not retain JSONDecodeError.doc or missing-field exception context.
        _invalid()


def _correlate(bundle, tenant, receiving_id, revision, plan_hash):
    if (type(bundle) is not dict or any(type(key) is not str for key in bundle)
        or set(bundle) != _BUNDLE_FIELDS):
        _invalid()
    expected = bundle['expectedSource']
    if type(expected) is not ExpectedLabelSource:
        _invalid()
    # Revalidate forged/missing tuple fields in preparation only. Preserve the
    # exact producer-owned object for later fixed raw/scalar assertion.
    ExpectedLabelSource(expected.tenant_id, expected.claim, expected.operation, expected.steps)
    source, operation, claim = (bundle[key] for key in ('restorationSource', 'operation', 'claim'))
    if any(type(value) is not dict for value in (source, operation, claim)):
        _invalid()
    c, o = expected.claim, expected.operation
    if (expected.tenant_id != tenant or o[0] != receiving_id or o[3] != revision or o[4] != plan_hash
        or _uuid(source['receivingId']) != receiving_id or _revision(source['serverRevision']) != revision
        or _hash(source['planHash']) != plan_hash or _uuid(operation['receiving_id']) != receiving_id
        or _revision(operation['revision']) != revision or _hash(operation['plan_hash']) != plan_hash
        or _uuid(source['physicalIntakeId']) != c[0] or _uuid(claim['physical_intake_id']) != c[0]
        or _uuid(operation['physical_intake_id']) != c[0] or o[1] != c[0]
        or _hash(source['physicalManifestHash']) != c[3] or _hash(claim['manifest_hash']) != c[3]
        or _hash(operation['physical_manifest_hash']) != c[3] or _hash(claim['semantic_hash']) != c[4]
        or _revision(claim['version']) != c[1] or type(claim['state']) is not str or claim['state'] != c[2]
        or claim['bound_trade_id'] is not None or c[5] is not None
        or type(source['workflowKind']) is not str or source['workflowKind'] != 'inventory_only'
        or type(operation['workflow_kind']) is not str or operation['workflow_kind'] != o[2]
        or type(operation['state']) is not str or operation['state'] != o[5]
        or operation['inventory_confirmed'] is not True
        or source['acceptedTradeId'] is not None or source['acceptedReceiptHash'] is not None
        or operation['accepted_trade_id'] is not None or operation['accepted_receipt_hash'] is not None
        or _hash(source['sourceSnapshotHash']) != _hash(operation['source_snapshot_hash'])):
        _invalid()
    admission = source['admission']
    if (type(admission) is not dict or any(type(key) is not str for key in admission)
        or set(admission) != _ADMISSION_FIELDS
        or any(type(value) is not bool for value in admission.values())
        or admission['everAdmitted'] is not True or admission['leaseActive'] is not False
        or admission['inventoryStepPresent'] is not True
        or admission['catalogStepPresent'] != (expected.steps[0][0] == 0)):
        _invalid()
    return expected


def _capture(db, receiving, actor, selector):
    receiving_id, revision, plan_hash, terminal = _selector(selector)
    tenant = _tenant(actor)
    if not isinstance(receiving, Receiving):
        _invalid()
    # This configured server producer owns fresh authority, coherent locks and
    # original proof verification. Its type alone never proves those actions.
    bundle = receiving.capture_restoration_bundle(db, actor, receiving_id)
    expected = _correlate(bundle, tenant, receiving_id, revision, plan_hash)
    value = confirmed_input(bundle, service_origin=SERVICE_ORIGIN, tenant_id=tenant)
    if (value['tenantId'] != tenant or value['receivingId'] != receiving_id
        or type(value['serverRevision']) is not int or value['serverRevision'] != revision
        or value['planHash'] != plan_hash or value['physicalIntakeId'] != expected.claim[0]
        or value['physicalManifestHash'] != expected.claim[3]):
        _invalid()
    if value['terminalBasisHash'] != terminal:
        raise LabelContextError(changed=True)
    body, hashed = canonical(value), input_hash(value)
    profile = value['labelProfile']
    return CapturedLabelContext(expected, body, hashed, tenant, receiving_id, revision, plan_hash,
        value['physicalIntakeId'], value['physicalManifestHash'], value['sourceSnapshotHash'],
        value['terminalReceiptHash'], value['providerHash'], value['confirmedRowsHash'], terminal,
        (profile['stock'], profile['layout'], profile['dpi']), len(value['confirmedRows']),
        sum(row['quantity'] for row in value['confirmedRows']))


def capture(db, receiving, actor, selector):
    """Prepare one immutable context; supplied transaction and authority remain owned by caller."""
    try:
        return _capture(db, receiving, actor, selector)
    except ServiceError:
        raise  # Preserve live producer authorization and typed source failures.
    except (LabelContractError, ValueError, TypeError, KeyError, AttributeError,
            OverflowError, UnicodeError, RecursionError):
        pass
    _invalid()  # Leave owned handlers before replacing a rejected private value.


def assert_current(db, receiving, expected_source, actor):
    """Source-only final CAS on the SAME transaction; no policy or publication grant."""
    receiving.assert_label_source_current(db, expected_source, actor)
