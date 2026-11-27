"""Owned current desktop fixture. All acceptance is UNRUN/unadmitted.

Real StationService/server/profile/vault/SQLite/renderers and real static UI.
Only the existing fictional broker transport supplies remote protocol replies.
Two gates delay real work/replies; neither manufactures a response or authority.
"""
import copy
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

MAX_ARTIFACT = 32 * 1024 * 1024
MAX_HOLD_SECONDS = 45
MAX_LEDGER_ROWS = 2000
MAX_COUNTER = (1 << 63) - 1
DEVICE_PASSWORD = 'fictional-device-password-001'
STAFF_PASSWORD = 'fixture-staff-password'


def asset_mimes(path):
    suffix = Path(path).suffix
    return {'.js': {'text/javascript', 'application/javascript'}, '.css': {'text/css'},
            '.png': {'image/png'}, '.ico': {'image/vnd.microsoft.icon', 'image/x-icon'}}[suffix]


class FixtureFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise FixtureFailure(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def error_type(error):
    # Fixed type vocabulary, never exception text, stack, peer or request data.
    name = type(error).__name__
    return name if name in ('FixtureFailure', 'AssertionError', 'BrokenPipeError',
        'ConnectionResetError', 'ConnectionAbortedError', 'TimeoutError', 'OSError',
        'ValueError', 'TypeError', 'RuntimeError', 'Error', 'TargetClosedError',
        'KeyboardInterrupt', 'SystemExit') else 'OtherException'


class ReplyHold:
    def __init__(self, kind):
        require(kind in ('pair_read', 'completed_reply', 'completed_download'), 'UNKNOWN_HOLD_KIND')
        self.kind = kind
        self._ready = threading.Event()
        self._release = threading.Event()
        self.observation = None
        self.used = False

    @property
    def ready(self):
        return self._ready.is_set()

    def enter(self, observation):
        require(not self.used, 'HOLD_REUSED')
        self.used = True
        self.observation = copy.deepcopy(observation)
        self._ready.set()
        # This wait runs in an owned HTTP thread, never Playwright's thread.
        require(self._release.wait(MAX_HOLD_SECONDS), 'OWNED_REPLY_HOLD_TIMEOUT')

    def release(self):
        self._release.set()


class ReadOnlyBroker:
    """Allow only owned reads and native login/logout after fictional seeding."""
    trust_env = False

    def __init__(self, remote, receipt):
        self.remote = remote
        self.gets = {'/api/me', '/api/square/status', '/api/receiving/readiness',
                     '/api/credit/customers',
                     '/api/receiving/' + receipt['hosted_receive']['receiving_id'],
                     '/api/intake-claims/' + receipt['intake_claim']['physical_intake_id']}
        self.calls = []
        self.blocked = []
        self._lock = threading.Lock()
        self.total_seen = 0
        self.overflow_count = 0
        self.first_overflow = None

    def _record(self, allowed, row):
        with self._lock:
            self.total_seen = min(MAX_COUNTER, self.total_seen + 1)
            if len(self.calls) + len(self.blocked) >= MAX_LEDGER_ROWS:
                self.overflow_count = min(MAX_COUNTER, self.overflow_count + 1)
                if self.first_overflow is None:
                    self.first_overflow = dict(row, allowed=allowed)
                # Never delegate a call whose evidence cannot fit the ledger.
                raise FixtureFailure('BROKER_LEDGER_LIMIT')
            (self.calls if allowed else self.blocked).append(row)

    def evidence(self):
        with self._lock:
            return copy.deepcopy({'calls': self.calls, 'blocked': self.blocked,
                'total_seen': self.total_seen, 'overflow_count': self.overflow_count,
                'first_overflow': self.first_overflow, 'row_limit': MAX_LEDGER_ROWS})

    def request(self, method, url, *, headers, json=None, **options):
        from station_cloud import ORIGIN
        from test_station_cloud import response
        value = urlsplit(url)
        allowed = (url.startswith(ORIGIN + '/') and not value.fragment and
                   (method == 'GET' and value.path in self.gets or
                    method == 'POST' and value.path in ('/api/auth/login', '/api/auth/logout')))
        row = {'method': method if method in ('GET', 'POST') else 'OTHER', 'route': self.route(value.path)}
        self._record(allowed, row)
        if not allowed:
            raise FixtureFailure('UNALLOWLISTED_BROKER_CALL')
        if value.path == '/api/square/status':
            actor = self.remote.sessions.get(headers.get('Authorization', '').removeprefix('Bearer '))
            if not actor:
                return response(401, {'error': 'Sign in'})
            # Owned setup metadata only; no claim of live Square readiness.
            return response(200, {'oauth_configured': True, 'connected': True,
                'connection_status': 'connected', 'environment': 'sandbox',
                'saved_environment': 'sandbox', 'requested_profile': 'receiving',
                'grants_verified': False, 'write_checkout_enabled': False, 'scopes': [],
                'requested_scopes': ['ITEMS_READ', 'ITEMS_WRITE', 'INVENTORY_READ', 'INVENTORY_WRITE']})
        return self.remote.request(method, url, headers=headers, json=json, **options)

    @staticmethod
    def route(path):
        if path.startswith('/api/receiving/') and path != '/api/receiving/readiness':
            return '/api/receiving/{operation}'
        if path.startswith('/api/intake-claims/'):
            return '/api/intake-claims/{claim}'
        return path if path in ('/api/me', '/api/square/status', '/api/receiving/readiness',
                                '/api/credit/customers', '/api/auth/login', '/api/auth/logout') else '/[unknown]'


class DesktopFixture:
    def __init__(self, case_id):
        require(case_id in ('DUI01', 'DUI02', 'DUI03', 'DUI04', 'DUI05', 'DUI06', 'DUI07'), 'FIXED_DESKTOP_CASE_REQUIRED')
        self.case_id = case_id
        self.hold = None
        self.server = None
        self.thread = None
        self.seed = None
        self.render_calls = 0
        self.responses = []
        self.response_overflow = False
        self.server_errors = []
        self.server_error_count = 0
        self.server_error_overflow_count = 0
        self.first_server_error_overflow = None
        self.asset_probe = []
        self.origin_probe = []
        self._active = set()
        self._pending = 0
        self._lock = threading.Lock()
        self._patches = []
        self._cancel = threading.Event()
        self.cleanup_complete = False

    def record_server_error(self, code, error):
        require(code in ('HTTP_REQUEST_EXCEPTION', 'HTTP_THREAD_EXCEPTION',
                         'HTTP_SERVER_LOOP_EXCEPTION'), 'FIXED_SERVER_ERROR_CODE')
        row = {'code': code, 'error_type': error_type(error)}
        with self._lock:
            self.server_error_count = min(MAX_COUNTER, self.server_error_count + 1)
            if len(self.server_errors) < MAX_LEDGER_ROWS:
                self.server_errors.append(row)
            else:
                self.server_error_overflow_count = min(MAX_COUNTER, self.server_error_overflow_count + 1)
                if self.first_server_error_overflow is None:
                    self.first_server_error_overflow = row

    def evidence(self):
        with self._lock:
            result = copy.deepcopy({'responses': self.responses,
                'response_overflow': self.response_overflow, 'server_errors': self.server_errors,
                'server_error_count': self.server_error_count,
                'server_error_overflow_count': self.server_error_overflow_count,
                'first_server_error_overflow': self.first_server_error_overflow,
                'active_threads': len(self._active), 'pending_threads': self._pending,
                'cleanup_complete': self.cleanup_complete})
        result['server_thread_alive'] = bool(self.thread and self.thread.is_alive())
        result['broker'] = self.broker.evidence() if hasattr(self, 'broker') else None
        return result

    def __enter__(self):
        # Imports occur only in a separately admitted Linux process, after the
        # complete native/dependency preflight. No required import substitutes.
        import test_station_label_publication as service_tests
        import roca_to_square as core
        from station_label_publication import OrdinaryLabels
        import station_receiving_label_worker as worker
        from station_server import make_handler
        self.core = core
        require(Path(core.APP_DIR).resolve() == Path(__file__).resolve().parents[1]
                and Path(core.HTML_APP_FILE).resolve() == Path(__file__).resolve().parents[1] / 'roca_square_label_app.html',
                'ACTUAL_SELECTED_DESKTOP_HTML_REQUIRED')
        try:
            self.seed = service_tests.OrdinaryReceiptLabelPublicationTests(
                'test_csv_receipt_actual_pair_exact_skus_and_restart_same_job')
            self.seed.setUp()
            self.service = self.seed.service
            name = 'Fictional A' if self.case_id == 'DUI01' else 'Fictional B'
            source = self.seed.fixture.receipt('Name,Game,Price,Quantity\n' + name + ',Magic,3.13,3\n')
            self.receipt = self.seed.received(source)
            self.ident = self.receipt['id']
            self.output = Path(core.OUTPUT_DIR)
            self.remote = self.seed.fixture.remote
            self.stock_baseline = self.remote.stock_calls
            self.before_cards = fingerprint(self.receipt['cards'])
            self.broker = ReadOnlyBroker(self.remote, self.receipt)
            self.service.cloud.transport = self.broker
            self.service.auth.set_password(DEVICE_PASSWORD)
            self.expected = self.seed.expected
            self.label_path = '/api/receipts/' + self.ident + '/labels'
            actual_pair, actual_run = OrdinaryLabels._pair, worker.run_pair
            owner = self

            def observed_pair(instance, job, *, staged=False):
                result = actual_pair(instance, job, staged=staged)
                if (instance.service is owner.service and not staged and job['state'] == 'published'
                        and owner.hold is not None and owner.hold.kind == 'pair_read' and not owner.hold.used):
                    owner.hold.enter(owner.durable())
                return result

            def observed_run(*args, **kwargs):
                owner.render_calls += 1
                require(kwargs.get('cancel_event') is None, 'FOREIGN_WORKER_CANCELLATION_CONTEXT')
                kwargs['cancel_event'] = owner._cancel
                return actual_run(*args, **kwargs)

            for item in (patch.object(OrdinaryLabels, '_pair', new=observed_pair),
                         patch.object(worker, 'run_pair', new=observed_run)):
                item.start()
                self._patches.append(item)
            base = make_handler(core)

            class ObservedHandler(base):
                def send_json(self, data, status=200, cookie=None):
                    if (self.command == 'POST' and self.path == owner.label_path and status == 200
                            and owner.hold is not None and owner.hold.kind == 'completed_reply' and not owner.hold.used):
                        require(type(data) is dict and type(data.get('receipt')) is dict,
                                'REAL_LABEL_RESPONSE_REQUIRED')
                        require(fingerprint(data['receipt']) == fingerprint(owner.current()),
                                'LABEL_REPLY_NOT_CURRENT_AT_SERVER_COMPLETION')
                        owner.hold.enter(owner.durable())
                    # No route fulfillment, modified status/body or skipped gate.
                    return super().send_json(data, status, cookie)

                def send_bytes(self, content, status=200, content_type='application/octet-stream', *, cookie=None, disposition=None):
                    route = owner.route(self.path)
                    if (self.command == 'GET' and status == 200
                            and route == '/api/receipt-label-publications/{receipt}/{job}/pdf'
                            and owner.hold is not None and owner.hold.kind == 'completed_download'
                            and not owner.hold.used):
                        # The real handler has finished authorization and read its
                        # real file. Hold only delivery; never substitute a response.
                        pair = owner.durable()
                        expected = pair['files']['pdf']
                        require(type(content) is bytes and content_type == 'application/pdf'
                                and len(content) == expected['bytes']
                                and hashlib.sha256(content).hexdigest() == expected['sha256'],
                                'REAL_AUTHORIZED_DOWNLOAD_BYTES_REQUIRED')
                        observation = dict(pair, completed_download={
                            'status': status, 'kind': 'pdf', 'bytes': len(content),
                            'sha256': expected['sha256'], 'content_type': content_type})
                        owner.hold.enter(observation)
                    with owner._lock:
                        if len(owner.responses) >= MAX_LEDGER_ROWS:
                            owner.response_overflow = True
                            raise FixtureFailure('SERVER_LEDGER_LIMIT')
                        owner.responses.append({'method': self.command, 'route': route,
                                                'status': status, 'bytes': len(content)})
                    return super().send_bytes(content, status, content_type, cookie=cookie, disposition=disposition)

            class ObservedServer(ThreadingHTTPServer):
                def process_request(self, request, client_address):
                    # Account for the interval between Thread.start and the new
                    # thread entering process_request_thread, too.
                    with owner._lock:
                        owner._pending += 1
                    try:
                        return super().process_request(request, client_address)
                    except BaseException:
                        with owner._lock:
                            owner._pending -= 1
                        raise

                def handle_error(self, request, client_address):
                    # BaseServer's raw traceback/stderr path is replaced by a
                    # bounded typed failure. No error becomes an accepted reply.
                    owner.record_server_error('HTTP_REQUEST_EXCEPTION', sys.exc_info()[1])

                def process_request_thread(self, request, client_address):
                    # Include handle_error and shutdown_request in the active
                    # interval: Handler.handle's finally is too early to quiesce.
                    with owner._lock:
                        owner._pending -= 1
                        owner._active.add(threading.get_ident())
                    try:
                        try:
                            return super().process_request_thread(request, client_address)
                        except BaseException as error:
                            owner.record_server_error('HTTP_THREAD_EXCEPTION', error)
                    finally:
                        with owner._lock:
                            owner._active.discard(threading.get_ident())

            def serve():
                try:
                    owner.server.serve_forever(poll_interval=0.01)
                except BaseException as error:
                    owner.record_server_error('HTTP_SERVER_LOOP_EXCEPTION', error)

            self.server = ObservedServer(('127.0.0.1', 0), ObservedHandler)
            self.server.station = self.service
            self.origin = 'http://127.0.0.1:' + str(self.server.server_port)
            self.thread = threading.Thread(target=serve, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def route(self, url):
        path = urlsplit(url).path
        if path == self.label_path:
            return '/api/receipts/{receipt}/labels'
        if path == '/api/receipts/' + self.ident:
            return '/api/receipts/{receipt}'
        if path == '/api/receipts/' + self.ident + '/pricing':
            return '/api/receipts/{receipt}/pricing'
        if re.fullmatch(r'/api/receipt-label-publications/' + re.escape(self.ident) + r'/[a-f0-9-]{36}/(?:pdf|zpl)', path):
            return '/api/receipt-label-publications/{receipt}/{job}/' + path.rsplit('/', 1)[1]
        return path

    def arm(self, kind):
        require(self.hold is None, 'ONE_HOLD_PER_CASE')
        self.hold = ReplyHold(kind)
        return self.hold

    def probe_assets(self, paths, source_pins):
        """Real local HTTP for the complete source asset closure, not mock UI.

        Some dynamic CSS is mounted only by another receipt family. Probe those
        real routes separately rather than claiming the native flow loaded them.
        """
        require(not self.asset_probe and len(paths) == 28, 'ONE_EXACT_ASSET_PROBE')
        for path in sorted(paths):
            connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            try:
                connection.request('GET', path, headers={'Origin': self.origin})
                response = connection.getresponse()
                data = response.read(4 * 1024 * 1024 + 1)
                expected = source_pins[path.lstrip('/')]
                hashed = hashlib.sha256(data).hexdigest()
                mime = (response.getheader('Content-Type') or '').split(';', 1)[0].lower()
                self.asset_probe.append({'route': path, 'status': response.status,
                                         'bytes': len(data), 'sha256': hashed, 'mime': mime})
                require(response.status == 200 and response.getheader('Cache-Control') == 'no-store'
                        and response.getheader('X-Content-Type-Options') == 'nosniff', 'REAL_STATIC_ROUTE_SECURITY')
                require(len(data) == expected['bytes'] and hashed == expected['sha256'], 'REAL_STATIC28_HTTP_DISK_EQUAL')
                require(mime in asset_mimes(path), 'REAL_STATIC28_MIME_REQUIRED')
            finally:
                connection.close()
        for purpose, headers in (
            ('negative_origin', {'Origin': 'https://unowned.invalid'}),
            ('negative_host', {'Origin': self.origin, 'Host': 'unowned.invalid'})):
            connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            try:
                connection.request('GET', '/api/version', headers=headers)
                response = connection.getresponse()
                raw = response.read(4097)
                self.origin_probe.append({'purpose': purpose, 'route': '/api/version',
                                          'status': response.status, 'bytes': len(raw)})
                require(response.status == 403 and len(raw) <= 4096, 'REAL_LOOPBACK_ORIGIN_HOST_DENIAL')
            finally:
                connection.close()

    def current(self):
        return self.service.store.receipt(self.ident)

    def session_hash(self):
        saved = self.service.cloud._saved()
        return fingerprint(saved['session_id']) if saved else None

    def remote_role(self, role):
        require(role in ('manager', 'cashier'), 'UNKNOWN_OWNED_ROLE')
        saved = self.service.cloud._saved()
        self.remote.sessions[saved['token']]['role'] = role

    def source_state(self, state):
        require(state in ('received', 'outcome_unknown'), 'UNKNOWN_OWNED_SOURCE_STATE')
        self.remote.receivings[('fictional-store', self.receipt['hosted_receive']['receiving_id'])]['state'] = state

    def durable(self):
        current = self.current()
        jobs = current.get('label_jobs', {})
        require(len(jobs) <= 1, 'DUPLICATE_LABEL_JOB')
        require(fingerprint(current['cards']) == self.before_cards, 'ORIGINAL_CARDS_CHANGED')
        require(self.remote.stock_calls == self.stock_baseline, 'PROVIDER_STOCK_CHANGED')
        broker = self.broker.evidence()
        require(not broker['blocked'] and not broker['overflow_count'], 'BROKER_CALL_DENIED_OR_OVERFLOW')
        with self.service.store.db() as db:
            count = db.execute("SELECT count(*) FROM events WHERE receipt_id=? AND kind='ordinary_labels_published'", (self.ident,)).fetchone()[0]
        if not jobs:
            return {'jobs': 0, 'published_events': count, 'renders': self.render_calls,
                    'cards_sha256': self.before_cards, 'stock_calls_unchanged': True}
        job_id, job = next(iter(jobs.items()))
        require(job['state'] == 'published', 'DURABLE_PUBLISHED_PAIR_REQUIRED')
        require(count == 1 and self.render_calls == 1, 'PAIR_MUST_PUBLISH_AND_RENDER_ONCE')
        files = {}
        for kind in ('pdf', 'zpl'):
            name = job['files'][kind]
            require(name == 'ss-receipt-label-' + job_id + '.' + kind, 'UNEXPECTED_PAIR_NAME')
            path = self.output / name
            info = path.lstat()
            require(not path.is_symlink() and 0 < info.st_size <= MAX_ARTIFACT, 'PAIR_FILE_BOUNDS')
            raw = path.read_bytes()
            hashed = hashlib.sha256(raw).hexdigest()
            require(hashed == job['stage']['files'][kind]['sha256'] and len(raw) == job['stage']['files'][kind]['bytes'], 'REAL_PAIR_BYTES_CHANGED')
            files[kind] = {'name': name, 'bytes': len(raw), 'sha256': hashed}
        return {'jobs': 1, 'published_events': count, 'renders': self.render_calls,
                'job_key_sha256': fingerprint(job_id), 'copy_count': job['terms']['copyCount'],
                'cards_sha256': self.before_cards, 'files': files, 'stock_calls_unchanged': True}

    def expected_sku(self):
        current = self.current()
        job = current['label_jobs'][current['label_job_current']]
        # Renderer counts use the verified hosted review ID, while immutable
        # local row IDs keep their original representation on the receipt.
        cards = {row['hosted_sku_resolution']['review_line_id']: row for row in self.receipt['cards']}
        require(len(cards) == len(self.receipt['cards']) and set(job['terms']['renderCounts']) <= set(cards),
                'EXACT_ORIGINAL_HOSTED_REVIEW_MAPPING_REQUIRED')
        require(job['terms']['copyCount'] == 1 and len(job['terms']['renderCounts']) == 1,
                'SINGLE_ACTUAL_BARCODE_REQUIRED')
        return cards[next(iter(job['terms']['renderCounts']))]['sku']

    def close(self):
        if self.cleanup_complete:
            return
        self._cancel.set()
        if self.hold:
            self.hold.release()
        if self.server:
            if self.thread and self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
        if self.thread and self.thread.ident is not None:
            self.thread.join(5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self._lock:
                active = bool(self._active) or bool(self._pending)
            if not active:
                break
            time.sleep(0.01)
        with self._lock:
            active = bool(self._active) or bool(self._pending)
        if active or self.thread and self.thread.is_alive():
            # Preserve the owned profile/output rather than deleting resources
            # still in use. No unknown or recursive production cleanup occurs.
            if self.seed is not None:
                self.seed.fixture.temp._finalizer.detach()
            raise FixtureFailure('OWNED_HTTP_THREADS_NOT_CLOSED')
        for item in reversed(self._patches):
            item.stop()
        self._patches.clear()
        if self.seed:
            self.seed.doCleanups()
            self.seed = None
        self.cleanup_complete = True

    def __exit__(self, *_):
        self.close()
