"""Whole-envelope receiving journal CAS shared by ordinary and restored actions.

Decrypting, parsing, hashing, encryption and staging occur before the short OS
guard. That guard compares the exact encrypted predecessor and current session;
it never spans a network request. A conflict reruns a typed per-key merge.
"""
import copy
import json
import os
from pathlib import Path
import stat
import threading
import uuid

from station_cloud import CloudError, ORIGIN
from station_store import StationError


def _error():
    return CloudError('The receiving journal changed on another app process. Keep the original action and retry its same key.',
                      status=409, pending=True, code='RECEIVING_JOURNAL_CHANGED')


class ReceivingJournalCAS:
    MAX_ATTEMPTS = 8

    def __init__(self, journal):
        self.journal = journal
        self.local = threading.local()

    @property
    def path(self):
        return Path(self.journal.vault.path)

    def _raw(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or self.path.is_symlink() or getattr(self.path, 'is_junction', lambda: False)():
            raise _error()
        try:return self.path.read_bytes()
        except FileNotFoundError:return None

    def snapshot(self):
        try:
            raw = self._raw()
            text = self.journal.vault.decode_bytes(raw) if raw is not None else ''
            rows = self.journal._parse_text(text)
        except CloudError:
            raise
        except (StationError, UnicodeError, OSError):
            raise CloudError('The saved hosted-receiving journal could not be opened. Keep the original action for owner recovery on this Windows account; do not replace it or send the batch through a local Square connection.',
                             status=409, pending=True) from None
        return raw, rows

    def read(self):
        raw, rows = self.snapshot()
        self.local.previous = raw
        self.local.has_previous = True
        return rows

    def _stage(self, rows):
        if not rows:
            return None
        text = json.dumps({'version': 1, 'origin': ORIGIN, 'operations': rows}, ensure_ascii=False, separators=(',', ':'))
        # Reuse the historical schema/fingerprint checks before any publication.
        self.journal._parse_text(text)
        encoded = self.journal.vault.prepare_bytes(text)
        if self.journal.vault.decode_bytes(encoded) != text:
            raise StationError('Credential verification failed.')
        stage = self.path.parent / ('.receiving-journal-' + uuid.uuid4().hex + '.stage')
        try:
            descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            stage.unlink(missing_ok=True)
            raise
        return stage

    def _commit(self, previous, stage, expected, saved):
        cloud = self.journal.cloud
        # All bytes and replacement data are prepared before this guard.
        with cloud._session_guard():
            current = cloud.assert_recovery_session_unlocked(expected, saved.get('tenant_id'), saved['store'])
            if current.get('role') not in ('owner', 'manager'):
                raise CloudError('An owner or manager must review hosted receiving.', status=403)
            if self._raw() != previous:
                return False
            if stage is None:
                if previous is not None:
                    self.path.unlink()
            else:
                os.replace(stage, self.path)
            return True

    def update(self, change, expected):
        """Rerun a detached, side-effect-free per-key merge on CAS conflicts."""
        saved = self.journal.session(expected)
        expected = saved['session_id']
        for _ in range(self.MAX_ATTEMPTS):
            previous, rows = self.snapshot()
            updated, result = change(copy.deepcopy(rows), saved)
            # Even a no-op needs the same session/raw predecessor check.
            unchanged = (json.dumps(updated,sort_keys=True,separators=(',',':'),ensure_ascii=False)
                         == json.dumps(rows,sort_keys=True,separators=(',',':'),ensure_ascii=False))
            stage = None if unchanged else self._stage(updated)
            try:
                if unchanged:
                    cloud = self.journal.cloud
                    with cloud._session_guard():
                        current = cloud.assert_recovery_session_unlocked(expected, saved.get('tenant_id'), saved['store'])
                        if current.get('role') not in ('owner', 'manager'):
                            raise CloudError('An owner or manager must review hosted receiving.', status=403)
                        committed = self._raw() == previous
                else:
                    committed = self._commit(previous, stage, expected, saved)
                if committed:
                    return result
            finally:
                if stage is not None:
                    stage.unlink(missing_ok=True)
        raise _error()

    def save(self, rows, expected=None):
        """Legacy full replacement requires this thread's exact preceding read.

        Typed writers use update(), which can merge on conflicts. A bare full
        replacement cannot infer which concurrent rows its caller meant to keep.
        """
        if not getattr(self.local, 'has_previous', False):
            raise _error()
        saved = self.journal.session(expected)
        expected = saved['session_id']
        stage = self._stage(copy.deepcopy(rows))
        try:
            if not self._commit(self.local.previous, stage, expected, saved):
                raise _error()
            self.local.has_previous = False
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
