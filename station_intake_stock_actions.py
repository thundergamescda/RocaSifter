"""Typed inventory-only original-stock actions with staged durable recovery.

The immutable SQL intent precedes the original-key journal and provider request.
Fresh verified same-child application precedes applied state and exact journal
ACK. Import/constructor performs no DDL, remote work or worker launch.
"""
import copy
import json
import math
import re
import secrets
import threading
import time
import uuid

from hosted_labels import fingerprint
from station_cloud import CloudError, ORIGIN
from station_profile import INTAKE_PROJECTION_FEATURE, INTAKE_STOCK_OPERATIONS_FEATURE, ProfilePolicyError
from station_store import StationError, now
from station_intake_receiving_projection import MARKER, _id, _uuid, _hash, _integer
import station_intake_receiving_registry as projections
import station_intake_stock_action_registry as actions
from station_intake_stock_action_codec import canonical, prepare_intent, validate_intent
from station_intake_stock_action_transport import IntakeStockActionTransport, validate_result


KINDS = frozenset(('commit', 'recover', 'authorize', 'cancel'))
BASE = frozenset(('sessionId', 'stockReceiptId', 'revision'))
FLAGS = frozenset(('samePhysicalDeliveryConfirmed', 'excludedNotReceivedAck',
                   'resumeSameOperationConfirmed', 'recordExistingAdmissionConfirmed'))
LOOKUP_KEYS = frozenset(('found', 'result', 'result_fingerprint', 'resolution', 'journal_row'))
MAX_RESPONSE = 10_000_000


def _error(message='Review the current saved stock record and its same original action; the original request key was kept.',
           *, code='INTAKE_STOCK_ACTION_STALE', status=409):
    return CloudError(message, status=status, pending=True, code=code)


def _row(value):
    return tuple(value[name] for name in actions.FIELDS) if value is not None else None


