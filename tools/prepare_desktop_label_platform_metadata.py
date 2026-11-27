"""UNRUN fixed own-run GitHub metadata preparation; no application/SDK import.

The source-only workflow and grant are disabled. Root separately reviews and
admits the trusted GitHub platform/HTTPS/Chrome-version query before any call.
API credentials are only an in-memory HTTPS header in this preparation step.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

REPOSITORY = 'thundergamescda/RocaSifter'
JOB_KEY = 'desktop_ui_seven'
JOB_NAME = 'Stock & Sleeve desktop UI seven'
WORKFLOW = '.github/workflows/desktop-platform-ui.yml'
START = time.monotonic()
MAX_JSON = 8 * 1024 * 1024


class PreparationFailure(RuntimeError):
    pass


def check(value, code):
    if not value:
        raise PreparationFailure(code)


def unique(pairs):
    value = {}
    for key, item in pairs:
        check(key not in value, 'DUPLICATE_METADATA_JSON_KEY')
        value[key] = item
    return value


def pin(path, maximum):
    path = Path(path)
    before = path.lstat()
    check(stat.S_ISREG(before.st_mode) and not path.is_symlink() and before.st_nlink == 1
          and 0 <= before.st_size <= maximum, 'REGULAR_BOUNDED_PREPARATION_FILE')
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    fd, hashed, count = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), hashlib.sha256(), 0
    try:
        check(stamp(os.fstat(fd)) == stamp(before), 'PREPARATION_FILE_OPEN_IDENTITY')
        while chunk := os.read(fd, 65536):
            count += len(chunk)
            check(count <= maximum, 'PREPARATION_FILE_STREAM_BOUND')
            hashed.update(chunk)
        check(count == before.st_size and stamp(os.fstat(fd)) == stamp(before) == stamp(path.lstat()),
              'PREPARATION_FILE_CHANGED')
    finally:
        os.close(fd)
    return {'bytes': count, 'sha256': hashed.hexdigest()}


def save(path, value):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    check(len(raw) <= MAX_JSON, 'PREPARATION_JSON_OUTPUT_BOUND')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    return pin(path, MAX_JSON)


def stock_chrome_version(home):
    binary = Path('/opt/google/chrome/chrome')
    before = pin(binary, 384 * 1024 * 1024)
    proc = subprocess.Popen([str(binary), '--product-version'], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
        env={'PATH': '/usr/bin:/bin', 'HOME': str(home), 'USER': 'ss-ci', 'LOGNAME': 'ss-ci',
             'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TMPDIR': str(home)})
    selected, fd, values = selectors.DefaultSelector(), None, {'stdout': bytearray(), 'stderr': bytearray()}
    try:
        fd = os.pidfd_open(proc.pid, 0)
        for name in values:
            stream = getattr(proc, name)
            os.set_blocking(stream.fileno(), False)
            selected.register(stream, selectors.EVENT_READ, name)
        stop = time.monotonic() + 15
        while selected.get_map():
            check(time.monotonic() < stop, 'STOCK_CHROME_VERSION_QUERY_TIMEOUT')
            for key, _ in selected.select(0.1):
                raw = os.read(key.fd, 2049)
                if not raw:
                    selected.unregister(key.fileobj)
                else:
                    check(len(values[key.data]) + len(raw) <= 2048, 'STOCK_CHROME_VERSION_OUTPUT_BOUND')
                    values[key.data].extend(raw)
        check(proc.wait(timeout=2) == 0, 'STOCK_CHROME_VERSION_QUERY_FAILED')
        version = values['stdout'].decode('ascii', errors='strict').strip()
        check(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+', version), 'STOCK_CHROME_VERSION_FORMAT')
        numbers = tuple(int(value) for value in version.split('.'))
        check((numbers[0] == 157 and numbers >= (157, 0, 8103, 78))
              or numbers == (158, 0, 8127, 36), 'ADMITTED_OFFICIAL_CHROME157_OR_EXACT158')
        check(pin(binary, 384 * 1024 * 1024) == before, 'STOCK_CHROME_VERSION_BINARY_BOOKEND')
        return {'version': version, 'binary': before, 'argv': [str(binary), '--product-version'],
                'returncode': 0, 'query_only_not_browser_or_application_acceptance': True}
    finally:
        selected.close()
        if proc.returncode is None:
            if fd is None:
                fd = os.pidfd_open(proc.pid, 0)  # still unreaped direct child, never a recycled PID
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL, None, 0)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
        if fd is not None:
            os.close(fd)
        proc.stdout.close()
        proc.stderr.close()


def main():
    check(len(sys.argv) == 1 and sys.platform == 'linux' and os.uname().machine == 'x86_64'
          and sys.version.split()[0] == '3.12.16' and sys.flags.isolated and sys.flags.no_site
          and sys.flags.ignore_environment and sys.dont_write_bytecode
          and sys.base_prefix == '/opt/hostedtoolcache/Python/3.12.16/x64', 'EXACT_PREINSTALLED_METADATA_PYTHON')
    check(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
          and os.environ.get('RUNNER_OS') == 'Linux' and os.environ.get('ImageOS') == 'ubuntu24'
          and os.environ.get('GITHUB_REPOSITORY') == REPOSITORY and os.environ.get('GITHUB_EVENT_NAME') == 'push'
          and os.environ.get('GITHUB_JOB') == JOB_KEY and os.environ.get('GITHUB_RUN_ATTEMPT') == '1',
          'FIXED_INITIAL_OWNED_GITHUB_JOB_ONLY')
    source_sha, run_id = os.environ['GITHUB_SHA'], os.environ['GITHUB_RUN_ID']
    check(re.fullmatch(r'[a-f0-9]{40}', source_sha) and re.fullmatch(r'[0-9]{1,24}', run_id)
          and os.environ['GITHUB_REF'] == 'refs/heads/' + os.environ['GITHUB_REF_NAME']
          and re.fullmatch(r'stock-sleeve-platform-ui-[A-Za-z0-9._-]{1,80}', os.environ['GITHUB_REF_NAME']),
          'FIXED_PREVIEW_BRANCH_SOURCE_RUN')
    repo, temporary = Path(os.environ['GITHUB_WORKSPACE']), Path(os.environ['RUNNER_TEMP'])
    check(repo.resolve(strict=True) == repo and temporary.resolve(strict=True) == temporary
          and Path(__file__).resolve() == repo / 'tools/prepare_desktop_label_platform_metadata.py',
          'EXACT_METADATA_SOURCE_AND_TEMP_PATHS')
    self_pin = pin(Path(__file__).resolve(), 1024 * 1024)
    check(self_pin['sha256'] == os.environ['SS_PLATFORM_METADATA_SOURCE_SHA256'], 'WORKFLOW_PINNED_METADATA_SOURCE')
    grant_file = repo / 'tools/desktop_label_platform_root_grant.json'
    grant_pin = pin(grant_file, 65536)
    check(grant_pin['sha256'] == os.environ['SS_PLATFORM_ROOT_GRANT_SHA256'], 'WORKFLOW_PINNED_ROOT_GRANT')
    with grant_file.open('rb') as stream:
        raw_grant = stream.read(65537)
    check(len(raw_grant) == grant_pin['bytes'] and hashlib.sha256(raw_grant).hexdigest() == grant_pin['sha256'],
          'ROOT_GRANT_READBACK')
    grant = json.loads(raw_grant, object_pairs_hook=unique)
    check(grant['format'] == 'stock-and-sleeve:trusted-platform-host-root-grant:v1'
          and grant['execution_hold'] is None and grant['host_execution_admitted'] is True
          and grant['foundation_scope'] == 'fresh-github-ubuntu24-preinstalled-chrome-functional-only',
          'ROOT_METADATA_AND_PLATFORM_GRANT_HOLD')
    for key in ('source_manifest_sha256', 'source_review_sha256', 'runtime_source_review_sha256', 'node_api_scope_sha256'):
        check(re.fullmatch(r'[a-f0-9]{64}', grant[key]), 'EXACT_ROOT_SOURCE_AND_API_GRANT_FIELDS')
    # Only the preparation step receives this token. No token is an argv value,
    # output field, URL parameter, file or child-process environment variable.
    token = os.environ.pop('SS_PLATFORM_API_TOKEN')
    check(type(token) is str and 20 <= len(token) <= 8192 and '\n' not in token and '\r' not in token,
          'PRIVATE_ACTIONS_READ_TOKEN_HEADER')
    import ssl
    import urllib.request
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise PreparationFailure('NO_METADATA_API_REDIRECT')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    receipts = []
    def request(path):
        check(time.monotonic() - START < 120, 'OWN_RUN_METADATA_TOTAL_DEADLINE')
        url = 'https://api.github.com/repos/' + REPOSITORY + path
        headers = {'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + token,
                   'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'StockSleeve-owned-platform-ci'}
        with opener.open(urllib.request.Request(url, headers=headers), timeout=25) as response:
            check(response.status == 200 and response.geturl() == url
                  and response.headers.get('Content-Encoding', 'identity') == 'identity', 'EXACT_OWN_RUN_API_RESPONSE')
            length = response.headers.get('Content-Length')
            check(length is None or length.isdecimal() and int(length) <= MAX_JSON, 'BOUNDED_API_DECLARED_SIZE')
            body = response.read(MAX_JSON + 1)
        check(len(body) <= MAX_JSON, 'BOUNDED_API_ACTUAL_SIZE')
        value = json.loads(body, object_pairs_hook=unique,
                           parse_constant=lambda _: check(False, 'NONFINITE_API_JSON'))
        receipts.append({'path': path, 'status': 200, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
        return value
    run = request('/actions/runs/' + run_id)
    check(type(run['id']) is int and run['id'] == int(run_id) and run['head_sha'] == source_sha
          and run['repository']['full_name'] == REPOSITORY and run['head_repository']['full_name'] == REPOSITORY
          and run['run_attempt'] == 1 and run['event'] == 'push' and run['head_branch'] == os.environ['GITHUB_REF_NAME']
          and run['path'].split('@', 1)[0] == WORKFLOW and run['status'] == 'in_progress', 'ACTUAL_RUN_REPOSITORY_HEAD_ATTEMPT_WORKFLOW')
    jobs = request('/actions/runs/' + run_id + '/attempts/1/jobs?per_page=100&page=1')
    check(type(jobs['total_count']) is int and jobs['total_count'] == len(jobs['jobs']) == 1, 'EXACT_ONE_OWN_ATTEMPT_JOB')
    candidate = jobs['jobs'][0]
    check(type(candidate['id']) is int and candidate['id'] > 0, 'ACTUAL_API_NUMERIC_JOB_ID')
    job = request('/actions/jobs/' + str(candidate['id']))
    for current in (candidate, job):
        check(current['id'] == candidate['id'] and current['run_id'] == int(run_id) and current['head_sha'] == source_sha
              and current['name'] == JOB_NAME and current['status'] == 'in_progress'
              and current['runner_name'] == os.environ['RUNNER_NAME']
              and type(current['runner_id']) is int and current['runner_id'] > 0
              and 'ubuntu-24.04' in current['labels'] and 'self-hosted' not in current['labels'],
              'ACTUAL_OWN_JOB_HEAD_NAME_RUNNER_LABELS')
    os.umask(0o077)
    nonce = os.urandom(16).hex()
    folder = temporary / ('stock-sleeve-platform-activation-' + nonce)
    folder.mkdir(mode=0o700)
    home = folder / 'version-query-home'
    home.mkdir(mode=0o700)
    chrome = stock_chrome_version(home)
    metadata = {'format': 'stock-and-sleeve:platform-postcheckout-host-metadata:v1', 'nonce': nonce,
        'source_sha': source_sha, 'job_id': str(job['id']), 'run_id': run_id, 'job_key': JOB_KEY,
        'image_version': os.environ['ImageVersion'], 'root_grant_sha256': grant_pin['sha256']}
    output_grant = folder / 'root-grant.json'
    fd = os.open(output_grant, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw_grant)
    metadata_pin = save(folder / 'host-metadata.json', metadata)
    receipt = {'successful': True, 'repository': REPOSITORY, 'run_id': int(run_id), 'attempt': 1,
        'job_id': job['id'], 'job_key': JOB_KEY, 'job_name': JOB_NAME, 'head_sha': source_sha,
        'runner_name': job['runner_name'], 'runner_id': job['runner_id'], 'api_responses': receipts,
        'preparation_source': self_pin, 'root_grant': grant_pin, 'metadata': metadata_pin,
        'stock_chrome': chrome, 'python': sys.version, 'platform_scope_only': True}
    receipt_pin = save(folder / 'metadata-api-receipt.json', receipt)
    check(pin(Path(__file__).resolve(), 1024 * 1024) == self_pin
          and pin(grant_file, 65536) == grant_pin and pin(output_grant, 65536) == grant_pin,
          'FINAL_PREPARATION_SOURCE_AND_ROOT_GRANT_BOOKEND')
    # GITHUB_OUTPUT is the runner-provided per-step command file. Values below
    # are generated canonical ASCII paths/hashes, never API response bodies.
    output = Path(os.environ['GITHUB_OUTPUT'])
    check(output.is_absolute() and not output.is_symlink() and output.is_file(), 'OWNED_STEP_OUTPUT_FILE')
    for value in (str(folder / 'host-metadata.json'), metadata_pin['sha256'], grant_pin['sha256']):
        check('\n' not in value and '\r' not in value, 'SINGLE_LINE_STEP_OUTPUT')
    with output.open('a', encoding='ascii') as stream:
        stream.write('metadata_path=' + str(folder / 'host-metadata.json') + '\n')
        stream.write('metadata_sha256=' + metadata_pin['sha256'] + '\n')
        stream.write('root_grant_sha256=' + grant_pin['sha256'] + '\n')
    print(json.dumps({'successful': True, 'actual_run_id': int(run_id), 'actual_job_id': job['id'],
                      'attempt': 1, 'head_sha': source_sha, 'receipt': receipt_pin}, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        code = str(error) if isinstance(error, PreparationFailure) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(error)) else 'PLATFORM_METADATA_PREPARATION_FAILED'
        print(json.dumps({'successful': False, 'error_type': type(error).__name__, 'error_code': code}, separators=(',', ':')), flush=True)
        raise SystemExit(1)
