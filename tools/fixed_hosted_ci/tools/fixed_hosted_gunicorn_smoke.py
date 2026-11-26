"""UNRUN source candidate: real reviewed Gunicorn27 gthread / real Flask app.

No application or vendor import precedes the fixed entry's exact installed/native
preflight. No acceptance definition, financial/provider operation or schema is
created here. Loopback HTTP is the explicit trusted proxy-to-WSGI hop only; the
unchanged hosted HTTPS/TLS-negative and real PostgreSQL TLS gates remain required.
"""
from datetime import datetime, timezone
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time

CONFIG = Path('/opt/fixed-hosted-ci')
ROOT = Path('/workspace')
EVIDENCE = Path('/ci-evidence')
CHILD_OUTPUT = EVIDENCE / 'fixed-gunicorn-child'
HOST = 'gunicorn-smoke.invalid'
PORT = 18765
STATE = {}
EXPECTED_IDS = tuple('GSM' + str(number).zfill(2) for number in range(1, 10))


def check(value, code):
    if not value:
        raise ValueError('GUNICORN_SMOKE_' + code)


def pre_request(worker, req):
    """Pinned gthread calls this after parsing and before WSGI dispatch."""
    tags = [value for name, value in req.headers if name == 'X-SS-GSM-PROBE']
    if not tags:
        return
    check(tags in (['gsm06-before'], ['gsm06-malformed'], ['gsm06-after'])
          and req.method == 'GET' and req.path == '/healthz', 'OWNED_DISPATCH_PROBE')
    event = {'tag': tags[0], 'pid': os.getpid(), 'parent_pid': os.getppid(),
             'owner_nonce_sha256': STATE['owner_nonce_sha256']}
    raw = (json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
    check(len(raw) <= 512, 'DISPATCH_EVENT_BOUND')
    path = CHILD_OUTPUT / ('fixed-gunicorn-dispatch-' + str(os.getpid()) + '.jsonl')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(),
              'DISPATCH_EVENT_FILE')
        check(os.write(descriptor, raw) == len(raw), 'DISPATCH_EVENT_COMPLETE_WRITE')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def dispatch_events(nonce_sha, worker_pids, master_pid):
    """Completed tagged response implies the fsynced pre-request event exists."""
    events = []
    for path in sorted(CHILD_OUTPUT.glob('fixed-gunicorn-dispatch-*.jsonl')):
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
              and 0 < info.st_size <= 16384, 'DISPATCH_FILE_BOUND')
        raw = path.read_bytes()
        check(len(raw) == info.st_size and raw.endswith(b'\n'), 'DISPATCH_FILE_COMPLETE')
        for line in raw.splitlines():
            event = json.loads(line)
            check(type(event) is dict and set(event) == {'tag', 'pid', 'parent_pid', 'owner_nonce_sha256'}
                  and event['tag'] in ('gsm06-before', 'gsm06-malformed', 'gsm06-after')
                  and type(event['pid']) is int and event['pid'] in worker_pids
                  and type(event['parent_pid']) is int and event['parent_pid'] == master_pid
                  and event['owner_nonce_sha256'] == nonce_sha
                  and path.name == 'fixed-gunicorn-dispatch-' + str(event['pid']) + '.jsonl',
                  'DISPATCH_EVENT_BINDING')
            events.append(event)
    check(len(events) <= 3, 'DISPATCH_VECTOR_BOUND')
    return sorted(events, key=lambda event: (event['tag'], event['pid']))


