"""Fixed seven-case functional controller for a separately trusted fresh CI VM.

The separate launcher must complete source/payload/platform preflight and obtain
Root admission before passing its one real sandboxed preinstalled Chrome here.
The unchanged seven cases, observations and all final gates remain mandatory.
"""
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time
from urllib.parse import urlsplit

from tools.desktop_label_browser_fixture import DesktopFixture, FixtureFailure, asset_mimes, error_type, fingerprint, require

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT.parent / 'evidence'
RUNTIME = ROOT.parent / 'runtime'

EXPECTED_IDS = ('DUI01', 'DUI02', 'DUI03', 'DUI04', 'DUI05', 'DUI06', 'DUI07')
EXPECTED_CAPTURES = (
    'DUI01_done', 'DUI02_denied', 'DUI02_recovered', 'DUI03_denied', 'DUI03_recovered',
    'DUI04_admin_context', 'DUI04_rejected', 'DUI04_recovered', 'DUI05_signed_out', 'DUI05_reopened',
    'DUI06_rejected', 'DUI06_recovered', 'DUI07_rejected', 'DUI07_recovered')
STATIC_ASSETS = frozenset((
    '/assets/stock-and-sleeve.ico', '/assets/stock-and-sleeve.png',
    '/cloud.css', '/cloud.js', '/import.css', '/import.js', '/intake-claim.css', '/intake-claim.js',
    '/intake.js', '/preview.js', '/receiving-actions.css', '/receiving-actions.js',
    '/receiving-intake-actions.css', '/receiving-intake-actions.js', '/receiving-labels.css',
    '/receiving-labels.js', '/receiving-restore.css', '/receiving-restore.js', '/receiving.css',
    '/receiving.js', '/setup.css', '/setup.js', '/shared-trade.css', '/shared-trade.js',
    '/station.css', '/station.js', '/trade-recovery.css', '/trade-recovery.js'))
GET_ROUTES = frozenset(('/', '/api/version', '/api/settings', '/api/receipts', '/api/auth',
    '/api/cloud/session', '/api/cloud/setup-status', '/api/cloud/pending', '/api/admin/settings',
    '/api/receipts/{receipt}', '/api/receipts/{receipt}/pricing',
    '/api/receipt-label-publications/{receipt}/{job}/pdf',
    '/api/receipt-label-publications/{receipt}/{job}/zpl')) | STATIC_ASSETS
POST_ROUTES = frozenset(('/api/receipts/{receipt}/labels', '/api/auth/login', '/api/auth/logout',
    '/api/cloud/login', '/api/cloud/logout', '/api/cloud/search'))
EXPECTED_HTTP_ERROR = {'DUI02': 400, 'DUI03': 502, 'DUI05': 409}
MASK_SELECTORS = ('input[type=password]', '#adminForm', '#cloudLoginForm', '#cloudMfa',
                  '#cloudCustomers', '#cloudAccount', '#cloudCustomerDialog', '#cloudEntryDialog', '#squareToken')
MAX_ROWS = 2000
MAX_SCREENSHOT = 1536 * 1024
MAX_SCREENSHOTS = 8 * 1024 * 1024
MAX_DOWNLOAD = 32 * 1024 * 1024


def regular(path, limit):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size <= limit,
            'REGULAR_BOUNDED_EVIDENCE_REQUIRED')
    raw = path.read_bytes()
    require(len(raw) == info.st_size, 'EVIDENCE_CHANGED_DURING_READ')
    return raw


def streamed_native_identity(path, limit):
    """Bounded native byte/identity check; never load the executable as data RAM."""
    path = Path(path)
    require(path.is_absolute() and all(not parent.is_symlink() for parent in (path, *path.parents)),
            'NATIVE_EXECUTABLE_PATH_OR_LINK')
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= limit
            and info.st_uid in (0, os.geteuid()), 'TRUSTED_PLATFORM_SINGLE_NATIVE_EXECUTABLE')
    stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_nlink,
                           value.st_mtime_ns, value.st_ctime_ns)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    count, hashed, started = 0, hashlib.sha256(), time.monotonic()
    try:
        require(stamp(os.fstat(descriptor)) == stamp(info), 'NATIVE_EXECUTABLE_OPEN_IDENTITY')
        while chunk := os.read(descriptor, 65536):
            count += len(chunk)
            require(count <= limit and time.monotonic() - started <= 60, 'NATIVE_EXECUTABLE_STREAM_BOUND')
            hashed.update(chunk)
        require(count == info.st_size and stamp(os.fstat(descriptor)) == stamp(info) == stamp(path.lstat()),
                'NATIVE_EXECUTABLE_CHANGED')
    finally:
        os.close(descriptor)
    return {'path': str(path), 'bytes': count, 'sha256': hashed.hexdigest(),
            'device': info.st_dev, 'inode': info.st_ino}


