"""Pure original-proof verification for an ordinary confirmed record reader.

This module never reconstructs financial source from mutable retail review/title,
never repairs a receipt, and never performs network, persistence or financial I/O.
Mutation/recovery verification remains in station_shared_trade, unchanged.
"""
import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid

from intake_manifest import validate_manifest, manifest_digest, semantic_fingerprint, source_lines, ManifestError
from station_store import StationError

_HEX = re.compile(r'[a-f0-9]{64}')
_LINE_KEYS = ('id', 'identity', 'quantity', 'unit_intake_cents', 'included', 'reviewed')


def _fail():
    raise StationError('The original accepted agreement or its local immutable cards cannot be verified. Keep the original trade reference for owner recovery; printing will not accept it again.')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _integer(value, maximum=100_000_000, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError()
    return value


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or not empty and not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError()
    return value


def _uuid(value, *, historical=False):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError()
    canonical = str(uuid.UUID(value))
    if not historical and canonical != value:
        raise ValueError()
    return canonical


def _digest(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError()
    return value


def _cents(value):
    if isinstance(value, bool):
        raise ValueError()
    raw = Decimal(str(value))
    if not raw.is_finite() or raw < 0 or raw != raw.quantize(Decimal('.01')):
        raise ValueError()
    return _integer(int(raw * 100))


def _lines(trade, bound):
    from station_shared_trade import IDENTITY_FIELDS, IDENTITY_LIMITS
    rows = trade.get('lines')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError()
    result = []; seen = set(); certificates = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - set(_LINE_KEYS) - {'line_total_cents', 'source_line_key'}:
            raise ValueError()
        ident = _uuid(row.get('id'))
        if ident in seen:
            raise ValueError()
        seen.add(ident)
        identity = row.get('identity')
        if not isinstance(identity, dict) or set(identity) - set(IDENTITY_FIELDS) - {'kind'}:
            raise ValueError()
        for key, value in identity.items():
            _text(value, IDENTITY_LIMITS.get(key, 10), empty=key not in ('name', 'game', 'kind'))
        if not identity.get('name') or not identity.get('game') or identity.get('kind') not in ('card', 'slab'):
            raise ValueError()
        if identity['game'].startswith('other:') and not identity.get('game_label'):
            raise ValueError()
        quantity = _integer(row.get('quantity'), 100000, 1); unit = _integer(row.get('unit_intake_cents'))
        if type(row.get('included')) is not bool or type(row.get('reviewed')) is not bool or row['included'] and not row['reviewed']:
            raise ValueError()
        if identity['kind'] == 'slab':
            if quantity != 1 or any(not identity.get(key) for key in ('grading_company', 'grade', 'certification')):
                raise ValueError()
            certificate = (identity['grading_company'].casefold(), identity['certification'].casefold())
            if certificate in certificates:
                raise ValueError()
            certificates.add(certificate)
        elif any(identity.get(key) for key in ('grading_company', 'grade', 'grade_qualifier', 'certification')):
            raise ValueError()
        if type(row.get('line_total_cents')) is not int or row['line_total_cents'] != quantity * unit or row['line_total_cents'] > 100_000_000:
            raise ValueError()
        if bound or 'source_line_key' in row:
            _digest(row.get('source_line_key'))
        keys = _LINE_KEYS + (('source_line_key',) if bound else ())
        result.append({key: copy.deepcopy(row[key]) for key in keys})
    if not any(row['included'] for row in rows):
        raise ValueError()
    _integer(sum(row['line_total_cents'] for row in rows if row['included']))
    return result


def _cards(receipt, included, bound):
    from station_shared_trade import IDENTITY_FIELDS, IDENTITY_LIMITS
    cards = receipt.get('cards')
    if not isinstance(cards, list) or not 1 <= len(cards) <= 1000:
        raise ValueError()
    expected = {row['id']: row for row in included}; selected = {}; seen = set()
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError()
        ident = _uuid(card.get('row_id'), historical=True)
        if ident in seen or type(card.get('include', True)) is not bool:
            raise ValueError()
        seen.add(ident)
        if not card.get('include', True):
            continue
        if ident not in expected:
            raise ValueError()
        original = expected[ident]; identity = {}
        for key, field in IDENTITY_FIELDS.items():
            value = card.get(field, '')
            if value is None:
                value = ''
            _text(value, IDENTITY_LIMITS[key], empty=True)
            identity[key] = value.strip()
        identity['game'] = identity['game'] or receipt.get('pricing_game', '')
        hint = re.search(r'\b(psa|bgs|cgc|sgc|tag|ace|hga|beckett|graded|slab)\b', identity['condition'], re.I)
        identity['kind'] = 'slab' if hint or any(identity[key] for key in ('grading_company', 'grade', 'grade_qualifier', 'certification')) else 'card'
        identity = {key: value for key, value in identity.items() if value or key in ('name', 'game', 'kind')}
        if identity != original['identity'] or _integer(card.get('qty'), 100000, 1) != original['quantity'] or _cents(card.get('intake_value')) != original['unit_intake_cents']:
            raise ValueError()
        if bound and card.get('source_line_key') != original['source_line_key']:
            raise ValueError()
        selected[ident] = {'identity': identity, 'quantity': original['quantity'], 'unitIntakeCents': original['unit_intake_cents'],
            **({'sourceLineKey': original['source_line_key']} if bound else {})}
    if selected.keys() != expected.keys():
        raise ValueError()
    return selected


def _items(purchase, included):
    rows = purchase.get('items')
    if not isinstance(rows, list) or len(rows) != len(included):
        raise ValueError()
    actual = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'row_id', 'qty', 'intake_value'}:
            raise ValueError()
        ident = _uuid(row['row_id'], historical=True)
        if ident in actual:
            raise ValueError()
        actual[ident] = (_integer(row['qty'], 100000, 1), _cents(row['intake_value']))
    if actual != {row['id']: (row['quantity'], row['unit_intake_cents']) for row in included}:
        raise ValueError()
    return actual


def ordinary_record_proof(receipt, trade):
    """Verify a confirmed ordinary mirror for reading; return only bounded proof.

    Authoritative current reader/store/session checks are the caller's duty.
    The exact historical source fingerprint remains mandatory. No source or
    action body is regenerated or saved, and no mutable retail state is trusted.
    """
    from station_shared_trade import SharedTrade
    try:
        if not isinstance(receipt, dict) or not isinstance(trade, dict) or 'original_trade_recovery' in receipt:
            raise ValueError()
        intent = receipt.get('shared_trade'); purchase = receipt.get('purchase')
        if not isinstance(intent, dict) or not isinstance(purchase, dict) or intent.get('state') != 'confirmed' or purchase.get('scope') != 'hosted':
            raise ValueError()
        _text(receipt.get('id'), 200); _text(intent.get('store'), 200)
        contract = SharedTrade._contract(receipt)
        if 'claim_binding_version' in intent and (type(intent['claim_binding_version']) is not int or intent['claim_binding_version'] != 1):
            raise ValueError()
        bound = intent.get('claim_binding_version') == 1
        canonical = _lines(trade, bound); included = [row for row in canonical if row['included']]
        ident = _uuid(trade.get('id')); customer_id = _uuid(trade.get('customer_id'))
        title = _text(trade.get('title'), 200); source_reference = _text(trade.get('source_reference'), 300)
        source = {'title': title, 'lines': canonical}
        if (_hash(source) != _digest(intent.get('source_hash')) or intent.get('trade_id') != ident
            or intent.get('customer_id') != customer_id or intent.get('source_reference', receipt.get('id')) != source_reference):
            raise ValueError()
        evidence = trade.get('accepted_receipt'); result = trade.get('acceptance_result')
        allowed = {'trade_schema_version','trade_id','version_before_acceptance','receipt_version','customer','title',
            'source_reference','currency','lines','lines_hash','intake_total_cents','cash_offer_cents','credit_offer_cents',
            'offer_terms','selected_offer','accepted_amount_cents','accepted_at','accepted_by','accepted_by_role',
            'customer_agreed','items_received','inventory_received','cash_payment_executed','credit_policy_state',
            'credit_entry_id','physical_intake','physical_intake_id','physical_manifest_hash'}
        result_keys = {'trade_id','state','version','accepted_amount_cents','offer_type','receipt_hash','credit_entry_id',
            'cash_payment_executed','inventory_received','credit_policy_state','checkout_eligible','balance_after_cents'}
        if not isinstance(evidence, dict) or not isinstance(result, dict) or set(evidence) - allowed or set(result) - result_keys:
            raise ValueError()
        if 'balance_after_cents' in result:
            _integer(result['balance_after_cents'], 2**63 - 1)
        kind = evidence.get('selected_offer'); amount = _integer(trade.get('accepted_amount_cents'), minimum=1)
        if kind not in ('cash', 'credit') or contract == 1 and kind != 'credit':
            raise ValueError()
        state = 'accepted_' + kind; version = _integer(trade.get('version'), minimum=1)
        digest = _digest(trade.get('receipt_hash'))
        if (trade.get('state') != state or trade.get('selected_offer') != kind or _hash(evidence) != digest
            or any(value.get('receipt_hash') != digest for value in (intent, purchase, result))
            or evidence.get('trade_id') != ident or result.get('trade_id') != ident or purchase.get('hosted_trade_id') != ident
            or result.get('state') != state or result.get('offer_type') != kind or purchase.get('type') != kind
            or _integer(result.get('version')) != version or _integer(evidence.get('receipt_version')) != version
            or _integer(evidence.get('version_before_acceptance')) + 1 != version
            or type(evidence.get('trade_schema_version')) is not int or evidence['trade_schema_version'] not in (1, 2)
            or evidence.get('currency') != 'USD' or evidence.get('title') != title or evidence.get('source_reference') != source_reference
            or evidence.get('lines_hash') != trade.get('lines_hash') or _hash(trade['lines']) != trade.get('lines_hash')
            or _hash(evidence.get('lines')) != _hash([row for row in trade['lines'] if row['included']])
            or any(_integer(value.get('accepted_amount_cents'), minimum=1) != amount for value in (evidence, result))
            or _cents(purchase.get('amount')) != amount or purchase.get('at') != evidence.get('accepted_at')
            or purchase.get('store') != intent.get('store')
            or any(value.get('cash_payment_executed') is not False or value.get('inventory_received') is not False for value in (evidence, result, purchase))
            or any(value.get('checkout_eligible') is not False for value in (result, purchase))
            or evidence.get('customer_agreed') is not True or evidence.get('items_received') is not True
            or evidence.get('accepted_by_role') not in ('owner', 'manager')):
            raise ValueError()
        customer = evidence.get('customer')
        if not isinstance(customer, dict) or set(customer) != {'id', 'display_name'} or customer.get('id') != customer_id:
            raise ValueError()
        _text(customer.get('display_name'), 200); _uuid(evidence.get('accepted_by'))
        if datetime.fromisoformat(_text(evidence.get('accepted_at'), 60)).tzinfo is None:
            raise ValueError()
        for field in ('cash_offer_cents', 'credit_offer_cents', 'intake_total_cents'):
            if _integer(evidence.get(field)) != _integer(trade.get(field)):
                raise ValueError()
        if evidence[kind + '_offer_cents'] != amount or sum(row['quantity'] * row['unit_intake_cents'] for row in included) != evidence['intake_total_cents'] or _cents(purchase.get('intake_total')) != evidence['intake_total_cents']:
            raise ValueError()
        if evidence['trade_schema_version'] == 2:
            terms = {key: trade.get(key) for key in ('cash_bps', 'credit_bps', 'cash_override_cents', 'credit_override_cents', 'override_reason')}
            for field in ('cash_bps', 'credit_bps'):
                _integer(terms[field], 10000)
            for field in ('cash_override_cents', 'credit_override_cents'):
                if terms[field] is not None:
                    _integer(terms[field])
            _text(terms['override_reason'], 500, empty=True)
            if _hash(evidence.get('offer_terms')) != _hash(terms):
                raise ValueError()
        policy = 'unreviewed' if kind == 'credit' else 'not_applicable'
        if any(value.get('credit_policy_state') != policy or 'credit_entry_id' not in value for value in (evidence, result, purchase)):
            raise ValueError()
        entry = evidence['credit_entry_id']
        if result['credit_entry_id'] != entry or purchase['credit_entry_id'] != entry or intent.get('credit_entry_id') != entry:
            raise ValueError()
        if kind == 'cash':
            if entry is not None or trade.get('ledger_entry_id') is not None:
                raise ValueError()
        elif _uuid(entry) != trade.get('ledger_entry_id'):
            raise ValueError()
        selected_kind = 'credit' if contract == 1 else intent.get('selected_offer')
        if selected_kind != kind:
            raise ValueError()
        if intent.get('accept_key') and (_integer(intent.get('expected_offer_cents'), minimum=1) != amount
            or _integer(intent.get('accept_version')) != evidence['version_before_acceptance']):
            raise ValueError()
        copies = (purchase.get('accepted_receipt'), purchase.get('acceptance_result'))
        if all(value is None for value in copies) and contract == 1:
            pass  # A recognized old mirror can be read without backfilling it.
        elif (not all(isinstance(value, dict) for value in copies)
            or _hash(copies[0]) != digest or _hash(copies[1]) != _hash(result)):
            raise ValueError()
        physical_token = None
        if bound:
            if receipt.get('source_manifest_error'):
                raise ValueError()
            claim = receipt.get('intake_claim'); manifest = validate_manifest(receipt.get('source_manifest'))
            if not isinstance(claim, dict):
                raise ValueError()
            claim_id = _uuid(intent.get('physical_intake_id')); manifest_hash = manifest_digest(manifest)
            _integer(claim.get('server_version'), minimum=1)
            if (claim.get('state') != 'confirmed' or claim.get('physical_intake_id') != claim_id
                or claim.get('source_manifest_hash') != manifest_hash or claim.get('manifest_hash') != manifest_hash
                or intent.get('physical_manifest_hash') != manifest_hash or claim.get('semantic_hash') != semantic_fingerprint(manifest)
                or evidence.get('physical_intake_id') != claim_id or trade.get('physical_intake_id') != claim_id
                or evidence.get('physical_manifest_hash') != manifest_hash or trade.get('physical_manifest_hash') != manifest_hash
                or claim.get('server_state') != 'consumed' or claim.get('bound_trade_id') != ident
                or claim.get('consumed_trade_receipt_hash') != digest or claim.get('store') != intent.get('store')):
                raise ValueError()
            original = {row['source_line_key']: row['quantity'] for row in source_lines(manifest)}; selected = {}
            for row in canonical:
                if row['source_line_key'] not in original:
                    raise ValueError()
                if row['included']:
                    selected[row['source_line_key']] = selected.get(row['source_line_key'], 0) + row['quantity']
            if any(quantity > original[key] for key, quantity in selected.items()):
                raise ValueError()
            if evidence['trade_schema_version'] == 2:
                expected = {'id': claim_id, 'manifest_hash': manifest_hash, 'semantic_hash': claim['semantic_hash'],
                    'selected_quantities': selected, 'original_quantities': original,
                    'excluded_quantities': {key: quantity - selected.get(key, 0) for key, quantity in original.items()},
                    'all_original_rows_claimed': True, 'excluded_rows_reusable': False, 'claim_reviewed': True}
                if _hash(evidence.get('physical_intake')) != _hash(expected):
                    raise ValueError()
            physical_token = {'claim': copy.deepcopy(claim), 'manifest': manifest}
        cards = _cards(receipt, included, bound); items = _items(purchase, included)
        token = _hash({'domain': 'stock-and-sleeve:ordinary-record-reader:v1', 'source': source,
            'intent': intent, 'purchase': purchase, 'cards': cards, 'items': items, 'physical': physical_token})
        return {'version': 1, 'scope': 'ordinary_hosted_original', 'tradeId': ident, 'receiptHash': digest,
            'sourceHash': intent['source_hash'], 'financialToken': token}
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, ManifestError, OverflowError):
        _fail()
