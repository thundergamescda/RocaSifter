"""Explicit local inventory-only records of a verified original physical intake.

This coordinator grants no provider, journal, claim or financial action. Full
source verification and serialization precede the existing short local guards.
The separate registry owns one atomic raw/scalar CAS publication transaction.
"""
import copy
from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
import secrets
import threading
import time
import uuid

from hosted_labels import fingerprint
from intake_label_contract import (LabelContractError, MAX_INPUT_BYTES,
    confirmed_input_from_proof, canonical as shared_label_canonical,
    input_hash as shared_label_input_hash)
from intake_manifest import source_lines, validate_manifest, manifest_digest, semantic_fingerprint, ManifestError
from station_cloud import CloudError, ORIGIN
from station_profile import INTAKE_PROJECTION_FEATURE, INTAKE_STOCK_OPERATIONS_FEATURE, ProfilePolicyError
from station_receiving_restoration_proof import verify_restoration_source, RestorationProofError
from station_shared_trade import IDENTITY_FIELDS
from station_store import StationError, now
import station_intake_receiving_registry as registry


MARKER = 'original_receiving_recovery'
BUNDLE = {'sessionId', 'tenantId', 'store', 'role', 'claim', 'history', 'operation', 'restorationSource'}
MARKER_FIELDS = {'version', 'originKind', 'serviceOrigin', 'tenantId', 'store', 'physicalIntakeId',
    'physicalManifestHash', 'sourceSnapshotHash', 'receivingId', 'serverRevision', 'planHash',
    'restorationSourceHash', 'proofHash', 'snapshotReference', 'recoveredAt'}
STATE_NAMES = {'ready', 'cancelled_before_write', 'catalog_pending', 'catalog_confirmed',
    'inventory_pending', 'outcome_unknown', 'needs_review', 'received'}
READ_KEYS = {'link', 'version', 'observation', 'active_version', 'active_observation',
    'ordinary_references', 'financial_links', 'financial_snapshots'}
LOCAL = re.compile(r'R-[A-Za-z0-9-]{1,100}')
HEX = re.compile(r'[a-f0-9]{64}')
MAX_RESPONSE = 10_000_000


def _error(message='The original stock review changed. Review it again; your import and original plan were kept.',
           *, code='INTAKE_STOCK_PROJECTION_CHANGED', status=409):
    return CloudError(message, status=status, code=code)


def _id(value):
    if type(value) is not str or not LOCAL.fullmatch(value): raise ValueError()
    return value


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value: raise ValueError()
    return value


def _hash(value):
    if type(value) is not str or not HEX.fullmatch(value): raise ValueError()
    return value


def _integer(value, minimum=0, maximum=2**63-1):
    if type(value) is not int or not minimum <= value <= maximum: raise ValueError()
    return value


def _tuple(row, fields):
    return tuple(row[key] for key in fields) if row is not None else None


def _encoded(value):
    raw = registry.canonical(value)
    if len(raw.encode('utf-8')) > MAX_RESPONSE:
        raise _error('The original stock record exceeds the supported response size. Keep it for owner review.')
    return raw


_SHARED_LABEL_PURPOSES = ('new_job', 'read_publication')
_SHARED_LABEL_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_SHARED_LABEL_ERRORS = {
    'INPUT_INVALID': ('Use the exact saved original-stock selection and shared-label purpose.', 400),
    'CONTEXT_INVALID': ('The shared-label context is invalid. Reload its original stock record.', 400),
    'CONTEXT_CHANGED': ('The shared-label context changed. Reload its original stock record.', 409),
    'REFRESH_REQUIRED': ('Explicitly refresh this saved original stock record before starting a new shared-label job.', 409),
    'NOT_READY': ('The verified original stock is not ready for a new shared-label job.', 409),
}


def _shared_label_error(kind):
    message, status = _SHARED_LABEL_ERRORS[kind]
    return _error(message, code='INTAKE_SHARED_LABEL_' + kind, status=status)


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class SharedLabelContext:
    """Private prepared facts, never a capability or public/asdict/log payload."""
    profile: object
    cloud_profile: object
    expected_projection: registry.ExpectedProjection
    purpose: str
    stock_receipt_id: str
    stock_revision: int
    session_id: str
    tenant_id: str
    store: str
    role: str
    receiving_id: str
    server_revision: int
    plan_hash: str
    selected_proof_hash: str
    saved_proof_hash: str
    physical_intake_id: str
    physical_manifest_hash: str
    source_snapshot_hash: str
    terminal_receipt_hash: str
    labels_verified: bool
    label_error_present: bool
    input_body: str
    input_hash: str
    terminal_basis_hash: str
    confirmed_rows_hash: str
    provider_hash: str
    label_profile: tuple
    row_count: int
    total_copies: int

    def __repr__(self):
        return '<SharedLabelContext>'

    __str__ = __repr__


def _shared_label_shape(context, expected_session_id):
    """Fixed private scalar/tuple checks only; no content codec or proof work."""
    try:
        if (type(context) is not SharedLabelContext or type(expected_session_id) is not str
            or not re.fullmatch('[a-f0-9]{32}', expected_session_id)
            or context.session_id != expected_session_id or type(context.session_id) is not str
            or type(context.purpose) is not str or context.purpose not in _SHARED_LABEL_PURPOSES):
            raise ValueError()
        _id(context.stock_receipt_id); _integer(context.stock_revision, 1)
        _integer(context.server_revision); _integer(context.row_count, 1, 1000)
        _integer(context.total_copies, context.row_count, 1000000)
        for value in (context.tenant_id, context.receiving_id, context.physical_intake_id):
            if type(value) is not str or _SHARED_LABEL_UUID.fullmatch(value) is None: raise ValueError()
        for value in (context.plan_hash, context.selected_proof_hash, context.saved_proof_hash,
            context.physical_manifest_hash, context.source_snapshot_hash, context.terminal_receipt_hash,
            context.input_hash, context.terminal_basis_hash, context.confirmed_rows_hash, context.provider_hash):
            _hash(value)
        if (type(context.store) is not str or not re.fullmatch('[a-z0-9][a-z0-9-]{2,49}', context.store)
            or type(context.role) is not str or context.role not in ('owner', 'manager')
            or type(context.labels_verified) is not bool or type(context.label_error_present) is not bool
            or type(context.input_body) is not str or not 1 <= len(context.input_body) <= MAX_INPUT_BYTES
            or type(context.label_profile) is not tuple or len(context.label_profile) != 3
            or type(context.label_profile[0]) is not str or context.label_profile[0] not in ('2x1', '3x1', '4x2')
            or type(context.label_profile[1]) is not str or context.label_profile[1] not in ('standard', 'fold', 'side')
            or type(context.label_profile[2]) is not int or context.label_profile[2] not in (203, 300)):
            raise ValueError()
        expected = context.expected_projection
        if (type(expected) is not registry.ExpectedProjection or expected.sources != ()
            or expected.ordinary_references != () or expected.financial_links != () or expected.financial_snapshots != ()
            or type(expected.child) is not tuple or len(expected.child) != 5
            or type(expected.version) is not tuple or len(expected.version) != len(registry.VERSION_FIELDS)
            or type(expected.link) is not tuple or len(expected.link) != len(registry.LINK_FIELDS)
            or expected.active_version != expected.version
            or type(expected.active_observation) is not tuple or len(expected.active_observation) != len(registry.OBSERVATION_FIELDS)
            or expected.observation is not None and (type(expected.observation) is not tuple
                or len(expected.observation) != len(registry.OBSERVATION_FIELDS))):
            raise ValueError()
        child, version, link = expected.child, expected.version, expected.link
        if (child[0] != context.stock_receipt_id or type(child[1]) is not str or child[2] is not None
            or type(child[3]) is not str or type(child[4]) is not int or child[4] != 0
            or version[:9] != (ORIGIN, context.tenant_id, context.receiving_id, context.server_revision,
                context.stock_receipt_id, context.physical_intake_id, context.physical_manifest_hash,
                context.source_snapshot_hash, context.plan_hash)
            or link[:9] != (ORIGIN, context.tenant_id, context.receiving_id, context.stock_receipt_id,
                context.server_revision, context.plan_hash, context.physical_intake_id,
                context.physical_manifest_hash, context.saved_proof_hash)):
            raise ValueError()
        key = (ORIGIN, context.tenant_id, context.receiving_id, context.server_revision)
        if (expected.active_observation[:5] != key + (context.saved_proof_hash,)
            or expected.observation is not None and expected.observation[:5] != key + (context.selected_proof_hash,)):
            raise ValueError()
        if context.purpose == 'new_job':
            if (context.selected_proof_hash != context.saved_proof_hash or expected.observation is None
                or expected.observation != expected.active_observation):
                raise _shared_label_error('REFRESH_REQUIRED')
            if not context.labels_verified or context.label_error_present:
                raise _shared_label_error('NOT_READY')
        return
    except CloudError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        pass
    raise _shared_label_error('CONTEXT_INVALID')


