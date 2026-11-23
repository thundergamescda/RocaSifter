"""Pure original receiving-plan proof and stock rows; never action authority.

Inputs must come from the caller's coherent authenticated, tenant-bound reads.
This verifier has no I/O, clock, journal, registry or service dependency. Original
plan/source/financial hashes are retained, not reconstructed as new requests.
"""
import copy
from datetime import datetime
import json
import re
import unicodedata
import uuid

from hosted_labels import fingerprint, profile as label_profile, readiness as label_readiness
from intake_manifest import validate_manifest, manifest_digest, semantic_fingerprint, source_lines, source_line_id, ManifestError
from catalog_physical_ownership import require_identity
from hosted.receiving_plan import normalize_plan, public_response_bytes
from hosted.security import ServiceError
from receiving_evidence import catalog_evidence, final_targets, provider_id
from square_variations import variant_sku
from station_cloud import CloudError
from station_receiving_budget import verified_receiving_budget, quantity_budget

STATE_DOMAIN = 'stock-and-sleeve:receiving-restoration-state:v1'
SOURCE_DOMAIN = 'stock-and-sleeve:receiving-restoration-source:v1'
PROOF_DOMAIN = 'stock-and-sleeve:receiving-restoration-proof:v1'
SOURCE_BYTES = 2_000_000
HASH = re.compile('[a-f0-9]{64}')
STATES = {'ready', 'catalog_pending', 'catalog_confirmed', 'inventory_pending',
          'outcome_unknown', 'needs_review', 'received', 'cancelled_before_write'}
SOURCE_FIELDS = {'version', 'receivingId', 'serverRevision', 'planHash', 'sourceSnapshotHash',
                 'physicalIntakeId', 'physicalManifestHash', 'workflowKind', 'acceptedTradeId',
                 'acceptedReceiptHash', 'normalizedPlan', 'admission', 'stateBasisHash', 'restorationSourceHash'}
ADMISSION_FIELDS = {'everAdmitted', 'catalogStepPresent', 'inventoryStepPresent', 'leaseActive'}
HEADER_FIELDS = ('physical_intake_id', 'physical_manifest_hash', 'source_snapshot_hash',
                 'client_label_profile', 'workflow_kind', 'accepted_trade_id', 'accepted_receipt_hash',
                 'provider', 'selected_quantities', 'excluded_quantities')
OPERATION_FIELDS = set(HEADER_FIELDS) | {'receiving_id', 'revision', 'state', 'plan_hash', 'targets',
    'inventory_confirmed', 'receipt', 'blocked_reasons', 'requires_fresh_mfa', 'catalog_confirmed',
    'catalog_resolutions', 'catalog_proof', 'catalog_proof_hash', 'catalog_resolutions_hash'}
TARGET_FIELDS = {'review_line_id', 'source_line_key', 'accepted_line_id', 'identity_hash', 'binding_id',
    'binding_version', 'binding_evidence_hash', 'source_sku', 'target_sku', 'variation_id', 'parent_id',
    'selling_cents', 'currency', 'quantity', 'action', 'binding_role', 'template_id', 'template_version',
    'template_evidence_hash'}
TARGET_REQUIRED = TARGET_FIELDS - {'accepted_line_id', 'binding_role', 'template_id', 'template_version', 'template_evidence_hash'}


class RestorationProofError(ValueError):
    """Original proof cannot safely become a local stock projection."""
    code = 'ORIGINAL_RECEIVING_PROOF_INVALID'


def _invalid():
    raise RestorationProofError('The exact original receiving plan, source or stock proof could not be verified. Keep its original reference; no stock or financial action is authorized.')


def restoration_state_hash(operation, admission):
    return fingerprint({'domain': STATE_DOMAIN, 'operation': operation, 'admission': admission})


def restoration_source_hash(source):
    return fingerprint({'domain': SOURCE_DOMAIN, 'source': {key: value for key, value in source.items() if key != 'restorationSourceHash'}})


