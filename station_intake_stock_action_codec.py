"""Pure inventory-only action terms; no profile, cloud, journal or authority.

Shape/hash agreement is not original-source/provider authority. Callers still
need the unchanged coherent proof verifier and B1 raw observation checks.
"""
import copy
from datetime import datetime
import hashlib
import json
import math
import re
import unicodedata
import uuid

from hosted.receiving_plan import normalize_plan
from receiving_evidence import catalog_evidence, final_targets, provider_id
from catalog_physical_ownership import validate_ownership, identity_for_ownership
from intake_manifest import source_line_id
from station_store import StationError


ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
DOMAIN = 'stock-and-sleeve:intake-stock-action:v1'
STATE_DOMAIN = 'stock-and-sleeve:receiving-restoration-state:v1'
SOURCE_DOMAIN = 'stock-and-sleeve:receiving-restoration-source:v1'
MAX_INTENT_BYTES = 65536
MAX_PUBLIC_BYTES = 8 * 1024 * 1024
MAX_INTEGER = 2**63 - 1
ACTIONS = frozenset(('commit', 'recover', 'authorize', 'cancel'))
STATES = frozenset(('ready', 'catalog_pending', 'catalog_confirmed', 'inventory_pending',
                    'outcome_unknown', 'needs_review', 'received', 'cancelled_before_write'))
INTENT_FIELDS = frozenset(('version', 'originKind', 'serviceOrigin', 'tenantId', 'store', 'intentId',
    'stockReceiptId', 'stockRevision', 'physicalIntakeId', 'physicalManifestHash', 'sourceSnapshotHash',
    'receivingId', 'serverRevision', 'planHash', 'restorationSourceHash', 'proofHash', 'stateBasisHash',
    'operationHash', 'provider', 'labelProfile', 'kind', 'requestKey', 'body', 'createdAt'))
SOURCE_FIELDS = frozenset(('version', 'receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash',
    'physicalIntakeId', 'physicalManifestHash', 'workflowKind', 'acceptedTradeId', 'acceptedReceiptHash',
    'normalizedPlan', 'admission', 'stateBasisHash', 'restorationSourceHash'))
HEADER_FIELDS = ('physical_intake_id', 'physical_manifest_hash', 'source_snapshot_hash',
    'client_label_profile', 'workflow_kind', 'accepted_trade_id', 'accepted_receipt_hash',
    'provider', 'selected_quantities', 'excluded_quantities')
OPERATION_FIELDS = frozenset(HEADER_FIELDS) | {'receiving_id', 'revision', 'state', 'plan_hash', 'targets',
    'inventory_confirmed', 'receipt', 'blocked_reasons', 'requires_fresh_mfa', 'catalog_confirmed',
    'catalog_resolutions', 'catalog_proof', 'catalog_proof_hash', 'catalog_resolutions_hash'}
TARGET_FIELDS = frozenset(('review_line_id', 'source_line_key', 'identity_hash', 'binding_id',
    'binding_version', 'binding_evidence_hash', 'source_sku', 'target_sku', 'variation_id', 'parent_id',
    'selling_cents', 'currency', 'quantity', 'action'))
TEMPLATE_FIELDS = frozenset(('binding_role', 'template_id', 'template_version', 'template_evidence_hash'))
ADMISSION_FIELDS = frozenset(('everAdmitted', 'catalogStepPresent', 'inventoryStepPresent', 'leaseActive'))


def _fail():
    raise StationError('The exact inventory-only action terms are invalid. Keep the original operation and request key.')


def _json_shape(value, depth=0):
    if depth > 32:
        _fail()
    kind = type(value)
    if kind is str:
        if '\x00' in value:
            _fail()
        value.encode('utf-8')
    elif kind is float:
        if not math.isfinite(value):
            _fail()
    elif kind is int:
        if not -MAX_INTEGER - 1 <= value <= MAX_INTEGER:
            _fail()
    elif value is None or kind is bool:
        return
    elif kind in (list, tuple):
        for item in value:
            _json_shape(item, depth + 1)
    elif kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail()
            _json_shape(key, depth + 1)
            _json_shape(item, depth + 1)
    else:
        _fail()


def canonical(value):
    try:
        _json_shape(value)
        result = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(result.encode('utf-8')) > MAX_PUBLIC_BYTES:
            _fail()
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail()


def _fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        _fail()
    return value


