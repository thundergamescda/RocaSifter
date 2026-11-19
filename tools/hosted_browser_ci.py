"""Finite real-HTTPS hosted portal acceptance; Linux container only.

No native desktop imports, live Square, injected authenticated cookies, fulfilled
auth routes, certificate bypass, tracing or password-bearing diagnostics.
The separate shell owns Docker/network/database capture and cleanup.
"""
import sys
sys.dont_write_bytecode = True

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import base64
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import ssl
import stat
import time
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

ROOT = Path('/workspace')
EVIDENCE = Path('/ci-evidence')
EXPECTED_IDS = tuple('HUI%02d' % number for number in range(1, 10))
REPORT_NAMES = ('pip-freeze.txt', 'pip-bootstrap-report.json',
                'pip-install-report.json', 'os-packages.txt')
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_LEDGER_ROWS = 5000
MAX_SCREENSHOT_BYTES = 1536 * 1024
MAX_SCREENSHOTS_TOTAL = 6 * 1024 * 1024
SAFE_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,95}\Z')
NEGATIVE_TLS_CODES = {
    'net::ERR_CERT_AUTHORITY_INVALID', 'net::ERR_CERT_COMMON_NAME_INVALID',
}
DROP_CODES = {
    'net::ERR_EMPTY_RESPONSE', 'net::ERR_CONNECTION_RESET',
    'net::ERR_FAILED', 'net::ERR_ABORTED',
}
EXPECTED_HTTP_ERRORS = {
    'HUI01': {('POST', '/api/auth/password/change', 403)},
    'HUI06': {('POST', '/api/auth/password/change', 403)},
    'HUI07': {('POST', '/api/auth/login', 401)},
}
SHARED_PRODUCTION_FILES = {
    'intake_manifest.py', 'catalog_physical_ownership.py', 'intake_physical_identity.py',
    'scanner_adapters.py', 'intake_label_contract.py',
}
HOSTED_AUTHORITY_FILES = {'hosted/credit_authority.py', 'hosted/staff_authority.py'}
BROWSER_TOOL_FILES = {
    'tools/hosted_browser_ci.py', 'tools/hosted_browser_fixture.py', 'tools/hosted_browser_cases.py',
    'tools/hosted_browser_constraints.txt', 'tools/hosted_browser_requirements.txt',
    'tools/playwright-1.64.1-seccomp.json', 'tools/hosted_browser-seccomp.json',
    'tools/HOSTED_BROWSER_SECCOMP_PROVENANCE.txt', 'tools/PLAYWRIGHT-LICENSE.txt',
    'tools/hosted_browser.Dockerfile', 'tools/hosted_browser.Dockerfile.dockerignore',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def require(condition, code):
    if not isinstance(code, str) or not SAFE_CODE.fullmatch(code):
        raise AssertionError('INVALID_FIXED_CHECK_CODE')
    if not condition:
        raise AssertionError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path, limit=MAX_FILE_BYTES):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), 'NONREGULAR_EVIDENCE_INPUT')
    require(info.st_size <= limit, 'EVIDENCE_INPUT_EXCEEDS_BOUND')
    return path.read_bytes()


def file_identity(path, limit):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_size <= limit,
            'BINARY_FILE_IDENTITY_GUARD')
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    require(path.stat().st_size == info.st_size, 'BINARY_CHANGED_WHILE_HASHING')
    return {'path': str(path), 'sha256': value.hexdigest(), 'bytes': info.st_size}