def _integer(value, maximum=2**63-1, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError()
    return value


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError()
    return value


def _hash(value):
    if type(value) is not str or not HASH.fullmatch(value):
        raise ValueError()
    return value


def _same(left, right):
    # Canonical JSON also distinguishes True/1/1.0, unlike Python equality.
    return fingerprint(left) == fingerprint(right)


def _ownership_v2(value):
    if 'physical_ownership_version' not in value:
        return False
    if type(value['physical_ownership_version']) is not int or value['physical_ownership_version'] != 2:
        raise ValueError()
    return True


def _sku(value):
    if type(value) is not str or not 1 <= len(value) <= 100 or any(unicodedata.category(char) in ('Cc', 'Cs') for char in value):
        raise ValueError()
    return value


def _date(value):
    if type(value) is not str or len(value) > 60 or datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError()


def _original_claim(claim, source, operation):
    if type(claim) is not dict:
        raise ValueError()
    _integer(claim.get('version'))
    ident = _uuid(claim.get('physical_intake_id'))
    manifest = validate_manifest(claim.get('source_manifest'))
    digest = manifest_digest(manifest); semantic = semantic_fingerprint(manifest)
    if (ident != source['physicalIntakeId'] or digest != source['physicalManifestHash']
        or claim.get('manifest_hash') != digest or claim.get('semantic_hash') != semantic):
        raise ValueError()
    policy = claim.get('policy')
    expected_policy = {'one_operation_per_claim': True, 'new_acceptance_requires_fresh_mfa': True,
                       'excluded_rows_reusable': False, 'physical_identity_proven_by_hash': False}
    if not _same(policy, expected_policy):
        raise ValueError()
    state = claim.get('state'); bound = claim.get('bound_trade_id')
    if bound is not None:
        _uuid(bound)
    if source['workflowKind'] == 'accepted_trade':
        if state != 'consumed' or bound != source['acceptedTradeId']:
            raise ValueError()
    else:
        historical = operation['state'] == 'cancelled_before_write' and source['admission']['everAdmitted'] is False
        if not ((state == 'open' and bound is None) or (historical and state == 'consumed' and bound is not None)):
            raise ValueError()
    # These are pure verification inputs, not a new locally saved claim intent.
    local = {'state': 'confirmed', 'physical_intake_id': ident, 'manifest_hash': digest,
             'source_manifest_hash': digest, 'semantic_hash': semantic, 'bound_trade_id': bound}
    return manifest, {'source_manifest': manifest, 'intake_claim': local}


def _targets(plan, operation, accepted):
    ownership_v2 = _ownership_v2(plan)
    allowed = TARGET_FIELDS | ({'physical_ownership'} if ownership_v2 else set())
    required = TARGET_REQUIRED | ({'physical_ownership'} if ownership_v2 else set())
    targets = operation.get('targets')
    if type(targets) is not list or len(targets) != len(plan['lines']):
        raise ValueError()
    lines = {row['review_line_id']: row for row in plan['lines']}; result = {}; identities = {}
    for target in targets:
        if type(target) is not dict or set(target) - allowed or not required <= set(target):
            raise ValueError()
        ident = _uuid(target['review_line_id']); row = lines.get(ident)
        if row is None or ident in result:
            raise ValueError()
        if not _same({key: target.get(key) for key in ('source_line_key', 'quantity', 'selling_cents')},
                     {key: row[key] for key in ('source_line_key', 'quantity', 'selling_cents')}):
            raise ValueError()
        _hash(target['source_line_key']); _integer(target['quantity'], 100000, 1)
        _integer(target['selling_cents'], 100_000_000)
        identity = accepted[row['accepted_line_id']]['identity'] if plan['workflow_kind'] == 'accepted_trade' else row['identity']
        if ownership_v2:
            if require_identity(target['physical_ownership'], identity) != require_identity(row['physical_ownership'], identity):
                raise ValueError()
        if (target.get('accepted_line_id') != row.get('accepted_line_id')
            or target['identity_hash'] != source_line_id(identity) or target['currency'] != 'USD'):
            raise ValueError()
        _hash(target['identity_hash']); _uuid(target['binding_id']); _integer(target['binding_version'])
        _hash(target['binding_evidence_hash']); _sku(target['source_sku']); _sku(target['target_sku'])
        provider_id(target['parent_id'])
        if target['action'] == 'reuse_existing':
            provider_id(target['variation_id'])
            if (target['variation_id'].startswith('#') or target['source_sku'] != target['target_sku']
                or any(key in target for key in ('binding_role', 'template_id', 'template_version', 'template_evidence_hash'))):
                raise ValueError()
        elif target['action'] == 'create_price':
            if (ownership_v2 or target['variation_id'] is not None or target.get('binding_role') != 'source_family'
                or target['target_sku'] != variant_sku(target['source_sku'], target['selling_cents'])):
                raise ValueError()
            _uuid(target.get('template_id')); _integer(target.get('template_version')); _hash(target.get('template_evidence_hash'))
        else:
            raise ValueError()
        result[ident] = target; identities[ident] = identity
    if len({target['variation_id'] or (target['parent_id'], target['target_sku']) for target in targets}) > 100:
        raise ValueError()
    return result, identities


def _admission(source, operation, creating):
    admission = source['admission']
    if type(admission) is not dict or set(admission) != ADMISSION_FIELDS or any(type(value) is not bool for value in admission.values()):
        raise ValueError()
    ever, catalog, inventory, lease = (admission[key] for key in ('everAdmitted', 'catalogStepPresent', 'inventoryStepPresent', 'leaseActive'))
    state = operation['state']
    if (ever != (catalog or inventory) or lease and not ever
        or state in ('ready', 'cancelled_before_write') and (ever or lease)
        or state in ('catalog_pending', 'catalog_confirmed') and not catalog
        or state in ('inventory_pending', 'received') and not inventory
        or state in ('outcome_unknown', 'needs_review') and not ever
        or catalog and not creating
        or operation['catalog_confirmed'] and not catalog
        or operation['inventory_confirmed'] and not inventory
        or inventory and creating and operation['catalog_confirmed'] is not True):
        raise ValueError()


def _terminal(operation, targets):
    proof = operation.get('receipt')
    if operation['state'] != 'received':
        if proof is not None:
            raise ValueError()
        return None, targets
    if operation['inventory_confirmed'] is not True or type(proof) is not dict:
        raise ValueError()
    ownership_v2 = _ownership_v2(operation)
    if _ownership_v2(proof) != ownership_v2:
        raise ValueError()
    hashed = _hash(proof.get('receipt_hash'))
    if fingerprint({key: value for key, value in proof.items() if key != 'receipt_hash'}) != hashed:
        raise ValueError()
    creating = any(target['action'] == 'create_price' for target in targets)
    header_fields = HEADER_FIELDS + (('physical_ownership_version',) if ownership_v2 else ())
    expected_keys = set(header_fields) | {'receipt_schema_version', 'receiving_id', 'revision', 'plan_hash',
        'confirmed_targets', 'inventory_confirmed', 'catalog_write_performed', 'received_at', 'provider_proof_hash', 'receipt_hash'}
    if creating:
        expected_keys |= {'catalog_proof_hash', 'catalog_resolutions_hash', 'catalog_resolutions'}
    if (set(proof) != expected_keys or type(proof.get('receipt_schema_version')) is not int
        or proof['receipt_schema_version'] != 1 or proof.get('inventory_confirmed') is not True
        or proof.get('catalog_write_performed') is not creating):
        raise ValueError()
    same_fields = header_fields + ('receiving_id', 'revision', 'plan_hash')
    if not _same({key: proof.get(key) for key in same_fields}, {key: operation.get(key) for key in same_fields}):
        raise ValueError()
    catalog = catalog_evidence(operation, targets)
    if creating and not _same({key: proof.get(key) for key in ('catalog_proof_hash', 'catalog_resolutions_hash', 'catalog_resolutions')},
                             {key: catalog[key] for key in ('catalog_proof_hash', 'catalog_resolutions_hash', 'catalog_resolutions')}):
        raise ValueError()
    resolved = final_targets(operation, targets)
    expected = [dict(target, confirmed_quantity=target['quantity']) for target in resolved]
    if not _same(proof.get('confirmed_targets'), expected):
        raise ValueError()
    _hash(proof.get('provider_proof_hash')); _date(proof.get('received_at'))
    return hashed, resolved


def verify_restoration_source(source, operation, claim, accepted_receipt=None):
    """Return verified immutable facts/selected stock rows; grants no action.

    All rows use original review UUIDs, never current CSV positions or financial
    row UUID substitutions. Financial identities and source identities remain
    separate, including original Unicode spelling and corrected intake names.
    """
    try:
        if (type(source) is not dict or set(source) != SOURCE_FIELDS
            or type(source.get('version')) is not int or source['version'] != 1
            or len(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')) > SOURCE_BYTES
            or type(operation) is not dict):
            raise ValueError()
        ownership_v2 = _ownership_v2(operation)
        if set(operation) != OPERATION_FIELDS | ({'physical_ownership_version'} if ownership_v2 else set()):
            raise ValueError()
        public_response_bytes(operation)
        _uuid(source['receivingId']); _integer(source['serverRevision'])
        for key in ('planHash', 'sourceSnapshotHash', 'physicalManifestHash', 'stateBasisHash', 'restorationSourceHash'):
            _hash(source[key])
        _uuid(source['physicalIntakeId'])
        if source['workflowKind'] not in ('accepted_trade', 'inventory_only'):
            raise ValueError()
        if source['workflowKind'] == 'accepted_trade':
            _uuid(source['acceptedTradeId']); _hash(source['acceptedReceiptHash'])
        elif source['acceptedTradeId'] is not None or source['acceptedReceiptHash'] is not None:
            raise ValueError()
        if source['restorationSourceHash'] != restoration_source_hash(source):
            raise ValueError()
        if (operation['state'] not in STATES or type(operation['revision']) is not int
            or operation['receiving_id'] != source['receivingId'] or operation['revision'] != source['serverRevision']
            or operation['plan_hash'] != source['planHash'] or type(operation['inventory_confirmed']) is not bool
            or type(operation['catalog_confirmed']) is not bool or operation['requires_fresh_mfa'] is not True
            or type(operation['blocked_reasons']) is not list or len(operation['blocked_reasons']) > 10
            or any(type(reason) is not str or len(reason) > 100 or any(ord(char) < 32 for char in reason) for reason in operation['blocked_reasons'])):
            raise ValueError()
        if source['stateBasisHash'] != restoration_state_hash(operation, source['admission']):
            raise ValueError()
        plan = normalize_plan(source['normalizedPlan'])
        if not _same(plan, source['normalizedPlan']):
            raise ValueError()
        if _ownership_v2(plan) != ownership_v2:
            raise ValueError()
        links = {'physicalIntakeId': 'physical_intake_id', 'physicalManifestHash': 'physical_manifest_hash',
                 'sourceSnapshotHash': 'source_snapshot_hash', 'workflowKind': 'workflow_kind',
                 'acceptedTradeId': 'accepted_trade_id', 'acceptedReceiptHash': 'accepted_receipt_hash'}
        if any(source[left] != plan.get(right) or operation.get(right) != plan.get(right) for left, right in links.items()):
            raise ValueError()
        chosen = label_profile(plan['client_label_profile'])
        if not _same(chosen, operation['client_label_profile']):
            raise ValueError()
        provider = operation['provider']
        if (type(provider) is not dict or set(provider) != {'merchant_id', 'environment', 'application_id', 'location_id'}
            or provider['environment'] not in ('production', 'sandbox') or provider['location_id'] != plan['location_id']):
            raise ValueError()
        for key in ('merchant_id', 'application_id', 'location_id'):
            provider_id(provider[key])
        manifest, receipt = _original_claim(claim, source, operation)
        budget = verified_receiving_budget(receipt, plan, operation, accepted_receipt)
        accepted = {row['id']: row for row in accepted_receipt['lines']} if plan['workflow_kind'] == 'accepted_trade' else {}
        targets, identities = _targets(plan, operation, accepted)
        catalog_evidence(operation, operation['targets'])
        creating = any(target['action'] == 'create_price' for target in operation['targets'])
        _admission(source, operation, creating)
        terminal_hash, resolved = _terminal(operation, operation['targets'])
        resolved_by_id = {target['review_line_id']: target for target in resolved}
        original = {row['source_line_key']: row['identity'] for row in source_lines(manifest)}
        rows = []
        for line in plan['lines']:
            review = line['review_line_id']; target = resolved_by_id[review]
            rows.append({'reviewLineId': review, 'acceptedLineId': line.get('accepted_line_id'),
                'sourceLineKey': line['source_line_key'], 'sourceIdentity': copy.deepcopy(original[line['source_line_key']]),
                'identity': copy.deepcopy(identities[review]), 'identityHash': target['identity_hash'],
                'quantity': line['quantity'], 'sellingCents': line['selling_cents'],
                'zeroPriceConfirmed': line['zero_price_confirmed'], 'reviewed': line['reviewed'],
                'unitIntakeCents': accepted[line['accepted_line_id']]['unit_intake_cents'] if accepted else None,
                'sourceSku': target['source_sku'], 'targetSku': target['target_sku'],
                'variationId': target['variation_id'], 'parentId': target['parent_id'], 'action': target['action']})
            if ownership_v2:
                rows[-1]['physicalOwnership'] = copy.deepcopy(target['physical_ownership'])
        labels_verified = False; label_error = None
        if terminal_hash is not None:
            try:
                label_readiness(resolved, chosen)
                labels_verified = True
            except ValueError as error:
                # Geometry/encoding alone cannot erase verified stock truth.
                # Malformed terminal/catalog/source proof never reaches here.
                label_error = str(error)[:500]
        result = {'version': 1, 'receivingId': source['receivingId'], 'serverRevision': source['serverRevision'],
            'planHash': source['planHash'], 'sourceSnapshotHash': source['sourceSnapshotHash'],
            'restorationSourceHash': source['restorationSourceHash'], 'stateBasisHash': source['stateBasisHash'],
            'state': operation['state'], 'workflowKind': plan['workflow_kind'], 'provider': copy.deepcopy(provider),
            'labelProfile': chosen, 'admission': copy.deepcopy(source['admission']),
            'quantityBudget': quantity_budget(budget), 'stockRows': rows,
            'terminalReceiptHash': terminal_hash, 'received': terminal_hash is not None,
            'labelsVerified': labels_verified, 'labelError': label_error}
        if ownership_v2:
            result['physicalOwnershipVersion'] = 2
        result['proofHash'] = fingerprint({'domain': PROOF_DOMAIN, 'proof': result})
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, ManifestError, ServiceError, CloudError):
        _invalid()