def post_worker_init(worker):
    """Gunicorn's actual post-app-init hook; inspect the real forked worker."""
    check(STATE and worker.pid == os.getpid() and os.getppid() == STATE['master_pid'], 'WORKER_PARENT')
    check(worker.cfg.workers == 2 and worker.cfg.threads == 2
          and type(worker).__module__ == 'gunicorn.workers.gthread'
          and worker.cfg.pre_request is pre_request, 'WORKER_CONFIGURATION')
    preflight = STATE['preflight']
    imports = STATE['imported_sources'](preflight, STATE['admission'])
    snapshot = preflight.source_snapshot(STATE['admission'])
    check(snapshot == STATE['source_before'], 'WORKER_SOURCE_STABLE')
    report = {
        'format': 'stock-and-sleeve:real-gunicorn-worker-proof:v1', 'passed': True,
        'pid': os.getpid(), 'parent_pid': os.getppid(), 'owner_nonce_sha256': STATE['owner_nonce_sha256'],
        'gunicorn_version': '27.0.1', 'worker_module': type(worker).__module__,
        'workers': worker.cfg.workers, 'threads': worker.cfg.threads,
        'parser_dispatch_hook_verified': True,
        'actual_imports': imports, 'source_snapshot': snapshot,
        'application': 'hosted.app:create_app()', 'application_testing': worker.wsgi.config['TESTING'],
        'provider_or_financial_request': False}
    # Publish only complete fsynced JSON; parent observation never races an
    # exclusive file that is still being written by another real worker.
    raw = (json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    check(len(raw) <= 4 * 1024 * 1024, 'WORKER_REPORT_BOUND')
    final = CHILD_OUTPUT / ('fixed-gunicorn-worker-' + str(os.getpid()) + '.json')
    stage = final.with_suffix('.json.stage')
    check(not final.exists(), 'FRESH_WORKER_REPORT')
    with stage.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    # Atomic, exclusive destination; never replace another worker's evidence.
    os.link(stage, final, follow_symlinks=False)
    stage.unlink()


def serve(preflight, admission, imported_sources):
    """Called only by the already-preflighted fixed --gunicorn-child entry."""
    check(os.environ.get('GUNICORN_CMD_ARGS') in (None, ''), 'AMBIENT_GUNICORN_ARGUMENTS')
    nonce = os.environ.get('SS_FIXED_GUNICORN_OWNER_NONCE', '')
    parent_pid = os.environ.get('SS_FIXED_GUNICORN_PARENT_PID', '')
    check(re.fullmatch('[a-f0-9]{32}', nonce) and re.fullmatch('[1-9][0-9]{0,9}', parent_pid)
          and os.getppid() == int(parent_pid), 'OWNED_PARENT_NONCE')
    check(any(row['name'] == 'gunicorn' and row['version'] == '27.0.1' for row in admission['wheels']),
          'EXACT_GUNICORN_ADMISSION')
    # Real source import only after the full twenty-wheel/native preflight.
    import hosted.app
    import gunicorn
    from gunicorn.app.wsgiapp import run
    check(gunicorn.__version__ == '27.0.1', 'ACTUAL_GUNICORN_VERSION')
    STATE.update(preflight=preflight, admission=admission, imported_sources=imported_sources,
                 source_before=preflight.source_snapshot(admission), master_pid=os.getpid(),
                 owner_nonce_sha256=hashlib.sha256(nonce.encode()).hexdigest())
    sys.argv = ['gunicorn', '--config', 'python:tools.fixed_hosted_gunicorn_smoke',
                '--bind', '127.0.0.1:' + str(PORT), '--workers', '2', '--threads', '2',
                '--worker-class', 'gthread', '--keep-alive', '2', '--timeout', '15',
                '--graceful-timeout', '10', '--forwarded-allow-ips', '127.0.0.1',
                '--log-level', 'warning', '--access-logfile', '-', '--error-logfile', '-',
                '--chdir', str(ROOT), 'hosted.app:create_app()']
    # No preload flag: the actual factory executes in each real Gunicorn worker.
    run()


def worker_reports(preflight):
    paths = sorted(CHILD_OUTPUT.glob('fixed-gunicorn-worker-*.json'))
    check(len(paths) <= 2, 'UNEXPECTED_WORKER_RESTART')
    return [preflight.load(path) for path in paths]


def process_identity(pid):
    try:
        status = Path('/proc/' + str(pid) + '/stat').read_text()
    except FileNotFoundError:
        return None
    end = status.rfind(')')
    check(end > 0, 'PROC_STAT_FORM')
    fields = status[end + 2:].split()
    return {'pid': pid, 'parent_pid': int(fields[1]), 'process_group': int(fields[2]),
            'start_ticks': int(fields[19])}


def request(path='/healthz', *, secure=True, host=HOST, connection=None, probe=None):
    owned = connection is None
    connection = connection or http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    headers = {'Host': host}
    if secure:
        headers['X-Forwarded-Proto'] = 'https'
    if probe is not None:
        check(probe in ('gsm06-before', 'gsm06-after'), 'OWNED_POSITIVE_DISPATCH_PROBE')
        headers['X-SS-GSM-Probe'] = probe
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        raw = response.read(65537)
        check(len(raw) <= 65536, 'HTTP_RESPONSE_BOUND')
        return response.status, dict(response.getheaders()), raw, response.will_close
    finally:
        if owned:
            connection.close()


def run(preflight, admission):
    """Parent-owned real master/two workers, bounded GET/parser/shutdown checks."""
    check(not CHILD_OUTPUT.exists(), 'FRESH_CHILD_OUTPUT')
    child_stdout, child_stderr = EVIDENCE / 'fixed-gunicorn.stdout.log', EVIDENCE / 'fixed-gunicorn.stderr.log'
    check(not child_stdout.exists() and not child_stderr.exists(), 'FRESH_PRIVATE_LOGS')
    environment = dict(os.environ)
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME', 'GUNICORN_CMD_ARGS'):
        check(not environment.get(key), 'AMBIENT_ENVIRONMENT')
    nonce = secrets.token_hex(16)
    environment.update(SS_FIXED_GUNICORN_OWNER_NONCE=nonce, SS_FIXED_GUNICORN_PARENT_PID=str(os.getpid()),
                       PUBLIC_BASE_URL='https://' + HOST,
                       APP_ENCRYPTION_KEYS=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii'),
                       BOOTSTRAP_TOKEN=secrets.token_hex(32), RECEIVING_WRITES_ENABLED='false')
    check(re.fullmatch(r'postgresql://postgres:[0-9a-f]{48}@db:5432/ss_ci_[0-9a-f]{16}\?sslmode=require',
                       environment.get('DATABASE_URL', '')), 'OWNED_TLS_FIXTURE_DSN')
    result = {'format': 'stock-and-sleeve:gunicorn27-real-gthread-smoke:v1', 'passed': False,
              'expected_case_ids': list(EXPECTED_IDS), 'successful_case_ids': [], 'cases': [],
              'started_utc': datetime.now(timezone.utc).isoformat(), 'provider_or_financial_requests': 0,
              'schema_mutation': False, 'scope': 'Real Gunicorn27 workers2/threads2 and actual Flask app on owned loopback proxy hop; not a public TLS or live-provider acceptance.'}
    master, master_identity, workers, forced_cleanup = None, None, [], False
    start = time.monotonic()
    case_clock = [start, time.process_time(), result['started_utc']]

    def success(ident, facts):
        check(ident in EXPECTED_IDS and ident not in result['successful_case_ids'], 'CASE_IDENTITY')
        result['successful_case_ids'].append(ident)
        finish_wall, finish_cpu = time.monotonic(), time.process_time()
        finish_utc = datetime.now(timezone.utc).isoformat()
        result['cases'].append({'id': ident, 'passed': True, 'facts': facts,
                                'started_utc': case_clock[2], 'finished_utc': finish_utc,
                                'elapsed_monotonic': finish_wall - case_clock[0],
                                'elapsed_parent_process_cpu': finish_cpu - case_clock[1]})
        case_clock[:] = [finish_wall, finish_cpu, finish_utc]

    def health(value):
        status, headers, raw, _ = value
        body = json.loads(raw)
        check(status == 200 and body == {'service': 'Stock & Sleeve hosted service', 'version': '0.1.0',
              'mode': 'pilot', 'status': 'running'}, 'REAL_HEALTH_SCHEMA')
        check(headers.get('Cache-Control') == 'no-store' and headers.get('X-Content-Type-Options') == 'nosniff'
              and headers.get('Strict-Transport-Security') == 'max-age=31536000', 'REAL_SECURITY_HEADERS')

    try:
        with child_stdout.open('xb') as stdout, child_stderr.open('xb') as stderr:
            master = subprocess.Popen([sys.executable, '-I', '-S', '-B', str(CONFIG / 'fixed_hosted_ci_entry.py'),
                                       '--gunicorn-child'], cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                      stdout=stdout, stderr=stderr, start_new_session=True)
            master_identity = process_identity(master.pid)
            check(master_identity and master_identity['parent_pid'] == os.getpid()
                  and master_identity['process_group'] == master.pid, 'OWNED_MASTER_SESSION')
            while True:
                check(master.poll() is None and time.monotonic() - start <= 30, 'REAL_WORKER_STARTUP')
                check(child_stdout.stat().st_size <= 4 * 1024 * 1024
                      and child_stderr.stat().st_size <= 4 * 1024 * 1024, 'PRIVATE_LOG_BOUND')
                reports = worker_reports(preflight) if CHILD_OUTPUT.exists() else []
                if len(reports) == 2:
                    break
                time.sleep(0.05)
            nonce_sha = hashlib.sha256(nonce.encode()).hexdigest()
            check(all(report['passed'] is True and report['parent_pid'] == master.pid
                      and report['owner_nonce_sha256'] == nonce_sha and report['workers'] == report['threads'] == 2
                      and report['application_testing'] is False
                      and report['parser_dispatch_hook_verified'] is True for report in reports), 'TWO_REAL_WORKER_PROOFS')
            workers = [process_identity(report['pid']) for report in reports]
            check(all(row and row['parent_pid'] == master.pid for row in workers)
                  and len({row['pid'] for row in workers}) == 2, 'TWO_DISTINCT_OS_WORKERS')
            success('GSM01', {'master': master_identity, 'workers': workers, 'version': '27.0.1',
                              'worker_import_source_proofs': reports})

            health(request())
            success('GSM02', {'health_200': True, 'real_security_headers': True})
            with http.client.HTTPConnection('127.0.0.1', PORT, timeout=10) as connection:
                first = request(connection=connection)
                health(first)
                check(connection.sock is not None and first[3] is False, 'KEEPALIVE_FIRST_SOCKET')
                first_socket = (connection.sock.fileno(), connection.sock.getsockname())
                second = request(connection=connection)
                health(second)
                check(connection.sock is not None and second[3] is False
                      and first_socket == (connection.sock.fileno(), connection.sock.getsockname()), 'KEEPALIVE_SAME_SOCKET')
            success('GSM03', {'two_requests_one_os_socket': True})

            code, _, raw, _ = request(secure=False)
            check(code == 400 and json.loads(raw).get('error') == 'Use the secure HTTPS service address.',
                  'HTTPS_BOUNDARY_PRESERVED')
            success('GSM04', {'unforwarded_insecure_request_rejected': True})
            code, _, _, _ = request(host='other-smoke.invalid')
            check(code == 400, 'TRUSTED_HOST_BOUNDARY')
            success('GSM05', {'wrong_host_rejected': True})

            worker_pids = {row['pid'] for row in workers}
            check(dispatch_events(nonce_sha, worker_pids, master.pid) == [], 'FRESH_DISPATCH_OBSERVATION')
            health(request(probe='gsm06-before'))
            before_dispatch = dispatch_events(nonce_sha, worker_pids, master.pid)
            check(len(before_dispatch) == 1 and before_dispatch[0]['tag'] == 'gsm06-before',
                  'POSITIVE_BEFORE_DISPATCH_WITNESS')
            with socket.create_connection(('127.0.0.1', PORT), timeout=10) as connection:
                connection.sendall(b'GET /healthz HTTP/1.1\r\nHost: gunicorn-smoke.invalid\r\n'
                                   b'X-Forwarded-Proto: https\r\nX-SS-GSM-Probe: gsm06-malformed\r\n'
                                   b'Content-Length: 0\r\nContent-Length: 1\r\n\r\n')
                connection.settimeout(10)
                response = bytearray()
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    check(len(response) + len(chunk) <= 65536, 'MALFORMED_RESPONSE_BOUND')
                    response.extend(chunk)
                head, separator, body = bytes(response).partition(b'\r\n\r\n')
                lines = head.split(b'\r\n')
                headers = dict(line.lower().split(b': ', 1) for line in lines[1:])
                check(separator and lines[0] == b'HTTP/1.1 400 Bad Request'
                      and headers.get(b'connection') == b'close'
                      and headers.get(b'content-type') == b'text/html'
                      and headers.get(b'content-length') == str(len(body)).encode('ascii')
                      and b'Invalid HTTP Header: &#x27;CONTENT-LENGTH&#x27;' in body,
                      'PINNED_GUNICORN_PARSER_ERROR_AND_COMPLETE_CLOSE')
            check(dispatch_events(nonce_sha, worker_pids, master.pid) == before_dispatch,
                  'MALFORMED_REQUEST_MUST_NOT_REACH_PRE_WSGI_HOOK')
            health(request(probe='gsm06-after'))
            observed_dispatch = dispatch_events(nonce_sha, worker_pids, master.pid)
            check(sorted(event['tag'] for event in observed_dispatch) == ['gsm06-after', 'gsm06-before'],
                  'TWO_POSITIVE_CONTROLS_NO_MALFORMED_DISPATCH')
            success('GSM06', {'conflicting_content_lengths_parser_400': True,
                              'valid_owned_https_forwarding': True, 'complete_parser_response_and_closed_socket': True,
                              'pre_wsgi_dispatch_events': observed_dispatch, 'malformed_dispatch_events': 0,
                              'subsequent_real_request_200': True})

            code, headers, body, _ = request('/')
            check(code == 200 and headers.get('Content-Type', '').startswith('text/html')
                  and b'<html' in body.lower() and 'default-src' in headers.get('Content-Security-Policy', ''),
                  'REAL_TEMPLATE_ROUTE')
            success('GSM07', {'real_portal_template_200': True, 'csp_present': True})
            code, _, body, _ = request('/readyz')
            ready = json.loads(body)
            check(code in (200, 503) and type(ready.get('database_ready')) is bool
                  and type(ready.get('service_ready')) is bool and ready.get('commercial_release') is False,
                  'READINESS_SCHEMA_NO_FALSE_RELEASE')
            success('GSM08', {'readiness_status': code, 'readiness': ready,
                              'existing_fixture_tls_url_preserved': True, 'no_schema_created': True})

            check(process_identity(master.pid) == master_identity, 'MASTER_IDENTITY_BEFORE_STOP')
            master.send_signal(signal.SIGTERM)
            check(master.wait(timeout=20) == 0, 'GRACEFUL_MASTER_EXIT')
            check(process_identity(master.pid) is None and all(process_identity(row['pid']) is None for row in workers),
                  'OWNED_MASTER_AND_WORKERS_GONE')
            child = preflight.load(CHILD_OUTPUT / 'fixed-entry-summary.json')
            check(child.get('passed') is True and child.get('mode') == 'gunicorn_child'
                  and child.get('source_stable') is True, 'CHILD_FINAL_IMPORT_PROOF')
            success('GSM09', {'normal_sigterm_exit0': True, 'all_owned_os_processes_gone': True,
                              'child_final_summary': child})
        result['successful_case_ids'] = sorted(result['successful_case_ids'])
        result['passed'] = result['successful_case_ids'] == list(EXPECTED_IDS)
    finally:
        if master is not None and master.poll() is None:
            current = process_identity(master.pid)
            check(current == master_identity and current['process_group'] == master.pid, 'CLEANUP_OWNER_IDENTITY')
            forced_cleanup = True
            os.killpg(master.pid, signal.SIGTERM)
            try:
                master.wait(timeout=15)
            except subprocess.TimeoutExpired:
                current = process_identity(master.pid)
                check(current == master_identity, 'HARD_CLEANUP_OWNER_IDENTITY')
                os.killpg(master.pid, signal.SIGKILL)
                master.wait(timeout=5)
        result['forced_cleanup'] = forced_cleanup
        result['master_exit_code'] = None if master is None else master.returncode
        result['worker_identity_complete'] = len(workers) == 2
        result['owned_processes_gone'] = bool(master is not None and len(workers) == 2 and process_identity(master.pid) is None
                                               and all(process_identity(row['pid']) is None for row in workers))
        result['finished_utc'] = datetime.now(timezone.utc).isoformat()
        result['elapsed_monotonic'] = time.monotonic() - start
        result['private_log_pins'] = []
        for path in (child_stdout, child_stderr):
            if path.exists():
                info = path.stat()
                check(info.st_size <= 4 * 1024 * 1024, 'FINAL_PRIVATE_LOG_BOUND')
                result['private_log_pins'].append({'name': path.name, 'bytes': info.st_size,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'raw_log_publication': False})
        result['passed'] = bool(result['passed'] and result['owned_processes_gone'] and not forced_cleanup)
        preflight.save('fixed-gunicorn-smoke.json', result)
    check(result['passed'], 'NINE_REAL_GTHREAD_CASES_REQUIRED')
    return result