def save_json(name, value):
    require(re.fullmatch(r'[a-z0-9-]+\.json', name) is not None, 'FIXED_EVIDENCE_NAME_REQUIRED')
    path = EVIDENCE / name
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    require(len(raw) <= 4 * 1024 * 1024, 'JSON_EVIDENCE_LIMIT')
    with path.open('xb') as stream:
        stream.write(raw)


TOOLS_INPUT_PATH = ROOT / 'tools/desktop_label_browser_inputs.json'
TOOLS_ADMISSION_PATH = ROOT / 'tools/desktop_label_browser_admission.json'

def source_snapshot():
    raw = regular(TOOLS_INPUT_PATH, 1024 * 1024)
    admitted = json.loads(regular(TOOLS_ADMISSION_PATH, 1024 * 1024))
    require(set(admitted) == {'version', 'execution_hold', 'source_inputs_sha256', 'source_count',
                             'expected_ids', 'expected_capture_names', 'required_linux_launcher'}
            and admitted['version'] == 1 and admitted['expected_ids'] == list(EXPECTED_IDS)
            and admitted['expected_capture_names'] == list(EXPECTED_CAPTURES)
            and admitted['required_linux_launcher'] is True, 'FIXED_ADMISSION_SCHEMA')
    require(admitted.get('execution_hold') is None, 'DESKTOP_BROWSER_EXECUTION_UNADMITTED')
    require(hashlib.sha256(raw).hexdigest() == admitted['source_inputs_sha256'], 'SOURCE_SELECTION_PIN')
    rows = json.loads(raw)['sources']
    require(len(rows) == admitted['source_count'] and len({r['path'] for r in rows}) == len(rows),
            'EXACT_SOURCE_SCOPE')
    result = []
    for row in rows:
        name = row['path']
        require(type(name) is str and not Path(name).is_absolute() and '..' not in Path(name).parts,
                'SOURCE_PATH_INVALID')
        path = ROOT / name
        for ancestor in (path, *path.parents):
            require(not ancestor.is_symlink(), 'SOURCE_SYMLINK_REFUSED')
        require(path.resolve().is_relative_to(ROOT), 'SOURCE_CONTAINMENT')
        require(path.stat().st_mode & 0o222 == 0, 'SOURCE_MUST_BE_READ_ONLY')
        data = regular(path, 4 * 1024 * 1024)
        actual = hashlib.sha256(data).hexdigest()
        require(actual == row['sha256'] and len(data) == row['bytes'], 'SOURCE_BYTES_CHANGED')
        result.append({'path': name, 'sha256': actual, 'bytes': len(data)})
    return result


