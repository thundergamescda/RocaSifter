"""Pure original/accepted/selected receiving budgets. No I/O or authority grant."""
from dataclasses import dataclass
from datetime import datetime
import re
import uuid

from hosted_labels import fingerprint
from intake_manifest import validate_manifest, manifest_digest, semantic_fingerprint, source_lines, normalize_identity, source_line_id, ManifestError
from station_cloud import CloudError

HASH = re.compile(r'[a-f0-9]{64}')


def _fail():
    raise CloudError('The original accepted quantities or receiving exclusions cannot be verified. Keep this same operation, source and saved request for owner recovery; no quantities or keys were replaced.', status=502, pending=True)


def _integer(value, maximum=1_000_000, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError()
    return value


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError()
    return value


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError()
    return value


def _map(value):
    if not isinstance(value, dict):
        raise ValueError()
    return {_hash(key): _integer(quantity) for key, quantity in value.items()}


def _claim_binding(claim, body):
    # Optional cached metadata may be older/absent, but a contradiction must
    # fail before either full financial evidence or a warm goods-only proof.
    # Historical inventory-only plans retain their own physical budget even
    # when a later agreement has consumed the same claim.
    if body.get('workflow_kind') == 'accepted_trade' and (
        claim.get('bound_trade_id') is not None and claim['bound_trade_id'] != body.get('accepted_trade_id')
        or claim.get('consumed_trade_receipt_hash') is not None and claim['consumed_trade_receipt_hash'] != body.get('accepted_receipt_hash')):
        raise ValueError()


def _original(receipt, body):
    manifest = validate_manifest(receipt.get('source_manifest')); claim = receipt.get('intake_claim')
    if (not isinstance(claim, dict) or claim.get('state') != 'confirmed'
        or _uuid(body.get('physical_intake_id')) != claim.get('physical_intake_id')
        or _hash(body.get('physical_manifest_hash')) != claim.get('manifest_hash')
        or claim.get('source_manifest_hash') != manifest_digest(manifest)
        or claim.get('semantic_hash') != semantic_fingerprint(manifest)):
        raise ValueError()
    _claim_binding(claim, body)
    original = {row['source_line_key']: row['quantity'] for row in source_lines(manifest)}
    return original, claim['semantic_hash']


@dataclass(frozen=True)
class AcceptedBudgetProof:
    """Minimal immutable goods proof; contains no customer name/contact/token."""
    trade_id: str
    receipt_hash: str
    claim_id: str
    manifest_hash: str
    semantic_hash: str
    original: tuple
    # accepted UUID, original source key, reviewed identity hash, quantity
    lines: tuple


def accepted_budget_proof(receipt, body, evidence):
    """Verify a complete original financial receipt before discarding its PII."""
    from station_shared_trade import fingerprint as financial_fingerprint
    try:
        original, semantic = _original(receipt, body)
        if (body.get('workflow_kind') != 'accepted_trade' or not isinstance(evidence, dict)
            or financial_fingerprint(evidence) != _hash(body.get('accepted_receipt_hash'))
            or evidence.get('trade_id') != _uuid(body.get('accepted_trade_id'))
            or evidence.get('physical_intake_id') != body['physical_intake_id']
            or evidence.get('physical_manifest_hash') != body['physical_manifest_hash']
            or type(evidence.get('trade_schema_version')) is not int or evidence['trade_schema_version'] not in (1, 2)
            or evidence.get('currency') != 'USD' or evidence.get('selected_offer') not in ('cash', 'credit')
            or evidence.get('customer_agreed') is not True or evidence.get('items_received') is not True
            or evidence.get('cash_payment_executed') is not False or evidence.get('inventory_received') is not False):
            raise ValueError()
        _integer(evidence.get('accepted_amount_cents'), 100_000_000, 1)
        if not isinstance(evidence.get('accepted_at'), str) or len(evidence['accepted_at']) > 60 or datetime.fromisoformat(evidence['accepted_at']).tzinfo is None:
            raise ValueError()
        rows = evidence.get('lines')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
            raise ValueError()
        seen = set(); accepted = {}; lines = []; total = 0
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'id','identity','quantity','unit_intake_cents','included','reviewed','source_line_key','line_total_cents'}
                or row.get('included') is not True or row.get('reviewed') is not True):
                raise ValueError()
            ident = _uuid(row['id']); key = _hash(row['source_line_key'])
            if ident in seen or key not in original:
                raise ValueError()
            seen.add(ident)
            identity = normalize_identity(row['identity'])
            quantity = _integer(row['quantity'], 100000, 1); unit = _integer(row['unit_intake_cents'], 100_000_000)
            if type(row['line_total_cents']) is not int or row['line_total_cents'] != quantity * unit:
                raise ValueError()
            total += row['line_total_cents']; accepted[key] = accepted.get(key, 0) + quantity
            if accepted[key] > original[key]:
                raise ValueError()
            lines.append((ident, key, source_line_id(identity), quantity))
        if _integer(evidence.get('intake_total_cents'), 100_000_000) != total:
            raise ValueError()
        if evidence['trade_schema_version'] == 2 or 'physical_intake' in evidence:
            expected = {'id': body['physical_intake_id'], 'manifest_hash': body['physical_manifest_hash'],
                'semantic_hash': semantic, 'selected_quantities': accepted, 'original_quantities': original,
                'excluded_quantities': {key: quantity - accepted.get(key, 0) for key, quantity in original.items()},
                'all_original_rows_claimed': True, 'excluded_rows_reusable': False, 'claim_reviewed': True}
            if fingerprint(evidence.get('physical_intake')) != fingerprint(expected):
                raise ValueError()
        return AcceptedBudgetProof(body['accepted_trade_id'], body['accepted_receipt_hash'], body['physical_intake_id'],
            body['physical_manifest_hash'], semantic, tuple(sorted(original.items())), tuple(lines))
    except (ValueError, TypeError, KeyError, AttributeError, ManifestError, OverflowError):
        _fail()


