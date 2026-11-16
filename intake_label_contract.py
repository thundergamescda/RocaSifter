"""Pure genuine-v2 label data and bounded plans; never proof or authority.

Projection callers must first verify the server capture or desktop B1 proof.
No desktop/cloud, renderer, database, filesystem or network is imported here.
Original codepoints, integer cents and IDs are retained without normalization.
"""
import copy
from functools import wraps
import hashlib
import json
import re
import unicodedata
import uuid

from intake_manifest import (normalize_identity, source_line_id, source_lines,
                             manifest_digest, semantic_fingerprint)
from catalog_physical_ownership import require_identity


SERVICE_ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_ROWS = 1000
MAX_TOTAL_COPIES = 1000000
MAX_ROW_COPIES = 100000
MAX_PART_COPIES = 1000
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_INTEGER = 2**63 - 1
RESERVED_BYTES = ROW_COMMAND_BYTES = 4096
STOCKS = {'2x1': (2, 1), '3x1': (3, 1), '4x2': (4, 2)}
PROVIDER_DOMAIN = 'stock-and-sleeve:intake-label-provider:v1'
ROWS_DOMAIN = 'stock-and-sleeve:intake-label-rows:v1'
ROW_DOMAIN = 'stock-and-sleeve:intake-label-row:v1'
TERMINAL_DOMAIN = 'stock-and-sleeve:intake-label-terminal:v1'
INPUT_DOMAIN = 'stock-and-sleeve:intake-label-input:v1'
PLAN_DOMAIN = 'stock-and-sleeve:intake-label-plan:v1'
PART_DOMAIN = 'stock-and-sleeve:intake-label-part:v1'
JOB_DOMAIN = 'stock-and-sleeve:intake-label-job:v1'
DOMAINS = frozenset((PROVIDER_DOMAIN, ROWS_DOMAIN, ROW_DOMAIN, TERMINAL_DOMAIN,
                     INPUT_DOMAIN, PLAN_DOMAIN, PART_DOMAIN, JOB_DOMAIN))
ROW_KEYS = frozenset(('reviewLineId', 'sourceLineKey', 'identityHash', 'quantity',
    'sellingCents', 'zeroPriceConfirmed', 'targetSku', 'game', 'printed'))
PRINT_LIMITS = {'name': 200, 'set': 200, 'number': 100, 'condition': 100, 'printing': 100}
INPUT_KEYS = frozenset(('version', 'originKind', 'serviceOrigin', 'tenantId', 'physicalIntakeId',
    'physicalManifestHash', 'sourceSnapshotHash', 'receivingId', 'serverRevision', 'planHash',
    'terminalReceiptHash', 'providerHash', 'labelProfile', 'confirmedRows',
    'confirmedRowsHash', 'terminalBasisHash'))
PLAN_KEYS = frozenset(('version', 'labelProfile', 'terminalBasisHash', 'confirmedRowsHash', 'rows',
    'totalCopies', 'copyOffset', 'copyCount', 'partSize', 'partCount', 'budget', 'renderPlanHash'))
SOURCE_KEYS = frozenset(('version', 'receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash',
    'physicalIntakeId', 'physicalManifestHash', 'workflowKind', 'acceptedTradeId',
    'acceptedReceiptHash', 'normalizedPlan', 'admission', 'stateBasisHash', 'restorationSourceHash'))
HEADER_KEYS = frozenset(('physical_intake_id', 'physical_manifest_hash', 'source_snapshot_hash',
    'client_label_profile', 'workflow_kind', 'accepted_trade_id', 'accepted_receipt_hash',
    'provider', 'selected_quantities', 'excluded_quantities'))
OPERATION_KEYS = HEADER_KEYS | frozenset(('receiving_id', 'revision', 'state', 'plan_hash', 'targets',
    'inventory_confirmed', 'receipt', 'blocked_reasons', 'requires_fresh_mfa', 'catalog_confirmed',
    'catalog_resolutions', 'catalog_proof', 'catalog_proof_hash', 'catalog_resolutions_hash'))
PROOF_KEYS = frozenset(('version', 'receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash',
    'restorationSourceHash', 'stateBasisHash', 'state', 'workflowKind', 'provider', 'labelProfile',
    'admission', 'quantityBudget', 'stockRows', 'terminalReceiptHash', 'received', 'labelsVerified',
    'labelError', 'proofHash'))
