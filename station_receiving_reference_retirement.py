"""Explicit retirement of an unreserved browser reminder, with no provider POST.

The feature is inactive until its profile capability and service/routes are
deliberately enabled. A reminder supplies no request body or request outcome.
The immutable fence prevents a later reservation with either original identity.
"""
import copy
import hashlib
import secrets
import stat
from urllib.parse import quote

from hosted_labels import fingerprint
from station_cloud import CloudError, ORIGIN
from station_receiving_restore import MARKER, _id, _integer
from station_store import now, StationError
import station_receiving_action_registry as intents
import station_receiving_reference_fences as fences


FEATURE = 'original_stock_reference_fences_v1'
RESOLUTION = 'local_reference_retired'


def _error(message='Keep this saved reminder for owner review. No request body or result has been reconstructed.',
           *, code='ORIGINAL_STOCK_REFERENCE_CHANGED', status=409):
    return CloudError(message, code=code, status=status, pending=True)


class OriginalReceivingReferenceRetirement:
    def __init__(self, service):
        self.service = service
        self.actions = service.receiving_actions
        self._previews = {}
        # Same preview lock/clock discipline as the action coordinator; no lock
        # is held across a network request or journal decoding.
        import threading
        self._lock = threading.RLock()

    @property
    def cloud(self):
        return self.service.cloud

    def clear(self):
        with self._lock:
            self._previews.clear()

    def _enabled(self):
        self.service.profile.require_feature(FEATURE)

    def _input(self, data, expected, kind):
        allowed = {'stockReceiptId', 'revision', 'reference', 'sessionId'}
        if kind == 'retire':
            allowed |= {'confirmationHash', 'retireLocalReferenceConfirmed'}
        try:
            if (type(data) is not dict or set(data) - allowed
                or type(expected) is not str or not 1 <= len(expected) <= 200
                or data.get('sessionId', expected) != expected):
                raise ValueError()
            _id(data.get('stockReceiptId'))
            if kind != 'status' or 'revision' in data:
                _integer(data.get('revision'), 1)
            fences._reference(data.get('reference'))
            if data['reference']['stockReceiptId'] != data['stockReceiptId']:
                raise ValueError()
            if kind == 'retire':
                value = data.get('confirmationHash')
                if (data.get('retireLocalReferenceConfirmed') is not True
                    or type(value) is not str or len(value) != 64
                    or any(char not in '0123456789abcdef' for char in value)):
                    raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise _error('Review the exact saved reminder and explicitly confirm its retirement.',
                         code='ORIGINAL_STOCK_REFERENCE_INPUT', status=400) from None

    def _context(self, data, expected, *, require_current=False):
        self.actions._stop()
        self._enabled()
        context = self.actions._context(data['stockReceiptId'], data.get('revision'), expected,
                                        require_current=require_current)
        ref = data['reference']
        bound = {'serviceOrigin': ORIGIN, 'tenantId': context['bundle']['tenantId'],
                 'store': context['bundle']['store'], 'stockReceiptId': context['child']['receipt']['id'],
                 'receivingId': context['proof']['receivingId'],
                 'serverRevision': context['proof']['serverRevision'], 'planHash': context['proof']['planHash']}
        if any(type(ref[key]) is not type(value) or ref[key] != value for key, value in bound.items()):
            raise _error()
        self.actions._session(context, expected)
        return context

    def _sql_absence(self, context, reference, *, include_active=True):
        tenant = context['bundle']['tenantId']
        with self.service.store.db() as db:
            if not fences.inspect_database(db):
                raise _error('This app profile needs its supported recovery upgrade before retiring a reminder.',
                             code='ORIGINAL_STOCK_REFERENCE_UPGRADE_REQUIRED')
            for field, value in (('intent_id', reference['intentId']), ('request_key', reference['requestKey'])):
                if db.execute('SELECT 1 FROM ' + intents.TABLE +
                              ' WHERE service_origin=? AND tenant_id=? AND ' + field + '=? LIMIT 1',
                              (ORIGIN, tenant, value)).fetchone():
                    raise _error('This reminder has a saved stock action. Check that original saved update instead.',
                                 code='ORIGINAL_STOCK_REFERENCE_HAS_ACTION')
            if include_active and db.execute('SELECT 1 FROM ' + intents.TABLE +
                    " WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND state IN ('pending','applied') LIMIT 1",
                    (ORIGIN, tenant, reference['receivingId'])).fetchone():
                raise _error('Check the other saved action for this original stock operation first.',
                             code='ORIGINAL_STOCK_REFERENCE_HAS_ACTION')

    def _journal_raw(self):
        """This feature's bounded bytes, without changing the legacy journal cap."""
        path = self.cloud.receiving.storage.path
        try:
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
                or getattr(path, 'is_junction', lambda: False)()
                or getattr(before, 'st_file_attributes', 0) & 0x400
                or before.st_size > fences.MAX_JOURNAL_BYTES):
                raise _error('This saved receiving journal needs owner review before reminder retirement.')
            with path.open('rb') as stream:
                raw = stream.read(fences.MAX_JOURNAL_BYTES + 1)
            after = path.lstat()
            if (len(raw) > fences.MAX_JOURNAL_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                   != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise _error('The receiving journal changed or needs owner review. Keep its saved bytes.')
            return raw
        except FileNotFoundError:
            return None
        except OSError:
            raise _error('The saved receiving journal could not be read. Keep it for owner recovery.') from None

    def _journal(self, reference, *, include_operation=True):
        previous = self._journal_raw()
        journal = self.cloud.receiving
        try:
            text = journal.vault.decode_bytes(previous) if previous is not None else ''
            rows = journal._parse_text(text)
        except CloudError:
            raise
        except (StationError, UnicodeError, OSError):
            raise _error('The saved receiving journal could not be opened. Keep it for owner recovery.') from None
        for row in rows:
            if row['store'] == reference['store'] and (
                row['request_key'] == reference['requestKey']
                or include_operation and (row['receiving_id'] == reference['receivingId']
                                          or row['receipt_id'] == reference['stockReceiptId'])):
                raise _error('A saved receiving journal entry still belongs to this update. Keep it for original-request recovery.',
                             code='ORIGINAL_STOCK_REFERENCE_HAS_JOURNAL')
        return previous

    def _lookup(self, context, reference, expected):
        self.actions.restoration._reauthorize(context, expected)
        saved = self.cloud._saved()
        self.cloud._expected(saved, expected)
        if (saved.get('tenant_id') != reference['tenantId'] or saved.get('store') != reference['store']
            or saved.get('role') not in ('owner', 'manager')):
            raise _error(status=403)
        result = self.cloud._call('GET', '/api/receiving/requests/' + quote(reference['requestKey'], safe=''),
                                  token=saved['token'])
        if (type(result) is not dict or set(result) != {'found', 'result'}
            or type(result['found']) is not bool):
            raise _error('The original request lookup is uncertain. Keep this reminder.', status=502)
        if result['found'] is not False or result['result'] is not None:
            raise _error('The original request has evidence to review. Keep its reference and recover that evidence.',
                         code='ORIGINAL_STOCK_REFERENCE_RESULT_PRESENT')
        return now()

    @staticmethod
    def _journal_hash(previous):
        return hashlib.sha256(previous).hexdigest() if previous is not None else None

    def _prepare(self, data, expected):
        reference = data['reference']
        context = self._context(data, expected, require_current=True)
        self._sql_absence(context, reference)
        previous = self._journal(reference)
        observed_at = self._lookup(context, reference, expected)
        # The lookup is not authority for the selected child or current role.
        # Refresh both without silently changing the saved child.
        context = self._context(data, expected, require_current=True)
        self._sql_absence(context, reference)
        args = self.actions._registry_args(context, expected)
        evidence = {'version': 1, 'resolutionKind': RESOLUTION, 'bodyAvailable': False,
                    'requestResultObserved': False, 'requestAttribution': 'unknown',
                    'lookupHash': hashlib.sha256(fences.canonical({'found': False, 'result': None}).encode()).hexdigest(),
                    'lookupObservedAt': observed_at, 'currentProofHash': context['proof']['proofHash'],
                    'restorationSourceHash': context['proof']['restorationSourceHash'],
                    'stateBasisHash': context['bundle']['restorationSource']['stateBasisHash'],
                    'operationHash': context['snapshot']['operation_snapshot_hash'],
                    'journalMatchingRowAbsent': True, 'journalPredecessorHash': self._journal_hash(previous)}
        prepared = fences.prepare_retirement(self.service.store, reference=reference, evidence=evidence,
                                             expected_journal_predecessor=previous, **args)
        facts = {'context': self.actions._facts(context), 'reference': fingerprint(reference),
                 'journalPredecessorHash': self._journal_hash(previous)}
        return context, prepared, facts

    def _final(self, context, reference, expected, *, previous, fence_row,
               require_absence=True, include_active=True):
        """Caller holds common OS guard then service lock. No decoding/hashing."""
        self.actions._stop()
        self._enabled()
        self.actions.restoration._final(context, expected)
        session = self.actions._session(context, expected, unlocked=True)
        if require_absence:
            if self._journal_raw() != previous:
                raise _error('The receiving journal changed. Check the same saved reminder again.')
            self._sql_absence(context, reference, include_active=include_active)
        with self.service.store.db() as db:
            if not fences.inspect_database(db):
                raise _error()
            actual = fences._select(db, reference['tenantId'], reference['intentId'], reference['requestKey'])
            if actual != fence_row:
                raise _error()
        return session

    def _purge(self):
        stamp = self.actions._time()
        self._previews = {key: value for key, value in self._previews.items() if value['expires'] > stamp}
        return stamp

    def preview(self, data, expected):
        self._input(data, expected, 'preview')
        context, prepared, facts = self._prepare(data, expected)
        confirmation = fingerprint({'domain': 'stock-and-sleeve:original-reference-retirement:v1',
                                    'random': secrets.token_hex(32), 'facts': facts})
        self.actions.restoration._reauthorize(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            self._final(context, data['reference'], expected,
                        previous=prepared.expected_journal_predecessor, fence_row=prepared.expected_fence)
            with self._lock:
                stamp = self._purge()
                while len(self._previews) >= 16:
                    self._previews.pop(next(iter(self._previews)))
                self._previews[confirmation] = {'reference': copy.deepcopy(data['reference']),
                    'facts': facts, 'sessionId': expected, 'expires': stamp + 300}
        return {'preview': {'version': 1, 'resolutionKind': RESOLUTION,
                    'reference': copy.deepcopy(data['reference']), 'bodyAvailable': False,
                    'requestResultObserved': False, 'requestAttribution': 'unknown',
                    'effect': 'retire_local_reminder_only', 'alreadyRetired': prepared.expected_fence is not None},
                'confirmationHash': confirmation, 'expiresIn': 300, 'review': self.actions._review(context)}

    def retire(self, data, expected):
        self._input(data, expected, 'retire')
        self._enabled()
        with self._lock:
            self._purge()
            cached = copy.deepcopy(self._previews.get(data['confirmationHash']))
        if (not cached or cached['sessionId'] != expected or cached['reference'] != data['reference']):
            raise _error('This reminder review expired. Review its same reference again.',
                         code='ORIGINAL_STOCK_REFERENCE_PREVIEW_EXPIRED')
        context, prepared, facts = self._prepare(data, expected)
        if facts != cached['facts']:
            raise _error()
        self.actions.restoration._reauthorize(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            session = self._final(context, data['reference'], expected,
                                 previous=prepared.expected_journal_predecessor, fence_row=prepared.expected_fence)
            with self._lock:
                self._purge()
                if self._previews.get(data['confirmationHash']) != cached:
                    raise _error(code='ORIGINAL_STOCK_REFERENCE_PREVIEW_EXPIRED')
            fences.commit_retirement(self.service.store, prepared, current_session_facts=session,
                current_journal_predecessor=prepared.expected_journal_predecessor, journal_absent_attested=True)
        # Fresh protected status can fail after a role/selection change; the
        # committed fence remains durable and can be read after sign-in.
        return self.status({'stockReceiptId': data['stockReceiptId'], 'reference': data['reference']}, expected)

    def status(self, data, expected):
        self._input(data, expected, 'status')
        context = self._context(data, expected)
        reference = data['reference']
        row = fences.read_fence(self.service.store, reference['tenantId'], intent_id=reference['intentId'])
        other = fences.read_fence(self.service.store, reference['tenantId'], request_key=reference['requestKey'])
        if row != other:
            raise _error()
        if row is not None and (row['reference_body'] != fences.canonical(reference)
            or row['financial_receipt_id'] != context['child']['receipt'][MARKER]['financialReceiptId']):
            raise _error()
        # A later different action B may be active. It does not invalidate A's
        # already-durable fence. Contradictory saved SQL/journal evidence for A
        # itself still requires owner review and must not clear its reminder.
        previous = None
        if row is not None:
            self._sql_absence(context, reference, include_active=False)
            previous = self._journal(reference, include_operation=False)
        expected_row = tuple(row[key] for key in fences.FIELDS) if row else None
        result = {'found': row is not None, 'retirement': None,
                  'requestDispatched': False, 'financialActionPerformed': False,
                  'review': self.actions._review(context)}
        if row is not None:
            result['retirement'] = {'version': 1, 'reference': copy.deepcopy(reference),
                'referenceFingerprint': row['reference_fingerprint'], 'evidenceFingerprint': row['evidence_fingerprint'],
                'resolutionKind': RESOLUTION, 'bodyAvailable': False,
                'requestResultObserved': False, 'requestAttribution': 'unknown'}
        self.actions.restoration._reauthorize(context, expected)
        with self.cloud._session_guard(), self.service.lock:
            self._final(context, reference, expected, previous=previous, fence_row=expected_row,
                        require_absence=row is not None, include_active=False)
        return result