def verified_receiving_budget(receipt, plan_body, operation, accepted_receipt=None, local_snapshot=None):
    """Keep physical O, accepted A, selected P and server exclusions distinct.

    accepted_receipt is complete original evidence or an internal immutable proof
    previously verified by accepted_budget_proof. It is never inferred from P+E.
    """
    try:
        original, semantic = _original(receipt, plan_body)
        kind = plan_body.get('workflow_kind')
        if kind not in ('accepted_trade', 'inventory_only') or not isinstance(operation, dict):
            raise ValueError()
        for key in ('physical_intake_id','physical_manifest_hash','source_snapshot_hash','workflow_kind'):
            if operation.get(key) != plan_body.get(key):
                raise ValueError()
        _hash(plan_body.get('source_snapshot_hash'))
        if local_snapshot is not None:
            intent = receipt.get('hosted_receive') or {}
            if fingerprint(local_snapshot) != intent.get('local_snapshot_hash'):
                raise ValueError()
        accepted = {}; by_accepted = {}; proof = None
        if kind == 'accepted_trade':
            for key in ('accepted_trade_id', 'accepted_receipt_hash'):
                if operation.get(key) != plan_body.get(key):
                    raise ValueError()
            proof = accepted_receipt if isinstance(accepted_receipt, AcceptedBudgetProof) else accepted_budget_proof(receipt, plan_body, accepted_receipt)
            if (proof.trade_id != plan_body['accepted_trade_id'] or proof.receipt_hash != plan_body['accepted_receipt_hash']
                or proof.claim_id != plan_body['physical_intake_id'] or proof.manifest_hash != plan_body['physical_manifest_hash']
                or proof.semantic_hash != semantic or dict(proof.original) != original):
                raise ValueError()
            for ident, key, identity, quantity in proof.lines:
                by_accepted[ident] = (key, identity, quantity); accepted[key] = accepted.get(key, 0) + quantity
            budget = accepted
        else:
            if operation.get('accepted_trade_id') is not None or operation.get('accepted_receipt_hash') is not None:
                raise ValueError()
            budget = original
        rows = plan_body.get('lines'); targets = operation.get('targets')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 1000 or not isinstance(targets, list) or len(targets) != len(rows):
            raise ValueError()
        by_review = {}; target_ids = set(); selected = {}; accepted_selected = {}
        for row in rows:
            ident = _uuid(row.get('review_line_id'))
            if ident in by_review or row.get('reviewed') is not True:
                raise ValueError()
            by_review[ident] = row
        for target in targets:
            ident = _uuid(target.get('review_line_id')); row = by_review.get(ident)
            if row is None or ident in target_ids:
                raise ValueError()
            target_ids.add(ident)
            key = _hash(row.get('source_line_key')); quantity = _integer(row.get('quantity'), 100000, 1)
            selling = _integer(row.get('selling_cents'), 100_000_000)
            if (key not in budget or type(row.get('zero_price_confirmed', False)) is not bool
                or selling == 0 and row.get('zero_price_confirmed') is not True
                or target.get('source_line_key') != key or type(target.get('quantity')) is not int or target['quantity'] != quantity
                or type(target.get('selling_cents')) is not int or target['selling_cents'] != selling):
                raise ValueError()
            selected[key] = selected.get(key, 0) + quantity
            if selected[key] > budget[key]:
                raise ValueError()
            if kind == 'accepted_trade':
                accepted_id = _uuid(row.get('accepted_line_id')); binding = by_accepted.get(accepted_id)
                if binding is None or target.get('accepted_line_id') != accepted_id or binding[:2] != (key, target.get('identity_hash')):
                    raise ValueError()
                accepted_selected[accepted_id] = accepted_selected.get(accepted_id, 0) + quantity
                if accepted_selected[accepted_id] > binding[2]:
                    raise ValueError()
        excluded = {key: quantity - selected.get(key, 0) for key, quantity in budget.items()}
        if _map(operation.get('selected_quantities')) != selected or _map(operation.get('excluded_quantities')) != excluded:
            raise ValueError()
        result = {'basis': kind, 'O': original, 'A': accepted if kind == 'accepted_trade' else None,
            'acceptedLineQuantities': {key: value[2] for key, value in by_accepted.items()}, 'P': selected,
            'selectedByAcceptedLine': accepted_selected,
            'F': {key: quantity - accepted.get(key, 0) for key, quantity in original.items()} if kind == 'accepted_trade' else {},
            'E': excluded}
        result['proofHash'] = fingerprint(result)
        return result
    except CloudError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ManifestError, OverflowError):
        _fail()


