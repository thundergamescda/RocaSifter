"""Durable, local ordinary-receipt label pairs; no inventory/provider writes.

The receipt JSON is the journal. Prepared stage identities precede publication;
only a CAS-committed, hash-verified pair is downloadable. This is deliberately
separate from original trade recovery and shared intake label delivery.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from station_store import StationError, now

PREFIX = 'ss-receipt-label-'
MAX_BYTES = 32 * 1024 * 1024
MAX_JOBS = 24
MAX_ATTEMPTS = 3
LEASE_SECONDS = 180
_LOCAL = threading.RLock()


def _fail(message):
    raise StationError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        _fail('Use the original label job identifier.')
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail('Choose a valid bounded label copy range.')
    return value


def _identity(info):
    return [info.st_dev, info.st_ino]


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _path(value, *, directory=False, create=False):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        _fail('The private label output path is invalid.')
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if item != path or not create:
                _fail('The private label output directory is unavailable.')
            item.mkdir(mode=0o700)
            info = item.lstat()
        if _reparse(info):
            _fail('Label output cannot follow symlinks or reparse points.')
    info = path.lstat()
    if path.resolve(strict=True) != path or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        _fail('The private label output path is invalid.')
    return path


def _read(path, expected=None):
    path = _path(path)
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if _identity(before) != _identity(opened) or not stat.S_ISREG(opened.st_mode):
            _fail('The label file changed while opening it.')
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (not 1 <= len(data) <= MAX_BYTES or _identity(path.lstat()) != _identity(opened)
            or after.st_size != len(data) or opened.st_mtime_ns != after.st_mtime_ns):
        _fail('The label file is incomplete or too large.')
    meta = {'identity': _identity(opened), 'bytes': len(data), 'mtimeNs': after.st_mtime_ns,
            'sha256': hashlib.sha256(data).hexdigest()}
    if expected is not None and meta != expected:
        _fail('The saved label pair no longer matches its original files. Keep this job for recovery.')
    return data, meta


def _unchanged_file(path, expected):
    """Short identity fence only; complete byte/hash reads happen detached."""
    info = _path(path).lstat()
    if (_identity(info) != expected['identity'] or info.st_size != expected['bytes']
            or info.st_mtime_ns != expected['mtimeNs']):
        _fail('The prepared label file changed before publication.')


def _sync_directory(path):
    # Windows file fsync is used below; directory fsync is a POSIX durability
    # primitive. Unsupported directory fsync fails publication on POSIX.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class OrdinaryLabels:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def _roots(self):
        output = _path(Path(self.service.core.OUTPUT_DIR).absolute(), directory=True, create=True)
        stage = _path(output / '.ss-receipt-label-stage', directory=True, create=True)
        return output, stage

    def _policy(self):
        return {'policy': self.service.profile.safe_snapshot(), 'binding': self.service.profile.binding()}

    def _authority(self, receipt, expected):
        policy = self._policy()
        intent = receipt.get('hosted_receive') or {}
        from station_intake_claim import centrally_bound
        if policy['policy']['profileMode'] == 'legacy_compatibility' and not intent and not centrally_bound(receipt):
            self.service.profile.require_legacy('ordinary local label publication')
            return {'scope': {'profile': policy, 'kind': 'legacy'}, 'saved': None}
        cloud = self.service.cloud
        with cloud.lock:
            saved = cloud._saved()
            if not expected:
                _fail('Sign in to this store before creating or opening managed labels.')
            cloud._expected(saved, expected)
            me = cloud._authenticated('GET', '/api/me', expected_session_id=expected)
            if (not saved or me.get('role') != saved['role'] or me.get('store') != saved['store']
                    or saved.get('tenant_id') is not None and me.get('tenant_id') != saved['tenant_id']):
                _fail('The staff role changed. Sign in again before opening labels.')
            if intent.get('state') == 'confirmed':
                proof = self.service.hosted_receiving.label_authority(receipt, expected)
                kind = {'kind': 'hosted_confirmed', 'source': proof}
            else:
                if centrally_bound(receipt) or intent or receipt.get('inventory_intent') != 'existing':
                    _fail('Recover and confirm this original hosted receiving operation before creating labels.')
                kind = {'kind': 'local_existing'}
            return {'scope': {'profile': policy, **kind}, 'saved': copy.deepcopy(saved)}

    @contextmanager
    def _fence(self, authority):
        # Never hold this OS session guard across network I/O or rendering.
        with self.service.profile.cloud_session_guard():
            if self._policy() != authority['scope']['profile']:
                _fail('The station profile changed. Keep this label job and reopen its original profile.')
            saved = authority['saved']
            if saved is not None:
                current = self.service.cloud._saved_unlocked()
                self.service.cloud._expected(current, saved['session_id'])
                if (not current or any(current.get(k) != saved.get(k) for k in ('token', 'store', 'tenant_id', 'role'))
                        or type(current.get('expires_at')) not in (int, float)
                        or not math.isfinite(current['expires_at']) or current['expires_at'] <= self.service.cloud.clock()):
                    _fail('The signed-in workspace changed. Keep this label job and sign in again.')
            with self.service.lock:
                yield

    def _capture(self, ident, layout, dpi, offset, count, expected):
        with self.service.lock:
            receipt = self.service.active_receipt(ident)
            from station_intake_claim import require_ordinary_receipt
            require_ordinary_receipt(receipt)
            if receipt.get('mode') == 'bulk':
                _fail('Move this receipt to Price & label before printing retail labels.')
            before_authority = self.store.receipt_compare_body(receipt)
        # Prime this family's real accepted-quantity proof before reconstructing
        # marks (important after restart). Never fetch it under service.lock.
        authority = self._authority(receipt, expected)
        with self.service.lock:
            current = self.service.active_receipt(ident)
            if self.store.receipt_compare_body(current) != before_authority:
                _fail('This receipt changed while its current label authority was read.')
            receipt = current
            cards = self.service.approved_cards(receipt)
            config = self.service.preferences()
            from hosted_labels import profile
            chosen = profile({'stock': '2x1', 'layout': layout or config['labelLayout'],
                              'dpi': config['printerDpi'] if dpi is None else dpi})
            hosted = receipt.get('hosted_receive', {}).get('state') == 'confirmed'
            if hosted:
                frozen = profile(receipt['hosted_receive']['label_profile'])
                if (chosen['layout'], chosen['dpi']) != (frozen['layout'], frozen['dpi']):
                    _fail('Use the label stock, layout and DPI frozen in this confirmed receiving receipt.')
                chosen = frozen
            if not 1 <= len(cards) <= 1000:
                _fail('Choose at most 1,000 reviewed rows per local label receipt.')
            quantities = {}
            for card in cards:
                ident_row = _uuid(card.get('row_id'))
                if ident_row in quantities:
                    _fail('Label rows must retain unique original identities.')
                quantities[ident_row] = _integer(card.get('qty'), 1, 100000)
                for field in ('card_name', 'card_set', 'card_num', 'condition', 'printing', 'sku'):
                    value = card.get(field, '')
                    if type(value) is not str or len(value) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                        _fail('Reviewed label fields exceed the bounded local renderer contract.')
            total = _integer(sum(quantities.values()), 1, 1000000)
            offset = _integer(offset, 0, total - 1)
            count = total - offset if count is None else _integer(count, 1, total - offset)
            if hosted:
                from station_receiving_label_plan import build_plan, part
                plan = build_plan(cards, chosen, offset, count)
                if plan['partCount'] != 1:
                    _fail('Choose a smaller explicit copy range; this pair exceeds the bounded label part size.')
                counts = part(plan, 0)['renderCounts']
            else:
                from station_receiving_label_plan import budget_for, RESERVED_BYTES
                budget = budget_for(chosen)
                limit = min(1000, (MAX_BYTES - RESERVED_BYTES) // budget['maxZplRowBytes'])
                if count > limit:
                    _fail('Choose a smaller explicit copy range; this pair exceeds the bounded label part size.')
                cursor = 0
                counts = {}
                for row in sorted(quantities):
                    upper = cursor + quantities[row]
                    selected = max(0, min(upper, offset + count) - max(cursor, offset))
                    if selected:
                        counts[row] = selected
                    cursor = upper
            original = copy.deepcopy(receipt)
            for key in ('revision', 'updated', 'label_jobs', 'label_job_current'):
                original.pop(key, None)
            original['files'] = {k: v for k, v in original.get('files', {}).items() if k not in ('pdf', 'zpl')}
        terms = {'profile': chosen, 'copyOffset': offset, 'copyCount': count, 'totalCopies': total,
                 'renderCounts': counts, 'renderer': 'hosted_confirmed' if hosted else 'legacy',
                 'basisHash': _hash({'receipt': original, 'cards': cards, 'scope': authority['scope']})}
        return receipt, cards, terms, authority

    @staticmethod
    def _jobs(receipt):
        jobs = receipt.get('label_jobs', {})
        if type(jobs) is not dict or len(jobs) > MAX_JOBS or len(_json(jobs).encode()) > 2 * 1024 * 1024:
            _fail('The saved label job journal needs recovery. No existing output was replaced.')
        for ident, job in jobs.items():
            _uuid(ident)
            if (type(job) is not dict or job.get('version') != 1 or job.get('jobId') != ident
                    or job.get('state') not in ('rendering', 'failed', 'prepared', 'published')
                    or type(job.get('terms')) is not dict or len(_json(job).encode()) > 256 * 1024
                    or type(job.get('priorAttempts')) is not list or len(job['priorAttempts']) >= MAX_ATTEMPTS
                    or type(job.get('leaseUntil')) not in (int, float) or not math.isfinite(job['leaseUntil'])
                    or job['leaseUntil'] < 0):
                _fail('The saved label job journal needs recovery. Its files were preserved.')
            _uuid(job.get('attempt'))
            for previous in job['priorAttempts']:
                _uuid(previous)
        return jobs

    def _current(self, ident, terms, layout, dpi, expected):
        receipt, cards, fresh, authority = self._capture(ident, layout, dpi, terms['copyOffset'], terms['copyCount'], expected)
        if fresh != terms:
            _fail('The reviewed receipt, exact SKU, quantity, source or profile changed during label preparation.')
        return receipt, cards, authority

    def _prepared(self, receipt, job_id, job, state):
        current = copy.deepcopy(receipt)
        current.setdefault('label_jobs', {})[job_id] = copy.deepcopy(job)
        current['label_job_current'] = job_id
        if state in ('published', 'selected'):
            current.setdefault('files', {}).update({k: job['files'][k] for k in ('pdf', 'zpl')})
        current['updated'] = now()
        current['revision'] = _integer(receipt.get('revision'), 0, 2**63 - 2) + 1
        self._jobs(current)
        body = _json(current)
        summary = _json(self.store._make_summary(current))
        return {'receipt': current, 'body': body, 'summary': summary,
                'digest': hashlib.sha256(summary.encode()).hexdigest(),
                'event': 'ordinary_labels_' + state, 'detail': job_id + ':' + _hash(job)}

    def _commit(self, expected_body, prepared, publish=None):
        current = prepared['receipt']
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE receipts SET updated=?,body=? WHERE id=? AND body=? AND deleted_at IS NULL',
                                 (current['updated'], prepared['body'], current['id'], expected_body))
            if changed.rowcount != 1:
                _fail('This receipt changed before label publication. Reload it; no edit was replaced.')
            db.execute('INSERT OR REPLACE INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest) VALUES (?,?,?,?,?)',
                       (current['id'], current['updated'], self.store.SUMMARY_VERSION, prepared['summary'], prepared['digest']))
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                       (current['id'], current['updated'], prepared['event'], prepared['detail']))
            if publish:
                publish()
        return current

    def _save(self, receipt, job_id, job, state, authority):
        expected = self.store.receipt_compare_body(receipt)
        prepared = self._prepared(receipt, job_id, job, state)
        with self._fence(authority):
            return self._commit(expected, prepared)

    def _stage(self, cards, terms, attempt, directory):
        owned = {'directory': str(directory), 'identity': _identity(directory.lstat()), 'files': {}}
        worker = None
        try:
            if terms['renderer'] == 'hosted_confirmed':
                from station_receiving_label_worker import run_pair, cleanup_attempt
                output, _ = self._roots()
                worker_root = _path(output / '.ss-receipt-label-worker', directory=True, create=True)
                worker = run_pair(cards, terms['profile'], terms['renderCounts'], worker_root)
                for kind in ('pdf', 'zpl'):
                    data, _ = _read(worker[kind])
                    self._write_stage(directory / ('labels.' + kind), data, owned, kind)
                cleanup_attempt(worker)
                worker = None
            else:
                from station_label_legacy_worker import run_pair
                # No forged hosted marks or modified original receipt quantities.
                selected = []
                for card in sorted(cards, key=lambda c: c['row_id']):
                    copies = terms['renderCounts'].get(card['row_id'], 0)
                    if copies:
                        value = copy.deepcopy(card)
                        value['qty'] = copies
                        selected.append(value)
                run_pair(selected, terms['profile'], directory, owned['files'])
            for kind in ('pdf', 'zpl'):
                _, meta = _read(directory / ('labels.' + kind))
                if meta['identity'] != owned['files'][kind]['identity']:
                    _fail('The private label attempt changed its output file identity.')
                owned['files'][kind] = meta
            _sync_directory(directory)
            return owned
        except BaseException:
            # Only identities successfully created by this exact attempt qualify.
            self._cleanup(owned)
            raise
        finally:
            if worker is not None:
                try:
                    from station_receiving_label_worker import cleanup_attempt
                    cleanup_attempt(worker)
                except (OSError, ValueError):
                    pass

    @staticmethod
    def _write_stage(path, data, owned, kind):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            owned['files'][kind] = {'identity': _identity(os.fstat(stream.fileno()))}
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _cleanup(owned):
        try:
            directory = _path(owned['directory'], directory=True)
            if _identity(directory.lstat()) != owned['identity']:
                return False
            children = list(directory.iterdir())
            expected = {'labels.' + k: v for k, v in owned['files'].items()}
            if any(p.name not in expected or _identity(_path(p).lstat()) != expected[p.name]['identity'] for p in children):
                return False
            for child in children:
                if (_identity(_path(directory, directory=True).lstat()) != owned['identity']
                        or _identity(_path(child).lstat()) != expected[child.name]['identity']):
                    return False
                child.unlink()
            if _identity(_path(directory, directory=True).lstat()) != owned['identity']:
                return False
            directory.rmdir()
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _pair(self, job, *, staged=False):
        output, stage = self._roots()
        owned = job['stage']
        attempt = _uuid(job['attempt'])
        directory = stage / attempt
        if owned['directory'] != str(directory):
            _fail('The saved label attempt is outside its original private directory.')
        if staged and _identity(_path(directory, directory=True).lstat()) != owned['identity']:
            _fail('The saved private label attempt changed identity.')
        result = {}
        for kind in ('pdf', 'zpl'):
            if job['files'].get(kind) != PREFIX + job['jobId'] + '.' + kind:
                _fail('The saved label output identity is invalid.')
            path = directory / ('labels.' + kind) if staged else output / job['files'][kind]
            result[kind] = _read(path, owned['files'][kind])[0]
        return result

    def _publish_pair(self, job):
        output, stage = self._roots()
        directory = stage / job['attempt']
        if _identity(_path(directory, directory=True).lstat()) != job['stage']['identity']:
            _fail('The private pair changed its directory identity before publication.')
        for kind in ('pdf', 'zpl'):
            source = directory / ('labels.' + kind)
            target = output / job['files'][kind]
            _unchanged_file(source, job['stage']['files'][kind])
            try:
                os.link(source, target, follow_symlinks=False)
            except FileExistsError:
                # A restart can find one or both links from this exact prepared
                # attempt. Matching bytes alone never establish file ownership.
                _unchanged_file(target, job['stage']['files'][kind])
            _unchanged_file(target, job['stage']['files'][kind])
        _sync_directory(output)

    def _published_response(self, ident, job_id, published, committed, terms, layout, dpi, expected):
        """Release no receipt until final disk I/O has current authority again.

        This never undoes an already committed pair. The exact committed receipt
        snapshot must still match, including label metadata excluded from the
        business basis. No file read or cleanup follows this final fence.
        """
        fresh, _, authority = self._current(ident, terms, layout, dpi, expected)
        expected_body = self.store.receipt_compare_body(committed)
        with self._fence(authority):
            if (self.store.receipt_compare_body(fresh) != expected_body
                    or self._jobs(fresh).get(job_id) != published):
                _fail('This receipt changed before its label result could be returned. Its committed pair was kept.')
            return fresh

    def labels(self, ident, layout=None, dpi=None, *, copy_offset=0, copy_count=None,
               job_id=None, expected_session_id=None):
        receipt, cards, terms, authority = self._capture(ident, layout, dpi, copy_offset, copy_count, expected_session_id)
        # Legacy callers without a request key get a stable same-source/range job.
        # A deliberate reprint supplies a fresh UUID. Network retries keep it.
        job_id = _uuid(job_id) if job_id is not None else str(uuid.uuid5(uuid.NAMESPACE_URL, 'ss-ordinary-label-v1:' + ident + ':' + _hash(terms)))
        output, stage = self._roots()
        reuse = False
        with _LOCAL, self._fence(authority):
            current = self.service.active_receipt(ident)
            expected_body = self.store.receipt_compare_body(receipt)
            if self.store.receipt_compare_body(current) != expected_body:
                _fail('This receipt changed before its label job could be reserved.')
            jobs = self._jobs(current)
            job = copy.deepcopy(jobs.get(job_id))
            if job is not None:
                if job.get('version') != 1 or job.get('jobId') != job_id or job.get('terms') != terms:
                    _fail('This label job belongs to different source, profile or copy-range terms. Keep its original identifier.')
                if job.get('state') == 'published':
                    reuse = True
                elif job.get('state') == 'rendering' and job.get('leaseUntil', math.inf) > time.time():
                    _fail('This label job is still running. Check the same job again; do not start a replacement.')
                elif job.get('state') not in ('rendering', 'prepared', 'failed'):
                    _fail('This saved label job needs recovery; no existing pair was replaced.')
            elif len(jobs) >= MAX_JOBS:
                _fail('This receipt has reached its bounded label-job history. Keep its existing pairs; no history was deleted.')
            if not reuse and (job is None or job['state'] != 'prepared'):
                attempts = [] if job is None else list(job.get('priorAttempts', [])) + [job['attempt']]
                if len(attempts) >= MAX_ATTEMPTS:
                    _fail('This label job needs recovery after its bounded attempts. Earlier attempts were preserved.')
                attempt = str(uuid.uuid4())
                directory = stage / attempt
                directory.mkdir(mode=0o700)
                _sync_directory(stage)
                job = {'version': 1, 'jobId': job_id, 'state': 'rendering', 'terms': terms, 'attempt': attempt,
                       'priorAttempts': attempts, 'leaseUntil': time.time() + LEASE_SECONDS,
                       'files': {kind: PREFIX + job_id + '.' + kind for kind in ('pdf', 'zpl')}}
                receipt = self._commit(expected_body, self._prepared(current, job_id, job, 'rendering'))
            else:
                receipt = current
        if reuse:
            self._pair(job)
            fresh, _, authority = self._current(ident, terms, layout, dpi, expected_session_id)
            if self._jobs(fresh).get(job_id) != job:
                _fail('This saved label job changed while its files were verified.')
            if (fresh.get('label_job_current') == job_id
                    and all(fresh.get('files', {}).get(k) == job['files'][k] for k in ('pdf', 'zpl'))):
                with self._fence(authority):
                    self.store.receipt_compare_body(fresh)
                    return fresh
            return self._save(fresh, job_id, job, 'selected', authority)
        if job['state'] == 'rendering':
            try:
                owned = self._stage(cards, terms, job['attempt'], stage / job['attempt'])
            except BaseException:
                # A proven failed attempt may retry immediately; a failed/uncertain
                # state write leaves the original lease in force, never absent.
                try:
                    fresh, _, fresh_authority = self._current(ident, terms, layout, dpi, expected_session_id)
                    if self._jobs(fresh).get(job_id) == job:
                        failed = dict(job, state='failed')
                        self._save(fresh, job_id, failed, 'failed', fresh_authority)
                except (OSError, ValueError):
                    pass
                raise
            # Keep a complete owned stage if authority/CAS becomes unavailable.
            # A running lease cannot be silently cleared or unknown files deleted.
            fresh, _, authority = self._current(ident, terms, layout, dpi, expected_session_id)
            if self._jobs(fresh).get(job_id) != job:
                _fail('This label job changed during rendering. Its private attempt was preserved.')
            prepared_job = dict(job, state='prepared', stage=owned)
            receipt = self._save(fresh, job_id, prepared_job, 'prepared', authority)
            job = prepared_job
        # Fresh canonical receiving/source and actor proof, then the short local
        # session/profile/receipt fence. No remote calls occur inside SQLite CAS.
        fresh, _, authority = self._current(ident, terms, layout, dpi, expected_session_id)
        if self._jobs(fresh).get(job_id) != job:
            _fail('This label job changed before publication. Keep its prepared pair for recovery.')
        # Hash the real staged pair outside the OS/SQLite guards. The callback
        # only fences those exact file identities and creates exclusive links.
        self._pair(job, staged=True)
        fresh, _, authority = self._current(ident, terms, layout, dpi, expected_session_id)
        if self._jobs(fresh).get(job_id) != job:
            _fail('This label job changed while its complete private pair was verified.')
        expected_body = self.store.receipt_compare_body(fresh)
        published = dict(job, state='published', publishedAt=now())
        prepared = self._prepared(fresh, job_id, published, 'published')
        try:
            with self._fence(authority):
                result = self._commit(expected_body, prepared, lambda: self._publish_pair(job))
        except BaseException:
            # Commit acknowledgement can be lost after SQLite committed. Read
            # durable state; uncertainty retains staged/final files unchanged.
            recovered = None
            try:
                recovered = self.store.receipt(ident)
                if recovered.get('label_jobs', {}).get(job_id) != published:
                    recovered = None
            except (OSError, ValueError, KeyError):
                recovered = None
            if recovered is not None:
                self._pair(published)
                # A fresh denial must propagate, not be swallowed into the old
                # commit acknowledgement error. Keep ambiguous stage/final links.
                return self._published_response(ident, job_id, published, recovered, terms,
                                                layout, dpi, expected_session_id)
            raise
        self._pair(published)
        self._cleanup(job['stage'])
        return self._published_response(ident, job_id, published, result, terms,
                                        layout, dpi, expected_session_id)

    def download(self, ident, job_id, kind, expected_session_id=None):
        _uuid(job_id)
        if kind not in ('pdf', 'zpl'):
            _fail('Choose the PDF or ZPL from this saved label pair.')
        with self.service.lock:
            receipt = self.service.active_receipt(ident)
            job = copy.deepcopy(self._jobs(receipt).get(job_id))
        if not job or job.get('state') != 'published':
            _fail('This label pair is not durably published. Recover the same job before opening it.')
        terms = job['terms']
        fresh, _, authority = self._current(ident, terms, terms['profile']['layout'], terms['profile']['dpi'], expected_session_id)
        expected_body = self.store.receipt_compare_body(fresh)
        pair = self._pair(job)
        # File reading is also detached. Revalidate current canonical source and
        # actor after blocking disk I/O, then fence the exact current receipt.
        latest, _, authority = self._current(ident, terms, terms['profile']['layout'], terms['profile']['dpi'], expected_session_id)
        with self._fence(authority):
            if (self.store.receipt_compare_body(latest) != expected_body
                    or self._jobs(latest).get(job_id) != job):
                _fail('This receipt changed before label delivery. Reopen its original job.')
            return {'content': pair[kind], 'filename': job['files'][kind],
                    'contentType': 'application/pdf' if kind == 'pdf' else 'application/vnd.zebra-zpl'}