STOCK_ROW_KEYS = frozenset(('reviewLineId', 'acceptedLineId', 'sourceLineKey', 'sourceIdentity',
    'identity', 'identityHash', 'quantity', 'sellingCents', 'zeroPriceConfirmed', 'reviewed',
    'unitIntakeCents', 'sourceSku', 'targetSku', 'variationId', 'parentId', 'action'))
TARGET_KEYS = frozenset(('review_line_id', 'source_line_key', 'accepted_line_id', 'identity',
    'identity_hash', 'binding_id', 'binding_version', 'binding_evidence_hash', 'source_sku',
    'target_sku', 'variation_id', 'parent_id', 'selling_cents', 'currency', 'quantity', 'action',
    'binding_role', 'template_id', 'template_version', 'template_evidence_hash',
    'source_binding_id', 'source_binding_version', 'source_binding_evidence_hash', 'confirmed_quantity'))


class LabelContractError(ValueError):
    code = 'INTAKE_LABEL_INPUT_INVALID'


def _fail():
    raise LabelContractError('The original confirmed label data or bounded copy plan is invalid.')


def _safe(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LabelContractError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, UnicodeError, RecursionError):
            pass
        # Leave our handler before replacing its exception; retain no owned context.
        _fail()
    return invoke


def _keys(value, expected):
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != set(expected):
        _fail()


def _ownership_v2(value, key='physical_ownership_version'):
    if type(value) is not dict:
        _fail()
    if key not in value:
        return False
    if type(value[key]) is not int or value[key] != 2:
        _fail()
    return True


def _integer(value, low=0, high=MAX_INTEGER):
    if type(value) is not int or not low <= value <= high:
        _fail()
    return value


def _text(value, limit, *, required=False):
    if (type(value) is not str or len(value) > limit or required and not value
        or any(unicodedata.category(char) in ('Cc', 'Cs', 'Cf') for char in value)):
        _fail()
    return value


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        _fail()
    return value