def quantity_budget(facts):
    """Public counts from already verified goods facts, never authorization."""
    if facts['basis'] != 'accepted_trade':
        return None
    return {'physicalOriginalCopies': sum(facts['O'].values()), 'acceptedCopies': sum(facts['A'].values()),
        'selectedCopies': sum(facts['P'].values()), 'acceptedNotReceivedCopies': sum(facts['E'].values()),
        'outsideAgreementCopies': sum(facts['F'].values())}


def operation_quantity_budget(receipt, operation, evidence):
    """Read-only history counts; does not invent plan review/zero-price facts."""
    try:
        body = {key: operation.get(key) for key in ('workflow_kind','physical_intake_id','physical_manifest_hash','accepted_trade_id','accepted_receipt_hash')}
        proof = accepted_budget_proof(receipt, body, evidence)
        accepted = {}; by_id = {row[0]: row[1:] for row in proof.lines}; selected = {}; by_line = {}; seen = set()
        for _, key, _, quantity in proof.lines:
            accepted[key] = accepted.get(key, 0) + quantity
        targets = operation.get('targets')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 1000:
            raise ValueError()
        for row in targets:
            review = _uuid(row.get('review_line_id')); ident = _uuid(row.get('accepted_line_id'))
            key = _hash(row.get('source_line_key')); quantity = _integer(row.get('quantity'), 100000, 1)
            if review in seen or ident not in by_id or by_id[ident][:2] != (key, row.get('identity_hash')):
                raise ValueError()
            seen.add(review); selected[key] = selected.get(key, 0) + quantity; by_line[ident] = by_line.get(ident, 0) + quantity
            if selected[key] > accepted[key] or by_line[ident] > by_id[ident][2]:
                raise ValueError()
        excluded = {key: quantity - selected.get(key, 0) for key, quantity in accepted.items()}
        if _map(operation.get('selected_quantities')) != selected or _map(operation.get('excluded_quantities')) != excluded:
            raise ValueError()
        original = dict(proof.original)
        return quantity_budget({'basis':'accepted_trade','O':original,'A':accepted,'P':selected,'E':excluded,
            'F':{key: quantity - accepted.get(key, 0) for key, quantity in original.items()}})
    except CloudError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        _fail()