def _hash(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        _fail()
    return value


def _integer(value, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _text(value, minimum=1, maximum=255):
    if (type(value) is not str or not minimum <= len(value) <= maximum
            or any(unicodedata.category(char) in ('Cc', 'Cs') for char in value)):
        _fail()
    return value


def _date(value):
    _text(value, maximum=60)
    if len(value.encode('utf-8')) > 240:
        _fail()
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail()


def _profile(value):
    if (type(value) is not dict or set(value) != {'stock', 'layout', 'dpi'}
            or value['stock'] not in ('2x1', '3x1', '4x2')
            or value['layout'] not in ('standard', 'fold', 'side')
            or type(value['dpi']) is not int or value['dpi'] not in (203, 300)):
        _fail()


def _provider(value):
    if (type(value) is not dict or set(value) != {'merchant_id', 'environment', 'application_id', 'location_id'}
            or value['environment'] not in ('production', 'sandbox')):
        _fail()
    for name in ('merchant_id', 'application_id', 'location_id'):
        if type(value[name]) is not str:
            _fail()
        provider_id(value[name])


def _quantities(value, *, zero=False):
    if type(value) is not dict or len(value) > 1000:
        _fail()
    for key, amount in value.items():
        _hash(key)
        _integer(amount, 0 if zero else 1, 1000000)


def _target(value, *, physical_v2=False):
    if type(value) is not dict or value.get('action') not in ('reuse_existing', 'create_price'):
        _fail()
    fields = TARGET_FIELDS | (TEMPLATE_FIELDS if value['action'] == 'create_price' else set())
    if physical_v2:fields=fields|{'physical_ownership'}
    if set(value) != fields:
        _fail()
    if physical_v2:
        ownership=validate_ownership(value['physical_ownership'])
        if value['action']!='reuse_existing' or source_line_id(identity_for_ownership(ownership))!=value['identity_hash']:
            _fail()
    _uuid(value['review_line_id']); _uuid(value['binding_id'])
    for key in ('source_line_key', 'identity_hash', 'binding_evidence_hash'):
        _hash(value[key])
    _integer(value['binding_version']); _integer(value['quantity'], 1, 100000)
    _integer(value['selling_cents'], 0, 100000000)
    _text(value['source_sku'], maximum=100); _text(value['target_sku'], maximum=100)
    provider_id(value['parent_id'])
    if value['currency'] != 'USD':
        _fail()
    if value['action'] == 'reuse_existing':
        provider_id(value['variation_id'])
        if value['variation_id'].startswith('#') or value['source_sku'] != value['target_sku']:
            _fail()
    else:
        if value['variation_id'] is not None or value['binding_role'] != 'source_family':
            _fail()
        _uuid(value['template_id']); _integer(value['template_version']); _hash(value['template_evidence_hash'])


def validate_operation(operation):
    """Bound known inventory-result storage shape, not fresh receiving proof."""
    try:
        physical_v2=type(operation) is dict and 'physical_ownership_version' in operation
        if physical_v2 and (type(operation['physical_ownership_version']) is not int or operation['physical_ownership_version']!=2):
            _fail()
        fields=OPERATION_FIELDS|({'physical_ownership_version'} if physical_v2 else set())
        if type(operation) is not dict or set(operation) != fields:
            _fail()
        canonical(operation)
        if (operation['workflow_kind'] != 'inventory_only' or operation['accepted_trade_id'] is not None
                or operation['accepted_receipt_hash'] is not None or operation['state'] not in STATES
                or type(operation['inventory_confirmed']) is not bool
                or type(operation['catalog_confirmed']) is not bool or operation['requires_fresh_mfa'] is not True):
            _fail()
        _uuid(operation['receiving_id']); _uuid(operation['physical_intake_id']); _integer(operation['revision'])
        for key in ('plan_hash', 'physical_manifest_hash', 'source_snapshot_hash'):
            _hash(operation[key])
        _provider(operation['provider']); _profile(operation['client_label_profile'])
        _quantities(operation['selected_quantities']); _quantities(operation['excluded_quantities'], zero=True)
        targets = operation['targets']
        if type(targets) is not list or not 1 <= len(targets) <= 1000:
            _fail()
        seen = set(); selected = {}
        for target in targets:
            _target(target,physical_v2=physical_v2)
            if target['review_line_id'] in seen:
                _fail()
            seen.add(target['review_line_id'])
            key = target['source_line_key']; selected[key] = selected.get(key, 0) + target['quantity']
        if canonical(selected) != canonical(operation['selected_quantities']):
            _fail()
        blocked = operation['blocked_reasons']
        if type(blocked) is not list or len(blocked) > 10:
            _fail()
        for reason in blocked:
            _text(reason, maximum=100)
        catalog_evidence(operation, targets)
        receipt = operation['receipt']; creating = any(row['action'] == 'create_price' for row in targets)
        if receipt is None:
            if operation['inventory_confirmed'] or operation['state'] == 'received':
                _fail()
        else:
            fields = set(HEADER_FIELDS) | {'receipt_schema_version', 'receiving_id', 'revision', 'plan_hash',
                'confirmed_targets', 'inventory_confirmed', 'catalog_write_performed', 'received_at',
                'provider_proof_hash', 'receipt_hash'}
            if physical_v2:fields.add('physical_ownership_version')
            if creating:
                fields |= {'catalog_proof_hash', 'catalog_resolutions_hash', 'catalog_resolutions'}
            if (type(receipt) is not dict or set(receipt) != fields or operation['state'] != 'received'
                    or operation['inventory_confirmed'] is not True or type(receipt['receipt_schema_version']) is not int
                    or receipt['receipt_schema_version'] != 1 or receipt['inventory_confirmed'] is not True
                    or type(receipt['catalog_write_performed']) is not bool or receipt['catalog_write_performed'] is not creating):
                _fail()
            same = HEADER_FIELDS + ('receiving_id', 'revision', 'plan_hash')
            if physical_v2:same+=('physical_ownership_version',)
            if canonical({key: receipt[key] for key in same}) != canonical({key: operation[key] for key in same}):
                _fail()
            expected = [dict(row, confirmed_quantity=row['quantity']) for row in final_targets(operation, targets)]
            if canonical(receipt['confirmed_targets']) != canonical(expected):
                _fail()
            if creating and canonical({key: receipt[key] for key in ('catalog_proof_hash', 'catalog_resolutions_hash', 'catalog_resolutions')}) != canonical({key: operation[key] for key in ('catalog_proof_hash', 'catalog_resolutions_hash', 'catalog_resolutions')}):
                _fail()
            _date(receipt['received_at']); _hash(receipt['provider_proof_hash']); _hash(receipt['receipt_hash'])
            if receipt['receipt_hash'] != _fingerprint({key: item for key, item in receipt.items() if key != 'receipt_hash'}):
                _fail()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError):
        _fail()


def validate_intent(intent):
    try:
        if (type(intent) is not dict or set(intent) != INTENT_FIELDS or type(intent['version']) is not int
                or intent['version'] != 1 or intent['originKind'] != 'physical_intake' or intent['serviceOrigin'] != ORIGIN):
            _fail()
        for key in ('tenantId', 'intentId', 'physicalIntakeId', 'receivingId'):
            _uuid(intent[key])
        for key in ('physicalManifestHash', 'sourceSnapshotHash', 'planHash', 'restorationSourceHash',
                    'proofHash', 'stateBasisHash', 'operationHash'):
            _hash(intent[key])
        _integer(intent['stockRevision'], 1); _integer(intent['serverRevision']); _date(intent['createdAt'])
        if (type(intent['store']) is not str or not re.fullmatch('[a-z0-9][a-z0-9-]{2,49}', intent['store'])
                or type(intent['stockReceiptId']) is not str or not re.fullmatch('R-[A-Za-z0-9-]{1,100}', intent['stockReceiptId'])
                or intent['kind'] not in ACTIONS or intent['requestKey'] != 'iss1_' + uuid.UUID(intent['intentId']).hex):
            _fail()
        _provider(intent['provider']); _profile(intent['labelProfile'])
        body = intent['body']; fields = {'request_key', 'revision', 'plan_hash'}
        if intent['kind'] == 'commit': fields |= {'confirmed', 'excluded_not_received_ack'}
        elif intent['kind'] == 'authorize': fields.add('resume_same_operation')
        elif intent['kind'] == 'cancel': fields.add('reason')
        if (type(body) is not dict or set(body) != fields or body['request_key'] != intent['requestKey']
                or type(body['revision']) is not int or body['revision'] != intent['serverRevision']
                or body['plan_hash'] != intent['planHash']):
            _fail()
        if intent['kind'] == 'commit' and (body['confirmed'] is not True or body['excluded_not_received_ack'] is not True): _fail()
        if intent['kind'] == 'authorize' and body['resume_same_operation'] is not True: _fail()
        if intent['kind'] == 'cancel':
            _text(body['reason'], 10, 200)
            if body['reason'] != body['reason'].strip() or any(unicodedata.category(char) == 'Cf' for char in body['reason']): _fail()
        if len(canonical(intent).encode('utf-8')) > MAX_INTENT_BYTES:
            _fail()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError):
        _fail()