def _hash(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        _fail()
    return value


def _charge_bytes(budget, count):
    if count > MAX_INPUT_BYTES - budget[1]:
        _fail()
    budget[1] += count


def _string_bytes(value, budget):
    if type(value) is not str:
        _fail()
    # Every codepoint needs at least one byte, plus the two JSON quotes.
    if len(value) + 2 > MAX_INPUT_BYTES - budget[1]:
        _fail()
    _charge_bytes(budget, 2)
    for char in value:
        if unicodedata.category(char) in ('Cc', 'Cs', 'Cf'):
            _fail()
        point = ord(char)
        size = 1 if point <= 0x7f else 2 if point <= 0x7ff else 3 if point <= 0xffff else 4
        _charge_bytes(budget, size + (1 if char in ('"', '\\') else 0))


def _walk(value, depth, budget):
    budget[0] += 1
    if depth > 64 or budget[0] > 100000:
        _fail()
    if value is None:
        _charge_bytes(budget, 4)
    elif type(value) is bool:
        _charge_bytes(budget, 4 if value else 5)
    elif type(value) is int:
        _integer(value, -MAX_INTEGER)
        _charge_bytes(budget, len(str(value)))
    elif type(value) is str:
        _string_bytes(value, budget)
    elif type(value) is list:
        _charge_bytes(budget, 2 + max(0, len(value) - 1))
        for item in value:
            _walk(item, depth + 1, budget)
    elif type(value) is dict:
        _charge_bytes(budget, 2 + max(0, len(value) - 1) + len(value))
        for key, item in value.items():
            _string_bytes(key, budget)
            _walk(item, depth + 1, budget)
    else:
        _fail()


@_safe
def canonical(value):
    _walk(value, 0, [0, 0])
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(raw.encode('utf-8')) > MAX_INPUT_BYTES:
        _fail()
    return raw


@_safe
def domain_hash(domain, value):
    if type(domain) is not str or domain not in DOMAINS:
        _fail()
    return hashlib.sha256(domain.encode('ascii') + b'\0' + canonical(value).encode('utf-8')).hexdigest()


def _same(left, right):
    return canonical(left) == canonical(right)


def _profile(value):
    _keys(value, ('stock', 'layout', 'dpi'))
    if (type(value['stock']) is not str or value['stock'] not in STOCKS
        or type(value['layout']) is not str or value['layout'] not in ('standard', 'fold', 'side')
        or type(value['dpi']) is not int or value['dpi'] not in (203, 300)):
        _fail()
    return dict(value)


def _provider(value):
    _keys(value, ('merchant_id', 'environment', 'application_id', 'location_id'))
    if value['environment'] not in ('production', 'sandbox'):
        _fail()
    for key in ('merchant_id', 'application_id', 'location_id'):
        _text(value[key], 255, required=True)
    return domain_hash(PROVIDER_DOMAIN, value)


def _sku(value):
    if (type(value) is not str or not 1 <= len(value) <= 100 or not value.strip()
        or any(not 32 <= ord(char) <= 126 for char in value)):
        _fail()
    return value


def _row(value):
    _keys(value, ROW_KEYS)
    _uuid(value['reviewLineId']); _hash(value['sourceLineKey']); _hash(value['identityHash'])
    _integer(value['quantity'], 1, MAX_ROW_COPIES); _integer(value['sellingCents'], 0, 100000000)
    if type(value['zeroPriceConfirmed']) is not bool or value['sellingCents'] == 0 and value['zeroPriceConfirmed'] is not True:
        _fail()
    _sku(value['targetSku']); _text(value['game'], 100, required=True)
    _keys(value['printed'], PRINT_LIMITS)
    for key, limit in PRINT_LIMITS.items():
        _text(value['printed'][key], limit, required=key == 'name')


def _terminal_body(value):
    return {key: item for key, item in value.items() if key not in ('confirmedRows', 'terminalBasisHash')}


@_safe
def validate_input(value):
    _keys(value, INPUT_KEYS)
    if type(value['version']) is not int or value['version'] != 1 or value['originKind'] != 'physical_intake' or value['serviceOrigin'] != SERVICE_ORIGIN:
        _fail()
    for key in ('tenantId', 'physicalIntakeId', 'receivingId'):
        _uuid(value[key])
    _integer(value['serverRevision'])
    for key in ('physicalManifestHash', 'sourceSnapshotHash', 'planHash', 'terminalReceiptHash',
                'providerHash', 'confirmedRowsHash', 'terminalBasisHash'):
        _hash(value[key])
    _profile(value['labelProfile'])
    rows = value['confirmedRows']
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS:
        _fail()
    previous, total = None, 0
    for row in rows:
        _row(row)
        if previous is not None and row['reviewLineId'] <= previous:
            _fail()
        previous = row['reviewLineId']; total += row['quantity']
    if total > MAX_TOTAL_COPIES or domain_hash(ROWS_DOMAIN, rows) != value['confirmedRowsHash']:
        _fail()
    if domain_hash(TERMINAL_DOMAIN, _terminal_body(value)) != value['terminalBasisHash']:
        _fail()
    canonical(value)
    return copy.deepcopy(value)


@_safe
def input_hash(value):
    return domain_hash(INPUT_DOMAIN, validate_input(value))


def _identity(value):
    normalized = normalize_identity(value)
    if not _same(value, normalized):
        _fail()
    return normalized


def _project_row(review, source, identity, quantity, cents, zero, sku):
    identity = _identity(identity)
    value = {'reviewLineId': review, 'sourceLineKey': source, 'identityHash': source_line_id(identity),
        'quantity': quantity, 'sellingCents': cents, 'zeroPriceConfirmed': zero, 'targetSku': sku,
        'game': identity['game'], 'printed': {key: identity.get(key, '') for key in PRINT_LIMITS}}
    _row(value)
    return value


def _made(*, service_origin, tenant_id, physical_id, manifest_hash, source_hash, receiving_id,
          revision, plan_hash, terminal_hash, provider, profile, rows):
    if service_origin != SERVICE_ORIGIN or type(service_origin) is not str:
        _fail()
    rows = sorted(rows, key=lambda row: row['reviewLineId'])
    value = {'version': 1, 'originKind': 'physical_intake', 'serviceOrigin': service_origin,
        'tenantId': tenant_id, 'physicalIntakeId': physical_id, 'physicalManifestHash': manifest_hash,
        'sourceSnapshotHash': source_hash, 'receivingId': receiving_id, 'serverRevision': revision,
        'planHash': plan_hash, 'terminalReceiptHash': terminal_hash, 'providerHash': _provider(provider),
        'labelProfile': _profile(profile), 'confirmedRows': rows, 'confirmedRowsHash': domain_hash(ROWS_DOMAIN, rows)}
    value['terminalBasisHash'] = domain_hash(TERMINAL_DOMAIN, _terminal_body(value))
    return validate_input(value)


def _admitted(value):
    _keys(value, ('everAdmitted', 'catalogStepPresent', 'inventoryStepPresent', 'leaseActive'))
    if (any(type(item) is not bool for item in value.values()) or value['everAdmitted'] is not True
        or value['inventoryStepPresent'] is not True or value['leaseActive'] is not False):
        _fail()


@_safe
def confirmed_input_from_proof(proof, *, service_origin, tenant_id, physical_intake_id, physical_manifest_hash):
    """Project only AFTER unchanged B1 verification; never verify its authority here."""
    ownership_v2 = _ownership_v2(proof, 'physicalOwnershipVersion')
    _keys(proof, PROOF_KEYS | ({'physicalOwnershipVersion'} if ownership_v2 else set()))
    if (type(proof['version']) is not int or proof['version'] != 1 or proof['workflowKind'] != 'inventory_only'
        or proof['state'] != 'received' or proof['received'] is not True):
        _fail()
    _admitted(proof['admission']); _hash(proof['proofHash']); _hash(proof['terminalReceiptHash'])
    if type(proof['stockRows']) is not list or not 1 <= len(proof['stockRows']) <= MAX_ROWS:
        _fail()
    rows = []
    for row in proof['stockRows']:
        _keys(row, STOCK_ROW_KEYS | ({'physicalOwnership'} if ownership_v2 else set()))
        if row['acceptedLineId'] is not None or row['unitIntakeCents'] is not None or row['reviewed'] is not True:
            _fail()
        if ownership_v2:
            require_identity(row['physicalOwnership'], row['identity'])
            if row['action'] != 'reuse_existing':
                _fail()
        if source_line_id(_identity(row['sourceIdentity'])) != row['sourceLineKey']:
            _fail()
        result = _project_row(row['reviewLineId'], row['sourceLineKey'], row['identity'],
            row['quantity'], row['sellingCents'], row['zeroPriceConfirmed'], row['targetSku'])
        if result['identityHash'] != row['identityHash']:
            _fail()
        rows.append(result)
    return _made(service_origin=service_origin, tenant_id=tenant_id, physical_id=physical_intake_id,
        manifest_hash=physical_manifest_hash, source_hash=proof['sourceSnapshotHash'], receiving_id=proof['receivingId'],
        revision=proof['serverRevision'], plan_hash=proof['planHash'], terminal_hash=proof['terminalReceiptHash'],
        provider=proof['provider'], profile=proof['labelProfile'], rows=rows)


def _index_targets(values, *, confirmed, ownership_v2=False):
    if type(values) is not list or not 1 <= len(values) <= MAX_ROWS:
        _fail()
    result = {}
    for value in values:
        if (type(value) is not dict
            or set(value) - (TARGET_KEYS | ({'physical_ownership'} if ownership_v2 else set()))
            or ownership_v2 and 'physical_ownership' not in value):
            _fail()
        ident = _uuid(value['review_line_id'])
        if (ident in result or value.get('accepted_line_id') is not None
            or value.get('action') not in ('reuse_existing', 'create_price')
            or ownership_v2 and value['action'] != 'reuse_existing'):
            _fail()
        if confirmed and (type(value['confirmed_quantity']) is not int
                          or value['confirmed_quantity'] != value['quantity']):
            _fail()
        result[ident] = value
    return result


@_safe
def confirmed_input(capture, *, service_origin, tenant_id):
    """Narrow already-verified server capture; expectedSource stays opaque.

    The hosted context adapter must bind actor/ExpectedLabelSource tenant first.
    No structural data type is proof that this precondition actually occurred.
    """
    _keys(capture, ('restorationSource', 'operation', 'claim', 'expectedSource'))
    source, operation, claim = (capture[key] for key in ('restorationSource', 'operation', 'claim'))
    ownership_v2 = _ownership_v2(operation)
    _keys(source, SOURCE_KEYS)
    _keys(operation, OPERATION_KEYS | ({'physical_ownership_version'} if ownership_v2 else set()))
    if (type(source['version']) is not int or source['version'] != 1
        or source['workflowKind'] != 'inventory_only' or operation['workflow_kind'] != 'inventory_only'
        or source['acceptedTradeId'] is not None or source['acceptedReceiptHash'] is not None
        or operation['accepted_trade_id'] is not None or operation['accepted_receipt_hash'] is not None
        or operation['state'] != 'received' or operation['inventory_confirmed'] is not True):
        _fail()
    _admitted(source['admission'])
    for source_key, operation_key in (('receivingId', 'receiving_id'), ('serverRevision', 'revision'),
            ('planHash', 'plan_hash'), ('sourceSnapshotHash', 'source_snapshot_hash'),
            ('physicalIntakeId', 'physical_intake_id'), ('physicalManifestHash', 'physical_manifest_hash')):
        if not _same(source[source_key], operation[operation_key]):
            _fail()
    if (type(claim) is not dict or claim.get('state') != 'open' or claim.get('bound_trade_id') is not None
        or claim.get('physical_intake_id') != source['physicalIntakeId']
        or claim.get('manifest_hash') != source['physicalManifestHash']
        or manifest_digest(claim['source_manifest']) != source['physicalManifestHash']
        or semantic_fingerprint(claim['source_manifest']) != claim['semantic_hash']):
        _fail()
    budget = {row['source_line_key']: row['quantity'] for row in source_lines(claim['source_manifest'])}
    plan = source['normalizedPlan']
    if _ownership_v2(plan) != ownership_v2:
        _fail()
    _keys(plan, ('physical_intake_id', 'physical_manifest_hash', 'source_snapshot_hash', 'workflow_kind',
        'location_id', 'new_inventory_confirmed', 'excluded_not_received_ack', 'external_ownership_confirmed',
        'client_label_profile', 'lines') + (('physical_ownership_version',) if ownership_v2 else ()))
    if (plan['workflow_kind'] != 'inventory_only' or plan['new_inventory_confirmed'] is not True
        or plan['excluded_not_received_ack'] is not True or plan['external_ownership_confirmed'] is not True
        or plan['physical_intake_id'] != source['physicalIntakeId']
        or plan['physical_manifest_hash'] != source['physicalManifestHash']
        or plan['source_snapshot_hash'] != source['sourceSnapshotHash']
        or plan['location_id'] != operation['provider']['location_id']
        or not _same(plan['client_label_profile'], operation['client_label_profile'])):
        _fail()
    receipt = operation['receipt']
    if (type(receipt) is not dict or receipt.get('workflow_kind') != 'inventory_only'
        or receipt.get('accepted_trade_id') is not None or receipt.get('accepted_receipt_hash') is not None
        or receipt.get('inventory_confirmed') is not True or type(receipt.get('receipt_schema_version')) is not int
        or receipt['receipt_schema_version'] != 1):
        _fail()
    if type(receipt.get('catalog_write_performed')) is not bool:
        _fail()
    if _ownership_v2(receipt) != ownership_v2:
        _fail()
    header_keys = HEADER_KEYS | ({'physical_ownership_version'} if ownership_v2 else set())
    receipt_keys = header_keys | {'receipt_schema_version', 'receiving_id', 'revision', 'plan_hash',
        'confirmed_targets', 'inventory_confirmed', 'catalog_write_performed', 'received_at',
        'provider_proof_hash', 'receipt_hash'}
    if receipt['catalog_write_performed']:
        receipt_keys |= {'catalog_proof_hash', 'catalog_resolutions_hash', 'catalog_resolutions'}
    _keys(receipt, receipt_keys)
    _hash(receipt['provider_proof_hash'])
    for key in header_keys | {'receiving_id', 'revision', 'plan_hash'}:
        if not _same(receipt[key], operation[key]):
            _fail()
    terminal_hash = _hash(receipt['receipt_hash'])
    if hashlib.sha256(canonical({key: value for key, value in receipt.items() if key != 'receipt_hash'}).encode('utf-8')).hexdigest() != terminal_hash:
        _fail()
    frozen = _index_targets(operation['targets'], confirmed=False, ownership_v2=ownership_v2)
    targets = _index_targets(receipt['confirmed_targets'], confirmed=True, ownership_v2=ownership_v2)
    if type(plan['lines']) is not list or not 1 <= len(plan['lines']) <= MAX_ROWS:
        _fail()
    rows, used, seen = [], {}, set()
    for line in plan['lines']:
        _keys(line, ('review_line_id', 'source_line_key', 'identity', 'quantity', 'selling_cents', 'zero_price_confirmed', 'reviewed')
              + (('physical_ownership',) if ownership_v2 else ()))
        ident = _uuid(line['review_line_id'])
        if ident in seen or line['reviewed'] is not True:
            _fail()
        seen.add(ident)
        result = _project_row(ident, line['source_line_key'], line['identity'], line['quantity'],
                              line['selling_cents'], line['zero_price_confirmed'], targets[ident]['target_sku'])
        ownership = require_identity(line['physical_ownership'], line['identity']) if ownership_v2 else None
        for target in (frozen[ident], targets[ident]):
            if ownership_v2 and require_identity(target['physical_ownership'], line['identity']) != ownership:
                _fail()
            if (target['source_line_key'] != result['sourceLineKey'] or target['identity_hash'] != result['identityHash']
                or type(target['quantity']) is not int or target['quantity'] != result['quantity']
                or type(target['selling_cents']) is not int or target['selling_cents'] != result['sellingCents']
                or target['target_sku'] != result['targetSku'] or target.get('currency') != 'USD'):
                _fail()
        key = result['sourceLineKey']; used[key] = used.get(key, 0) + result['quantity']
        if key not in budget or used[key] > budget[key]:
            _fail()
        rows.append(result)
    if set(frozen) != seen or set(targets) != seen or not _same(operation['selected_quantities'], used):
        _fail()
    if not _same(operation['excluded_quantities'], {key: count - used.get(key, 0) for key, count in budget.items()}):
        _fail()
    return _made(service_origin=service_origin, tenant_id=tenant_id, physical_id=source['physicalIntakeId'],
        manifest_hash=source['physicalManifestHash'], source_hash=source['sourceSnapshotHash'], receiving_id=source['receivingId'],
        revision=source['serverRevision'], plan_hash=source['planHash'], terminal_hash=terminal_hash,
        provider=operation['provider'], profile=plan['client_label_profile'], rows=rows)


def _budget(profile):
    chosen = _profile(profile); width, height = STOCKS[chosen['stock']]; dpi = chosen['dpi']
    row_bytes = ((width * dpi + 7) // 8) * height * dpi * 2 + ROW_COMMAND_BYTES
    return {'maxCopiesPerPart': MAX_PART_COPIES, 'maxArtifactBytes': MAX_ARTIFACT_BYTES,
            'maxZplRowBytes': row_bytes, 'reservedBytes': RESERVED_BYTES}


def _plan_body(value):
    return {key: item for key, item in value.items() if key != 'renderPlanHash'}


@_safe
def build_plan(value, copy_offset=0, copy_count=None):
    value = validate_input(value)
    total = sum(row['quantity'] for row in value['confirmedRows'])
    offset = _integer(copy_offset, 0, total - 1)
    count = total - offset if copy_count is None else _integer(copy_count, 1, total - offset)
    budget = _budget(value['labelProfile'])
    size = min(MAX_PART_COPIES, (MAX_ARTIFACT_BYTES - RESERVED_BYTES) // budget['maxZplRowBytes'])
    if size < 1:
        _fail()
    plan = {'version': 1, 'labelProfile': value['labelProfile'], 'terminalBasisHash': value['terminalBasisHash'],
        'confirmedRowsHash': value['confirmedRowsHash'], 'rows': [{'reviewLineId': row['reviewLineId'],
            'quantity': row['quantity'], 'rowHash': domain_hash(ROW_DOMAIN, row)} for row in value['confirmedRows']],
        'totalCopies': total, 'copyOffset': offset, 'copyCount': count,
        'partSize': size, 'partCount': (count + size - 1) // size, 'budget': budget}
    plan['renderPlanHash'] = domain_hash(PLAN_DOMAIN, _plan_body(plan))
    return validate_plan(plan)


@_safe
def validate_plan(value):
    _keys(value, PLAN_KEYS)
    if type(value['version']) is not int or value['version'] != 1:
        _fail()
    _profile(value['labelProfile']); _hash(value['terminalBasisHash']); _hash(value['confirmedRowsHash'])
    _hash(value['renderPlanHash'])
    rows = value['rows']
    if type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS:
        _fail()
    total, previous = 0, None
    for row in rows:
        _keys(row, ('reviewLineId', 'quantity', 'rowHash')); ident = _uuid(row['reviewLineId']); _hash(row['rowHash'])
        total += _integer(row['quantity'], 1, MAX_ROW_COPIES)
        if previous is not None and ident <= previous:
            _fail()
        previous = ident
    if total > MAX_TOTAL_COPIES or _integer(value['totalCopies'], 1, MAX_TOTAL_COPIES) != total:
        _fail()
    offset = _integer(value['copyOffset'], 0, total - 1)
    count = _integer(value['copyCount'], 1, total - offset)
    budget = _budget(value['labelProfile'])
    if not _same(value['budget'], budget):
        _fail()
    size = min(MAX_PART_COPIES, (MAX_ARTIFACT_BYTES - RESERVED_BYTES) // budget['maxZplRowBytes'])
    if _integer(value['partSize'], 1, MAX_PART_COPIES) != size or _integer(value['partCount'], 1) != (count + size - 1) // size:
        _fail()
    if domain_hash(PLAN_DOMAIN, _plan_body(value)) != value['renderPlanHash']:
        _fail()
    return copy.deepcopy(value)


@_safe
def part(plan, index):
    value = validate_plan(plan); index = _integer(index, 0, value['partCount'] - 1)
    start = value['copyOffset'] + index * value['partSize']
    count = min(value['partSize'], value['copyCount'] - index * value['partSize'])
    cursor, counts = 0, {}
    for row in value['rows']:
        upper = cursor + row['quantity']; selected = max(0, min(upper, start + count) - max(cursor, start))
        if selected:
            counts[row['reviewLineId']] = selected
        cursor = upper
    if sum(counts.values()) != count:
        _fail()
    result = {'version': 1, 'renderPlanHash': value['renderPlanHash'], 'partIndex': index,
        'copyOffset': start, 'copyCount': count, 'renderCounts': counts}
    result['partHash'] = domain_hash(PART_DOMAIN, result)
    return result


@_safe
def validate_render_counts(value, render_counts):
    value = validate_input(value)
    if type(render_counts) is not dict or not render_counts:
        _fail()
    quantities = {row['reviewLineId']: row['quantity'] for row in value['confirmedRows']}; counts = {}
    for ident, quantity in render_counts.items():
        _uuid(ident)
        if ident not in quantities:
            _fail()
        counts[ident] = _integer(quantity, 1, quantities[ident])
    if sum(counts.values()) > MAX_PART_COPIES:
        _fail()
    if sum(counts.values()) * _budget(value['labelProfile'])['maxZplRowBytes'] + RESERVED_BYTES > MAX_ARTIFACT_BYTES:
        _fail()
    return dict(sorted(counts.items()))


@_safe
def job_basis_hash(value, plan, renderer_build_hash, *, purpose='initial', reprint_of_job_id=None):
    value = validate_input(value); plan = validate_plan(plan); _hash(renderer_build_hash)
    if not _same(plan, build_plan(value, plan['copyOffset'], plan['copyCount'])):
        _fail()
    if purpose == 'initial':
        if reprint_of_job_id is not None:
            _fail()
    elif purpose == 'range_reprint':
        _uuid(reprint_of_job_id)
    else:
        _fail()
    return domain_hash(JOB_DOMAIN, {'terminalBasisHash': value['terminalBasisHash'],
        'renderPlanHash': plan['renderPlanHash'], 'rendererBuildHash': renderer_build_hash,
        'purpose': purpose, 'reprintOfJobId': reprint_of_job_id})
