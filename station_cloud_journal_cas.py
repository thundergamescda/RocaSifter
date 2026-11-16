"""Typed pending/intake merges for cooperating current CloudClients.

Journal crypto, validation, hashing and staging are detached from the short
session OS guard. No network or local receipt/financial commit occurs here.
This does not fence an already-running pre-CAS writer or migrate old profiles.
"""
import copy
import json
import math
import os
from pathlib import Path
import stat
import threading
import uuid

from station_cloud import CloudError, ORIGIN, ROLES
from station_store import StationError


def _changed():
    return CloudError('The saved action journal changed. Keep the original request and recover its same key.',
                      status=409, pending=True, code='CLOUD_JOURNAL_CHANGED')


class CloudJournalCAS:
    MAX_ATTEMPTS = 8

    def __init__(self, cloud, channel):
        if channel not in ('pending', 'intake'):
            raise ValueError('Unsupported cloud journal.')
        self.cloud, self.channel = cloud, channel
        self.local = threading.local()

    @property
    def vault(self):
        return self.cloud.pending_vault if self.channel == 'pending' else self.cloud.intake_vault

    @property
    def path(self):
        return Path(self.vault.path)

    def session(self, expected=None):
        self.cloud._cancel_guard()
        saved = self.cloud._saved()
        self.cloud._expected(saved, expected)
        if (not saved or type(saved.get('expires_at')) not in (int, float)
                or not math.isfinite(saved['expires_at']) or saved['expires_at'] <= self.cloud.clock()):
            raise CloudError('Sign in to the store that owns this saved action.', status=401)
        if self.channel == 'intake' and saved['role'] not in ('owner', 'manager'):
            raise CloudError('An owner or manager must identify the central physical intake.', status=403)
        return dict(saved)

    def _parse(self, text):
        parser = self.cloud._parse_pending_text if self.channel == 'pending' else self.cloud._parse_intake_text
        return parser(text)

    def _raw(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(info.st_mode) or self.path.is_symlink()
                or getattr(self.path, 'is_junction', lambda: False)()
                or bool(getattr(info, 'st_file_attributes', 0) & 0x400)):
            raise _changed()
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def snapshot(self):
        try:
            previous = self._raw()
            text = self.vault.decode_bytes(previous) if previous is not None else ''
            return previous, self._parse(text)
        except CloudError:
            raise
        except (StationError, UnicodeError, OSError, ValueError):
            message=('The saved physical-intake journal could not be opened. Keep the original saved action for owner recovery and use the same Windows account; do not create a replacement intake.'
                     if self.channel == 'intake' else
                     'The saved pending-action journal could not be opened. Keep its original files and use the same Windows account for owner recovery.')
            raise CloudError(message,
                             status=409, pending=True) from None

    def read(self):
        previous, rows = self.snapshot()
        self.local.previous = previous
        self.local.has_previous = True
        return rows

    @staticmethod
    def binding(row):
        return {name: copy.deepcopy(row.get(name)) for name in
                ('kind', 'store', 'request_key', 'customer_id', 'trade_id', 'receipt_id', 'fingerprint')}

    @staticmethod
    def _identity(row):
        return row['store'], row['request_key']

    def _matching(self, rows, binding):
        matches = [row for row in rows if self._identity(row) == (binding['store'], binding['request_key'])]
        if len(matches) > 1:
            raise _changed()
        if matches and self.binding(matches[0]) != binding:
            raise _changed()
        return matches[0] if matches else None

    def _check_stage(self, prepared):
        path, identity = prepared
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
                or (info.st_dev, info.st_ino) != identity):
            raise _changed()

    def _discard(self, prepared):
        if prepared is None:
            return
        path, _ = prepared
        try:self._check_stage(prepared)
        except FileNotFoundError:return
        path.unlink()

    def _stage(self, rows):
        if not rows:
            return None
        text = json.dumps({'version': 1, 'origin': ORIGIN, 'operations': rows}, separators=(',', ':'))
        self._parse(text)
        encoded = self.vault.prepare_bytes(text)
        if self.vault.decode_bytes(encoded) != text:
            raise StationError('Credential verification failed.')
        path = self.path.parent / ('.cloud-' + self.channel + '-' + uuid.uuid4().hex + '.stage')
        prepared = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                info = os.fstat(stream.fileno())
                prepared = (path, (info.st_dev, info.st_ino))
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            return prepared
        except BaseException:
            self._discard(prepared)
            raise

    def _commit(self, previous, prepared, saved):
        # The same guard also serializes sign-out/replacement. Its bounded
        # existing session/policy reader is not a journal crypto/parse operation.
        with self.cloud._session_guard():
            current = self.cloud.assert_recovery_session_unlocked(saved['session_id'], saved.get('tenant_id'), saved['store'])
            if current.get('role') not in ROLES:
                raise _changed()
            if self.channel == 'intake' and current['role'] not in ('owner', 'manager'):
                raise CloudError('An owner or manager must identify the central physical intake.', status=403)
            if self._raw() != previous:
                return False
            if prepared is None:
                if previous is not None:
                    self.path.unlink()
            else:
                self._check_stage(prepared)
                os.replace(prepared[0], self.path)
            return True

    def _update(self, action, original, saved, result=None):
        # No user callback or operation executes while a storage guard is held.
        original = self.original(original, saved)
        binding = self.binding(original)
        result = copy.deepcopy(result)
        result_fingerprint = self.cloud._fingerprint(result) if action == 'remember' else None
        for _ in range(self.MAX_ATTEMPTS):
            previous, rows = self.snapshot()
            updated = copy.deepcopy(rows)
            existing = self._matching(updated, binding)
            output = None
            if action == 'reserve':
                if existing is not None:
                    output = (copy.deepcopy(existing), False)
                else:
                    self.cloud._preflight_body(original['body'])
                    conflicts = [row for row in updated if row['store'] == saved['store']]
                    allowed_void = (self.channel == 'pending' and original['kind'] == 'trade_void' and conflicts
                        and all(row['kind'] == 'trade_accept' and row.get('trade_id') == original.get('trade_id')
                                and row.get('receipt_id') == original.get('receipt_id') for row in conflicts))
                    if conflicts and not allowed_void:
                        raise CloudError('Resolve the saved pending action in this store before starting another.', status=409, pending=True)
                    if len(updated) >= 50:
                        raise CloudError('Recover the saved actions on this computer before continuing.', status=409, pending=True)
                    updated.append(copy.deepcopy(original))
                    output = (copy.deepcopy(original), True)
            elif action == 'remember':
                if existing is None or type(result) is not dict:
                    raise _changed()
                if 'result' in existing:
                    if existing.get('result_fingerprint') != result_fingerprint or existing['result'] != result:
                        raise _changed()
                else:
                    existing['result'] = copy.deepcopy(result)
                    existing['result_fingerprint'] = result_fingerprint
                output = copy.deepcopy(result)
            elif action == 'finish':
                if existing is not None:
                    updated.remove(existing)
            else:
                raise ValueError('Unsupported journal merge.')
            # Preserve exact bytes on a no-op, but still validate current session
            # and predecessor. All serialization/encryption is outside the guard.
            unchanged = json.dumps(updated, sort_keys=True, separators=(',', ':')) == json.dumps(rows, sort_keys=True, separators=(',', ':'))
            prepared = None if unchanged else self._stage(updated)
            try:
                if unchanged:
                    with self.cloud._session_guard():
                        current = self.cloud.assert_recovery_session_unlocked(saved['session_id'], saved.get('tenant_id'), saved['store'])
                        if self.channel == 'intake' and current['role'] not in ('owner', 'manager'):
                            raise CloudError('An owner or manager must identify the central physical intake.', status=403)
                        committed = self._raw() == previous
                else:
                    committed = self._commit(previous, prepared, saved)
                if committed:
                    return output
            except CloudError as error:
                if action in ('remember', 'finish'):
                    error.pending = True
                raise
            finally:
                self._discard(prepared)
        raise _changed()

    def reserve(self, row, saved):
        return self._update('reserve', copy.deepcopy(row), saved)

    def remember(self, row, result, saved):
        return self._update('remember', row, saved, result)

    def finish(self, row, saved):
        return self._update('finish', row, saved)

    def original(self, row, saved, *, key=None, trade_only=False):
        """Validate the caller's detached pre-I/O row, never a replacement row."""
        row = copy.deepcopy(row)
        if (not isinstance(row, dict) or row.get('store') != saved['store']
                or key is not None and row.get('request_key') != key
                or trade_only and not str(row.get('kind', '')).startswith('trade_')):
            raise _changed()
        self._parse(json.dumps({'version': 1, 'origin': ORIGIN, 'operations': [row]},
                               sort_keys=True, separators=(',', ':')))
        return row

    def finish_key(self, key, saved, *, trade_only=False, original_row=None):
        if original_row is not None:
            return self.finish(self.original(original_row, saved, key=key, trade_only=trade_only), saved)
        previous, rows = self.snapshot()
        matches = [row for row in rows if self._identity(row) == (saved['store'], key)]
        # None is a captured absence, not permission to adopt the latest row.
        if matches:
            raise _changed()
        # A missing row still needs final current-authority/predecessor proof.
        with self.cloud._session_guard():
            current = self.cloud.assert_recovery_session_unlocked(saved['session_id'], saved.get('tenant_id'), saved['store'])
            if self.channel == 'intake' and current['role'] not in ('owner', 'manager'):
                raise CloudError('An owner or manager must identify the central physical intake.', status=403)
            if self._raw() != previous:
                raise _changed()

    def save(self, rows, expected=None):
        """Strict compatibility replacement; never used by typed production writes."""
        if not getattr(self.local, 'has_previous', False):
            raise _changed()
        saved = self.session(expected)
        prepared = self._stage(copy.deepcopy(rows))
        try:
            if not self._commit(self.local.previous, prepared, saved):
                raise _changed()
            self.local.has_previous = False
        finally:
            self._discard(prepared)