def _encoded(value):
    raw = canonical(value)
    if len(raw.encode('utf-8')) > MAX_RESPONSE:
        raise _error(code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
    return raw


class IntakeStockActions:
    def __init__(self, service, *, clock=None):
        self.service = service
        self.clock = clock or time.monotonic
        self._previews = {}
        self._lock = threading.RLock()

    @property
    def cloud(self):
        return self.service.cloud

    @property
    def projection(self):
        return self.service.intake_stock_projection

    @property
    def transport(self):
        return IntakeStockActionTransport(self.cloud)

    def clear(self):
        with self._lock:
            self._previews.clear()

    def _stop(self):
        event = getattr(self.service, 'stop_event', None)
        if event is not None and event.is_set():
            raise _error('The app is stopping. Keep the saved original action for recovery.', status=503)

    def _time(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise _error(code='INTAKE_STOCK_ACTION_PREVIEW_EXPIRED')
        return value

    def _purge(self):
        stamp = self._time()
        self._previews = {key: value for key, value in self._previews.items() if value['expires'] > stamp}
        return stamp

    def _input(self, data, expected, kind):
        try:
            if (type(data) is not dict or type(expected) is not str
                or not re.fullmatch('[a-f0-9]{32}', expected) or data.get('sessionId') != expected):
                raise ValueError()
            if kind == 'activate':
                if set(data) != {'sessionId', 'enableOriginalStockOperationsConfirmed'} or data['enableOriginalStockOperationsConfirmed'] is not True:
                    raise ValueError()
                return
            _id(data.get('stockReceiptId')); _integer(data.get('revision'), 1)
            required = set(BASE)
            if kind == 'preview':
                required.add('action')
                if data.get('action') not in KINDS: raise ValueError()
                if data['action'] == 'cancel':
                    required.add('reason'); reason = data.get('reason')
                    if (type(reason) is not str or not 10 <= len(reason) <= 200 or reason != reason.strip()
                        or any(ord(char) < 32 or ord(char) == 127 for char in reason)): raise ValueError()
            elif kind == 'status':
                if 'intentId' in data: required.add('intentId'); _uuid(data['intentId'])
            elif kind == 'perform':
                required |= {'confirmationHash', 'performOriginalStockActionConfirmed'}
                required |= set(data) & FLAGS
                if data.get('performOriginalStockActionConfirmed') is not True: raise ValueError()
                if any(data[name] is not True for name in set(data) & FLAGS): raise ValueError()
                _hash(data.get('confirmationHash'))
            elif kind == 'apply':
                required |= {'intentId', 'confirmationHash', 'applySavedResultConfirmed'}
                _uuid(data.get('intentId')); _hash(data.get('confirmationHash'))
                if data.get('applySavedResultConfirmed') is not True: raise ValueError()
            else:
                raise ValueError()
            if set(data) != required: raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise _error('Use only this current saved record and its exact typed action confirmations.',
                         code='INTAKE_STOCK_ACTION_INPUT', status=400) from None

    def _state(self):
        try:
            with self.service.store.db() as db:
                value = actions.initialization_state(db)
        except StationError as error:
            raise _error('Original-stock action metadata needs owner recovery; it was not recreated.',
                         code='INTAKE_STOCK_ACTION_FEATURE_RECOVERY_REQUIRED') from error
        if value not in ('absent', 'present'):
            raise _error(code='INTAKE_STOCK_ACTION_FEATURE_RECOVERY_REQUIRED')
        return value

    def _prerequisite(self):
        try:
            self.service.profile.require_feature(INTAKE_PROJECTION_FEATURE)
        except ProfilePolicyError as error:
            if error.code in ('PROFILE_FEATURE_UPGRADE_REQUIRED', 'PROFILE_FEATURE_DEPENDENCY_REQUIRED'):
                raise _error('Explicitly enable saved original-stock records before stock actions.',
                             code='INTAKE_STOCK_ACTION_PROJECTION_REQUIRED') from error
            raise
        self.projection._enabled()

    def _enabled(self):
        self._prerequisite()
        try:
            self.service.profile.require_feature(INTAKE_STOCK_OPERATIONS_FEATURE)
        except ProfilePolicyError as error:
            if error.code == 'PROFILE_FEATURE_DEPENDENCY_REQUIRED':
                raise _error(code='INTAKE_STOCK_ACTION_PROJECTION_REQUIRED') from error
            if error.code == 'PROFILE_FEATURE_UPGRADE_REQUIRED':
                raise _error('Explicitly enable original-stock actions on this computer first.',
                             code='INTAKE_STOCK_ACTION_FEATURE_REQUIRED') from error
            raise
        if self._state() == 'absent':
            raise _error('Original-stock action setup is unfinished. Explicitly resume setup; saved work was kept.',
                         code='INTAKE_STOCK_ACTION_FEATURE_RESUME_REQUIRED')

    def _context(self, ident, revision, expected, *, allow_refresh=False):
        self._stop(); self._enabled()
        return self.projection.operation_context(ident, revision, expected, allow_refresh=allow_refresh)

    def _session(self, context, expected, *, unlocked=False):
        if not unlocked:
            with self.cloud._session_guard():
                return self._session(context, expected, unlocked=True)
        saved = self.cloud.assert_recovery_session_unlocked(expected, context['bundle']['tenantId'], context['bundle']['store'])
        expiry = saved.get('expires_at'); stamp = self.cloud.clock()
        if (saved.get('role') != context['bundle']['role'] or saved.get('role') not in ('owner', 'manager')
            or type(expiry) not in (int, float) or not math.isfinite(expiry)
            or type(stamp) not in (int, float) or not math.isfinite(stamp) or expiry <= stamp):
            raise _error(code='INTAKE_STOCK_ACTION_SESSION_CHANGED', status=403)
        return (expected, context['bundle']['tenantId'], context['bundle']['store'], saved['role'], expiry)

    def _pair(self, context, intent_id=None):
        pair = actions.read_pair(self.service.store, context['bundle']['tenantId'],
                                 context['proof']['receivingId'], intent_id=intent_id)
        if type(pair) is not dict or set(pair) != {'active', 'intent'}:
            raise _error(code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
        for value in pair.values():
            if value is not None and value['receipt_id'] != context['child']['receipt']['id']:
                raise _error('A saved action belongs to another registered stock record. Keep its original key for owner review.')
        return pair

    def _intent(self, row, context):
        intent = json.loads(row['intent_body'])
        validate_intent(intent)
        if canonical(intent) != row['intent_body']: raise _error()
        self._bound(intent, context)
        return intent

    @staticmethod
    def _bound(intent, context):
        validate_intent(intent)
        bundle = context['bundle']; proof = context['proof']; child = context['child']['receipt']; marker = child[MARKER]
        facts = {'serviceOrigin': ORIGIN, 'tenantId': bundle['tenantId'], 'store': bundle['store'],
            'stockReceiptId': child['id'], 'physicalIntakeId': marker['physicalIntakeId'],
            'physicalManifestHash': marker['physicalManifestHash'], 'sourceSnapshotHash': proof['sourceSnapshotHash'],
            'receivingId': proof['receivingId'], 'serverRevision': proof['serverRevision'], 'planHash': proof['planHash'],
            'provider': proof['provider'], 'labelProfile': proof['labelProfile']}
        if any(canonical(intent[name]) != canonical(value) for name, value in facts.items()): raise _error()

    @staticmethod
    def _facts(context):
        return {'selection': context['selection'], 'child': context['child']['hash'],
            'registry': fingerprint(context['registry']), 'proof': context['proof']['proofHash'],
            'source': fingerprint(context['bundle']['restorationSource']),
            'operation': fingerprint(context['bundle']['operation']), 'claim': fingerprint(context['bundle']['claim']),
            'profile': id(context['profile']), 'cloudProfile': id(context['cloudProfile']),
            'sessionId': context['bundle']['sessionId'], 'role': context['bundle']['role']}

    def _assert_pair(self, context, pair, expected, session_facts, *, intent_id=None):
        self._stop(); self._enabled()
        self.projection.assert_operation_current(context, expected)
        current = self._session(context, expected, unlocked=True)
        with self.service.store.db() as db:
            actions.assert_pair_current(db, tenant_id=context['bundle']['tenantId'],
                receiving_id=context['proof']['receivingId'], server_revision=context['proof']['serverRevision'],
                proof_hash=context['selectedProofHash'], session_facts=session_facts, current_session_facts=current,
                expected_projection=context['expected'], expected_action=_row(pair['intent']), expected_active=_row(pair['active']),
                intent_id=intent_id)

    def _read_response(self, context, pair, expected, payload, *, intent_id=None):
        _encoded(payload); self.projection._reauthorize(context, expected)
        session = self._session(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            self._assert_pair(context, pair, expected, session, intent_id=intent_id)
        return payload

    def _common(self, context, pair, expected):
        return {'receipt': context['child']['receipt'], 'proof': context['proof'],
            'expected_projection': context['expected'], 'expected_action': _row(pair['intent']),
            'expected_active': _row(pair['active']), 'session_facts': self._session(context, expected), 'stamp': now()}

    def _commit(self, context, expected, prepared, *, reservation=False):
        self.projection._reauthorize(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            self._stop(); self._enabled(); self.projection.assert_operation_current(context, expected)
            method = actions.commit_reservation if reservation else actions.commit_transition
            return method(self.service.store, prepared, current_session_facts=self._session(context, expected, unlocked=True))

    @staticmethod
    def _bindings(context):
        proof = context['proof']; bundle = context['bundle']; receipt = context['child']['receipt']
        return {'version': 1, 'originKind': 'physical_intake', 'serviceOrigin': ORIGIN,
            'tenantId': bundle['tenantId'], 'store': bundle['store'], 'stockReceiptId': receipt['id'],
            'revision': receipt['revision'], **{name: proof[name] for name in ('receivingId', 'serverRevision', 'planHash', 'proofHash')}}

    @staticmethod
    def _public_intent(row, result_fingerprint=None):
        if row is None: return None
        result_fingerprint = result_fingerprint or row['result_fingerprint']
        return {'intentId': row['intent_id'], 'requestKey': row['request_key'], 'action': row['action_kind'],
            'state': row['state'], 'revision': row['revision'], 'resultObserved': result_fingerprint is not None,
            'resultFingerprint': result_fingerprint, 'providerActionAttribution': 'unknown'}

    @staticmethod
    def _operation(context):
        return {'state': context['proof']['state'], 'admission': copy.deepcopy(context['proof']['admission']),
                'proofHash': context['proof']['proofHash'], 'counts': copy.deepcopy(context['counts'])}

    def _new_intent(self, context, kind, reason=None):
        try:
            return prepare_intent(kind, intent_id=str(uuid.uuid4()), stock_receipt_id=context['child']['receipt']['id'],
                stock_revision=context['child']['receipt']['revision'], tenant_id=context['bundle']['tenantId'],
                store=context['bundle']['store'], restoration_source=context['bundle']['restorationSource'],
                operation=context['bundle']['operation'], proof_hash=context['proof']['proofHash'], created_at=now(), reason=reason)
        except StationError as error:
            raise _error('This original operation does not currently allow that stock action.',
                         code='INTAKE_STOCK_ACTION_NOT_AVAILABLE') from error

    @staticmethod
    def _mode(context, row, intent):
        if row and row['state'] in ('applied', 'acknowledged'): return 'apply_saved_result'
        admission = context['proof']['admission']
        if (row and row['state'] == 'pending' and intent['kind'] == 'commit' and admission['everAdmitted']
            and (admission['catalogStepPresent'] or admission['inventoryStepPresent'])
            and context['proof']['state'] != 'cancelled_before_write'):
            return 'record_existing_admission'
        return 'dispatch_action'

    def _eligible(self, intent, context, mode):
        self._bound(intent, context)
        if context['child']['receipt'][MARKER]['proofHash'] != context['proof']['proofHash']:
            raise _error('Explicitly refresh and reload this saved stock record before any dispatch.',
                         code='INTAKE_STOCK_ACTION_REFRESH_REQUIRED')
        if mode == 'record_existing_admission':
            admission = context['proof']['admission']
            if (intent['kind'] != 'commit' or not admission['everAdmitted']
                or not (admission['catalogStepPresent'] or admission['inventoryStepPresent'])): raise _error()
            return
        if mode != 'dispatch_action': raise _error(code='INTAKE_STOCK_ACTION_RESULT_AVAILABLE')
        try:
            candidate = prepare_intent(intent['kind'], intent_id=intent['intentId'], stock_receipt_id=intent['stockReceiptId'],
                stock_revision=context['child']['receipt']['revision'], tenant_id=intent['tenantId'], store=intent['store'],
                restoration_source=context['bundle']['restorationSource'], operation=context['bundle']['operation'],
                proof_hash=context['proof']['proofHash'], created_at=intent['createdAt'], reason=intent['body'].get('reason'))
        except StationError as error:
            raise _error('Keep the same saved request; this operation no longer permits its dispatch.',
                         code='INTAKE_STOCK_ACTION_NOT_AVAILABLE') from error
        if canonical(candidate['body']) != canonical(intent['body']): raise _error()
        if intent['kind'] != 'recover' and candidate['operationHash'] != intent['operationHash']: raise _error()

    @staticmethod
    def _confirm_flags(data, intent, mode):
        required = set()
        if mode == 'record_existing_admission': required.add('recordExistingAdmissionConfirmed')
        elif mode == 'dispatch_action':
            if intent['kind'] == 'commit': required |= {'samePhysicalDeliveryConfirmed', 'excludedNotReceivedAck'}
            elif intent['kind'] == 'authorize': required.add('resumeSameOperationConfirmed')
        else:
            raise _error(code='INTAKE_STOCK_ACTION_RESULT_AVAILABLE')
        if set(data) & FLAGS != required or any(data.get(name) is not True for name in required):
            raise _error('Confirm exactly this action and its displayed mode.', code='INTAKE_STOCK_ACTION_INPUT', status=400)

    def _lookup(self, intent, expected, *, persist=False):
        value = self.transport.lookup(intent, expected, persist=persist)
        if type(value) is not dict or set(value) != LOOKUP_KEYS or type(value['found']) is not bool:
            raise _error(code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
        if value['found']:
            validate_result(value['result'], intent)
            if value['result_fingerprint'] != fingerprint(value['result']) or value['resolution'] not in ('journal', 'request_lookup'):
                raise _error(code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
        elif value['result'] is not None or value['result_fingerprint'] is not None or value['resolution'] != 'not_found':
            raise _error(code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
        if value['journal_row'] is not None and type(value['journal_row']) is not dict: raise _error()
        return value

    def _cache_quote(self, context, pair, expected, intent, mode, kind, payload, *, lookup=None, result_fingerprint=None):
        facts = self._facts(context)
        confirmation = fingerprint({'domain': 'stock-and-sleeve:intake-stock-action-quote:v1',
            'random': secrets.token_hex(32), 'facts': facts, 'intent': intent, 'mode': mode,
            'resultFingerprint': result_fingerprint})
        value = {'sessionId': expected, 'stockReceiptId': context['child']['receipt']['id'],
            'revision': context['child']['receipt']['revision'], 'intent': copy.deepcopy(intent),
            'facts': facts, 'pair': {'active': _row(pair['active']), 'intent': _row(pair['intent'])},
            'mode': mode, 'kind': kind, 'result_fingerprint': result_fingerprint,
            'journal_row': copy.deepcopy(lookup['journal_row']) if lookup else None,
            'lookup_found': lookup['found'] if lookup else None}
        result = copy.deepcopy(payload)
        result['confirmationHash' if kind == 'perform' else 'applyConfirmationHash'] = confirmation
        _encoded(result); self.projection._reauthorize(context, expected)
        session = self._session(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            self._assert_pair(context, pair, expected, session, intent_id=intent['intentId'])
            with self._lock:
                stamp = self._purge()
                while len(self._previews) >= 16: self._previews.pop(next(iter(self._previews)))
                self._previews[confirmation] = dict(value, expires=stamp+300)
        return result

    def _cached(self, data, expected, kind):
        with self._lock:
            self._purge(); value = copy.deepcopy(self._previews.get(data['confirmationHash']))
        if (not value or value['kind'] != kind or value['sessionId'] != expected
            or value['stockReceiptId'] != data['stockReceiptId'] or value['revision'] != data['revision']
            or kind == 'apply' and value['intent']['intentId'] != data['intentId']):
            raise _error('Reload the current saved record and review the same action again.',
                         code='INTAKE_STOCK_ACTION_PREVIEW_EXPIRED')
        return value

    def _live(self, confirmation, cached):
        with self._lock:
            self._purge()
            if self._previews.get(confirmation) != cached:
                raise _error(code='INTAKE_STOCK_ACTION_PREVIEW_EXPIRED')

    def _journal_preflight(self, state, authority, expected):
        for row in self.cloud.receiving.read():
            if not row['request_key'].startswith('iss1_'): continue
            try:
                if state != 'present' or row['store'] != authority['store']: raise ValueError()
                ident = str(uuid.UUID(hex=row['request_key'][5:]))
                pair = actions.read_pair(self.service.store, authority['tenantId'], row['receiving_id'], intent_id=ident)
                saved = pair['intent']
                if saved is None or saved['state'] == 'acknowledged': raise ValueError()
                intent = json.loads(saved['intent_body']); validate_intent(intent)
                if (canonical(intent) != saved['intent_body'] or intent['tenantId'] != authority['tenantId']
                    or intent['store'] != row['store'] or intent['kind'] != row['kind']
                    or intent['stockReceiptId'] != row['receipt_id'] or intent['receivingId'] != row['receiving_id']
                    or intent['requestKey'] != row['request_key'] or canonical(intent['body']) != canonical(row['body'])
                    or canonical(intent['labelProfile']) != canonical(row['label_profile'])): raise ValueError()
                if 'result' in row:
                    validate_result(row['result'], intent)
                    if row['result_fingerprint'] != fingerprint(row['result']): raise ValueError()
            except (StationError, ValueError, TypeError, KeyError, AttributeError) as error:
                raise _error('A saved stock-action journal has no matching typed action. Keep it for owner recovery.',
                             code='INTAKE_STOCK_ACTION_FEATURE_RECOVERY_REQUIRED') from error

    def activate(self, data, expected):
        self.clear()
        try:
            self._input(data, expected, 'activate'); self._stop(); self._prerequisite()
            with self.cloud.lock:
                before = self.service.profile; setup = getattr(self.service, 'setup', None)
                if setup is not None and setup.policy is not before: raise _error(code='INTAKE_STOCK_ACTION_POLICY_CHANGED')
                authority = self.cloud.record_authority(expected_session_id=expected)
                if authority.get('role') not in ('owner', 'manager'): raise _error(status=403)
                if before.binding() != {'tenant_id': authority['tenantId'], 'service_origin': ORIGIN}: raise _error()
                state = self._state()
                self._journal_preflight(state, authority, expected)
                with self.cloud._session_guard(), self.service.lock:
                    if (self.service.profile is not before or self.cloud.profile is not before
                        or getattr(self.service, 'setup', None) is not setup
                        or setup is not None and setup.policy is not before): raise _error()
                    saved = self.cloud.assert_recovery_session_unlocked(expected, authority['tenantId'], authority['store'])
                    if saved.get('role') != authority['role'] or saved.get('role') not in ('owner', 'manager'): raise _error(status=403)
                    self._prerequisite(); self._state()
                    facts = (expected, saved['tenant_id'], saved['store'], saved['role'], saved['expires_at'])
                    try:
                        updated = before.activate_feature(INTAKE_STOCK_OPERATIONS_FEATURE, self.service.store,
                                                          current_session_facts=facts)
                    except (StationError, OSError) as error:
                        if isinstance(error, ProfilePolicyError) and error.code == 'PROFILE_FEATURE_DEPENDENCY_REQUIRED':
                            raise _error('Enable saved original-stock records first.', code='INTAKE_STOCK_ACTION_PROJECTION_REQUIRED') from error
                        if isinstance(error, ProfilePolicyError) and error.code == 'PROFILE_FEATURE_PRE_EPOCH_METADATA':
                            raise _error(code='INTAKE_STOCK_ACTION_FEATURE_RECOVERY_REQUIRED') from error
                        raise _error('Saved work was kept. Close and reopen the desktop app before explicitly resuming action setup.',
                                     code='INTAKE_STOCK_ACTION_FEATURE_RESTART_REQUIRED') from error
                    self.service.profile = updated; self.cloud.profile = updated
                    if setup is not None: setup.policy = updated
                result = {'version': 1, 'feature': INTAKE_STOCK_OPERATIONS_FEATURE, 'enabled': True,
                    'reloadRequired': True, 'providerActionPerformed': False, 'financialActionPerformed': False}
                _encoded(result)
                return result
        finally:
            self.clear()

    def preview(self, data, expected):
        try:
            self._input(data, expected, 'preview')
            with self.cloud.lock:
                context = self._context(data['stockReceiptId'], data['revision'], expected)
                pair = self._pair(context); row = pair['intent']
                if row:
                    intent = self._intent(row, context)
                    if intent['kind'] != data['action']:
                        raise _error('Check the saved action before choosing another kind.', code='INTAKE_STOCK_ACTION_PENDING')
                    if row['state'] != 'pending' or self._lookup(intent, expected)['found']:
                        raise _error('Check and explicitly apply this saved action result.', code='INTAKE_STOCK_ACTION_RESULT_AVAILABLE')
                    if intent['kind'] == 'cancel' and data['reason'].strip() != intent['body']['reason']: raise _error()
                else:
                    intent = self._new_intent(context, data['action'], data.get('reason'))
                mode = self._mode(context, row, intent)
                self._eligible(intent, context, mode)
                active = None if row is None else {key: value for key, value in self._public_intent(row).items()
                    if key in ('intentId', 'requestKey', 'action', 'state', 'revision')}
                payload = {**self._bindings(context), 'action': intent['kind'], 'executionMode': mode,
                    'state': context['proof']['state'], 'admission': copy.deepcopy(context['proof']['admission']),
                    'counts': copy.deepcopy(context['counts']), 'provider': copy.deepcopy(context['proof']['provider']),
                    'labelProfile': copy.deepcopy(context['proof']['labelProfile']),
                    'rows': copy.deepcopy(context['proof']['stockRows']), 'activeIntent': active,
                    'requiresFreshMfa': mode == 'dispatch_action' and intent['kind'] in ('commit', 'authorize'),
                    'expiresIn': 300, 'financialActionPerformed': False}
                return self._cache_quote(context, pair, expected, intent, mode, 'perform', payload)
        except (CloudError, ProfilePolicyError):
            self.clear(); raise
        except (StationError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            self.clear(); raise _error() from error
        except Exception:
            self.clear(); raise

    def _allowed(self, context, row):
        if context['child']['receipt'][MARKER]['proofHash'] != context['proof']['proofHash']: return []
        if row:
            if row['state'] != 'pending': return []
            intent = self._intent(row, context)
            try: self._eligible(intent, context, self._mode(context, row, intent))
            except (StationError, CloudError): return []
            return [intent['kind']]
        values = []
        for kind in ('commit', 'recover', 'authorize', 'cancel'):
            # Only a private eligibility probe; no ID is exposed or reserved.
            try: self._new_intent(context, kind, 'Original stock cancellation review' if kind == 'cancel' else None)
            except CloudError as error:
                if error.code != 'INTAKE_STOCK_ACTION_NOT_AVAILABLE': raise
            else: values.append(kind)
        return values

    def _saved_basis(self, row, intent, outcome):
        if row['state'] == 'pending':
            return outcome['result_fingerprint'] if outcome['found'] else None
        saved = _hash(row['result_fingerprint'])
        if outcome['found'] and outcome['result_fingerprint'] != saved:
            raise _error('The original request result conflicts with its durable application.', code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
        if not outcome['found'] and outcome['journal_row'] is not None:
            raise _error('The retained action and present journal disagree. Keep both for owner recovery.',
                         code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
        if row['state'] == 'acknowledged' and outcome['journal_row'] is not None:
            raise _error('An acknowledged journal entry has reappeared. Keep it for owner recovery.',
                         code='INTAKE_STOCK_ACTION_OWNER_RECOVERY')
        return saved

    def status(self, data, expected):
        try:
            self._input(data, expected, 'status')
            with self.cloud.lock:
                context = self._context(data['stockReceiptId'], data['revision'], expected, allow_refresh=True)
                pair = self._pair(context, data.get('intentId')); row = pair['intent']
                basis = None; outcome = None; intent = None
                if row:
                    intent = self._intent(row, context)
                    outcome = self._lookup(intent, expected, persist=False)
                    basis = self._saved_basis(row, intent, outcome)
                payload = {**self._bindings(context), 'operation': self._operation(context),
                    'intent': self._public_intent(row, basis), 'allowedActions': self._allowed(context, pair['active']),
                    'localRefreshRequired': context['child']['receipt'][MARKER]['proofHash'] != context['proof']['proofHash'],
                    'applyConfirmationHash': None, 'financialActionPerformed': False}
                if basis is not None and row['state'] in ('pending', 'applied'):
                    # This quote never manufactures a result from the operation
                    # state. The original request result or applied row is its
                    # independent basis; current proof only scopes local apply.
                    payload['allowedActions'] = []
                    return self._cache_quote(context, pair, expected, intent, 'apply_saved_result', 'apply',
                        payload, lookup=outcome, result_fingerprint=basis)
                return self._read_response(context, pair, expected, payload, intent_id=data.get('intentId'))
        except (CloudError, ProfilePolicyError):
            self.clear(); raise
        except (StationError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            self.clear(); raise _error() from error
        except Exception:
            self.clear(); raise

    def _before_dispatch(self, intent, reserved, cached, confirmation, expected):
        # The transport has staged the original journal. Decode/check its same
        # identity here, outside the short SQL/session publication guards.
        journal = self.cloud.receiving.row(intent['requestKey'], expected, intent['kind'])
        if journal and 'result' in journal:
            raise _error('A saved result appeared before dispatch. Check and explicitly apply it.',
                         code='INTAKE_STOCK_ACTION_RESULT_AVAILABLE')
        journal_fields = {'kind', 'store', 'receipt_id', 'receiving_id', 'request_key', 'body',
                          'label_profile', 'fingerprint', 'created_at'}
        if (not journal or set(journal) != journal_fields or journal['kind'] != intent['kind']
            or journal['store'] != intent['store'] or journal['request_key'] != intent['requestKey']
            or journal['receipt_id'] != intent['stockReceiptId']
            or journal['receiving_id'] != intent['receivingId'] or canonical(journal['body']) != canonical(intent['body'])
            or canonical(journal['label_profile']) != canonical(intent['labelProfile'])
            or journal['fingerprint'] != fingerprint({'body': intent['body'], 'label_profile': intent['labelProfile']})
            or type(journal['created_at']) not in (int, float) or not math.isfinite(journal['created_at'])): raise _error()
        context = self._context(intent['stockReceiptId'], cached['revision'], expected)
        pair = self._pair(context, intent['intentId'])
        if (_row(pair['intent']) != _row(reserved) or pair['intent']['state'] != 'pending'
            or _row(pair['active']) != _row(reserved) or self._facts(context) != cached['facts']
            or self._mode(context, pair['intent'], intent) != cached['mode']): raise _error()
        self._eligible(intent, context, cached['mode'])
        self.projection._reauthorize(context, expected); session = self._session(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            self._assert_pair(context, pair, expected, session, intent_id=intent['intentId'])
            self._live(confirmation, cached)

    def _current(self, ident, expected, *, committed_body=None):
        # Internal continuation only. Public inputs never receive a revision
        # override: a failed call requires reload and a fresh current quote.
        local = self.projection._raw(ident)
        if committed_body is not None and local['raw'] != committed_body: raise _error()
        context = self._context(ident, local['receipt']['revision'], expected, allow_refresh=True)
        if context['child']['raw'] != local['raw']: raise _error()
        return context

    def _sync_observation(self, context, intent, result_fingerprint, expected):
        for _ in range(3):
            self._bound(intent, context)
            pair = self._pair(context, intent['intentId']); row = pair['intent']
            if (not row or row['intent_body'] != canonical(intent)
                or row['state'] != 'pending' and row['result_fingerprint'] != result_fingerprint): raise _error()
            prepared = self.projection.prepare_operation_observation(context)
            self.projection._reauthorize(context, expected); session = self._session(context, expected)
            with self.cloud._session_guard(), self.service.lock:
                self._assert_pair(context, pair, expected, session, intent_id=intent['intentId'])
                committed = projections.commit(self.service.store, prepared, context['expected'])
            context = self._current(intent['stockReceiptId'], expected, committed_body=committed['receipt_body'])
            self._bound(intent, context)
            if context['child']['receipt'][MARKER]['proofHash'] == context['proof']['proofHash']:
                return context
        raise _error('The same original operation is progressing. Reload and check its saved result again.',
                     code='INTAKE_STOCK_ACTION_PENDING')

    def _mark_applied(self, context, intent, result_fingerprint, expected):
        pair = self._pair(context, intent['intentId']); row = pair['intent']
        if row is None or row['intent_body'] != canonical(intent): raise _error()
        if row['state'] == 'acknowledged':
            if row['result_fingerprint'] != result_fingerprint: raise _error()
            return row
        prepared = actions.prepare_applied(**self._common(context, pair, expected), result_fingerprint=result_fingerprint)
        return self._commit(context, expected, prepared)

    def _complete(self, context, intent, result_fingerprint, journal_row, expected, *, request_dispatched=False):
        _hash(result_fingerprint); self._bound(intent, context)
        context = self._sync_observation(context, intent, result_fingerprint, expected)
        row = self._mark_applied(context, intent, result_fingerprint, expected)
        if row['state'] != 'acknowledged':
            # Journal work remains outside SQL/service/session publication
            # guards. Only the already-durable applied result permits absence.
            self.transport.acknowledge(intent, expected, applied_result_fingerprint=result_fingerprint,
                                       expected_journal_row=journal_row)
            context = self._current(intent['stockReceiptId'], expected)
            context = self._sync_observation(context, intent, result_fingerprint, expected)
            row = self._mark_applied(context, intent, result_fingerprint, expected)
            if row['state'] != 'acknowledged':
                pair = self._pair(context, intent['intentId'])
                prepared = actions.prepare_acknowledged(**self._common(context, pair, expected))
                row = self._commit(context, expected, prepared)
        return self._completed_response(context, row, expected, request_dispatched=request_dispatched)

    def _completed_response(self, context, row, expected, *, request_dispatched=False):
        pair = self._pair(context, row['intent_id'])
        if _row(pair['intent']) != _row(row): raise _error()
        payload = {**self._bindings(context), 'intent': self._public_intent(row),
            'operation': self._operation(context), 'requestDispatched': request_dispatched,
            'reloadRequired': True, 'financialActionPerformed': False}
        return self._read_response(context, pair, expected, payload, intent_id=row['intent_id'])

    def perform(self, data, expected):
        try:
            self._input(data, expected, 'perform')
            with self.cloud.lock:
                cached = self._cached(data, expected, 'perform'); intent = cached['intent']
                self._confirm_flags(data, intent, cached['mode'])
                context = self._context(data['stockReceiptId'], data['revision'], expected)
                pair = self._pair(context, intent['intentId']); row = pair['intent']
                self._bound(intent, context)
                if self._facts(context) != cached['facts']: raise _error()
                if row and row['intent_body'] != canonical(intent): raise _error()
                if row and row['state'] == 'acknowledged':
                    return self._completed_response(context, row, expected)
                if row and row['state'] != 'pending':
                    raise _error('Check and explicitly apply the saved result.', code='INTAKE_STOCK_ACTION_RESULT_AVAILABLE')
                if cached['pair']['intent'] is not None and _row(row) != cached['pair']['intent']: raise _error()
                if (cached['pair']['active'] is not None and _row(pair['active']) != cached['pair']['active']
                    or pair['active'] is not None and pair['active']['intent_id'] != intent['intentId']): raise _error()
                mode = self._mode(context, row, intent)
                if mode != cached['mode']: raise _error()
                self._eligible(intent, context, mode)
                prepared = actions.prepare_reservation(**self._common(context, pair, expected), intent=intent)
                reserved = self._commit(context, expected, prepared, reservation=True)
                outcome = self.transport.perform(intent, expected, dispatch_confirmed=True,
                    before_dispatch=lambda: self._before_dispatch(intent, reserved, cached, data['confirmationHash'], expected))
                if type(outcome) is not dict or set(outcome) != {'result', 'result_fingerprint', 'resolution', 'journal_row'}: raise _error()
                validate_result(outcome['result'], intent)
                if outcome['result_fingerprint'] != fingerprint(outcome['result']): raise _error()
                if outcome['resolution'] not in ('dispatched', 'journal', 'request_lookup'): raise _error()
                context = self._current(intent['stockReceiptId'], expected)
                return self._complete(context, intent, outcome['result_fingerprint'], outcome['journal_row'], expected,
                                      request_dispatched=outcome['resolution'] == 'dispatched')
        except (CloudError, ProfilePolicyError):
            self.clear(); raise
        except (StationError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            self.clear(); raise _error() from error
        except Exception:
            self.clear(); raise

    def apply_saved_result(self, data, expected):
        try:
            self._input(data, expected, 'apply')
            with self.cloud.lock:
                cached = self._cached(data, expected, 'apply'); intent = cached['intent']
                context = self._context(data['stockReceiptId'], data['revision'], expected, allow_refresh=True)
                pair = self._pair(context, intent['intentId']); row = pair['intent']
                if (not row or self._facts(context) != cached['facts']
                    or _row(pair['intent']) != cached['pair']['intent'] or _row(pair['active']) != cached['pair']['active']): raise _error()
                self._bound(intent, context)
                outcome = self._lookup(intent, expected, persist=False)
                basis = self._saved_basis(row, intent, outcome)
                if (basis is None or basis != cached['result_fingerprint']
                    or outcome['found'] != cached['lookup_found'] or outcome['journal_row'] != cached['journal_row']): raise _error()
                # Full quote/CAS before the first persistent journal merge. A
                # later source change still fails the B1/action publication CAS.
                self.projection._reauthorize(context, expected); session = self._session(context, expected)
                with self.cloud._session_guard(), self.service.lock:
                    self._assert_pair(context, pair, expected, session, intent_id=intent['intentId'])
                    self._live(data['confirmationHash'], cached)
                if row['state'] == 'pending' or outcome['journal_row'] is not None:
                    outcome = self._lookup(intent, expected, persist=True)
                    if not outcome['found'] or outcome['result_fingerprint'] != basis: raise _error()
                # Only an already-applied durable basis can enter completion
                # with no journal. No current operation state grants this path.
                if outcome['journal_row'] is None and row['state'] != 'applied': raise _error()
                return self._complete(context, intent, basis, outcome['journal_row'], expected)
        except (CloudError, ProfilePolicyError):
            self.clear(); raise
        except (StationError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            self.clear(); raise _error() from error
        except Exception:
            self.clear(); raise