def request_fingerprint(intent):
    validate_intent(intent)
    return _fingerprint({'domain': DOMAIN, 'intent': intent})


def prepare_intent(kind, *, intent_id, stock_receipt_id, stock_revision, tenant_id, store,
                   restoration_source, operation, proof_hash, created_at, reason=None):
    try:
        source = restoration_source
        if (type(source) is not dict or set(source) != SOURCE_FIELDS or type(source['version']) is not int
                or source['version'] != 1 or source['workflowKind'] != 'inventory_only'
                or source['acceptedTradeId'] is not None or source['acceptedReceiptHash'] is not None):
            _fail()
        validate_operation(operation)
        if len(canonical(source).encode('utf-8')) > 2000000:
            _fail()
        normalized = normalize_plan(source['normalizedPlan'])
        if canonical(normalized) != canonical(source['normalizedPlan']) or normalized['workflow_kind'] != 'inventory_only':
            _fail()
        admission = source['admission']
        if type(admission) is not dict or set(admission) != ADMISSION_FIELDS or any(type(value) is not bool for value in admission.values()):
            _fail()
        if source['restorationSourceHash'] != _fingerprint({'domain': SOURCE_DOMAIN, 'source': {key: value for key, value in source.items() if key != 'restorationSourceHash'}}):
            _fail()
        if source['stateBasisHash'] != _fingerprint({'domain': STATE_DOMAIN, 'operation': operation, 'admission': admission}):
            _fail()
        for left, right in (('receivingId', 'receiving_id'), ('serverRevision', 'revision'), ('planHash', 'plan_hash'),
                ('physicalIntakeId', 'physical_intake_id'), ('physicalManifestHash', 'physical_manifest_hash'),
                ('sourceSnapshotHash', 'source_snapshot_hash')):
            if type(source[left]) is not type(operation[right]) or source[left] != operation[right]:
                _fail()
        if normalized['location_id'] != operation['provider']['location_id'] or canonical(normalized['client_label_profile']) != canonical(operation['client_label_profile']):
            _fail()
        state = operation['state']
        if kind in ('commit', 'cancel'):
            eligible = state == 'ready' and not any(admission.values())
        elif kind == 'authorize':
            eligible = (state == 'catalog_confirmed' and admission['everAdmitted'] and admission['catalogStepPresent']
                and not admission['inventoryStepPresent'] and not admission['leaseActive']
                and operation['catalog_confirmed'] is True and operation['inventory_confirmed'] is False
                and any(row['action'] == 'create_price' for row in operation['targets']))
        elif kind == 'recover':
            eligible = (state in ('catalog_pending', 'inventory_pending', 'outcome_unknown', 'needs_review')
                and admission['everAdmitted'] and (admission['catalogStepPresent'] or admission['inventoryStepPresent']))
        else:
            _fail()
        if not eligible or kind != 'cancel' and reason is not None:
            _fail()
        key = 'iss1_' + uuid.UUID(_uuid(intent_id)).hex
        body = {'request_key': key, 'revision': source['serverRevision'], 'plan_hash': source['planHash']}
        if kind == 'commit': body.update(confirmed=True, excluded_not_received_ack=True)
        elif kind == 'authorize': body['resume_same_operation'] = True
        elif kind == 'cancel': body['reason'] = reason
        intent = {'version': 1, 'originKind': 'physical_intake', 'serviceOrigin': ORIGIN,
            'tenantId': tenant_id, 'store': store, 'intentId': intent_id, 'stockReceiptId': stock_receipt_id,
            'stockRevision': stock_revision, 'physicalIntakeId': source['physicalIntakeId'],
            'physicalManifestHash': source['physicalManifestHash'], 'sourceSnapshotHash': source['sourceSnapshotHash'],
            'receivingId': source['receivingId'], 'serverRevision': source['serverRevision'], 'planHash': source['planHash'],
            'restorationSourceHash': source['restorationSourceHash'], 'proofHash': proof_hash,
            'stateBasisHash': source['stateBasisHash'], 'operationHash': _fingerprint(operation),
            'provider': copy.deepcopy(operation['provider']), 'labelProfile': copy.deepcopy(operation['client_label_profile']),
            'kind': kind, 'requestKey': key, 'body': body, 'createdAt': created_at}
        validate_intent(intent)
        return intent
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError):
        _fail()