def save_bytes(name, payload):
    require(bool(re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,79}', name)), 'INVALID_EVIDENCE_NAME')
    require(isinstance(payload, bytes) and len(payload) <= MAX_FILE_BYTES, 'EVIDENCE_OUTPUT_EXCEEDS_BOUND')
    with (EVIDENCE / name).open('xb') as target:
        target.write(payload)


def save_json(name, value):
    save_bytes(name, (json.dumps(value, indent=2, allow_nan=False) + '\n').encode('utf-8'))


def walk_error(error):
    raise error


def source_snapshot():
    result = {}
    total = 0
    for directory, dirs, files in os.walk(ROOT, topdown=True, followlinks=False, onerror=walk_error):
        dirs.sort()
        for name in dirs:
            require(not (Path(directory) / name).is_symlink(), 'SOURCE_DIRECTORY_LINK')
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(ROOT).as_posix()
            require(not relative.startswith(('.git/', 'Testing/')), 'UNEXPECTED_COPIED_SOURCE')
            require(not name.startswith('.env') and path.suffix not in ('.key', '.pem', '.db', '.sqlite', '.sqlite3', '.pyc'),
                    'FORBIDDEN_COPIED_SOURCE')
            data = read_regular(path, 16 * 1024 * 1024)
            total += len(data)
            require(total <= MAX_SOURCE_BYTES, 'COPIED_SOURCE_EXCEEDS_BOUND')
            result[relative] = {'sha256': digest(data), 'bytes': len(data)}
    require('hosted/app.py' in result and 'tools/hosted_browser_cases.py' in result,
            'REQUIRED_COPIED_SOURCE_MISSING')
    return result


def verify_source_scope(snapshot):
    required = SHARED_PRODUCTION_FILES | BROWSER_TOOL_FILES | HOSTED_AUTHORITY_FILES | {'requirements-hosted.txt'}
    require(required.issubset(snapshot), 'COPIED_SOURCE_REQUIRED_INPUT_MISSING')
    require(len(snapshot) == 81 and
            all(path in required or path.startswith('hosted/') for path in snapshot),
            'COPIED_SOURCE_SCOPE_MISMATCH')



def launch_stderr_excerpt(error):
    """Bounded browser stderr only, before any browser context or sign-in.

    This excludes the SDK exception, stack and launch-wrapper rows. The browser receives
    only safe_env, no database or merchant credentials, and no target app URL.
    Actual test-page exceptions never enter this diagnostic channel.
    """
    rows = []
    matched = 0
    fixture_dsn = os.environ.get('DATABASE_URL', '')
    for line in str(error).splitlines():
        found = re.search(r'\[pid=([0-9]{1,10})\]\[err\]\s*(.*)', line)
        if not found:
            continue
        matched += 1
        if len(rows) >= 40:
            continue
        value = found.group(2)
        if fixture_dsn:
            value = value.replace(fixture_dsn, '[fixture database redacted]')
        if re.search(r'(?i)authorization|bearer\s|password|secret|csrf|postgres(?:ql)?://|\btoken\b', value):
            value = '[sensitive startup line redacted]'
        value = re.sub(r'https?://\S+', '[url]', value, flags=re.IGNORECASE)
        value = ''.join(character if character == '\t' or character.isprintable() else ' '
                        for character in value)
        rows.append({'pid': int(found.group(1)), 'stderr': value[:512],
                     'line_truncated': len(value) > 512})
    return {
        'scope': 'Selected browser-process stderr before any context/credentials; SDK wrapper and stack excluded',
        'maximum_rows': 40, 'maximum_characters_per_row': 512,
        'matching_rows': matched, 'truncated': matched > 40,
        'rows': rows,
    }

class DiscardOutput(io.TextIOBase):
    """Count diagnostics without retaining or exporting private text."""
    def __init__(self):
        self.characters = 0

    def write(self, value):
        self.characters += len(value)
        return len(value)

    def flush(self):
        return None


def safe_error_type(error):
    name = type(error).__name__
    return name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name) else 'Exception'


def safe_error(error):
    if isinstance(error, AssertionError) and SAFE_CODE.fullmatch(str(error)):
        return str(error)
    fixture_code = getattr(error, 'code', None)
    if (type(error).__name__ == 'PortalFixtureError' and isinstance(fixture_code, str) and
            re.fullmatch(r'fixture_[a-z0-9_]{1,80}', fixture_code)):
        return fixture_code.upper()
    kind = safe_error_type(error)
    if kind in ('TargetClosedError', 'Error') and type(error).__module__.startswith('playwright.'):
        # Inspect private exception text only for exact known classifications.
        # Never retain/export its message, command line, environment or stack.
        message = str(error)
        for marker, code in (
            ('Failed to move to new namespace', 'CHROMIUM_NAMESPACE_SETUP_FAILED'),
            ('No usable sandbox!', 'CHROMIUM_SANDBOX_UNAVAILABLE'),
            ('error while loading shared libraries:', 'CHROMIUM_SHARED_LIBRARY_UNAVAILABLE'),
            ("Executable doesn't exist", 'BUNDLED_CHROMIUM_EXECUTABLE_MISSING'),
        ):
            if marker in message:
                return code
    return 'UNEXPECTED_' + re.sub(r'(?<!^)(?=[A-Z])', '_', kind).upper()


def public_path(url):
    parsed = urlsplit(url)
    path = parsed.path
    if not re.fullmatch(r'/[A-Za-z0-9_./-]{0,179}', path):
        return '/[redacted]'
    parts = path.split('/')
    return '/'.join('[id]' if len(part) > 64 or
                    re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}', part)
                    else part for part in parts)


def origin_of(url):
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        return None
    return parsed.scheme + '://' + parsed.netloc


def process_record(pid):
    require(isinstance(pid, int) and pid > 0, 'INVALID_BROWSER_PROCESS_ID')
    directory = Path('/proc') / str(pid)
    fields = {}
    for line in (directory / 'status').read_text().splitlines():
        key, _, value = line.partition(':')
        fields[key] = value.strip()
    command = (directory / 'cmdline').read_bytes().split(b'\0')
    blocked = (b'--no-sandbox', b'--disable-setuid-sandbox', b'--disable-seccomp-filter-sandbox',
               b'--disable-namespace-sandbox', b'--single-process')
    disabled = any(arg.split(b'=', 1)[0] in blocked for arg in command)
    ticks = (directory / 'stat').read_text().rsplit(') ', 1)[1].split()[19]
    return {
        'pid': pid, 'start_ticks': ticks,
        'effective_uid': int(fields['Uid'].split()[1]),
        'no_new_privileges': int(fields.get('NoNewPrivs', '-1')),
        'seccomp': int(fields.get('Seccomp', '-1')),
        'seccomp_filters': int(fields.get('Seccomp_filters', '-1')),
        'sandbox_disabling_flag': disabled,
    }