class IntakeStockProjection:
    def __init__(self, service, *, clock=None):
        self.service = service
        self.clock = clock or time.monotonic
        self._previews = {}
        self._namespace = None
        self._lock = threading.RLock()

    @property
    def cloud(self):
        return self.service.cloud

    def clear(self):
        with self._lock:
            self._previews.clear()
            self._namespace = None

    def _time(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise _error(code='INTAKE_STOCK_PREVIEW_EXPIRED')
        return value

    def _purge(self):
        stamp = self._time()
        self._previews = {key: value for key, value in self._previews.items() if value['expires'] > stamp}
        return stamp

    def _selection(self, data):
        fields = ({'anchorKind', 'receiptId', 'revision', 'receivingId', 'expectedServerRevision',
                   'expectedPlanHash', 'sessionId'} if data.get('anchorKind') == 'physical_intake'
                  else {'anchorKind', 'stockReceiptId', 'revision', 'sessionId'})
        return {key: data[key] for key in fields}

    def _input(self, data, expected, kind):
        try:
            if (type(data) is not dict or type(expected) is not str
                or not re.fullmatch('[a-f0-9]{32}', expected) or data.get('sessionId') != expected):
                raise ValueError()
            if kind == 'activate':
                if set(data) != {'sessionId', 'enableOriginalStockRecordsConfirmed'} or data['enableOriginalStockRecordsConfirmed'] is not True:
                    raise ValueError()
                return
            if kind == 'view':
                if set(data) != {'stockReceiptId', 'revision', 'sessionId'}: raise ValueError()
                _id(data['stockReceiptId']); _integer(data['revision'], 1)
                return
            if data.get('anchorKind') not in ('physical_intake', 'stock_record'): raise ValueError()
            base = {'anchorKind', 'revision', 'sessionId'}
            if data['anchorKind'] == 'physical_intake':
                base |= {'receiptId', 'receivingId', 'expectedServerRevision', 'expectedPlanHash'}
                _id(data.get('receiptId')); _uuid(data.get('receivingId'))
                _integer(data.get('expectedServerRevision')); _hash(data.get('expectedPlanHash'))
            else:
                base |= {'stockReceiptId'}
                _id(data.get('stockReceiptId'))
            _integer(data.get('revision'), 1)
            if kind == 'adopt':
                base |= {'expectedServerRevision', 'expectedPlanHash', 'expectedRestorationSourceHash', 'proofHash',
                         'confirmationHash', 'reviewOriginalPlanConfirmed', 'samePhysicalDeliveryConfirmed', 'restoreExisting'}
                _integer(data.get('expectedServerRevision')); _hash(data.get('expectedPlanHash'))
                for key in ('expectedRestorationSourceHash', 'proofHash', 'confirmationHash'): _hash(data.get(key))
                if data.get('reviewOriginalPlanConfirmed') is not True: raise ValueError()
                if any(type(data.get(key)) is not bool for key in ('samePhysicalDeliveryConfirmed', 'restoreExisting')):
                    raise ValueError()
            elif kind == 'rows':
                base |= {'confirmationHash', 'proofHash', 'offset', 'limit'}
                _hash(data.get('confirmationHash')); _hash(data.get('proofHash'))
                _integer(data.get('offset'), 0, 1000); _integer(data.get('limit'), 1, 100)
            elif kind != 'preview': raise ValueError()
            if set(data) != base: raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise _error('Use the exact saved original-stock selection and confirmations; its rows, prices and origin cannot be overridden.',
                         code='INTAKE_STOCK_INPUT_INVALID', status=400) from None

    def _enabled(self):
        self.service.profile.require_feature(INTAKE_PROJECTION_FEATURE)
        try:
            with self.service.store.db() as db:
                state = registry.initialization_state(db)
        except StationError as error:
            raise _error('Original stock metadata needs owner review. Saved work was kept; do not recreate or repair it from this review.',
                         code='INTAKE_STOCK_FEATURE_RECOVERY_REQUIRED') from error
        if state == 'absent':
            raise _error('Original stock setup is incomplete on this computer. Explicitly finish enabling original stock records before reviewing them; saved work was kept.',
                         code='INTAKE_STOCK_FEATURE_RESUME_REQUIRED')
        if state != 'present':
            raise _error('Original stock metadata could not be recognized. Keep saved work for owner review.',
                         code='INTAKE_STOCK_FEATURE_RECOVERY_REQUIRED')

    def _authority(self, bundle, expected, *, history=False):
        try:
            fields = BUNDLE-{'operation', 'restorationSource'} if history else BUNDLE
            if (type(bundle) is not dict or set(bundle) != fields or bundle['sessionId'] != expected
                or bundle['role'] not in ('owner', 'manager')): raise ValueError()
            _uuid(bundle['tenantId'])
            if type(bundle['store']) is not str or not re.fullmatch('[a-z0-9][a-z0-9-]{2,49}', bundle['store']):
                raise ValueError()
            if self.service.profile.binding() != {'tenant_id': bundle['tenantId'], 'service_origin': ORIGIN}:
                raise ValueError()
            claim = bundle['claim']
            registry.validate_claim_storage_fields(claim)
            if (type(claim) is not dict or type(claim.get('physical_reference')) is not str
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/-]{2,99}', claim['physical_reference'])):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _error('Use the original store’s bound hosted profile and current owner or manager sign-in.',
                         code='INTAKE_STOCK_PROFILE_REQUIRED') from None
        namespace = (id(self.service.profile), id(self.cloud.profile), ORIGIN, bundle['tenantId'], bundle['store'], expected, bundle['role'])
        with self._lock:
            if namespace != self._namespace:
                self._previews.clear()
                self._namespace = namespace

    def _raw(self, ident, *, active=True):
        _id(ident)
        with self.service.store.db() as db:
            # One SQLite snapshot checks the exact child transport bound before
            # its body can be materialized or decoded in Python. Unknown or
            # oversized bytes deny; they never masquerade as a missing child.
            row = db.execute('''SELECT id,
                CASE WHEN typeof(body)='text' AND length(CAST(body AS BLOB))<=8388608 THEN body END AS body,
                typeof(body) AS body_type,length(CAST(body AS BLOB)) AS body_size,
                deleted_at,updated,pickup_pending FROM receipts WHERE id=?''', (ident,)).fetchone()
        if not row or active and row['deleted_at'] is not None: raise _error()
        if row['body_type'] != 'text' or type(row['body_size']) is not int or row['body_size'] > 8_388_608 or row['body'] is None:
            raise _error('The saved stock record exceeds its supported body size or type. Keep it for owner review.',
                         code='INTAKE_STOCK_PROJECTION_INVALID')
        try:
            receipt = json.loads(row['body'])
            if (type(receipt) is not dict or receipt.get('id') != row['id']
                or receipt.get('deleted_at') != row['deleted_at'] or receipt.get('updated') != row['updated']
                or type(row['pickup_pending']) is not int or row['pickup_pending'] not in (0, 1)):
                raise ValueError()
            _integer(receipt.get('revision'), 1)
        except (ValueError, TypeError, KeyError): raise _error() from None
        return {'receipt': receipt, 'raw': row['body'], 'deleted': row['deleted_at'], 'updated': row['updated'],
                'pickupPending': row['pickup_pending'], 'hash': registry.digest(row['body'])}

    def _marker(self, receipt):
        try:
            marker = receipt[MARKER]
            if (type(marker) is not dict or set(marker) != MARKER_FIELDS
                or type(marker['version']) is not int or marker['version'] != 2
                or marker['originKind'] != 'physical_intake' or marker['serviceOrigin'] != ORIGIN
                or type(marker['store']) is not str or not re.fullmatch('[a-z0-9][a-z0-9-]{2,49}', marker['store'])):
                raise ValueError()
            for key in ('tenantId', 'physicalIntakeId', 'receivingId'): _uuid(marker[key])
            for key in ('physicalManifestHash', 'sourceSnapshotHash', 'planHash', 'restorationSourceHash', 'proofHash'):
                _hash(marker[key])
            _integer(marker['serverRevision'])
            if marker['snapshotReference'] != marker['receivingId']+':'+str(marker['serverRevision'])+':'+marker['proofHash']:
                raise ValueError()
            stamp = marker['recoveredAt']
            if type(stamp) is not str or len(stamp) > 60 or datetime.fromisoformat(stamp.replace('Z', '+00:00')).tzinfo is None:
                raise ValueError()
            if (receipt.get('purchase') is not None or 'purchase' not in receipt
                or any(key in receipt for key in ('shared_trade', 'tracked_credit', 'original_trade_recovery'))
                or receipt.get('requires_purchase') is not False or receipt.get('mode') != 'labels'
                or receipt.get('inventory_intent') is not None or receipt.get('pickup_pending') is not False
                or receipt.get('prepared') is not False or receipt.get('files') != {}): raise ValueError()
            return marker
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _error('The saved original-stock kind or origin needs owner review.', code='INTAKE_STOCK_PROJECTION_INVALID') from None

    def _counts(self, claim, operation, proof):
        original = {row['source_line_key']: row['quantity'] for row in source_lines(claim['source_manifest'])}
        selected = {}
        for row in proof['stockRows']:
            if row['acceptedLineId'] is not None or row['unitIntakeCents'] is not None: raise _error()
            key = row['sourceLineKey']; selected[key] = selected.get(key, 0)+row['quantity']
        excluded = {key: value-selected.get(key, 0) for key, value in original.items()}
        if (any(key not in original or value > original[key] for key, value in selected.items())
            or fingerprint(selected) != fingerprint(operation['selected_quantities'])
            or fingerprint(excluded) != fingerprint(operation['excluded_quantities'])): raise _error()
        return {'basis': 'physical_intake', 'physicalOriginalCopies': sum(original.values()),
                'selectedCopies': sum(selected.values()), 'excludedCopies': sum(excluded.values())}

    def _state(self, tenant, ident, revision, *, proof_hash=None):
        state = registry.read(self.service.store, tenant, ident, server_revision=revision, proof_hash=proof_hash)
        if type(state) is not dict or set(state) != READ_KEYS: raise _error(code='INTAKE_STOCK_REGISTRY_INVALID')
        if state['ordinary_references'] or state['financial_links'] or state['financial_snapshots']:
            raise _error('A saved original receiving or agreement record already references this plan. Open or review that record; no parallel stock record was created.',
                         code='INTAKE_STOCK_LOCAL_OCCUPIED')
        return state

    def _saved(self, child, state, *, observation=None):
        """Validate the exact registered local snapshot; never fresh authority."""
        try:
            receipt = child['receipt']; marker = self._marker(receipt)
            version = state['version']; observation = observation or state['observation']
            if not version or not observation: raise ValueError()
            source = json.loads(observation['restoration_source_body'])
            operation = json.loads(observation['operation_body']); claim = json.loads(observation['claim_body'])
            registry.validate_claim_storage_fields(claim)
            if any(registry.canonical(value) != raw for value, raw in (
                    (source, observation['restoration_source_body']), (operation, observation['operation_body']),
                    (claim, observation['claim_body']))): raise ValueError()
            if (registry.digest(observation['operation_body']) != observation['operation_hash']
                or registry.digest(observation['claim_body']) != observation['claim_hash']): raise ValueError()
            proof = verify_restoration_source(source, operation, claim, accepted_receipt=None)
            if proof['workflowKind'] != 'inventory_only': raise ValueError()
            expected = {'service_origin': ORIGIN, 'tenant_id': marker['tenantId'], 'receiving_id': marker['receivingId'],
                'server_revision': marker['serverRevision'], 'receipt_id': receipt['id'],
                'physical_intake_id': marker['physicalIntakeId'], 'physical_manifest_hash': marker['physicalManifestHash'],
                'source_snapshot_hash': marker['sourceSnapshotHash'], 'plan_hash': marker['planHash']}
            if any(version.get(key) != value for key, value in expected.items()): raise ValueError()
            if (version['created_at'] != receipt['created']
                or version['normalized_plan_body'] != registry.canonical(source['normalizedPlan'])
                or observation['proof_hash'] != marker['proofHash'] or proof['proofHash'] != marker['proofHash']
                or observation['restoration_source_hash'] != marker['restorationSourceHash']
                or proof['restorationSourceHash'] != marker['restorationSourceHash']
                or any(observation.get(key) != expected[key] for key in ('service_origin', 'tenant_id', 'receiving_id', 'server_revision'))):
                raise ValueError()
            stored_bundle = {'tenantId': marker['tenantId'], 'store': marker['store'], 'claim': claim}
            rebuilt = self._build(stored_bundle, proof, receipt['id'], existing=receipt,
                                  stamp=receipt['updated'], revision=receipt['revision'])
            # Complete child equality closes annotations and hidden extra-key
            # loopholes, while preserving original timestamps and tombstones.
            if registry.canonical(rebuilt) != child['raw']: raise ValueError()
            self._counts(claim, operation, proof)
            return proof
        except (ValueError, TypeError, KeyError, AttributeError, ManifestError, RestorationProofError):
            raise _error('The registered original-stock snapshot or its frozen rows changed. Keep it for owner review.',
                         code='INTAKE_STOCK_PROJECTION_INVALID') from None

    def _expected(self, context):
        state = context['registry']; anchor = context['anchor']; child = context['child']
        sources = ((anchor['receipt']['id'], anchor['raw'], anchor['deleted']),) if context['anchorKind'] == 'physical_intake' else ()
        return registry.ExpectedProjection(sources=sources,
            child=(child['receipt']['id'], child['raw'], child['deleted'], child['updated'], child['pickupPending']) if child else None,
            link=_tuple(state['link'], registry.LINK_FIELDS), version=_tuple(state['version'], registry.VERSION_FIELDS),
            observation=_tuple(state['observation'], registry.OBSERVATION_FIELDS),
            active_version=_tuple(state['active_version'], registry.VERSION_FIELDS),
            active_observation=_tuple(state['active_observation'], registry.OBSERVATION_FIELDS),
            ordinary_references=tuple((row['id'], row['body'], row['deleted_at']) for row in state['ordinary_references']),
            financial_links=tuple(tuple(row.values()) for row in state['financial_links']),
            financial_snapshots=tuple(tuple(row.values()) for row in state['financial_snapshots']))

    def _historical_summary(self, marker, expected):
        """Only a strictly newer accepted workflow may supply view metadata.

        No financial body, fresh inventory proof or action authority is made
        from this read. Its original saved inventory proof stays separate.
        """
        bundle = self.cloud.read_intake_receiving_history(marker['physicalIntakeId'], expected_session_id=expected)
        self._authority(bundle, expected, history=True)
        try:
            if bundle['tenantId'] != marker['tenantId'] or bundle['store'] != marker['store']: raise ValueError()
            claim = bundle['claim']; manifest = validate_manifest(claim.get('source_manifest'))
            if (claim.get('physical_intake_id') != marker['physicalIntakeId']
                or claim.get('manifest_hash') != marker['physicalManifestHash']
                or manifest_digest(manifest) != marker['physicalManifestHash']
                or semantic_fingerprint(manifest) != claim.get('semantic_hash')
                or claim.get('state') != 'consumed'): raise ValueError()
            _integer(claim.get('version')); _uuid(claim.get('bound_trade_id'))
            policy = {'one_operation_per_claim': True, 'new_acceptance_requires_fresh_mfa': True,
                      'excluded_rows_reusable': False, 'physical_identity_proven_by_hash': False}
            if type(claim.get('policy')) is not dict or set(claim['policy']) != set(policy) or any(
                    claim['policy'][key] is not value for key, value in policy.items()): raise ValueError()
            history = bundle['history']
            if (type(history) is not dict or set(history) != {'version', 'physical_intake_id', 'operations', 'lookup_complete'}
                or type(history['version']) is not int or history['version'] != 1
                or history['physical_intake_id'] != marker['physicalIntakeId'] or history['lookup_complete'] is not True
                or type(history['operations']) is not list or len(history['operations']) != 1): raise ValueError()
            op = history['operations'][0]
            if (type(op) is not dict or op.get('receiving_id') != marker['receivingId']
                or op.get('workflow_kind') != 'accepted_trade'
                or op.get('physical_intake_id') != marker['physicalIntakeId']
                or op.get('physical_manifest_hash') != marker['physicalManifestHash']
                or op.get('state') not in STATE_NAMES): raise ValueError()
            _integer(op.get('revision'), marker['serverRevision']+1); _hash(op.get('plan_hash'))
            return bundle, {'receivingId': op['receiving_id'], 'serverRevision': op['revision'],
                            'planHash': op['plan_hash'], 'workflowKind': 'accepted_trade'}
        except (ValueError, TypeError, KeyError, AttributeError, ManifestError):
            raise _error('The current workflow history contradicts this saved original-stock record. Keep it for owner review.',
                         code='INTAKE_STOCK_HISTORICAL_EVIDENCE_INVALID') from None

    def _context(self, data, expected, *, historical=False, pending=None):
        self._enabled(); policy = self.service.profile; cloud_policy = self.cloud.profile
        current_summary = None
        if data['anchorKind'] == 'physical_intake':
            anchor, bundle, proof, counts = self.service.intake_receiving_review._proof(data, expected)
            anchor = dict(anchor, hash=registry.digest(anchor['raw']))
            self._authority(bundle, expected)
            state = self._state(bundle['tenantId'], proof['receivingId'], proof['serverRevision'], proof_hash=proof['proofHash'])
            child = self._raw(state['version']['receipt_id'], active=False) if state['version'] else None
            saved_observation = (state['active_observation'] if child and state['active_version']
                and state['active_version']['receipt_id'] == child['receipt']['id'] else state['observation'])
            old = self._saved(child, state, observation=saved_observation) if child else None
            selected_proof_hash = proof['proofHash']
        else:
            anchor = self._raw(data['stockReceiptId'], active=False)
            if anchor['receipt']['revision'] != data['revision']:
                if (not pending or pending.get('receipt_id') != anchor['receipt']['id']
                    or pending.get('receipt_body') != anchor['raw'] or anchor['deleted'] is not None): raise _error()
            marker = self._marker(anchor['receipt'])
            if policy.binding() != {'tenant_id': marker['tenantId'], 'service_origin': ORIGIN}: raise _error()
            state = self._state(marker['tenantId'], marker['receivingId'], marker['serverRevision'], proof_hash=marker['proofHash'])
            child = anchor; old = self._saved(child, state)
            selected_proof_hash = marker['proofHash']
            try:
                bundle = self.cloud.read_intake_receiving_source(marker['physicalIntakeId'], marker['receivingId'], expected_session_id=expected)
            except CloudError as error:
                if not historical or error.code != 'INTAKE_RECEIVING_FINANCIAL_ORIGIN': raise
                bundle, current_summary = self._historical_summary(marker, expected)
                # This remains the independently verified SAVED proof. Only
                # view() uses the separate fresh history summary below.
                proof = old
                observation = state['observation']
                counts = self._counts(json.loads(observation['claim_body']), json.loads(observation['operation_body']), old)
            else:
                self._authority(bundle, expected)
                if bundle['tenantId'] != marker['tenantId'] or bundle['store'] != marker['store']: raise _error()
                proof = verify_restoration_source(bundle['restorationSource'], bundle['operation'], bundle['claim'], accepted_receipt=None)
                counts = self._counts(bundle['claim'], bundle['operation'], proof)
                if all(old[key] == proof[key] for key in ('receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash')):
                    state = self._state(marker['tenantId'], marker['receivingId'], marker['serverRevision'], proof_hash=proof['proofHash'])
                    selected_proof_hash = proof['proofHash']
        if proof['workflowKind'] != 'inventory_only': raise _error()
        link = state['link']
        if child and (not link or state['version']['receipt_id'] != child['receipt']['id']): raise _error()
        same_plan = bool(current_summary is None and (not child or all(old[key] == proof[key] for key in (
            'receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash'))))
        active = bool(child and link and link['active_receipt_id'] == child['receipt']['id']
                      and link['server_revision'] == old['serverRevision'] and link['plan_hash'] == old['planHash'])
        if active and (link['proof_hash'] != child['receipt'][MARKER]['proofHash']
            or link['physical_intake_id'] != child['receipt'][MARKER]['physicalIntakeId']
            or link['physical_manifest_hash'] != child['receipt'][MARKER]['physicalManifestHash']): raise _error()
        if not historical and (not same_plan or child and not active):
            raise _error('This saved record is historical. Review the current revision from its original physical intake; the old record was kept.',
                         code='INTAKE_STOCK_HISTORICAL_PLAN')
        if link and not child and proof['serverRevision'] <= link['server_revision']: raise _error()
        if child and same_plan:
            if (registry.canonical(bundle['restorationSource']['normalizedPlan']) != state['version']['normalized_plan_body']
                or fingerprint(old['provider']) != fingerprint(proof['provider'])
                or fingerprint(old['labelProfile']) != fingerprint(proof['labelProfile'])): raise _error()
        context = {'anchorKind': data['anchorKind'], 'anchor': anchor, 'bundle': bundle, 'proof': proof, 'counts': counts,
            'registry': state, 'child': child, 'savedObservationProof': old, 'profile': policy, 'cloudProfile': cloud_policy,
            'selection': self._selection(data), 'samePlan': same_plan, 'activePlan': active, 'currentSummary': current_summary,
            'selectedProofHash': selected_proof_hash}
        context['expected'] = self._expected(context)
        return context

    def _reauthorize(self, context, expected):
        current = self.cloud.record_authority(expected_session_id=expected)
        if current.get('role') not in ('owner', 'manager'): raise _error(status=403)
        if any(current.get(key) != context['bundle'][key] for key in ('sessionId', 'tenantId', 'store', 'role')):
            raise _error()
        if self.service.profile is not context['profile'] or self.cloud.profile is not context['cloudProfile']: raise _error()
        self._enabled()
        if self.service.profile.binding() != {'tenant_id': current['tenantId'], 'service_origin': ORIGIN}: raise _error()

    def _final(self, context, expected):
        """Caller holds existing session guard then service lock. No encoding."""
        if self.service.profile is not context['profile'] or self.cloud.profile is not context['cloudProfile']: raise _error()
        self._enabled()
        bundle = context['bundle']
        current = self.cloud.assert_recovery_session_unlocked(expected, bundle['tenantId'], bundle['store'])
        if current.get('role') != bundle['role'] or current.get('role') not in ('owner', 'manager'): raise _error(status=403)
        selected = context['registry']['version']
        revision = selected['server_revision'] if selected else context['proof']['serverRevision']
        with self.service.store.db() as db:
            registry.assert_current(db, context['expected'], bundle['tenantId'], context['proof']['receivingId'], revision,
                                    context['selectedProofHash'])

    def shared_label_context(self, stock_receipt_id, revision, expected_session_id, *, purpose):
        """B1-only preparation: new jobs need durable readiness; retained reads do not."""
        try:
            if type(purpose) is not str or purpose not in _SHARED_LABEL_PURPOSES:
                raise _shared_label_error('INPUT_INVALID')
            data = {'anchorKind': 'stock_record', 'stockReceiptId': stock_receipt_id,
                    'revision': revision, 'sessionId': expected_session_id}
            self._input(data, expected_session_id, 'preview')
            with self.cloud.lock:
                source = self._context(data, expected_session_id, historical=False)
                child = source['child']
                if (not child or child['deleted'] is not None or not source['samePlan']
                    or not source['activePlan'] or source['currentSummary'] is not None
                    or child['receipt']['id'] != stock_receipt_id or child['receipt']['revision'] != revision):
                    raise _shared_label_error('CONTEXT_CHANGED')
                marker = self._marker(child['receipt'])
                proof, saved, bundle = source['proof'], source['savedObservationProof'], source['bundle']
                if (type(saved) is not dict or proof['workflowKind'] != 'inventory_only'
                    or proof['state'] != 'received' or proof['received'] is not True
                    or type(proof['labelsVerified']) is not bool
                    or proof['labelError'] is not None and type(proof['labelError']) is not str
                    or bundle['tenantId'] != marker['tenantId'] or bundle['store'] != marker['store']
                    or any(proof[key] != marker[key] for key in ('receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash'))
                    or source['selectedProofHash'] != proof['proofHash']
                    or bundle['claim']['physical_intake_id'] != marker['physicalIntakeId']
                    or bundle['claim']['manifest_hash'] != marker['physicalManifestHash']
                    or bundle['restorationSource']['physicalIntakeId'] != marker['physicalIntakeId']
                    or bundle['restorationSource']['physicalManifestHash'] != marker['physicalManifestHash']):
                    raise _shared_label_error('CONTEXT_CHANGED')
                _hash(proof['terminalReceiptHash'])
                if purpose == 'new_job':
                    if (marker['proofHash'] != proof['proofHash'] or saved['proofHash'] != proof['proofHash']
                        or saved['received'] is not True or saved['state'] != 'received'
                        or saved['terminalReceiptHash'] != proof['terminalReceiptHash']
                        or source['expected'].observation is None
                        or source['expected'].observation != source['expected'].active_observation):
                        raise _shared_label_error('REFRESH_REQUIRED')
                    if proof['labelsVerified'] is not True or proof['labelError'] is not None:
                        raise _shared_label_error('NOT_READY')
                # The unchanged B1 _context already verified BOTH saved and
                # fresh source/proof. It still computes original readiness;
                # this bridge adds no geometry/render/readiness preflight.
                value = confirmed_input_from_proof(proof, service_origin=ORIGIN, tenant_id=bundle['tenantId'],
                    physical_intake_id=marker['physicalIntakeId'], physical_manifest_hash=marker['physicalManifestHash'])
                body, hashed = shared_label_canonical(value), shared_label_input_hash(value)
                profile = value['labelProfile']
                prepared = SharedLabelContext(source['profile'], source['cloudProfile'], source['expected'], purpose,
                    stock_receipt_id, revision, expected_session_id, bundle['tenantId'], bundle['store'], bundle['role'],
                    value['receivingId'], value['serverRevision'], value['planHash'], proof['proofHash'], marker['proofHash'],
                    value['physicalIntakeId'], value['physicalManifestHash'], value['sourceSnapshotHash'],
                    value['terminalReceiptHash'], proof['labelsVerified'], proof['labelError'] is not None, body, hashed,
                    value['terminalBasisHash'], value['confirmedRowsHash'], value['providerHash'],
                    (profile['stock'], profile['layout'], profile['dpi']), len(value['confirmedRows']),
                    sum(row['quantity'] for row in value['confirmedRows']))
                self._reauthorize(source, expected_session_id)
                with self.cloud._session_guard(), self.service.lock:
                    self.assert_shared_label_current(prepared, expected_session_id)
                return prepared
        except (CloudError, ProfilePolicyError):
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError,
                StationError, RestorationProofError, LabelContractError):
            pass
        raise _shared_label_error('CONTEXT_INVALID')

    def assert_shared_label_current(self, context, expected_session_id):
        """Caller holds session guard then service.lock; source-only local raw CAS.

        Mandatory inherited profile/session gates retain their owned metadata
        loading. No NEW content codec, proof/readiness/network or write runs.
        """
        try:
            _shared_label_shape(context, expected_session_id)
            if self.service.profile is not context.profile or self.cloud.profile is not context.cloud_profile:
                raise _shared_label_error('CONTEXT_CHANGED')
            self._enabled()
            current = self.cloud.assert_recovery_session_unlocked(expected_session_id, context.tenant_id, context.store)
            if current.get('role') != context.role or current.get('role') not in ('owner', 'manager'):
                raise _shared_label_error('CONTEXT_CHANGED')
            if self.service.profile.binding() != {'tenant_id': context.tenant_id, 'service_origin': ORIGIN}:
                raise _shared_label_error('CONTEXT_CHANGED')
            with self.service.store.db() as db:
                registry.assert_current(db, context.expected_projection, context.tenant_id, context.receiving_id,
                                        context.server_revision, context.selected_proof_hash)
            return
        except (CloudError, ProfilePolicyError):
            raise
        except (StationError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            pass
        raise _shared_label_error('CONTEXT_CHANGED')

    def operation_context(self, stock_receipt_id, revision, expected_session_id, *, allow_refresh=False):
        """Current existing v2 context for the separately gated action service.

        This bridge never adopts/restores a child or relaxes its client revision.
        Read-only status and exact saved-result completion may inspect a newer
        observation; starting or dispatching an action requires an explicit B1
        refresh first. All selection remains in this call's context.
        """
        if type(allow_refresh) is not bool:
            raise _error(code='INTAKE_STOCK_ACTION_INPUT', status=400)
        data = {'anchorKind': 'stock_record', 'stockReceiptId': stock_receipt_id,
                'revision': revision, 'sessionId': expected_session_id}
        self._input(data, expected_session_id, 'preview')
        with self.cloud.lock:
            context = self._context(data, expected_session_id, historical=False)
            child = context['child']
            if (not child or child['deleted'] is not None or not context['samePlan']
                or not context['activePlan'] or context['currentSummary'] is not None):
                raise _error(code='INTAKE_STOCK_ACTION_STALE')
            if not allow_refresh and child['receipt'][MARKER]['proofHash'] != context['proof']['proofHash']:
                raise _error('Explicitly refresh this saved stock record, reload it, and review the original action again.',
                             code='INTAKE_STOCK_ACTION_REFRESH_REQUIRED')
            self._reauthorize(context, expected_session_id)
            with self.cloud._session_guard(), self.service.lock:
                self.assert_operation_current(context, expected_session_id)
            return context

    def prepare_operation_observation(self, context):
        """Prepare only reuse/refresh of this already registered active child.

        Call outside short publication guards, after the action service has
        verified an exact saved request result or durable applied basis. This
        method grants no request/result authority and performs no publication.
        """
        child = context['child']
        if (not child or child['deleted'] is not None or not context['samePlan']
            or not context['activePlan'] or context['currentSummary'] is not None):
            raise _error(code='INTAKE_STOCK_ACTION_STALE')
        receipt = child['receipt']; proof = context['proof']; bundle = context['bundle']
        same = receipt[MARKER]['proofHash'] == proof['proofHash']
        if same:
            rebuilt = self._build(bundle, proof, receipt['id'], existing=receipt,
                                  stamp=receipt['updated'], revision=receipt['revision'])
            if registry.canonical(rebuilt) != child['raw']:
                raise _error(code='INTAKE_STOCK_ACTION_STALE')
            candidate = copy.deepcopy(receipt)
        else:
            candidate = self._build(bundle, proof, receipt['id'], existing=receipt)
        return registry.prepare(self.service.store, candidate, proof, bundle['restorationSource'],
            bundle['operation'], bundle['claim'], existing=receipt, mode='reuse' if same else 'refresh')

    def assert_operation_current(self, context, expected_session_id):
        """Caller holds existing session guard then service.lock; no encoding."""
        self.service.profile.require_feature(INTAKE_STOCK_OPERATIONS_FEATURE)
        if (not context['child'] or context['child']['deleted'] is not None
            or not context['samePlan'] or not context['activePlan'] or context['currentSummary'] is not None):
            raise _error(code='INTAKE_STOCK_ACTION_STALE')
        self._final(context, expected_session_id)

    def _facts(self, context, proposed_id):
        proof = context['proof']; bundle = context['bundle']
        return {'selection': context['selection'], 'profile': id(context['profile']), 'cloudProfile': id(context['cloudProfile']),
            'serviceOrigin': ORIGIN, 'tenantId': bundle['tenantId'], 'store': bundle['store'], 'role': bundle['role'],
            'sessionId': bundle['sessionId'], 'anchorHash': context['anchor']['hash'],
            'registryHash': fingerprint(context['registry']), 'childHash': context['child']['hash'] if context['child'] else None,
            'receivingId': proof['receivingId'], 'serverRevision': proof['serverRevision'], 'planHash': proof['planHash'],
            'sourceHash': registry.digest(registry.canonical(bundle['restorationSource'])),
            'operationHash': registry.digest(registry.canonical(bundle['operation'])),
            'claimHash': registry.digest(registry.canonical(bundle['claim'])), 'proofHash': proof['proofHash'],
            'proposedReceiptId': proposed_id}

    def _cached(self, data, expected):
        with self._lock:
            self._purge(); cached = copy.deepcopy(self._previews.get(data['confirmationHash']))
        if not cached or cached['facts']['sessionId'] != expected or cached['facts']['selection'] != self._selection(data):
            raise _error('This original-stock review expired or changed. Review it again.', code='INTAKE_STOCK_PREVIEW_EXPIRED')
        return cached

    def _live(self, confirmation):
        with self._lock:
            self._purge()
            if confirmation not in self._previews: raise _error(code='INTAKE_STOCK_PREVIEW_EXPIRED')

    def _preview_dto(self, context):
        p = context['proof']; child = context['child']; data = context['selection']
        result = {key: copy.deepcopy(p[key]) for key in ('receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash',
            'restorationSourceHash', 'stateBasisHash', 'proofHash', 'workflowKind', 'state', 'admission', 'provider',
            'labelProfile', 'received', 'terminalReceiptHash', 'labelsVerified', 'labelError')}
        result.update(version=1, originKind='physical_intake', anchorKind=context['anchorKind'],
            serviceOrigin=ORIGIN, tenantId=context['bundle']['tenantId'], store=context['bundle']['store'],
            receiptId=data.get('receiptId'), stockReceiptId=data.get('stockReceiptId'), revision=data['revision'],
            physicalIntakeId=context['bundle']['claim']['physical_intake_id'],
            physicalManifestHash=context['bundle']['claim']['manifest_hash'],
            physicalReference=context['bundle']['claim']['physical_reference'], counts=copy.deepcopy(context['counts']),
            rows=copy.deepcopy(p['stockRows'][:100]), rowsOffset=0, rowsLimit=100, totalRows=len(p['stockRows']),
            existingProjectionId=child['receipt']['id'] if child else None, restoreRequired=bool(child and child['deleted']),
            existingProjectionRevision=child['receipt']['revision'] if child else None,
            existingProjectionCreatedAt=child['receipt']['created'] if child else None,
            existingProjectionRecoveredAt=child['receipt'][MARKER]['recoveredAt'] if child else None,
            samePhysicalDeliveryRequired=context['anchorKind'] == 'physical_intake',
            refreshRequired=bool(child and child['receipt'][MARKER]['proofHash'] != p['proofHash']), allowedActions=[])
        return result

    def _build(self, bundle, proof, ident, *, existing=None, stamp=None, revision=None):
        stamp = stamp or now(); marker = existing[MARKER] if existing else None
        cards = []
        for row in proof['stockRows']:
            if row['acceptedLineId'] is not None or row['unitIntakeCents'] is not None: raise _error()
            card = {field: row['identity'].get(key, '') for key, field in IDENTITY_FIELDS.items()}
            card.update(row_id=row['reviewLineId'], source_line_key=row['sourceLineKey'], qty=row['quantity'],
                intake_value=0, final_price=row['sellingCents']/100, include=True, reviewed=True,
                price_confirmed=row['sellingCents'] > 0 or row['zeroPriceConfirmed'], price_missing=False,
                entry_source='hosted_intake_receiving_projection', storage_location='', sku=row['targetSku'],
                sku_override='', operator_price=True)
            cards.append(card)
        mark = {'version': 2, 'originKind': 'physical_intake', 'serviceOrigin': ORIGIN, 'tenantId': bundle['tenantId'],
            'store': bundle['store'], 'physicalIntakeId': bundle['claim']['physical_intake_id'],
            'physicalManifestHash': bundle['claim']['manifest_hash'], 'sourceSnapshotHash': proof['sourceSnapshotHash'],
            'receivingId': proof['receivingId'], 'serverRevision': proof['serverRevision'], 'planHash': proof['planHash'],
            'restorationSourceHash': proof['restorationSourceHash'], 'proofHash': proof['proofHash'],
            'snapshotReference': proof['receivingId']+':'+str(proof['serverRevision'])+':'+proof['proofHash'],
            'recoveredAt': marker['recoveredAt'] if marker else stamp}
        games = {row['identity']['game'] for row in proof['stockRows']}
        result = dict(id=ident, source_hash='hosted-original-intake:'+proof['receivingId']+':'+str(proof['serverRevision']),
            source_name='Original physical-intake stock plan', title='Original stock plan',
            created=existing['created'] if existing else stamp, updated=stamp,
            revision=revision if revision is not None else existing['revision']+1 if existing else 1,
            mode='labels', cards=cards, source_manifest=copy.deepcopy(bundle['claim']['source_manifest']),
            inventory_status='received' if proof['received'] else 'not_received', inventory_intent=None,
            pricing_game=next(iter(games)) if len(games) == 1 else '', mixed_games=len(games) > 1,
            purchase=None, requires_purchase=False, prepared=False, pickup_pending=False,
            intake_source='hosted_intake_receiving_projection', intake_summary={}, files={}, error=proof['labelError'] or '',
            channel='tcgplayer', notes='', customer_reference='', storage_strategy='bin', storage_location='',
            cash_percent=35, credit_percent=50, hosted_label_profile=copy.deepcopy(proof['labelProfile']))
        if existing and 'deleted_at' in existing: result['deleted_at'] = existing['deleted_at']
        result[MARKER] = mark
        claim = bundle['claim']
        result['intake_claim'] = {'state': 'confirmed', 'store': bundle['store'], 'source_manifest_hash': claim['manifest_hash'],
            'physical_intake_id': claim['physical_intake_id'], 'manifest_hash': claim['manifest_hash'],
            'semantic_hash': claim['semantic_hash'], 'server_state': claim['state'], 'server_version': claim['version'],
            'physical_reference': claim['physical_reference'], 'bound_trade_id': claim['bound_trade_id']}
        result['hosted_receive'] = {'version': 2, 'restoration_projection': True, 'receiving_id': proof['receivingId'],
            'server_revision': proof['serverRevision'], 'plan_hash': proof['planHash'], 'store': bundle['store'],
            'state': 'confirmed' if proof['received'] else 'cancelled_before_write' if proof['state'] == 'cancelled_before_write'
                else 'planned' if proof['state'] == 'ready' else 'needs_attention', 'server_state': proof['state'],
            'label_profile': copy.deepcopy(proof['labelProfile']), 'snapshot_reference': mark['snapshotReference'],
            'message': 'Exact original physical-intake plan; no provider, payment or credit action.'}
        return result

    def activate(self, data, expected):
        # Every attempt retires confirmations, including an epoch write whose
        # later initializer failed. A prior policy object is never reused.
        self.clear()
        self._input(data, expected, 'activate')
        try:
            with self.cloud.lock:
                before = self.service.profile
                setup = getattr(self.service, 'setup', None)
                if setup is not None and setup.policy is not before:
                    raise _error('Reload the store setup before enabling original-stock records.', code='INTAKE_STOCK_POLICY_CHANGED')
                authority = self.cloud.record_authority(expected_session_id=expected)
                if authority.get('role') not in ('owner', 'manager'): raise _error(status=403)
                if before.binding() != {'tenant_id': authority['tenantId'], 'service_origin': ORIGIN}: raise _error()
                with self.cloud._session_guard(), self.service.lock:
                    if (self.service.profile is not before or getattr(self.service, 'setup', None) is not setup
                        or setup is not None and setup.policy is not before): raise _error()
                    saved = self.cloud.assert_recovery_session_unlocked(expected, authority['tenantId'], authority['store'])
                    if saved.get('role') != authority['role'] or saved.get('role') not in ('owner', 'manager'): raise _error(status=403)
                    try:
                        with self.service.store.db() as db:
                            state = registry.initialization_state(db)
                    except StationError as error:
                        raise _error('Original stock metadata needs owner review. Your saved work was kept; setup will not recreate or repair it.',
                                     code='INTAKE_STOCK_FEATURE_RECOVERY_REQUIRED') from error
                    if state not in ('absent', 'present'):
                        raise _error('Original stock metadata could not be recognized. Keep saved work for owner review.',
                                     code='INTAKE_STOCK_FEATURE_RECOVERY_REQUIRED')
                    facts = (expected, saved['tenant_id'], saved['store'], saved['role'], saved['expires_at'])
                    try:
                        updated = before.activate_feature(INTAKE_PROJECTION_FEATURE, self.service.store, current_session_facts=facts)
                    except (StationError, OSError) as error:
                        if isinstance(error, ProfilePolicyError) and error.code == 'PROFILE_FEATURE_PRE_EPOCH_METADATA':
                            raise _error('Original stock metadata needs owner review before this profile can enable it. Your saved work was kept.',
                                         code='INTAKE_STOCK_FEATURE_RECOVERY_REQUIRED') from error
                        # The policy epoch may be durable even when later DDL
                        # failed. Browser reload does not reconstruct this
                        # process's immutable policy consumers. Reopen the
                        # desktop app; never reload policy or retry implicitly.
                        raise _error('Original stock setup could not finish. Your saved work was kept. Close and reopen the desktop app before explicitly finishing setup or reviewing owner recovery.',
                                     code='INTAKE_STOCK_FEATURE_RESTART_REQUIRED') from error
                    self.service.profile = updated; self.cloud.profile = updated
                    if setup is not None: setup.policy = updated
                result = {'version': 1, 'feature': INTAKE_PROJECTION_FEATURE, 'enabled': True, 'reloadRequired': True,
                          'providerActionPerformed': False, 'financialActionPerformed': False}
                _encoded(result)
                return result
        finally:
            self.clear()

    def preview(self, data, expected):
        self._input(data, expected, 'preview')
        try:
            with self.cloud.lock:
                context = self._context(data, expected); child = context['child']
                proposed = child['receipt']['id'] if child else 'R-'+time.strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:12].upper()
                facts = self._facts(context, proposed)
                confirmation = fingerprint({'domain': 'stock-and-sleeve:intake-stock-projection-preview:v1',
                                            'random': secrets.token_hex(32), 'facts': facts})
                result = {'preview': self._preview_dto(context), 'confirmationHash': confirmation, 'expiresIn': 300}
                _encoded(result); self._reauthorize(context, expected)
                with self.cloud._session_guard(), self.service.lock:
                    self._final(context, expected)
                    with self._lock:
                        stamp = self._purge()
                        while len(self._previews) >= 16: self._previews.pop(next(iter(self._previews)))
                        self._previews[confirmation] = {'facts': facts, 'expires': stamp+300,
                            'restoreRequired': bool(child and child['deleted']),
                            'samePhysicalDeliveryRequired': context['anchorKind'] == 'physical_intake'}
                return result
        except Exception:
            self.clear(); raise

    def rows(self, data, expected):
        self._input(data, expected, 'rows')
        try:
            with self.cloud.lock:
                cached = self._cached(data, expected); context = self._context(data, expected)
                if self._facts(context, cached['facts']['proposedReceiptId']) != cached['facts']: raise _error()
                proof = context['proof']; total = len(proof['stockRows'])
                if data['proofHash'] != proof['proofHash'] or data['offset'] >= total: raise _error()
                result = {'version': 1, 'originKind': 'physical_intake', **context['selection'],
                    'serviceOrigin': ORIGIN, 'tenantId': context['bundle']['tenantId'], 'store': context['bundle']['store'],
                    'receivingId': proof['receivingId'], 'serverRevision': proof['serverRevision'], 'planHash': proof['planHash'],
                    'proofHash': proof['proofHash'], 'offset': data['offset'], 'limit': data['limit'], 'totalRows': total,
                    'rows': copy.deepcopy(proof['stockRows'][data['offset']:data['offset']+data['limit']])}
                _encoded(result); self._reauthorize(context, expected)
                with self.cloud._session_guard(), self.service.lock:
                    self._final(context, expected); self._live(data['confirmationHash'])
                return result
        except Exception:
            self.clear(); raise

    def _pending_match(self, cached, context, facts):
        if facts == cached['facts']: return False
        pending = cached.get('pending')
        # Reconcile only a known successful local commit made by THIS cached
        # confirmation, then compare every remote/source/profile/selection fact.
        if not pending or not context['child'] or context['child']['deleted'] is not None: raise _error()
        if (context['child']['receipt']['id'] != pending['receipt_id']
            or context['child']['raw'] != pending['receipt_body']
            or context['registry']['link']['revision'] != pending['link_revision']): raise _error()
        mutable = {'registryHash', 'childHash'}
        # The stock-record anchor is itself the child. A successful refresh is
        # reconciled with the original browser revision only through this exact
        # retained winner, not an arbitrary changed local record.
        if context['anchorKind'] == 'stock_record': mutable.add('anchorHash')
        if any(facts[key] != value for key, value in cached['facts'].items() if key not in mutable): raise _error()
        return True

    def _winner_match(self, cached, context, facts):
        """An independently committed identical first adoption is the winner.

        Only a newly created active revision1 child in the separate registry
        can satisfy this path. Every original source, remote and authority fact
        remains equal. This never chooses an ordinary/financial/malformed row.
        """
        child = context['child']; link = context['registry']['link']
        if cached.get('pending'): return False
        if (cached['facts']['childHash'] is not None or context['anchorKind'] != 'physical_intake'
            or not child or child['deleted'] is not None or child['receipt']['revision'] != 1
            or not link or link['revision'] != 1 or not context['activePlan']
            or child['receipt'][MARKER]['proofHash'] != context['proof']['proofHash']): return False
        if any(facts[key] != value for key, value in cached['facts'].items()
               if key not in ('registryHash', 'childHash')): return False
        observation = context['registry']['observation']; bundle = context['bundle']
        return bool(observation and observation['restoration_source_body'] == registry.canonical(bundle['restorationSource'])
            and observation['operation_body'] == registry.canonical(bundle['operation'])
            and observation['claim_body'] == registry.canonical(bundle['claim']))

    def _response(self, context, committed, confirmation, expected):
        proof = context['proof']; child = context['child']
        if (not child or child['deleted'] is not None or child['receipt']['id'] != committed['receipt_id']
            or child['raw'] != committed['receipt_body']
            or context['registry']['link']['revision'] != committed['link_revision']): raise _error()
        result = {'stockReceipt': copy.deepcopy(child['receipt']), 'projection': {'version': 1, 'originKind': 'physical_intake',
            'receivingId': proof['receivingId'], 'serverRevision': proof['serverRevision'], 'planHash': proof['planHash'],
            'proofHash': proof['proofHash'], 'reused': committed['reused'], 'restored': committed['restored'],
            'refreshed': committed['refreshed']}, 'providerActionPerformed': False, 'financialActionPerformed': False}
        _encoded(result); self._reauthorize(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            self._final(context, expected); self._live(confirmation)
        return result

    def adopt(self, data, expected):
        self._input(data, expected, 'adopt')
        try:
            with self.cloud.lock:
                cached = self._cached(data, expected)
                context = self._context(data, expected, pending=cached.get('pending'))
                proof = context['proof']; child = context['child']
                facts = self._facts(context, cached['facts']['proposedReceiptId'])
                winner = self._winner_match(cached, context, facts)
                reconciled = False if winner else self._pending_match(cached, context, facts)
                for key, value in (('expectedServerRevision', proof['serverRevision']), ('expectedPlanHash', proof['planHash']),
                                   ('expectedRestorationSourceHash', proof['restorationSourceHash']), ('proofHash', proof['proofHash'])):
                    if data[key] != value: raise _error()
                if data['samePhysicalDeliveryConfirmed'] is not cached['samePhysicalDeliveryRequired']:
                    raise _error('Confirm that the selected intake is the same physical delivery.', code='INTAKE_STOCK_PHYSICAL_CONFIRMATION_REQUIRED')
                if data['restoreExisting'] != cached['restoreRequired']: raise _error(code='INTAKE_STOCK_RESTORE_REQUIRED')
                if reconciled:
                    return self._response(context, cached['pending'], data['confirmationHash'], expected)
                if winner:
                    committed = {'receipt_id': child['receipt']['id'], 'receipt_body': child['raw'],
                        'link_revision': context['registry']['link']['revision'], 'reused': True, 'restored': False, 'refreshed': False}
                    self._reauthorize(context, expected)
                    with self.cloud._session_guard(), self.service.lock:
                        self._final(context, expected); self._live(data['confirmationHash'])
                        with self._lock: self._previews[data['confirmationHash']]['pending'] = copy.deepcopy(committed)
                    return self._response(context, committed, data['confirmationHash'], expected)
                restoring = bool(child and child['deleted'])
                observation = context['registry']['observation']
                same = bool(observation and observation['proof_hash'] == proof['proofHash']
                    and observation['restoration_source_body'] == registry.canonical(context['bundle']['restorationSource'])
                    and observation['operation_body'] == registry.canonical(context['bundle']['operation'])
                    and observation['claim_body'] == registry.canonical(context['bundle']['claim']))
                if observation and observation['proof_hash'] == proof['proofHash'] and not same: raise _error()
                mode = 'restore' if restoring else 'reuse' if child and same else 'refresh' if child else 'create'
                existing = child['receipt'] if child else None
                receipt = (copy.deepcopy(existing) if mode == 'reuse' else
                           self._build(context['bundle'], proof, cached['facts']['proposedReceiptId'], existing=existing))
                if restoring: receipt.pop('deleted_at', None)
                prepared = registry.prepare(self.service.store, receipt, proof, context['bundle']['restorationSource'],
                    context['bundle']['operation'], context['bundle']['claim'], existing=existing, mode=mode)
                # All candidate response serialization is outside publication
                # guards, including the complete returned child.
                prospective = {'stockReceipt': receipt, 'projection': {'version': 1, 'originKind': 'physical_intake',
                    'receivingId': proof['receivingId'], 'serverRevision': proof['serverRevision'], 'planHash': proof['planHash'],
                    'proofHash': proof['proofHash'], 'reused': child is not None, 'restored': restoring, 'refreshed': mode == 'refresh'},
                    'providerActionPerformed': False, 'financialActionPerformed': False}
                _encoded(prospective); self._reauthorize(context, expected)
                try:
                    with self.cloud._session_guard(), self.service.lock:
                        self._final(context, expected); self._live(data['confirmationHash'])
                        committed = registry.commit(self.service.store, prepared, context['expected'], restore_existing=restoring)
                        with self._lock:
                            self._previews[data['confirmationHash']]['pending'] = copy.deepcopy(committed)
                except StationError:
                    # A cooperating second process may have published the
                    # identical first projection after our last raw read.
                    # Reread/reverify outside all guards; other conflicts stay
                    # errors and no failed SQL publication is relabeled green.
                    current = self._context(data, expected)
                    current_facts = self._facts(current, cached['facts']['proposedReceiptId'])
                    if not self._winner_match(cached, current, current_facts): raise
                    local = current['child']
                    committed = {'receipt_id': local['receipt']['id'], 'receipt_body': local['raw'],
                        'link_revision': current['registry']['link']['revision'], 'reused': True, 'restored': False, 'refreshed': False}
                    self._reauthorize(current, expected)
                    with self.cloud._session_guard(), self.service.lock:
                        self._final(current, expected); self._live(data['confirmationHash'])
                        with self._lock: self._previews[data['confirmationHash']]['pending'] = copy.deepcopy(committed)
                    return self._response(current, committed, data['confirmationHash'], expected)
                # A lost response has a durable winner. Rebuild a NEW context
                # from that winner and reauthorize before exposing any receipt.
                local = self._raw(committed['receipt_id'])
                if local['raw'] != committed['receipt_body']: raise _error()
                post_data = {'anchorKind': 'stock_record', 'stockReceiptId': local['receipt']['id'],
                             'revision': local['receipt']['revision'], 'sessionId': expected}
                post = self._context(post_data, expected)
                if post['child']['raw'] != local['raw'] or post['proof']['proofHash'] != proof['proofHash']: raise _error()
                return self._response(post, committed, data['confirmationHash'], expected)
        except Exception:
            # A successfully RETURNED response retains exact winner facts for
            # a reply lost downstream. Any local failure retires this scope;
            # a new preview can still open the durable same-ID winner.
            self.clear()
            raise

    def view(self, data, expected):
        self._input(data, expected, 'view')
        try:
            with self.cloud.lock:
                selection = {'anchorKind': 'stock_record', **data}
                context = self._context(selection, expected, historical=True)
                old = context['savedObservationProof']; current = context['proof']
                summary = context['currentSummary'] or current
                verification = {'status': 'same_plan' if context['samePlan'] and context['activePlan'] else 'historical_plan',
                    'currentReceivingId': summary['receivingId'], 'currentServerRevision': summary['serverRevision'],
                    'currentPlanHash': summary['planHash'], 'currentWorkflowKind': summary['workflowKind'],
                    'samePlan': context['samePlan'], 'activePlan': context['activePlan'],
                    'refreshRequired': bool(context['samePlan'] and context['activePlan'] and old['proofHash'] != current['proofHash'])}
                result = {'version': 1, 'originKind': 'physical_intake', 'stockReceipt': copy.deepcopy(context['child']['receipt']),
                    'saved': {key: copy.deepcopy(old[key]) for key in ('receivingId', 'serverRevision', 'planHash', 'proofHash',
                        'state', 'received', 'provider', 'labelProfile', 'labelsVerified', 'labelError')},
                    'currentVerification': verification, 'readOnly': True,
                    'providerActionPerformed': False, 'financialActionPerformed': False}
                _encoded(result); self._reauthorize(context, expected)
                with self.cloud._session_guard(), self.service.lock: self._final(context, expected)
                return result
        except Exception:
            self.clear(); raise