class BrowserCase:
    def __init__(self, browser, fixture, case_id, total_capture_bytes):
        self.fixture, self.case_id = fixture, case_id
        self.network, self.console, self.errors, self.assertions = [], [], [], []
        self._requests = {}
        self.label_requests = []
        self.captures, self.downloads = [], []
        self.download_events, self._download_objects = [], {}
        self._expected_download = None
        self.total_capture_bytes = total_capture_bytes
        self.overflow = False
        self.bootstrap_complete = False
        self.context_closed = False
        self.pages = []
        self.context = browser.new_context(viewport={'width': 1280, 'height': 900},
            accept_downloads=True, ignore_https_errors=False, service_workers='block', locale='en-US')
        self.context.route('**/*', self._route)
        self.context.on('request', self._request)
        self.context.on('response', self._response)
        self.context.on('requestfinished', lambda r: self._terminal(r, 'finished'))
        self.context.on('requestfailed', lambda r: self._terminal(r, 'failed'))
        self.context.on('page', self._page)
        self.page = self.context.new_page()
        self.context.set_default_timeout(15000)

    def _add(self, collection, row):
        if len(collection) >= MAX_ROWS:
            self.overflow = True
        else:
            collection.append(row)

    def check(self, condition, code):
        require(condition, code)
        require(re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', code) is not None, 'ASSERTION_CODE_INVALID')
        self.assertions.append(code)

    def _classify(self, request):
        url = request.url
        blob = re.fullmatch(r'blob:' + re.escape(self.fixture.origin) + r'/[a-f0-9-]{36}', url)
        if blob:
            return '/[owned-blob-download]', request.method == 'GET', True
        value = urlsplit(url)
        local = value.scheme + '://' + value.netloc == self.fixture.origin
        path = self.fixture.route(url) if local else '/[external]'
        allowed = (local and not value.query and not value.fragment and
                   (request.method == 'GET' and path in GET_ROUTES or
                    request.method == 'POST' and path in POST_ROUTES))
        return path if allowed else '/[unallowlisted]', allowed, False

    def _route(self, route):
        _, allowed, _ = self._classify(route.request)
        if allowed:
            route.continue_()
        else:
            route.abort('blockedbyclient')

    def _request(self, request):
        path, allowed, blob = self._classify(request)
        row = {'sequence': len(self.network), 'method': request.method if request.method in ('GET', 'POST') else 'OTHER',
               'route': path, 'allowed': allowed, 'owned_blob': blob, 'status': None, 'terminal': None,
               'resource_type': request.resource_type, 'query_present': bool(urlsplit(request.url).query)}
        self._requests[request] = row
        self._add(self.network, row)
        if path == '/api/receipts/{receipt}/labels':
            try:
                value = request.post_data_json
                require(type(value) is dict and set(value) == {'layout', 'dpi', 'copyOffset', 'copyCount', 'jobId'},
                        'OBSERVED_LABEL_REQUEST_SHAPE')
                require(type(value['layout']) is str and type(value['jobId']) is str
                        and all(type(value[key]) is int for key in ('dpi', 'copyOffset', 'copyCount'))
                        and value['layout'] == 'side' and value['dpi'] == 203 and value['copyOffset'] == 0
                        and value['copyCount'] == 1 and re.fullmatch(r'[a-f0-9-]{36}', value['jobId']),
                        'OBSERVED_LABEL_REQUEST_TERMS')
                nonce = request.headers.get('x-ss-local-session', '')
                require(re.fullmatch(r'[a-f0-9]{32}', nonce), 'REAL_STAFF_SESSION_HEADER_REQUIRED')
                row['job_key_sha256'] = fingerprint(value['jobId'])
                row['staff_context_sha256'] = fingerprint(nonce)
                row['copy_offset'], row['copy_count'] = value['copyOffset'], value['copyCount']
            except Exception:
                row['label_shape_error'] = True
            self.label_requests.append(row)

    def _response(self, response):
        row = self._requests.get(response.request)
        if row is None:
            self.overflow = True
            return
        row['status'] = response.status
        headers = response.headers
        row['mime'] = headers.get('content-type', '').split(';', 1)[0].lower()
        row['no_store'] = headers.get('cache-control') == 'no-store'
        row['nosniff'] = headers.get('x-content-type-options') == 'nosniff'
        row['frame_deny'] = headers.get('x-frame-options') == 'DENY'
        row['referrer_none'] = headers.get('referrer-policy') == 'no-referrer'
        row['self_csp'] = (headers.get('content-security-policy', '').startswith("default-src 'self';")
                           and "script-src 'self';" in headers.get('content-security-policy', ''))

    def _terminal(self, request, terminal):
        row = self._requests.get(request)
        if row is None:
            self.overflow = True
            return
        row['terminal'] = terminal
        if terminal == 'failed':
            failure = request.failure or ''
            # Blob handoff may abort a navigation only when a real matching
            # download was observed. HTTP aborts are never admitted here.
            row['owned_blob_abort'] = row['owned_blob'] and failure == 'net::ERR_ABORTED'
            row['failure_sha256'] = hashlib.sha256(failure.encode()).hexdigest()

    def _page(self, page):
        self.pages.append(page)
        page.on('console', self._console)
        page.on('pageerror', lambda e: self._add(self.errors, {'code': 'UNEXPECTED_PAGE_ERROR',
            'message_sha256': hashlib.sha256(str(e).encode()).hexdigest()}))
        page.on('crash', lambda: self._add(self.errors, {'code': 'PAGE_CRASH'}))
        page.on('download', lambda download: self._download_event(page, download))
        page.on('dialog', lambda dialog: (self._add(self.errors, {'code': 'UNEXPECTED_BROWSER_DIALOG'}), dialog.dismiss()))

    def _download_event(self, page, download):
        if len(self.download_events) >= MAX_ROWS:
            self.overflow = True
            return
        pending = self._expected_download
        owned_blob = re.fullmatch(r'blob:' + re.escape(self.fixture.origin) + r'/[a-f0-9-]{36}',
                                  download.url) is not None
        filename = download.suggested_filename
        expected = bool(pending is not None and not pending['used']
                        and owned_blob and page in self.pages and filename == pending['filename'])
        row = {'sequence': len(self.download_events), 'expected': expected,
               'kind': pending['kind'] if expected else 'UNEXPECTED',
               'owned_blob': owned_blob, 'owned_page': page in self.pages,
               'filename_sha256': hashlib.sha256(filename.encode()).hexdigest()}
        self._add(self.download_events, row)
        self._download_objects[download] = row['sequence']
        if expected:
            pending['used'] = True
            pending['event_sequence'] = row['sequence']
        else:
            self._add(self.errors, {'code': 'UNEXPECTED_BROWSER_DOWNLOAD_EVENT'})

    def _console(self, message):
        location = message.location
        value = urlsplit(location.get('url', ''))
        path = self.fixture.route(location.get('url', '')) if value.scheme + '://' + value.netloc == self.fixture.origin else '/[external]'
        match = re.fullmatch(r'Failed to load resource: the server responded with a status of (400|409|502) \([A-Za-z ]+\)', message.text)
        status = int(match[1]) if match else None
        expected = (message.type == 'error' and path == '/api/receipts/{receipt}/labels'
                    and status == EXPECTED_HTTP_ERROR.get(self.case_id))
        self._add(self.console, {'type': message.type, 'route': path,
            'message_sha256': hashlib.sha256(message.text.encode()).hexdigest(),
            'expected_label_http_error': expected, 'status': status})

    def wait(self, page, predicate, code, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            page.wait_for_timeout(50)  # pump real Playwright routing/events
        require(False, code)

    def label_terminal(self, index, status):
        self.wait(self.page, lambda: len(self.label_requests) > index and self.label_requests[index]['terminal'] is not None,
                  'ACTUAL_LABEL_HTTP_TERMINAL_REQUIRED')
        row = self.label_requests[index]
        self.check(row['terminal'] == 'finished' and row['status'] == status, 'EXPECTED_ACTUAL_LABEL_HTTP_STATUS')
        self.check(not row.get('label_shape_error'), 'EXACT_NATIVE_LABEL_REQUEST')
        return row

    def generic_reads(self):
        return sum(r['route'] == '/api/receipts/{receipt}' and r['method'] == 'GET' for r in self.network)

    def bootstrap(self, page):
        from playwright.sync_api import expect
        if not self.fixture.asset_probe:
            source_pins = {row['path']: row for row in json.loads(regular(TOOLS_INPUT_PATH, 1024 * 1024))['sources']}
            self.fixture.probe_assets(STATIC_ASSETS, source_pins)
        page.goto(self.fixture.origin + '/', wait_until='domcontentloaded')
        scripts = {path for path in STATIC_ASSETS if path.endswith('.js')}
        self.wait(page, lambda: all(any(r['route'] == asset and r['status'] == 200 and r['terminal'] == 'finished'
                                      for r in self.network) for asset in scripts), 'REAL14_SCRIPT_BOOTSTRAP_REQUIRED')
        self.check(len(STATIC_ASSETS) == 28, 'FIXED28_REAL_ASSETS')
        expect(page.locator('#version')).to_have_text('2.6.0')
        expect(page.locator('#cloudStoreName')).to_have_text('fictional-store')
        self.bootstrap_complete = True

    def capture(self, page, name):
        require(name in EXPECTED_CAPTURES and name.startswith(self.case_id + '_'), 'FIXED_CAPTURE_NAME')
        require(not any(r['name'] == name for r in self.captures), 'CAPTURE_REPEATED')
        require(page.url.startswith(self.fixture.origin + '/') and not page.is_closed(), 'CAPTURE_OWNED_PAGE')
        masks = []
        for selector in MASK_SELECTORS:
            locator = page.locator(selector)
            require(locator.count() <= 16, 'CAPTURE_MASK_SCOPE_BOUNDS')
            masks.append(locator)
        # Native screenshot masks do not unhide, mutate app state or alter CSP.
        raw = page.screenshot(type='png', full_page=False, scale='css', animations='disabled',
                              caret='hide', mask=masks, mask_color='#111111', timeout=10000)
        require(24 <= len(raw) <= MAX_SCREENSHOT and raw[:8] == b'\x89PNG\r\n\x1a\n', 'BOUNDED_REAL_PNG_REQUIRED')
        width, height = struct.unpack('>II', raw[16:24])
        require((width, height) == (1280, 900), 'EXACT_CAPTURE_VIEWPORT')
        self.total_capture_bytes[0] += len(raw)
        require(self.total_capture_bytes[0] <= MAX_SCREENSHOTS, 'TOTAL_CAPTURE_BYTES')
        with (EVIDENCE / (name + '.png')).open('xb') as stream:
            stream.write(raw)
        self.captures.append({'name': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                              'width': width, 'height': height, 'masked_selectors': list(MASK_SELECTORS)})

    def download(self, page, kind):
        from contextlib import closing
        import pypdfium2 as pdfium
        import zxingcpp
        import station_receiving_label_render as raster
        self.check(kind in ('pdf', 'zpl'), 'TYPED_REAL_DOWNLOAD')
        if kind == 'pdf':
            link = page.locator('#printLabels')
        else:
            page.locator('#doneView details.other-files > summary').click()
            link = page.locator('#outputFiles a').filter(has_text='Zebra ZPL')
        require(self._expected_download is None, 'NO_OVERLAPPING_EXPECTED_DOWNLOAD')
        pair_before = self.fixture.durable()
        pending = {'kind': kind, 'filename': pair_before['files'][kind]['name'],
                   'used': False, 'event_sequence': None}
        self._expected_download = pending
        try:
            with page.expect_download(timeout=20000) as observed:
                link.click()
            actual = observed.value
            require(pending['used'] and pending['event_sequence'] is not None
                    and self._download_objects.get(actual) == pending['event_sequence'],
                    'REAL_DOWNLOAD_EVENT_CORRELATED_TO_EXPECTED_CLICK')
        finally:
            self._expected_download = None
        require(actual.failure() is None, 'BROWSER_DOWNLOAD_FAILED')
        pair = self.fixture.durable()
        require(pair == pair_before, 'EXPECTED_DOWNLOAD_PRESERVES_DURABLE_PAIR')
        expected = pair['files'][kind]
        require(actual.suggested_filename == expected['name'], 'REAL_DOWNLOAD_NAME')
        target = RUNTIME / (self.case_id + '-' + kind + '-download.' + kind)
        require(not target.exists(), 'DOWNLOAD_TARGET_MUST_BE_FRESH')
        actual.save_as(target)
        owned_identity = (target.lstat().st_dev, target.lstat().st_ino)
        raw = regular(target, MAX_DOWNLOAD)
        require(len(raw) == expected['bytes'] and hashlib.sha256(raw).hexdigest() == expected['sha256'],
                'BROWSER_HTTP_DISK_JOURNAL_BYTES_EQUAL')
        sku = self.fixture.expected_sku()
        if kind == 'pdf':
            with closing(pdfium.PdfDocument(target)) as document:
                require(len(document) == 1, 'REAL_DOWNLOADED_PDF_PAGE_COUNT')
                with closing(document[0]) as pdf_page:
                    with closing(pdf_page.render(scale=203 / 72)) as bitmap:
                        view = bitmap.to_pil()
                        try:
                            owned = view.copy()
                        finally:
                            view.close()
                        with closing(owned) as image:
                            require(sku in [value.text for value in zxingcpp.read_barcodes(image)],
                                    'ACTUAL_DOWNLOADED_PDF_EXACT_SKU')
        else:
            require(raster.zpl_hex(sku) in raw.decode('ascii') and '^PQ1^XZ' in raw.decode('ascii'),
                    'ACTUAL_DOWNLOADED_ZPL_EXACT_SKU')
        self.downloads.append({'kind': kind, 'bytes': len(raw), 'sha256': expected['sha256'],
                               'verified_http_disk_journal': True, 'actual_pdf_decode': kind == 'pdf',
                               'event_sequence': pending['event_sequence']})
        require((target.lstat().st_dev, target.lstat().st_ino) == owned_identity and not target.is_symlink(),
                'OWNED_DOWNLOAD_IDENTITY_CHANGED')
        target.unlink()  # exact file created only by this owned download

    def snapshot(self):
        # Called only after browser.close has completed. No exported ledger
        # retains references mutated by Playwright callbacks or HTTP threads.
        return copy.deepcopy({'network': self.network, 'console': self.console,
            'errors': self.errors, 'overflow': self.overflow, 'captures': self.captures,
            'downloads': self.downloads, 'download_events': self.download_events,
            'expected_download_active': self._expected_download is not None,
            'context_closed': self.context_closed,
            'all_pages_closed': all(page.is_closed() for page in self.pages)})

    def validate(self, final, owned):
        self.check(final['context_closed'] and final['all_pages_closed'], 'REAL_CONTEXT_AND_PAGES_CLOSED')
        self.check(owned['cleanup_complete'] and not owned['active_threads'] and not owned['pending_threads']
                   and not owned['server_thread_alive'], 'REAL_HTTP_FIXTURE_QUIESCED')
        self.check(not owned['response_overflow'] and not owned['server_error_count']
                   and not owned['server_errors'] and not owned['server_error_overflow_count']
                   and owned['first_server_error_overflow'] is None, 'NO_SERVER_ERROR_OR_OVERFLOW')
        broker = owned['broker']
        self.check(broker is not None and not broker['blocked'] and not broker['overflow_count']
                   and broker['first_overflow'] is None and broker['row_limit'] == MAX_ROWS
                   and broker['total_seen'] == len(broker['calls']), 'NO_BLOCKED_BROKER_CALL_OR_OVERFLOW')
        self.check(not final['overflow'] and not final['errors'], 'NO_LEDGER_OVERFLOW_OR_PAGE_ERROR')
        events = final['download_events']
        self.check(not final['expected_download_active']
                   and [event['sequence'] for event in events] == list(range(len(events)))
                   and all(event['expected'] and event['owned_blob'] and event['owned_page']
                           for event in events), 'ALL_BROWSER_DOWNLOAD_EVENTS_ARE_EXPECTED_AND_OWNED')
        self.check(len(events) == len(final['downloads'])
                   and len({item['event_sequence'] for item in final['downloads']}) == len(events)
                   and {item['event_sequence'] for item in final['downloads']} ==
                       {event['sequence'] for event in events},
                   'EVERY_FINAL_DOWNLOAD_EVENT_HAS_ONE_VERIFIED_DISK_JOURNAL_ARTIFACT')
        self.check(all(r['allowed'] and not r['query_present'] for r in final['network']), 'NO_UNALLOWLISTED_NETWORK')
        self.check(all(r['terminal'] == 'finished' or r.get('owned_blob_abort') and bool(final['downloads'])
                       for r in final['network']), 'ALL_ACTUAL_REQUESTS_TERMINAL')
        http = [r for r in final['network'] if not r['owned_blob']]
        self.check(all(r.get('no_store') and r.get('nosniff') and r.get('frame_deny') and r.get('referrer_none')
                       and r.get('self_csp') for r in http), 'REAL_RESPONSE_SECURITY_HEADERS')
        self.check(all(r['mime'] in asset_mimes(r['route']) if r['route'] in STATIC_ASSETS else
                       r['mime'] == ('text/html' if r['route'] == '/' else 'application/pdf' if r['route'].endswith('/pdf') else
                                     'application/vnd.zebra-zpl' if r['route'].endswith('/zpl') else 'application/json')
                       for r in http), 'EXACT_REAL_HTTP_MIME_SCOPE')
        negative = [r for r in http if r['status'] != 200]
        expected = EXPECTED_HTTP_ERROR.get(self.case_id)
        self.check((len(negative) == 1 and negative[0]['route'] == '/api/receipts/{receipt}/labels'
                    and negative[0]['status'] == expected) if expected else not negative, 'EXACT_HTTP_ERROR_SCOPE')
        self.check(all(m['type'] not in ('error', 'warning') or m['expected_label_http_error']
                       for m in final['console']), 'NO_UNEXPECTED_CONSOLE_ERROR')
        self.check(all(any(r['route'] == m['route'] and r['status'] == m['status'] for r in negative)
                       for m in final['console'] if m['expected_label_http_error']), 'CONSOLE_HTTP_ERROR_CORRESPONDENCE')
        self.check(all(any(s['method'] == r['method'] and s['route'] == r['route'] and s['status'] == r['status']
                           for s in owned['responses']) for r in http), 'BROWSER_HTTP_MATCHES_REAL_SERVER_LEDGER')

    def close(self):
        self.context.close()
        self.context_closed = True


def run_browser_suite(browser, runtime_identity):
    """Called after a future reviewed launcher preflight; closes owned browser."""
    try:
        return _run_browser_suite(browser, runtime_identity)
    finally:
        if browser.is_connected():
            browser.close()


def _run_browser_suite(browser, runtime_identity):
    require(sys.platform == 'linux' and os.geteuid() > 0 and os.getegid() > 0, 'OWNED_LINUX_NONROOT_ONLY')
    before = source_snapshot()  # admission remains HOLD in this source candidate
    require(importlib.metadata.version('playwright') == '1.65.2' and
            importlib.metadata.version('greenlet') == '3.5.6', 'EXACT_PLAYWRIGHT_PINS')
    require(type(runtime_identity) is dict and set(runtime_identity) ==
            {'browser', 'driver', 'platform_admission_sha256'} and
            runtime_identity['platform_admission_sha256'] == os.environ.get('SS_PLATFORM_ADMISSION_SHA256') and
            re.fullmatch(r'[a-f0-9]{64}', runtime_identity['platform_admission_sha256']),
            'EXACT_REVIEWED_PLATFORM_RUNTIME_ADMISSION')
    require(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+', browser.version),
            'ACTUAL_PREINSTALLED_CHROME_VERSION_REQUIRED')
    from tools.desktop_label_browser_platform_sandbox import sandbox_probe, owned_browser_processes_gone
    from tools.desktop_label_browser_cases import CASES
    require(tuple(item[0] for item in CASES) == EXPECTED_IDS, 'EXACT7_BROWSER_SELECTION')
    readiness = browser.new_context(ignore_https_errors=False, service_workers='block')
    try:
        sandbox = sandbox_probe(browser, readiness, runtime_identity['driver']['process']['pid'],
                                '/opt/google/chrome/chrome', browser.version)
    finally:
        readiness.close()
    pids = [sandbox['browser']] + sandbox['renderers']
    executable = (Path('/proc') / str(sandbox['browser']['pid']) / 'exe').resolve(strict=True)
    require(executable == Path('/opt/google/chrome/chrome'),
            'EXACT_OFFICIAL_PLATFORM_CHROME_EXECUTABLE')
    actual_binary = streamed_native_identity(executable, 384 * 1024 * 1024)
    require(actual_binary == runtime_identity['browser'], 'EXACT_ACTUAL_PLATFORM_CHROME_BYTES')
    attempts = []
    suite_errors = []
    total = [0]
    cleanup = True
    try:
        for case_id, case in CASES:
            fixture, ctx = DesktopFixture(case_id), None
            prerequisite_failure = False
            row = {'id': case_id, 'status': 'FAIL', 'assertions': [], 'cleanup_complete': False,
                   'native_flow_complete': False, 'teardown_errors': []}
            try:
                fixture.__enter__()
                ctx = BrowserCase(browser, fixture, case_id, total)
                case(ctx)
                # Preserve the actual disk/SQLite observation before owned
                # cleanup. Native completion alone never grants evidence PASS.
                row.update(native_flow_complete=True, durable=fixture.durable())
            except Exception as error:
                row['error_type'] = error_type(error)
                row['error_code'] = str(error) if isinstance(error, FixtureFailure) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(error)) else 'DESKTOP_BROWSER_CASE_FAILED'
                prerequisite_failure = ctx is None or not ctx.bootstrap_complete
            finally:
                if ctx is not None:
                    try:
                        ctx.close()
                    except Exception as error:
                        cleanup = False
                        row['teardown_errors'].append({'code': 'CONTEXT_CLOSE_FAILED', 'error_type': error_type(error)})
                try:
                    fixture.close()
                    row['cleanup_complete'] = fixture.cleanup_complete
                except Exception as error:
                    cleanup = False
                    row['teardown_errors'].append({'code': 'FIXTURE_CLOSE_FAILED', 'error_type': error_type(error)})
                # Retain observers until the browser itself has closed: later
                # context/close events must still reach these original ledgers.
                attempts.append((row, ctx, fixture))
            if not cleanup or prerequisite_failure:
                break  # preserve failure; never retry or reuse a live fixture
    finally:
        try:
            browser.close()
        except Exception as error:
            cleanup = False
            suite_errors.append({'code': 'BROWSER_CLOSE_FAILED', 'error_type': error_type(error)})
    browser_closed = not browser.is_connected()
    if not browser_closed:
        cleanup = False
        suite_errors.append({'code': 'BROWSER_STILL_CONNECTED'})
    results, networks, consoles, errors, server_errors, brokers, captures, downloads, download_events = [], [], [], [], [], [], [], [], []
    for row, ctx, fixture in attempts:
        # No PASS, live-list export or final validation occurs before every
        # context/fixture close and the real browser-close protocol has finished.
        owned = fixture.evidence()
        final = ctx.snapshot() if ctx is not None else None
        row['evidence_finalized'] = bool(browser_closed and final is not None and final['context_closed']
            and final['all_pages_closed'] and owned['cleanup_complete'] and not owned['active_threads']
            and not owned['pending_threads'] and not owned['server_thread_alive'] and not row['teardown_errors'])
        if ctx is not None:
            networks.append({'id': row['id'], 'rows': final['network'], 'overflow': final['overflow']})
            consoles.append({'id': row['id'], 'rows': final['console']})
            errors.append({'id': row['id'], 'rows': final['errors']})
            captures.extend(final['captures'])
            downloads.extend({'id': row['id'], **d} for d in final['downloads'])
            download_events.extend({'id': row['id'], **event} for event in final['download_events'])
            try:
                require(row['evidence_finalized'], 'FINAL_EVIDENCE_NOT_QUIESCED')
                ctx.validate(final, owned)
                row['final_validation_passed'] = True
            except Exception as error:
                row['final_validation_passed'] = False
                row['final_error_type'] = error_type(error)
                row['final_error_code'] = str(error) if isinstance(error, FixtureFailure) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(error)) else 'FINAL_EVIDENCE_VALIDATION_FAILED'
            row['assertions'] = list(ctx.assertions)
        server_errors.append({'id': row['id'], 'rows': owned['server_errors'],
            'total_seen': owned['server_error_count'], 'overflow_count': owned['server_error_overflow_count'],
            'first_overflow': owned['first_server_error_overflow'], 'response_overflow': owned['response_overflow'],
            'active_threads': owned['active_threads'], 'pending_threads': owned['pending_threads'],
            'server_thread_alive': owned['server_thread_alive'], 'teardown_errors': copy.deepcopy(row['teardown_errors'])})
        brokers.append({'id': row['id'], 'ledger': owned['broker']})
        row.update(actual_static_http_probe=copy.deepcopy(fixture.asset_probe),
            actual_loopback_origin_host_probe=copy.deepcopy(fixture.origin_probe),
            server_responses=owned['responses'], broker_ledger=owned['broker'],
            deferred_gate=copy.deepcopy({'kind': fixture.hold.kind, 'used': fixture.hold.used,
                'ready': fixture.hold.ready, 'observation': fixture.hold.observation} if fixture.hold else None))
        if (row['native_flow_complete'] and 'error_code' not in row and row.get('final_validation_passed')
                and row['evidence_finalized'] and not suite_errors):
            row['status'] = 'PASS'
        results.append(row)
    try:
        process_cleanup = owned_browser_processes_gone(pids)
    except Exception as error:
        process_cleanup = False
        suite_errors.append({'code': 'OWNED_PROCESS_CHECK_FAILED', 'error_type': error_type(error)})
    try:
        after = source_snapshot()
        if before != after:
            suite_errors.append({'code': 'SOURCE_BEFORE_AFTER_DIFFERENT'})
    except Exception as error:
        after = []
        suite_errors.append({'code': 'SOURCE_AFTER_CHECK_FAILED', 'error_type': error_type(error)})
    passed = (len(results) == 7 and all(r['status'] == 'PASS' and r['cleanup_complete'] for r in results)
              and tuple(c['name'] for c in captures) == EXPECTED_CAPTURES and len(downloads) == 8 and len(download_events) == 8
              and cleanup and process_cleanup and not suite_errors)
    save_json('desktop-network-ledger.json', networks)
    save_json('desktop-console-ledger.json', consoles)
    save_json('desktop-page-errors.json', errors)
    save_json('desktop-download-events.json', download_events)
    save_json('desktop-server-errors.json', server_errors)
    save_json('desktop-broker-ledger.json', brokers)
    save_json('desktop-source-before.json', before)
    save_json('desktop-source-after.json', after)
    save_json('desktop-sandbox.json', {'actual_sandbox': sandbox, 'owned_processes_gone': process_cleanup,
        'full_debian_browser': actual_binary,
        'platform_admission_sha256': runtime_identity['platform_admission_sha256'],
        'identity_scope': 'main browser and readiness-renderer identities; launcher separately attests actual Node pipe/argv/process and all container descendants'})
    save_json('desktop-browser-summary.json', {'scope': 'Owned current desktop UI, fictional broker only',
        'status': 'PASS' if passed else 'FAIL', 'expected_ids': list(EXPECTED_IDS), 'cases': results,
        'captures': captures, 'downloads': downloads, 'download_events': download_events,
        'cleanup_complete': cleanup,
        'browser_closed': browser_closed, 'suite_errors': suite_errors,
        'final_validation_after_context_fixture_and_browser_close': True,
        'launcher_driver_and_container_cleanup_attestation_required': True,
        'separate_trusted_platform_source_and_node_scope_admission_required': True,
        'no_live_provider_claim': True, 'no_windows_or_hardware_claim': True})
    require(passed, 'DESKTOP_BROWSER_ACCEPTANCE_FAILED')


if __name__ == '__main__':
    raise SystemExit('DESKTOP_BROWSER_REQUIRES_SEPARATELY_REVIEWED_LINUX_LAUNCHER')