def sandbox_probe(browser, context):
    page = context.new_page()
    page.goto('data:text/html,<title>Owned sandbox readiness</title>',
              wait_until='domcontentloaded')
    # This owned, credential-free document exists only to create a renderer.
    session = browser.new_browser_cdp_session()
    try:
        info = session.send('SystemInfo.getProcessInfo')['processInfo']
    finally:
        session.detach()
    main = [process_record(int(item['id'])) for item in info if item['type'] == 'browser']
    renderers = [process_record(int(item['id'])) for item in info if item['type'] == 'renderer']
    require(len(main) == 1 and bool(renderers), 'SANDBOX_PROCESSES_MISSING')
    for item in main + renderers:
        require(item['effective_uid'] == 10001 and item['no_new_privileges'] == 1 and
                item['seccomp'] == 2 and not item['sandbox_disabling_flag'],
                'BROWSER_SANDBOX_PROCESS_GUARD')
    require(all(item['seccomp_filters'] > main[0]['seccomp_filters'] for item in renderers),
            'RENDERER_SECCOMP_FILTER_NOT_ADDITIONAL')
    page.close()
    return {'browser': main[0], 'renderers': renderers,
            'explicit_chromium_sandbox': True, 'additional_renderer_filter': True}


def owned_browser_processes_gone(identities):
    deadline = time.monotonic() + 5
    while True:
        remaining = []
        for identity in identities:
            try:
                current = process_record(identity['pid'])
                if current['start_ticks'] == identity['start_ticks']:
                    remaining.append(identity)
            except (FileNotFoundError, ProcessLookupError):
                pass
            except Exception:
                return False
        if not remaining:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class CaseContext:
    def __init__(self, fixture, browser):
        self.fixture, self.browser = fixture, browser
        self.passwords = {role: account.password for role, account in fixture.accounts.items()}
        self.case_id = None
        self.contexts = []
        self.network = []
        self.js_errors = []
        self.console_events = []
        self.screenshots = []
        self.layout_overflow = None
        self.screenshot_bytes = 0
        self.ledger_overflow = False
        self.cleanup_complete = True

    @staticmethod
    def require(condition, code):
        require(condition, code)

    def _append(self, collection, row):
        if len(collection) >= MAX_LEDGER_ROWS:
            self.ledger_overflow = True
        else:
            collection.append(row)

    def _event(self, request, event, *, status=None, failure=None, blocked=False):
        candidate = origin_of(request.url)
        known = {
            self.fixture.origin: 'trusted',
            self.fixture.untrusted_origin: 'negative_ca',
            self.fixture.hostname_mismatch_origin: 'negative_hostname',
        }
        method = request.method if request.method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS') else 'OTHER'
        path = public_path(request.url) if candidate in known else '/[external]'
        allowed_failure = False
        if event == 'failed':
            tls_codes = {
                self.fixture.untrusted_origin: 'net::ERR_CERT_AUTHORITY_INVALID',
                self.fixture.hostname_mismatch_origin: 'net::ERR_CERT_COMMON_NAME_INVALID',
            }
            allowed_failure = (
                self.case_id == 'HUI02' and candidate in tls_codes
                and method == 'GET' and path == '/' and failure == tls_codes[candidate]
            ) or (
                self.case_id == 'HUI08' and candidate == self.fixture.origin
                and method == 'POST' and path == '/api/auth/password/change'
                and failure in DROP_CODES
            )
        expected_http_error = (
            event == 'response' and candidate == self.fixture.origin and
            ((method == 'GET' and path == '/api/me' and status == 401 and
              self.case_id in EXPECTED_IDS) or
             (method, path, status) in EXPECTED_HTTP_ERRORS.get(self.case_id, set()))
        )
        self._append(self.network, {
            'case': self.case_id, 'event': event, 'origin_kind': known.get(candidate, 'external'),
            'method': method, 'path': path, 'status': status,
            'failure_code': failure if failure in NEGATIVE_TLS_CODES | DROP_CODES else
                            ('OTHER_NETWORK_FAILURE' if failure else None),
            'expected_failure': allowed_failure, 'blocked_external': blocked,
            'expected_http_error': expected_http_error,
        })

    def _page(self, page):
        page.on('pageerror', lambda error: self._append(
            self.js_errors, {'case': self.case_id, 'code': 'UNHANDLED_PAGE_ERROR'}))

        def console(message):
            if message.type != 'error':
                return
            resource = message.text.startswith('Failed to load resource:')
            url = message.location.get('url', '')
            kinds = {
                self.fixture.origin: 'trusted',
                self.fixture.untrusted_origin: 'negative_ca',
                self.fixture.hostname_mismatch_origin: 'negative_hostname',
            }
            kind = kinds.get(origin_of(url), 'unattributed')
            self._append(self.console_events, {
                'case': self.case_id,
                'code': 'RESOURCE_ERROR' if resource else 'APPLICATION_CONSOLE_ERROR',
                'origin_kind': kind, 'path': public_path(url) if kind != 'unattributed' else '/[unattributed]',
            })
        page.on('console', console)
        page.on('crash', lambda: self._append(
            self.js_errors, {'case': self.case_id, 'code': 'PAGE_CRASH'}))

    def new_context(self, viewport=None):
        viewport = viewport or {'width': 1440, 'height': 1000}
        require(viewport in ({'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}),
                'UNAPPROVED_VIEWPORT')
        context = self.browser.new_context(
            viewport=viewport, device_scale_factor=1, locale='en-US',
            timezone_id='America/Boise', color_scheme='light', reduced_motion='reduce',
            ignore_https_errors=False, accept_downloads=False, service_workers='block',
        )
        context.set_default_timeout(10000)
        context.set_default_navigation_timeout(15000)
        allowed = {self.fixture.origin}
        if self.case_id == 'HUI02':
            allowed |= {self.fixture.untrusted_origin, self.fixture.hostname_mismatch_origin}

        def route(candidate):
            if origin_of(candidate.request.url) in allowed:
                candidate.continue_()
            else:
                self._event(candidate.request, 'blocked', blocked=True)
                candidate.abort('blockedbyclient')

        context.route('**/*', route)
        context.on('request', lambda request: self._event(request, 'request'))
        context.on('response', lambda response: self._event(response.request, 'response', status=response.status))
        context.on('requestfailed', lambda request: self._event(request, 'failed', failure=request.failure))
        context.on('page', self._page)

        def websocket(socket):
            self._append(self.js_errors, {'case': self.case_id, 'code': 'UNEXPECTED_WEBSOCKET'})
            socket.close(code=1008, reason='No fixture websocket transport')
        context.route_web_socket('**/*', websocket)
        self.contexts.append(context)
        return context

    def record_layout_overflow(self, page):
        if self.layout_overflow is not None:
            return
        allowed = {
            'tags': ('HTML', 'BODY', 'MAIN', 'HEADER', 'IMG', 'DIV', 'STRONG', 'SPAN', 'B',
                     'SECTION', 'P', 'H1', 'H2', 'H3', 'FORM', 'LABEL', 'INPUT', 'SELECT',
                     'OPTION', 'BUTTON', 'DL', 'DT', 'DD', 'UL', 'LI', 'FIELDSET', 'LEGEND',
                     'A', 'FOOTER', 'PRE'),
            'ids': ('signin', 'invitation', 'pilotSetup', 'workspace', 'storeName', 'signOut',
                    'connectSquare', 'refreshSquare', 'squareStatus', 'squareRenewalStatus',
                    'staffPanel', 'staffList', 'refreshStaff', 'inviteStaffForm', 'staffInviteEmail',
                    'staffInviteRole', 'staffInvitationResult', 'staffInvitationLink', 'staffStatus',
                    'cleanupObserver', 'cleanupObserverConfig', 'cleanupObserverTitle',
                    'cleanupObserverGate', 'cleanupObserverCoverage', 'cleanupObserverReasons',
                    'cleanupObserverCadence', 'cleanupObserverStale', 'cleanupObserverSave',
                    'cleanupObserverCheck', 'cleanupObserverRequest', 'accountSecurity',
                    'mfaEnrollForm', 'mfaEnrollment', 'mfaManageForm', 'mfaRecoveryResult',
                    'mfaSecuritySummary', 'passwordChangeSection', 'passwordChangeForm',
                    'passwordChangeNotice', 'passwordChangeCurrent', 'passwordChangeNew',
                    'passwordChangeConfirm', 'passwordChangeSubmit', 'passwordChangeStatus'),
            'classes': ('shell', 'mark', 'badge', 'intro', 'eyebrow', 'panel', 'panel-heading',
                        'status-card', 'status', 'help', 'quiet', 'setup-links', 'staff-row',
                        'cleanup-observer', 'cleanup-state', 'cleanup-facts', 'cleanup-reasons',
                        'cleanup-actions', 'cleanup-check', 'cleanup-timing', 'cleanup-boundary'),
        }
        try:
            require(self.case_id == 'HUI04' and origin_of(page.url) == self.fixture.origin,
                    'LAYOUT_DIAGNOSTIC_CONTEXT')
            value = page.evaluate('''allowed=>{
                const tags=new Set(allowed.tags),ids=new Set(allowed.ids),classes=new Set(allowed.classes);
                let numbersBounded=false;
                const metric=value=>{
                    if(!Number.isFinite(value)){numbersBounded=true;return 0}
                    if(Math.abs(value)>1048576){numbersBounded=true;return Math.sign(value)*1048576}
                    return Math.round(value*100)/100;
                };
                const viewport=metric(window.innerWidth),root=document.documentElement,body=document.body;
                const rows=[],walker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT);
                let node=root,visited=0,offenders=0;
                while(node&&visited<1000){
                    const index=visited++;
                    if(node.getClientRects().length){
                        const rect=node.getBoundingClientRect(),style=getComputedStyle(node);
                        if(rect.width>0&&rect.height>0&&style.visibility!=='hidden'&&
                           style.visibility!=='collapse'&&Number(style.opacity)!==0){
                            const left=metric(rect.left),right=metric(rect.right),width=metric(rect.width),
                                  client=metric(node.clientWidth),scroll=metric(node.scrollWidth);
                            if(right>viewport||left<0||scroll>client){
                                offenders++;
                                const tokens=[];
                                for(let tokenIndex=0;tokenIndex<Math.min(node.classList.length,16);tokenIndex++){
                                    const token=node.classList[tokenIndex];
                                    if(classes.has(token)&&tokens.length<4&&!tokens.includes(token))tokens.push(token);
                                }
                                rows.push({index,tag:tags.has(node.tagName)?node.tagName:null,
                                    id:ids.has(node.id)?node.id:null,classes:tokens,left,right,width,
                                    client_width:client,scroll_width:scroll,
                                    score:Math.max(right-viewport,-left,scroll-client)});
                                rows.sort((a,b)=>b.score-a.score||a.index-b.index);
                                if(rows.length>32)rows.pop();
                            }
                        }
                    }
                    node=walker.nextNode();
                }
                return {viewport_width:viewport,document_client_width:metric(root.clientWidth),
                    document_scroll_width:metric(root.scrollWidth),body_client_width:metric(body?body.clientWidth:0),
                    body_scroll_width:metric(body?body.scrollWidth:0),visited_nodes:visited,offender_count:offenders,
                    scan_truncated:!!node,records_truncated:offenders>32,numbers_bounded:numbersBounded,
                    nodes:rows.map(({score,...row})=>row)};
            }''', allowed)
            number = lambda item: type(item) in (int, float) and item == item and abs(item) <= 1048576
            metrics = ('viewport_width', 'document_client_width', 'document_scroll_width',
                       'body_client_width', 'body_scroll_width')
            require(type(value) is dict and set(value) == set(metrics) |
                    {'visited_nodes', 'offender_count', 'scan_truncated', 'records_truncated',
                     'numbers_bounded', 'nodes'}, 'LAYOUT_DIAGNOSTIC_SHAPE')
            require(all(number(value[key]) for key in metrics) and
                    type(value['visited_nodes']) is int and 0 <= value['visited_nodes'] <= 1000 and
                    type(value['offender_count']) is int and 0 <= value['offender_count'] <= value['visited_nodes'] and
                    all(type(value[key]) is bool for key in ('scan_truncated', 'records_truncated', 'numbers_bounded')) and
                    type(value['nodes']) is list and len(value['nodes']) <= 32, 'LAYOUT_DIAGNOSTIC_BOUNDS')
            for row in value['nodes']:
                require(type(row) is dict and set(row) ==
                        {'index', 'tag', 'id', 'classes', 'left', 'right', 'width', 'client_width', 'scroll_width'} and
                        type(row['index']) is int and 0 <= row['index'] < value['visited_nodes'] and
                        (row['tag'] is None or row['tag'] in allowed['tags']) and
                        (row['id'] is None or row['id'] in allowed['ids']) and
                        type(row['classes']) is list and len(row['classes']) <= 4 and
                        all(type(item) is str and item in allowed['classes'] for item in row['classes']) and
                        all(number(row[key]) for key in ('left', 'right', 'width', 'client_width', 'scroll_width')),
                        'LAYOUT_DIAGNOSTIC_NODE')
            require(len(json.dumps(value, allow_nan=False).encode('utf-8')) <= 32768,
                    'LAYOUT_DIAGNOSTIC_BYTES')
            self.layout_overflow = {'case': 'HUI04', 'status': 'captured', 'snapshot': value}
        except Exception:
            # Never export browser exceptions or replace the original failing width assertion.
            self.layout_overflow = {'case': 'HUI04', 'status': 'not_captured',
                                    'code': 'LAYOUT_DIAGNOSTIC_UNAVAILABLE'}

    def screenshot(self, page, name, mask_selectors=()):
        require(isinstance(name, str) and bool(re.fullmatch(r'hui[0-9]{2}-[a-z0-9-]{1,48}\.png', name)),
                'INVALID_SCREENSHOT_NAME')
        require(name.startswith(self.case_id.lower() + '-') and
                all(item['name'] != name for item in self.screenshots) and len(self.screenshots) < 16,
                'SCREENSHOT_IDENTITY_OR_COUNT')
        require(origin_of(page.url) == self.fixture.origin, 'SCREENSHOT_ORIGIN')
        defaults = ('input', 'textarea', '#mfaRecoveryCodes', '#mfaSetupKey',
                    '#staffInvitationResult', '#staffList', '#storeName')
        selectors = defaults + tuple(mask_selectors)
        require(all(type(selector) is str and selector in defaults for selector in selectors),
                'SCREENSHOT_MASK_SELECTOR')
        masks = [page.locator(selector) for selector in selectors]
        # Hide the sensitive render too; full-page mask coordinates can drift.
        # Screenshot-only style preserves geometry and is restored by Playwright.
        privacy_style = ','.join(part for selector in selectors
                                 for part in (selector, selector + ' *')) + '{visibility:hidden!important}'
        payload = page.screenshot(type='png', full_page=True, animations='disabled',
                                  mask=masks, mask_color='#27272A', timeout=15000,
                                  style=privacy_style)
        require(len(payload) <= MAX_SCREENSHOT_BYTES and
                self.screenshot_bytes + len(payload) <= MAX_SCREENSHOTS_TOTAL,
                'SCREENSHOT_BYTES_EXCEED_BOUND')
        require(payload.startswith(b'\x89PNG\r\n\x1a\n'), 'SCREENSHOT_NOT_PNG')
        self.screenshot_bytes += len(payload)
        self.screenshots.append({
            'name': name, 'case': self.case_id, 'bytes': len(payload),
            'sha256': digest(payload), 'viewport': page.viewport_size,
            'mask_policy': 'All inputs/textarea plus MFA seed/recovery and staff/invitation/store identity displays',
            'base64': base64.b64encode(payload).decode('ascii'),
        })

    def close_contexts(self):
        for context in reversed(self.contexts):
            try:
                context.close()
            except Exception:
                self.cleanup_complete = False
        self.contexts.clear()

    def verify_ledgers(self):
        require(not self.ledger_overflow, 'BROWSER_LEDGER_OVERFLOW')
        require(not self.js_errors, 'BROWSER_PAGE_ERRORS')
        require(not any(row['code'] == 'APPLICATION_CONSOLE_ERROR' for row in self.console_events),
                'BROWSER_CONSOLE_APPLICATION_ERRORS')
        require(not any(row['blocked_external'] for row in self.network), 'BROWSER_EXTERNAL_REQUEST_ATTEMPT')
        responses = [row for row in self.network if row['event'] == 'response']
        require(not any(row['status'] >= 400 and not row['expected_http_error'] for row in responses),
                'UNEXPECTED_HTTP_ERROR_RESPONSE')
        failures = [row for row in self.network if row['event'] == 'failed']
        require(not any(not row['expected_failure'] for row in failures), 'UNEXPECTED_LOCAL_REQUEST_FAILURE')
        for console in self.console_events:
            if console['code'] == 'RESOURCE_ERROR':
                require(any(row['case'] == console['case'] and
                            row['origin_kind'] == console['origin_kind'] and
                            row['path'] == console['path'] and
                            ((row['event'] == 'failed' and row['expected_failure']) or
                             (row['event'] == 'response' and row['expected_http_error']))
                            for row in self.network), 'UNEXPECTED_RESOURCE_CONSOLE_ERROR')
        dropped = [row for row in failures if row['case'] == 'HUI08']
        require(len(dropped) == 1, 'LOST_REPLY_NETWORK_FAILURE_COUNT')
        for kind in ('negative_ca', 'negative_hostname'):
            require(sum(row['case'] == 'HUI02' and row['origin_kind'] == kind for row in failures) == 1,
                    'NEGATIVE_TLS_FAILURE_COUNT')
        for viewport in ({'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}):
            require(any(item['case'] == 'HUI04' and item['viewport'] == viewport
                        for item in self.screenshots), 'REQUIRED_LAYOUT_SCREENSHOT_MISSING')


def web_probe(fixture):
    context = ssl.create_default_context(cafile=str(fixture.ca_path))
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    with opener.open(fixture.origin + '/healthz', timeout=10) as response:
        require(response.status == 200, 'TRUSTED_PYTHON_HTTPS_READINESS')
        require(len(response.read(4097)) <= 4096, 'READINESS_RESPONSE_EXCEEDS_BOUND')
    with fixture.database.transaction() as connection:
        db = connection.execute(
            'SELECT current_database() AS name,current_schema() AS schema,'
            '(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()) AS ssl,'
            '(SELECT version FROM pg_stat_ssl WHERE pid=pg_backend_pid()) AS tls_version,'
            "current_setting('server_version_num') AS server_version_num").fetchone()
    require(db['ssl'] is True and re.fullmatch(r'ss_ci_[0-9a-f]{16}', db['name']) and
            re.fullmatch(r'ss_test_[0-9a-f]{32}', db['schema']), 'OWNED_DATABASE_TLS_PROBE')
    return {
        'web': {'https': True, 'ca_verified': True, 'hostname_verified': True,
                'health_status': 200, 'python_certificate_bypass': False},
        'database': {'owned_database_name_matches': True, 'owned_schema_matches': True,
                     'ssl': db['ssl'], 'tls_version': db['tls_version'],
                     'server_version_num': int(db['server_version_num'])},
    }


def main():
    started = now()
    require(sys.platform == 'linux' and os.geteuid() == 10001, 'LINUX_NONROOT_FIXTURE_REQUIRED')
    require(sys.version_info[:3] == (3, 12, 16), 'EXACT_PYTHON_VERSION_REQUIRED')
    require(os.environ.get('SS_HOSTED_BROWSER_FIXTURE') == '1' and
            re.fullmatch(r'[0-9a-f]{40}', os.environ.get('SS_CI_SOURCE_SHA', '')),
            'EXPLICIT_HOSTED_BROWSER_ADMISSION_REQUIRED')
    require(EVIDENCE.is_dir() and not EVIDENCE.is_symlink() and EVIDENCE.stat().st_uid == 10001,
            'OWNED_EVIDENCE_DIRECTORY_REQUIRED')
    require(not any(os.environ.get(name) for name in ('DEBUG', 'PWDEBUG', 'NODE_OPTIONS', 'NODE_EXTRA_CA_CERTS')),
            'UNAPPROVED_DEBUG_OR_TRUST_ENV')
    sys.path.insert(0, str(ROOT))
    before = source_snapshot()
    save_json('copied-source-before.json', before)
    for name in REPORT_NAMES:
        save_bytes(name, read_regular(Path('/opt/hosted-browser-evidence') / name))
    sink = DiscardOutput()
    results = []
    log = []
    errors = []
    error_types = []
    stage = 'source_scope'
    failure_stage = None
    runtime = {'scope': 'Pinned hosted Linux Chromium; sandbox readiness incomplete',
               'started': False, 'readiness_complete': False,
               'playwright_driver_started': False, 'launch_attempted': False,
               'launched': False, 'pid_observation_complete': False,
               'explicit_chromium_sandbox': True,
               'ignore_https_errors': False, 'uid': os.geteuid()}
    tls = {'scope': 'Owned fixture probe only', 'before': None, 'after': None}
    fixture = browser = playwright = cases_context = None
    fixture_cleanup = browser_cleanup = True
    sandbox_pids = []
    with redirect_stdout(sink), redirect_stderr(sink):
        try:
            verify_source_scope(before)
            stage = 'dependency_imports'
            from playwright.sync_api import sync_playwright
            import playwright as playwright_package
            from tools.hosted_browser_fixture import PortalFixture
            from tools.hosted_browser_cases import CASES
            stage = 'dependency_versions'
            require(tuple(item[0] for item in CASES) == EXPECTED_IDS and
                    len({item[0] for item in CASES}) == len(EXPECTED_IDS),
                    'EXACT_BROWSER_CASE_SELECTION')
            require(importlib.metadata.version('playwright') == '1.64.1' and
                    importlib.metadata.version('pyee') == '14.0.0' and
                    importlib.metadata.version('greenlet') == '3.5.6', 'PINNED_BROWSER_DEPENDENCIES')
            stage = 'fixture_setup'
            fixture = PortalFixture(EVIDENCE)
            fixture.__enter__()
            stage = 'verified_https_probe'
            tls['before'] = web_probe(fixture)
            stage = 'playwright_driver_start'
            playwright = sync_playwright().start()
            runtime['playwright_driver_started'] = True
            stage = 'bundled_binary_identity'
            executable = Path(playwright.chromium.executable_path)
            require(executable.is_file() and executable.resolve().is_relative_to('/opt/ss-playwright'),
                    'BUNDLED_CHROMIUM_EXECUTABLE_REQUIRED')
            binary = file_identity(executable, 512 * 1024 * 1024)
            metadata_path = Path(playwright_package.__file__).parent / 'driver/package/browsers.json'
            metadata = json.loads(read_regular(metadata_path))
            chromium = next(item for item in metadata['browsers'] if item['name'] == 'chromium')
            safe_env = {key: os.environ[key] for key in
                        ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'PLAYWRIGHT_BROWSERS_PATH')
                        if key in os.environ}
            runtime.update(binary=binary, bundled_revision=chromium['revision'],
                           expected_chromium_version=chromium['browserVersion'],
                           browser_metadata_sha256=digest(read_regular(metadata_path)),
                           launch_attempted=True, launched=False)
            stage = 'chromium_launch'
            browser = playwright.chromium.launch(
                executable_path=str(executable), headless=True, chromium_sandbox=True,
                timeout=30000, env=safe_env,
            )
            runtime['launched'] = True
            stage = 'chromium_version_check'
            require(browser.version == chromium['browserVersion'], 'BUNDLED_CHROMIUM_VERSION_MISMATCH')
            stage = 'sandbox_probe'
            readiness_context = browser.new_context(ignore_https_errors=False, service_workers='block')
            try:
                sandbox = sandbox_probe(browser, readiness_context)
            finally:
                readiness_context.close()
            sandbox_pids = [sandbox['browser']] + sandbox['renderers']
            runtime = {
                'scope': 'Actual bundled Chromium and Linux process sandbox readiness',
                'started': True, 'python': sys.version.split()[0],
                'readiness_complete': True, 'playwright_driver_started': True,
                'pid_observation_complete': True,
                'playwright': importlib.metadata.version('playwright'),
                'pyee': importlib.metadata.version('pyee'),
                'greenlet': importlib.metadata.version('greenlet'),
                'chromium_version': browser.version, 'bundled_revision': chromium['revision'],
                'browser_metadata_sha256': digest(read_regular(metadata_path)),
                'binary': binary,
                'explicit_executable_path': True, 'headless': True,
                'launch_attempted': True, 'launched': True,
                'explicit_chromium_sandbox': True, 'ignore_https_errors': False,
                'no_auth_route_fulfillment': True, 'no_authenticated_cookie_injection': True,
                'locale': 'en-US', 'timezone': 'America/Boise',
                'viewports': [{'width': 1440, 'height': 1000}, {'width': 390, 'height': 844}],
                'sandbox': sandbox,
            }
            cases_context = CaseContext(fixture, browser)
            stage = 'browser_cases'
            for identifier, function in CASES:
                cases_context.case_id = identifier
                case_started = time.monotonic()
                log.append('START ' + identifier)
                outcome = {'id': identifier, 'started': True, 'outcome': 'fail', 'code': None}
                try:
                    function(cases_context)
                    outcome['outcome'] = 'pass'
                except Exception as error:
                    outcome['code'] = safe_error(error)
                finally:
                    cases_context.close_contexts()
                outcome['duration_seconds'] = round(time.monotonic() - case_started, 6)
                results.append(outcome)
                log.append(('PASS ' if outcome['outcome'] == 'pass' else 'FAIL ') + identifier +
                           (' ' + outcome['code'] if outcome['code'] else '') +
                           ' duration_seconds=' + str(outcome['duration_seconds']))
            stage = 'ledgers_and_final_probe'
            cases_context.verify_ledgers()
            require(fixture.provider_attempts == 0, 'PROVIDER_TRANSPORT_ATTEMPT')
            tls['after'] = web_probe(fixture)
            stage = 'completed'
        except Exception as error:
            failure_stage = stage
            if stage == 'chromium_launch' and browser is None and not runtime['launched']:
                runtime['launch_stderr'] = launch_stderr_excerpt(error)
            errors.append(safe_error(error))
            error_types.append(safe_error_type(error))
        finally:
            if cases_context is not None:
                cases_context.close_contexts()
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    browser_cleanup = False
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    browser_cleanup = False
            # Probe only PIDs supplied by this actual browser's own CDP session.
            # Compare start ticks as well as PID to avoid blaming a reused PID.
            if not owned_browser_processes_gone(sandbox_pids):
                browser_cleanup = False
            if fixture is not None:
                try:
                    fixture.__exit__(None, None, None)
                    fixture_cleanup = fixture.cleanup_complete is True
                except Exception:
                    fixture_cleanup = False
    for identifier in EXPECTED_IDS:
        if not any(row['id'] == identifier for row in results):
            results.append({'id': identifier, 'started': False, 'outcome': 'not_run',
                            'code': 'FIXTURE_NOT_READY', 'duration_seconds': 0})
            log.append('NOT_RUN ' + identifier + ' FIXTURE_NOT_READY')
    after = source_snapshot()
    stable = before == after
    save_json('copied-source-after.json', after)
    save_json('browser-runtime.json', runtime)
    save_json('tls-client-probe.json', tls)
    screenshots = cases_context.screenshots if cases_context else []
    manifest = [{key: value for key, value in item.items() if key != 'base64'} for item in screenshots]
    save_json('screenshots-manifest.json', {
        'scope': 'Masked fictional fixture UI only; no unmasked input/MFA/recovery displays',
        'count': len(manifest), 'total_png_bytes': sum(item['bytes'] for item in manifest),
        'screenshots': manifest,
    })
    save_json('screenshots.json', {'encoding': 'base64 PNG', 'screenshots': screenshots})
    network = cases_context.network if cases_context else []
    js_errors = cases_context.js_errors if cases_context else []
    console = cases_context.console_events if cases_context else []
    save_json('network-ledger.json', {
        'scope': 'Browser method/path/status only; no query/fragment/body/headers/cookies',
        'overflow': cases_context.ledger_overflow if cases_context else False,
        'events': network,
    })
    save_json('js-errors.json', {'scope': 'Fixed codes only; no raw JS/console text',
                                'page_errors': js_errors, 'console_errors': console})
    save_bytes('browser-cases.log', ('\n'.join(log) + '\n').encode('utf-8'))
    passed = (
        not errors and stable and browser_cleanup and fixture_cleanup and
        cases_context is not None and cases_context.cleanup_complete and runtime['readiness_complete'] and
        tuple(row['id'] for row in results) == EXPECTED_IDS and
        all(row['started'] and row['outcome'] == 'pass' for row in results) and
        tls['before'] is not None and tls['after'] is not None
    )
    save_json('browser-summary.json', {
        'scope': 'Nine finite real-HTTPS hosted portal browser flows; not desktop/provider/production acceptance',
        'started_utc': started, 'finished_utc': now(), 'source_sha': os.environ['SS_CI_SOURCE_SHA'],
        'passed': passed, 'exit_code': 0 if passed else 1,
        'expected_case_ids': list(EXPECTED_IDS), 'expected_count': len(EXPECTED_IDS),
        'started_case_ids': [row['id'] for row in results if row['started']],
        'successful_case_ids': [row['id'] for row in results if row['outcome'] == 'pass'],
        'case_results': results, 'failed_count': sum(row['outcome'] == 'fail' for row in results),
        'not_run_count': sum(row['outcome'] == 'not_run' for row in results),
        'error_codes': errors, 'error_types': error_types,
        'last_stage': stage, 'failure_stage': failure_stage,
        'source_scope_matches': (SHARED_PRODUCTION_FILES | BROWSER_TOOL_FILES | HOSTED_AUTHORITY_FILES |
                                {'requirements-hosted.txt'}).issubset(before) and len(before) == 81 and
            all(path.startswith('hosted/') or path in SHARED_PRODUCTION_FILES or
                path in BROWSER_TOOL_FILES or path == 'requirements-hosted.txt' for path in before),
        'source_stable': stable, 'copied_source_count': len(before),
        'browser_cleanup_complete': browser_cleanup, 'fixture_cleanup_complete': fixture_cleanup,
        'browser_pid_observation_complete': runtime['pid_observation_complete'],
        'fixture_cleanup_error_codes': [
            value.upper() if isinstance(value, str) and re.fullmatch(r'fixture_[a-z0-9_]{1,80}', value)
            else 'INVALID_FIXTURE_CLEANUP_CODE'
            for value in (fixture.cleanup_errors if fixture else [])
        ],
        'contexts_cleanup_complete': cases_context.cleanup_complete if cases_context else False,
        'layout_overflow': cases_context.layout_overflow if cases_context else None,
        'suppressed_private_diagnostic_characters': sink.characters,
        'provider_attempts': fixture.provider_attempts if fixture else 0,
        'screenshots_manifest_sha256': digest(read_regular(EVIDENCE / 'screenshots-manifest.json')),
        'case_log_sha256': digest(read_regular(EVIDENCE / 'browser-cases.log')),
        'limits': 'No held Windows runtime, live provider, deployed database, store data or commercial release acceptance.',
    })
    return 0 if passed else 1


if __name__ == '__main__':
    # Early failures intentionally disclose only a fixed code, never exception text.
    try:
        raise SystemExit(main())
    except Exception as error:
        print('HOSTED_BROWSER_RUNNER_FAILED ' + safe_error(error), file=sys.stderr)
        raise SystemExit(1)

