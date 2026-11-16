"""Verified local projections of original accepted trades, without reacceptance."""
import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid

from intake_manifest import validate_manifest, manifest_digest, semantic_fingerprint, source_lines, ManifestError
from station_cloud import CloudError, ORIGIN
from station_store import StationError, now

MARKER = 'original_trade_recovery'
SOURCE = 'financial_source_snapshot'
_HASH = re.compile(r'[a-f0-9]{64}')
_LINE_KEYS = ('id', 'identity', 'quantity', 'unit_intake_cents', 'included', 'reviewed', 'source_line_key')
_MARKER_KEYS = {'version', 'serviceOrigin', 'tenantId', 'tradeId', 'receiptHash', 'sourceReference',
                'recoveredAt', 'fromReceiptId', 'inventoryVerification', 'receivingId'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _error(message='The original hosted trade evidence cannot be verified. Keep its reference for owner recovery.', *, code='ORIGINAL_TRADE_EVIDENCE_INVALID', status=409):
    return CloudError(message, status=status, code=code)


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid canonical UUID')
    return value


def _text(value, limit=300, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or not empty and not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid bounded text')
    return value


def _integer(value, maximum=100_000_000, *, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid integer')
    return value


def _cents(value):
    if isinstance(value, bool):
        raise ValueError('Invalid cents')
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
        raise ValueError('Invalid cents')
    return _integer(int(amount * 100))


def recovered_marker(receipt):
    """Return a detached typed marker; a malformed present marker never falls back."""
    if MARKER not in receipt:
        return None
    value = receipt[MARKER]
    try:
        if not isinstance(value, dict) or set(value) != _MARKER_KEYS or type(value['version']) is not int or value['version'] != 1:
            raise ValueError()
        if value['serviceOrigin'] != ORIGIN:
            raise ValueError()
        _uuid(value['tenantId']); _uuid(value['tradeId'])
        if not isinstance(value['receiptHash'], str) or not _HASH.fullmatch(value['receiptHash']):
            raise ValueError()
        _text(value['sourceReference'], 300)
        if datetime.fromisoformat(value['recoveredAt']).tzinfo is None:
            raise ValueError()
        if value['fromReceiptId'] is not None:
            _text(value['fromReceiptId'], 200)
        state = value['inventoryVerification']
        if state not in ('not_checked', 'no_operation', 'existing_operation'):
            raise ValueError()
        if state == 'existing_operation':
            _uuid(value['receivingId'])
        elif value['receivingId'] is not None:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _error('The recovered original-trade marker is invalid. Keep the original record for owner recovery.') from None
    return copy.deepcopy(value)


def immutable_recovery_marker(receipt):
    value = recovered_marker(receipt)
    if value is not None:
        value.pop('inventoryVerification'); value.pop('receivingId')
    return value


def _canonical_lines(lines):
    from station_shared_trade import IDENTITY_FIELDS, IDENTITY_LIMITS
    if not isinstance(lines, list) or not 1 <= len(lines) <= 1000:
        raise ValueError('Invalid original lines')
    result = []; seen = set(); certificates = set()
    for line in lines:
        if not isinstance(line, dict) or set(line) - set(_LINE_KEYS) - {'line_total_cents'}:
            raise ValueError('Unexpected original line fields')
        ident = _uuid(line.get('id'))
        if ident in seen:
            raise ValueError('Duplicate original line')
        seen.add(ident)
        identity = line.get('identity')
        if not isinstance(identity, dict) or set(identity) - set(IDENTITY_FIELDS) - {'kind'}:
            raise ValueError('Unexpected original identity fields')
        for key, value in identity.items():
            _text(value, IDENTITY_LIMITS.get(key, 10), empty=key not in ('name', 'game', 'kind'))
        if identity.get('kind') not in ('card', 'slab') or not identity.get('name') or not identity.get('game'):
            raise ValueError('Incomplete original identity')
        if identity['game'].startswith('other:') and not identity.get('game_label'):
            raise ValueError('Unnamed additional game')
        quantity = _integer(line.get('quantity'), 100000, minimum=1)
        unit = _integer(line.get('unit_intake_cents'))
        if type(line.get('included')) is not bool or type(line.get('reviewed')) is not bool or line['included'] and not line['reviewed']:
            raise ValueError('Unreviewed accepted line')
        if identity['kind'] == 'slab':
            if quantity != 1 or any(not identity.get(key) for key in ('grading_company', 'grade', 'certification')):
                raise ValueError('Incomplete individual slab')
            cert = (identity['grading_company'].casefold(), identity['certification'].casefold())
            if line['included'] and cert in certificates:
                raise ValueError('Duplicate accepted slab')
            if line['included']:
                certificates.add(cert)
        key = line.get('source_line_key')
        if not isinstance(key, str) or not _HASH.fullmatch(key):
            raise ValueError('Missing original source key')
        if 'line_total_cents' in line and (type(line['line_total_cents']) is not int or line['line_total_cents'] != quantity * unit):
            raise ValueError('Wrong original line total')
        result.append({key: copy.deepcopy(line[key]) for key in _LINE_KEYS})
    if not any(line['included'] for line in result):
        raise ValueError('No accepted lines')
    return result


def validate_recovered_projection(receipt):
    """Pure financial/source checks, independent of mutable retail review flags."""
    marker = recovered_marker(receipt)
    if marker is None:
        return None
    try:
        source = receipt.get(SOURCE)
        if not isinstance(source, dict) or set(source) != {'version', 'title', 'lines'} or type(source['version']) is not int or source['version'] != 1:
            raise ValueError()
        _text(source['title'], 200)
        lines = _canonical_lines(source['lines'])
        if lines != source['lines'] or receipt.get('title') != source['title']:
            raise ValueError()
        manifest = validate_manifest(receipt.get('source_manifest'))
        claim = receipt.get('intake_claim') or {}; intent = receipt.get('shared_trade') or {}
        evidence = (receipt.get('purchase') or {}).get('accepted_receipt')
        result = (receipt.get('purchase') or {}).get('acceptance_result')
        if not isinstance(evidence, dict) or not isinstance(result, dict):
            raise ValueError()
        if (_hash(evidence) != marker['receiptHash'] or evidence.get('trade_id') != marker['tradeId']
            or evidence.get('source_reference') != marker['sourceReference']
            or evidence.get('title') != source['title'] or evidence.get('lines') != [dict(line, line_total_cents=line['quantity'] * line['unit_intake_cents']) for line in lines if line['included']]):
            raise ValueError()
        original = {line['source_line_key']: line['quantity'] for line in source_lines(manifest)}
        selected = {}
        for line in lines:
            if line['source_line_key'] not in original:
                raise ValueError()
            if line['included']:
                selected[line['source_line_key']] = selected.get(line['source_line_key'], 0) + line['quantity']
        if any(quantity > original[key] for key, quantity in selected.items()):
            raise ValueError()
        physical = evidence.get('physical_intake')
        expected_physical = {'id': claim.get('physical_intake_id'), 'manifest_hash': manifest_digest(manifest),
            'semantic_hash': semantic_fingerprint(manifest), 'selected_quantities': selected,
            'original_quantities': original, 'excluded_quantities': {key: quantity - selected.get(key, 0) for key, quantity in original.items()},
            'all_original_rows_claimed': True, 'excluded_rows_reusable': False, 'claim_reviewed': True}
        if (physical != expected_physical or claim.get('state') != 'confirmed' or claim.get('server_state') != 'consumed'
            or claim.get('bound_trade_id') != marker['tradeId'] or claim.get('source_manifest_hash') != manifest_digest(manifest)
            or claim.get('manifest_hash') != manifest_digest(manifest) or claim.get('semantic_hash') != semantic_fingerprint(manifest)
            or evidence.get('physical_intake_id') != claim.get('physical_intake_id') or evidence.get('physical_manifest_hash') != manifest_digest(manifest)
            or intent.get('state') != 'confirmed' or intent.get('trade_id') != marker['tradeId']
            or intent.get('receipt_hash') != marker['receiptHash'] or intent.get('source_reference') != marker['sourceReference']
            or intent.get('physical_intake_id') != claim.get('physical_intake_id') or intent.get('physical_manifest_hash') != manifest_digest(manifest)
            or intent.get('source_hash') != _hash({'title': source['title'], 'lines': lines})
            or any(key in intent for key in ('create_key', 'accept_key', 'void_key'))):
            raise ValueError()
        purchase = receipt['purchase']; kind = evidence.get('selected_offer'); amount = evidence.get('accepted_amount_cents')
        _integer(amount, minimum=1)
        if (kind not in ('cash', 'credit') or intent.get('selected_offer') != kind or intent.get('customer_id') != evidence.get('customer', {}).get('id')
            or purchase.get('scope') != 'hosted' or purchase.get('type') != kind or _cents(purchase.get('amount')) != amount
            or purchase.get('at') != evidence.get('accepted_at') or _cents(purchase.get('intake_total')) != evidence.get('intake_total_cents')
            or purchase.get('hosted_trade_id') != marker['tradeId'] or purchase.get('receipt_hash') != marker['receiptHash']
            or result.get('receipt_hash') != marker['receiptHash'] or result.get('trade_id') != marker['tradeId']
            or result.get('offer_type') != kind or result.get('state') != 'accepted_' + kind
            or type(result.get('accepted_amount_cents')) is not int or result['accepted_amount_cents'] != amount
            or any(value.get('cash_payment_executed') is not False or value.get('inventory_received') is not False for value in (purchase, evidence, result))
            or purchase.get('checkout_eligible') is not False or result.get('checkout_eligible') is not False):
            raise ValueError()
        policy = 'unreviewed' if kind == 'credit' else 'not_applicable'
        if any(value.get('credit_policy_state') != policy for value in (purchase, evidence, result)):
            raise ValueError()
        if any('credit_entry_id' not in value for value in (purchase, evidence, result)) or not purchase['credit_entry_id'] == evidence['credit_entry_id'] == result['credit_entry_id']:
            raise ValueError()
        if kind == 'cash' and purchase['credit_entry_id'] is not None:
            raise ValueError()
        if kind == 'credit':
            _uuid(purchase['credit_entry_id'])
        from station_shared_trade import IDENTITY_FIELDS
        included = [line for line in lines if line['included']]
        cards = receipt.get('cards')
        if not isinstance(cards, list) or len(cards) != len(included):
            raise ValueError()
        seen = set()
        by_id = {line['id']: line for line in included}
        for card in cards:
            ident = _uuid(card.get('row_id'))
            if ident in seen or ident not in by_id:
                raise ValueError()
            seen.add(ident); line = by_id[ident]
            identity = {key: card.get(field) for key, field in IDENTITY_FIELDS.items() if card.get(field) not in (None, '')}
            identity['kind'] = line['identity']['kind']
            if (identity != line['identity'] or type(card.get('qty')) is not int or card['qty'] != line['quantity']
                or card.get('include') is not True or card.get('source_line_key') != line['source_line_key']
                or _cents(card.get('intake_value')) != line['unit_intake_cents']):
                raise ValueError()
        if type(receipt.get('retail_review_required')) is not bool or receipt.get('mode') != 'trade' or receipt.get('requires_purchase') is not True:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, ManifestError):
        raise _error('The recovered original agreement or displayed financial cards changed. Keep its original proof for owner recovery.') from None
    return {'marker': marker, 'source': {'title': source['title'], 'lines': copy.deepcopy(lines)},
            'kind': kind, 'amountCents': amount, 'selectedQuantities': selected, 'originalQuantities': original}


def recovered_source_snapshot(receipt):
    value = validate_recovered_projection(receipt)
    return value['source'] if value is not None else None


class OriginalTradeRecovery:
    def __init__(self, service, *, clock=None):
        self.service = service; self.clock = clock or time.monotonic
        self._previews = {}; self._lock = threading.RLock()

    @property
    def cloud(self):
        return self.service.cloud

    def clear(self):
        with self._lock:
            self._previews.clear()

    def _input(self, data, *, recover=False):
        allowed = {'tradeId', 'fromReceiptId', 'revision', 'sessionId'}
        if recover:
            allowed |= {'expectedReceiptHash', 'expectedCustomerId', 'confirmationHash', 'samePhysicalDeliveryConfirmed', 'restoreExisting'}
        try:
            if not isinstance(data, dict) or set(data) - allowed:
                raise ValueError()
            _uuid(data.get('tradeId'))
            linked = data.get('fromReceiptId')
            if linked is not None:
                _text(linked, 200); _integer(data.get('revision'), minimum=1)
            elif 'revision' in data and data['revision'] is not None:
                raise ValueError()
            if recover:
                _uuid(data.get('expectedCustomerId'))
                for key in ('expectedReceiptHash', 'confirmationHash'):
                    if not isinstance(data.get(key), str) or not _HASH.fullmatch(data[key]):
                        raise ValueError()
                for key in ('restoreExisting', 'samePhysicalDeliveryConfirmed'):
                    if key in data and type(data[key]) is not bool:
                        raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise _error('Reopen the original-trade preview before recovery.', code='ORIGINAL_TRADE_PREVIEW_INVALID', status=400) from None

    def _authority(self, bundle, expected):
        try:
            if set(bundle) != {'sessionId', 'tenantId', 'store', 'role', 'trade', 'claim'} or bundle['sessionId'] != expected or bundle['role'] not in ('owner', 'manager'):
                raise ValueError()
            _uuid(bundle['tenantId']); _text(bundle['store'], 200)
            profile = self.service.profile.safe_snapshot(); binding = self.service.profile.binding()
            if profile['profileMode'] != 'hosted' or binding != {'tenant_id': bundle['tenantId'], 'service_origin': ORIGIN}:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise _error('Open this store’s verified hosted profile before recovering its original trade.', code='ORIGINAL_TRADE_PROFILE_REQUIRED') from None

    def _build(self, bundle, from_receipt_id=None):
        trade, claim = bundle['trade'], bundle['claim']
        try:
            _uuid(trade['id']); _uuid(trade['customer_id']); _uuid(claim['physical_intake_id'])
            manifest = validate_manifest(claim['source_manifest'])
            if (claim.get('state') != 'consumed' or claim.get('bound_trade_id') != trade['id']
                or claim.get('manifest_hash') != manifest_digest(manifest) or claim.get('semantic_hash') != semantic_fingerprint(manifest)
                or trade.get('physical_intake_id') != claim['physical_intake_id'] or trade.get('physical_manifest_hash') != claim['manifest_hash']
                or trade.get('state') not in ('accepted_cash', 'accepted_credit')):
                raise ValueError()
            lines = _canonical_lines(trade['lines']); evidence = trade['accepted_receipt']; result = trade['acceptance_result']
            kind = evidence['selected_offer']; amount = _integer(evidence['accepted_amount_cents'], minimum=1)
            _text(trade['title'], 200); _text(trade['source_reference'], 300)
            games = {line['identity']['game'] for line in lines if line['included']}
            cards = []
            from station_shared_trade import IDENTITY_FIELDS
            for line in lines:
                if not line['included']:
                    continue
                card = {field: line['identity'].get(key, '') for key, field in IDENTITY_FIELDS.items()}
                card.update(row_id=line['id'], source_line_key=line['source_line_key'], qty=line['quantity'],
                    intake_value=line['unit_intake_cents'] / 100, final_price=0,
                    include=True, reviewed=False, price_confirmed=False, price_missing=True,
                    entry_source='hosted_recovery', storage_location='', sku_override='')
                cards.append(card)
            source = {'version': 1, 'title': trade['title'], 'lines': lines}
            marker = {'version': 1, 'serviceOrigin': ORIGIN, 'tenantId': bundle['tenantId'], 'tradeId': trade['id'],
                'receiptHash': trade['receipt_hash'], 'sourceReference': trade['source_reference'], 'recoveredAt': now(),
                'fromReceiptId': from_receipt_id, 'inventoryVerification': 'not_checked', 'receivingId': None}
            purchase = {'scope': 'hosted', 'type': kind, 'amount': amount / 100, 'at': evidence['accepted_at'],
                'intake_total': evidence['intake_total_cents'] / 100,
                'items': [{'row_id': line['id'], 'qty': line['quantity'], 'intake_value': line['unit_intake_cents'] / 100} for line in lines if line['included']],
                'hosted_trade_id': trade['id'], 'credit_entry_id': result['credit_entry_id'], 'receipt_hash': trade['receipt_hash'],
                'store': bundle['store'], 'credit_policy_state': evidence['credit_policy_state'], 'checkout_eligible': False,
                'cash_payment_executed': False, 'inventory_received': False,
                'accepted_receipt': copy.deepcopy(evidence), 'acceptance_result': copy.deepcopy(result)}
            receipt = {'id': 'R-' + time.strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:6].upper(),
                'source_hash': 'hosted-original-trade:' + _hash([ORIGIN, bundle['tenantId'], trade['id'], trade['receipt_hash']]),
                'source_name': 'Recovered original hosted trade', 'title': trade['title'], 'created': now(), 'mode': 'trade',
                'cards': cards, 'source_manifest': manifest, SOURCE: source, MARKER: marker,
                'inventory_status': 'not_received', 'inventory_intent': None, 'pricing_game': next(iter(games)) if len(games) == 1 else '',
                'mixed_games': len(games) > 1, 'intake_summary': {}, 'purchase': purchase, 'requires_purchase': True,
                'customer_reference': '', 'notes': '', 'channel': 'tcgplayer', 'storage_strategy': 'bin', 'storage_location': '',
                'prepared': False, 'intake_source': 'hosted_recovery', 'pickup_pending': False, 'cash_percent': 35, 'credit_percent': 50,
                'files': {}, 'error': '', 'retail_review_required': True,
                'intake_claim': {'state': 'confirmed', 'store': bundle['store'], 'source_manifest_hash': manifest_digest(manifest),
                    'physical_intake_id': claim['physical_intake_id'], 'manifest_hash': claim['manifest_hash'], 'semantic_hash': claim['semantic_hash'],
                    'physical_reference': claim['physical_reference'], 'server_state': 'consumed', 'server_version': claim['version'],
                    'bound_trade_id': trade['id'], 'consumed_trade_receipt_hash': trade['receipt_hash'],
                    'message': 'Original consumed physical intake recovered. Excluded copies remain nonreusable.'},
                'shared_trade': {'state': 'confirmed', 'store': bundle['store'], 'customer_id': trade['customer_id'],
                    'offer_contract_version': 2, 'selected_offer': kind, 'expected_offer_cents': amount,
                    'trade_id': trade['id'], 'source_reference': trade['source_reference'],
                    'source_hash': _hash({'title': source['title'], 'lines': lines}), 'source_revision': 0,
                    'claim_binding_version': 1, 'physical_intake_id': claim['physical_intake_id'], 'physical_manifest_hash': claim['manifest_hash'],
                    'server_version': trade['version'], 'receipt_hash': trade['receipt_hash'], 'credit_entry_id': result['credit_entry_id'],
                    'message': 'Recovered original cash agreement. Payment execution is not recorded by Stock & Sleeve.' if kind == 'cash' else
                        'Recovered original shared credit receipt. No new credit was issued; checkout remains disabled.'}}
            validate_recovered_projection(receipt)
            self.service.shared_trades._terminal(receipt, trade)
            self.service.shared_trades._confirmed_claim(receipt, trade, bundle['sessionId'], claim)
            if len(json.dumps(receipt).encode()) > 8 * 1024 * 1024:
                raise ValueError()
            return receipt
        except CloudError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, ManifestError):
            raise _error(code='SOURCE_RECOVERY_EVIDENCE_UNAVAILABLE') from None

    def _linked(self, data, claim, trade_id, *, db=None, expected_manifest_hash=None):
        ident = data.get('fromReceiptId')
        if ident is None:
            return None
        if db is None:
            receipt = self.service.active_receipt(ident)
        else:
            row = db.execute('SELECT id,body,deleted_at FROM receipts WHERE id=?', (ident,)).fetchone()
            if row is None or row['deleted_at'] is not None:
                raise _error('This imported batch is no longer active. Preview its original trade again.', code='ORIGINAL_TRADE_PREVIEW_CHANGED')
            receipt = json.loads(row['body'])
            if receipt.get('id') != row['id'] or receipt.get('deleted_at') is not None:
                raise _error(code='ORIGINAL_TRADE_PREVIEW_CHANGED')
        if receipt.get('revision') != data['revision']:
            raise _error('This imported batch changed. Preview the original trade again.', code='ORIGINAL_TRADE_PREVIEW_CHANGED')
        if receipt.get('shared_trade') or receipt.get('hosted_receive') or receipt.get('purchase') or receipt.get('tracked_credit'):
            raise _error('This batch already has saved financial or receiving work. Recover that original action first; its journal cannot be replaced.', code='ORIGINAL_TRADE_INPUT_LOCKED')
        try:
            manifest = validate_manifest(receipt.get('source_manifest'))
            if semantic_fingerprint(manifest) != claim['semantic_hash']:
                raise ValueError()
            if expected_manifest_hash is not None and manifest_digest(manifest) != expected_manifest_hash:
                raise ValueError()
            intake = receipt.get('intake_claim')
            if intake and (intake.get('state') != 'confirmed' or intake.get('physical_intake_id') != claim['physical_intake_id']):
                raise ValueError()
        except (ValueError, TypeError, KeyError, ManifestError):
            raise _error('The imported original source does not match that physical intake. Keep both original references for owner review.', code='ORIGINAL_TRADE_SOURCE_MISMATCH') from None
        return {'id': ident, 'revision': receipt['revision'], 'manifestHash': manifest_digest(manifest)}

    def _existing(self, bundle):
        values = self.service.store.original_trade_receipts(bundle['trade']['id'])
        if len(values) > 1:
            raise _error('Several local records reference this trade. An owner must resolve them; recovery will not choose or overwrite one.', code='ORIGINAL_TRADE_LOCAL_AMBIGUOUS')
        if values:
            self._validate_existing(values[0], bundle)
            return values[0]
        return None

    def _validate_existing(self, receipt, bundle):
        marker = recovered_marker(receipt)
        if marker and (marker['tenantId'] != bundle['tenantId'] or marker['serviceOrigin'] != ORIGIN):
            raise _error(code='ORIGINAL_TRADE_STORE_MISMATCH')
        self.service.shared_trades._terminal(receipt, bundle['trade'])
        self.service.shared_trades._confirmed_claim(receipt, bundle['trade'], bundle['sessionId'], bundle['claim'])

    def _facts(self, bundle, data, projection):
        linked = self._linked(data, bundle['claim'], bundle['trade']['id'])
        existing = self._existing(bundle)
        marker = recovered_marker(projection)
        return {'version': 1, 'serviceOrigin': ORIGIN, 'tenantId': bundle['tenantId'], 'tradeId': marker['tradeId'],
            'receiptHash': marker['receiptHash'], 'customerId': projection['shared_trade']['customer_id'],
            'offerType': projection['purchase']['type'], 'amountCents': bundle['trade']['accepted_amount_cents'],
            'physicalIntakeId': projection['intake_claim']['physical_intake_id'], 'manifestHash': projection['intake_claim']['manifest_hash'],
            'fromReceiptId': linked['id'] if linked else None, 'fromRevision': linked['revision'] if linked else None,
            'fromManifestHash': linked['manifestHash'] if linked else None,
            'existingReceiptId': existing['id'] if existing else None, 'existingDeletedAt': existing.get('deleted_at') if existing else None}

    def preview(self, data, expected):
        self._input(data)
        with self.cloud.lock:
            bundle = self.cloud.read_original_trade(data['tradeId'], expected_session_id=expected)
            self._authority(bundle, expected)
            projection = self._build(bundle, data.get('fromReceiptId')); facts = self._facts(bundle, data, projection)
            confirmation = _hash({'domain': 'stock-and-sleeve:original-trade-recovery:v1', 'random': secrets.token_hex(32), 'session': expected, 'facts': facts})
            evidence = projection['purchase']['accepted_receipt']; physical = evidence['physical_intake']
            included = [line for line in bundle['trade']['lines'] if line['included']]
            preview = {key: facts[key] for key in ('version', 'tradeId', 'receiptHash', 'customerId', 'offerType', 'amountCents',
                        'physicalIntakeId', 'fromReceiptId', 'fromRevision', 'existingReceiptId')}
            preview.update(customerName=evidence['customer']['display_name'], acceptedAt=evidence['accepted_at'], title=evidence['title'],
                physicalReference=bundle['claim']['physical_reference'], originalRows=len(projection['source_manifest']['rows']),
                originalCopies=sum(physical['original_quantities'].values()), acceptedRows=len(included),
                acceptedCopies=sum(physical['selected_quantities'].values()), excludedCopies=sum(physical['excluded_quantities'].values()),
                samePhysicalDeliveryRequired=facts['fromReceiptId'] is not None, restoreRequired=bool(facts['existingDeletedAt']),
                lines=[{'lineId': line['id'], 'name': line['identity']['name'], 'game': line['identity'].get('game_label') or line['identity']['game'],
                        'quantity': line['quantity'], 'unitIntakeCents': line['unit_intake_cents'], 'identity': copy.deepcopy(line['identity'])} for line in included])
            # A different process can sign out/change staff while the original
            # source is being verified. Recheck just before releasing its PII;
            # all network, hashing and DTO construction happened above.
            with self.cloud._session_guard():
                self.cloud.assert_recovery_session_unlocked(expected, bundle['tenantId'], bundle['store'])
                with self._lock:
                    current = self.clock()
                    self._previews = {key: value for key, value in self._previews.items() if value['expires'] > current}
                    while len(self._previews) >= 16:
                        self._previews.pop(next(iter(self._previews)))
                    self._previews[confirmation] = {'facts': facts, 'session': expected, 'expires': current + 300}
                return {'preview': preview, 'confirmationHash': confirmation, 'expiresIn': 300}

    def recover(self, data, expected):
        self._input(data, recover=True)
        with self.cloud.lock:
            with self._lock:
                current_time = self.clock()
                self._previews = {key: value for key, value in self._previews.items() if value['expires'] > current_time}
                cached = copy.deepcopy(self._previews.get(data['confirmationHash']))
            if not cached or cached['expires'] <= self.clock() or cached['session'] != expected:
                raise _error('The original-trade preview expired or changed. Reopen it before recovery.', code='ORIGINAL_TRADE_PREVIEW_EXPIRED')
            facts = cached['facts']
            if (data['tradeId'] != facts['tradeId'] or data['expectedReceiptHash'] != facts['receiptHash']
                or data['expectedCustomerId'] != facts['customerId'] or data.get('fromReceiptId') != facts['fromReceiptId']
                or data.get('revision') != facts['fromRevision']):
                raise _error('The original-trade selection changed. Preview it again.', code='ORIGINAL_TRADE_PREVIEW_CHANGED')
            if facts['fromReceiptId'] is not None and data.get('samePhysicalDeliveryConfirmed') is not True:
                raise _error('Confirm that the imported batch refers to this same physical delivery.', code='ORIGINAL_TRADE_PHYSICAL_CONFIRMATION_REQUIRED')
            restore = data.get('restoreExisting', False)
            if facts['existingDeletedAt'] and not restore:
                raise _error('Explicitly confirm restoration of the same deleted local record.', code='ORIGINAL_TRADE_RESTORE_REQUIRED')
            if restore and not facts['existingDeletedAt']:
                raise _error('No deleted original record was selected for restoration.', code='ORIGINAL_TRADE_PREVIEW_CHANGED')
            bundle = self.cloud.read_original_trade(data['tradeId'], expected_session_id=expected)
            self._authority(bundle, expected)
            projection = self._build(bundle, data.get('fromReceiptId')); current = self._facts(bundle, data, projection)
            ignored = {'existingReceiptId', 'existingDeletedAt'}
            if any(current[key] != value for key, value in facts.items() if key not in ignored):
                raise _error('The original-trade evidence changed. Preview it again.', code='ORIGINAL_TRADE_PREVIEW_CHANGED')
            if facts['existingReceiptId'] and current['existingReceiptId'] != facts['existingReceiptId']:
                raise _error(code='ORIGINAL_TRADE_PREVIEW_CHANGED')
            try:
                with self.service.profile.cloud_session_guard():
                    saved = self.cloud.assert_recovery_session_unlocked(expected, facts['tenantId'], bundle['store'])
                    if saved.get('role') not in ('owner', 'manager'):
                        raise _error('The signed-in store changed. No original record was committed.', code='ORIGINAL_TRADE_SESSION_CHANGED')
                    self._authority(bundle, expected)
                    self._linked(data, bundle['claim'], data['tradeId'])
                    result = self.service.store.commit_original_trade(projection, service_origin=ORIGIN,
                        tenant_id=facts['tenantId'], trade_id=facts['tradeId'], expected_existing_id=facts['existingReceiptId'],
                        expected_deleted_at=facts['existingDeletedAt'], restore_existing=restore,
                        validate_existing=lambda receipt: self._validate_existing(receipt, bundle),
                        validate_linked=lambda db: self._linked(data, bundle['claim'], data['tradeId'], db=db,
                            expected_manifest_hash=facts['fromManifestHash']))
            except (OSError, sqlite3.Error):
                raise _error('The original agreement remains saved online, but this computer could not save its local recovery. Retry this preview when storage is available; do not accept the trade again.', code='ORIGINAL_TRADE_STORAGE_UNAVAILABLE', status=503) from None
            return {'receipt': result['receipt'], 'recovery': {'tradeId': facts['tradeId'], 'receiptHash': facts['receiptHash'],
                    'reused': result['reused'], 'restored': result['restored']}}
