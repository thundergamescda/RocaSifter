"""UNRUN fixed GitHub-VM companion for the frozen seven desktop UI flows.

Root's separately SHA-pinned grant and new Node/API verdict are mandatory.
This source performs no apt/pip/browser install, policy change or vendor-script
execution while staging. Only the separately admitted runtime launches vendors.
The existing native-image admission and shipping pipeline are not inherited.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
import zipfile

START = time.monotonic()
TOOL_CHILDREN_CLEAN = True
TOOL_LOG_DIRECTORY = None
TOOL_RESULTS = []
TOTAL_SECONDS = 1800
RUNTIME_SECONDS = 1000
CHUNK = 65536
IDS = tuple('DUI%02d' % n for n in range(1, 8))
CAPTURES = ('DUI01_done', 'DUI02_denied', 'DUI02_recovered', 'DUI03_denied', 'DUI03_recovered',
            'DUI04_admin_context', 'DUI04_rejected', 'DUI04_recovered', 'DUI05_signed_out', 'DUI05_reopened',
            'DUI06_rejected', 'DUI06_recovered', 'DUI07_rejected', 'DUI07_recovered')
RUNTIME_REPORTS = frozenset(('platform-foundation.json', 'platform-native-mappings.json',
    'desktop-runtime-driver.json', 'desktop-runtime-summary.json', 'desktop-network-ledger.json',
    'desktop-console-ledger.json', 'desktop-page-errors.json', 'desktop-download-events.json',
    'desktop-server-errors.json', 'desktop-broker-ledger.json', 'desktop-source-before.json',
    'desktop-source-after.json', 'desktop-sandbox.json', 'desktop-browser-summary.json'))
HOST_REPORTS = frozenset(('host-bootstrap.json', 'host-stage-inventory.json', 'host-process-cleanup.json',
    'host-runtime-result.json', 'host-functional-acceptance.json', 'host-tool-results.json',
    'host-stage-progress.json', 'host-summary.json'))
TEMPLATES = ('desktop_label_browser_fixture.py', 'desktop_label_browser_cases.py',
    'desktop_label_browser_ci.py', 'desktop_label_browser_platform_runtime.py',
    'desktop_label_browser_platform_sandbox.py', 'desktop_label_browser_installed_files.json',
    'desktop_label_browser_inputs.json', 'desktop_label_browser_admission.json',
    'desktop_label_browser_platform.json')


class HostFailure(RuntimeError):
    pass


def check(value, code):
    if not value:
        raise HostFailure(code)


def deadline():
    check(time.monotonic() - START < TOTAL_SECONDS - 30, 'PLATFORM_HOST_DEADLINE')


def digest(value):
    check(type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value), 'SHA256_VALUE_REQUIRED')
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        check(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def canonical(name):
    check(type(name) is str and 0 < len(name) <= 512 and not name.startswith('/')
          and '\\' not in name and ':' not in name and '\0' not in name
          and all(part not in ('', '.', '..') and len(part) <= 255 for part in name.split('/')),
          'CANONICAL_OWNED_RELATIVE_PATH')
    check(not name.lower().endswith(('.pth', '.pyc', '.pyo', '.egg-link'))
          and not any(part.lower() == '__pycache__' for part in name.split('/'))
          and Path(name).name.lower() not in ('sitecustomize.py', 'usercustomize.py'),
          'NO_OWNED_AUTOLOAD_OR_BYTECODE')
    return name


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


def pin(path, maximum=192 * 1024 * 1024, readonly=False):
    path = Path(path)
    check(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)), 'PIN_PATH_OR_LINK')
    before = path.lstat()
    check(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 <= before.st_size <= maximum
          and (not readonly or before.st_mode & 0o222 == 0), 'PIN_REGULAR_SINGLE_BOUNDED_FILE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    count, hashed = 0, hashlib.sha256()
    try:
        check(stamp(os.fstat(fd)) == stamp(before), 'PIN_OPEN_IDENTITY')
        while chunk := os.read(fd, CHUNK):
            count += len(chunk)
            check(count <= maximum, 'PIN_STREAM_BOUND')
            hashed.update(chunk)
        check(count == before.st_size and stamp(os.fstat(fd)) == stamp(before) == stamp(path.lstat()),
              'PIN_FILE_CHANGED')
    finally:
        os.close(fd)
    return {'path': str(path), 'bytes': count, 'sha256': hashed.hexdigest(),
            'device': before.st_dev, 'inode': before.st_ino}


def read_json(path, expected=None, maximum=4 * 1024 * 1024):
    before = pin(path, maximum)
    if expected is not None:
        check(before['sha256'] == digest(expected), 'JSON_EXPECTED_PIN')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(maximum + 1)
    check(len(raw) == before['bytes'] and hashlib.sha256(raw).hexdigest() == before['sha256']
          and pin(path, maximum) == before, 'JSON_BOOKEND')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: check(False, 'JSON_NONFINITE')), before


def save(path, value):
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    check(len(raw) <= 4 * 1024 * 1024, 'HOST_JSON_REPORT_BOUND')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
    return pin(path, 4 * 1024 * 1024)


def private_directory(path):
    Path(path).mkdir(mode=0o700)
    info = Path(path).lstat()
    check(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
          'FRESH_OWNED_PRIVATE_DIRECTORY')


def safe_error(error):
    return {'error_type': type(error).__name__, 'error_code': str(error) if isinstance(error, HostFailure)
            and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(error)) else 'PLATFORM_HOST_OPERATION_FAILED'}


def process(pid):
    root = Path('/proc') / str(pid)
    with (root / 'stat').open('rb') as stream:
        raw = stream.read(8193)
    check(len(raw) <= 8192, 'PROC_STAT_BOUND')
    fields = raw.rsplit(b') ', 1)[1].split()
    result = {'pid': pid, 'ppid': int(fields[1]), 'pgid': int(fields[2]), 'sid': int(fields[3]),
              'start_ticks': int(fields[19]), 'state': fields[0].decode('ascii'), 'uid': root.stat().st_uid}
    return result


def identity(row):
    return row['pid'], row['start_ticks']


def pidfd(pid, expected):
    check(identity(process(pid)) == identity(expected), 'PROCESS_PRE_PIDFD_IDENTITY')
    fd = os.pidfd_open(pid, 0)
    try:
        check(identity(process(pid)) == identity(expected), 'PROCESS_PIDFD_BOOKEND')
    except BaseException:
        os.close(fd)
        raise
    return fd


def signal_owned(fd, row, selected_signal):
    check(selected_signal in (signal.SIGTERM, signal.SIGKILL), 'ONLY_OWNED_TERMINATION_SIGNALS')
    try:
        current = process(row['pid'])
    except (FileNotFoundError, ProcessLookupError):
        return
    if identity(current) != identity(row):
        return  # the old birth is gone; the persistent pidfd never targets this replacement
    check(current['uid'] == os.geteuid(), 'SIGNAL_OWNED_PID_BIRTH_UID')
    try:
        signal.pidfd_send_signal(fd, selected_signal, None, 0)
    except ProcessLookupError:
        pass


def command(argv, environment, seconds=60):
    """Only fixed trusted platform tools. Bounded pipes, no shell/secret args."""
    global TOOL_CHILDREN_CLEAN
    check(argv[0] in ('/usr/bin/git', '/usr/bin/curl') and TOOL_CHILDREN_CLEAN
          and TOOL_LOG_DIRECTORY is not None and len(TOOL_RESULTS) < 32, 'FIXED_HOST_TOOL_COMMAND')
    deadline()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=environment, close_fds=True, start_new_session=True)
    TOOL_CHILDREN_CLEAN = False
    row, fd, selected = None, None, selectors.DefaultSelector()
    data = {'stdout': bytearray(), 'stderr': bytearray()}
    try:
        row = process(proc.pid)
        check(row['ppid'] == os.getpid() and row['sid'] == proc.pid and row['uid'] == os.geteuid(), 'OWNED_TOOL_PROCESS')
        fd = pidfd(proc.pid, row)
        for name in data:
            stream = getattr(proc, name)
            os.set_blocking(stream.fileno(), False)
            selected.register(stream, selectors.EVENT_READ, name)
        stop = time.monotonic() + min(seconds, max(1, TOTAL_SECONDS - 30 - (time.monotonic() - START)))
        while selected.get_map():
            check(time.monotonic() < stop, 'OWNED_HOST_TOOL_TIMEOUT')
            for key, _ in selected.select(0.1):
                raw = os.read(key.fd, CHUNK)
                if not raw:
                    selected.unregister(key.fileobj)
                else:
                    check(len(data[key.data]) + len(raw) <= 8192, 'OWNED_HOST_TOOL_OUTPUT_BOUND')
                    data[key.data].extend(raw)
        check(proc.wait(timeout=5) == 0, 'OWNED_HOST_TOOL_FAILED')
        return bytes(data['stdout'])
    finally:
        selected.close()
        if proc.poll() is None:
            if fd is not None:
                signal_owned(fd, row, signal.SIGKILL)
            else:
                # Still an unreaped direct Popen child, so no PID reuse yet.
                current = process(proc.pid)
                check(current['ppid'] == os.getpid() and current['uid'] == os.geteuid(), 'TOOL_PRE_PIDFD_FAILURE_OWNERSHIP')
                temporary = pidfd(proc.pid, current)
                try:
                    signal_owned(temporary, current, signal.SIGKILL)
                finally:
                    os.close(temporary)
            proc.wait(timeout=5)
        if fd is not None:
            os.close(fd)
        proc.stdout.close()
        proc.stderr.close()
        TOOL_CHILDREN_CLEAN = proc.returncode is not None
        raw_pins = []
        index = len(TOOL_RESULTS)
        for name, raw in data.items():
            path = TOOL_LOG_DIRECTORY / ('tool-%02d-%s.log-data' % (index, name))
            with path.open('xb') as stream:
                stream.write(raw)
            raw_pins.append(pin(path, 8192))
        TOOL_RESULTS.append({'tool': argv[0], 'returncode': proc.returncode,
            'terminated_and_reaped': TOOL_CHILDREN_CLEAN, 'raw_output_private_pins': raw_pins})


def subreaper():
    # This own-process attribute preserves ancestry for double-forked Chrome
    # children. It is an explicitly declared trusted-platform bootstrap API.
    import ctypes
    bootstrap_native = bootstrap_providers()
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    prctl.restype = ctypes.c_int
    set_child_subreaper, get_child_subreaper = 36, 37
    value = ctypes.c_int(-1)
    check(prctl(get_child_subreaper, ctypes.addressof(value), 0, 0, 0) == 0 and value.value in (0, 1), 'GET_OWN_SUBREAPER')
    before = value.value
    check(prctl(set_child_subreaper, 1, 0, 0, 0) == 0, 'SET_OWN_SUBREAPER')
    check(prctl(get_child_subreaper, ctypes.addressof(value), 0, 0, 0) == 0 and value.value == 1, 'VERIFY_OWN_SUBREAPER')
    return {'actual_before': before, 'actual_after': value.value, 'pre_prctl_provider_files': bootstrap_native, 'native_scope': 'Trusted platform ctypes/libffi/already-loaded-libc prctl own-process child-subreaper only; no host security policy change.'}


def bootstrap_providers():
    with Path('/proc/self/maps').open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    check(len(raw) <= 1024 * 1024, 'BOOTSTRAP_MAPPED_PROVIDER_READ_BOUND')
    paths = set()
    for line in raw.decode('utf-8', errors='strict').splitlines():
        fields = line.split(None, 5)
        check(len(fields) >= 5, 'BOOTSTRAP_MAPS_ROW')
        if 'x' not in fields[1] or len(fields) != 6 or fields[5].startswith('['):
            continue
        check(fields[5].startswith('/') and not fields[5].endswith(' (deleted)'), 'BOOTSTRAP_EXECUTABLE_PROVIDER_FILE')
        path = Path(fields[5]).resolve(strict=True)
        check(path.is_relative_to('/usr') or path.is_relative_to('/opt/hostedtoolcache/Python/3.12.16/x64'),
              'ONLY_TRUSTED_PLATFORM_BOOTSTRAP_PROVIDER_ROOTS')
        paths.add(path)
    check(len(paths) <= 128, 'BOOTSTRAP_MAPPED_PROVIDER_COUNT')
    return [pin(path, 192 * 1024 * 1024) for path in sorted(paths, key=str)]


class Children:
    def __init__(self, child):
        self.child, self.records, self.actions = child, {}, []
        self.root = process(child.pid)
        check(self.root['ppid'] == os.getpid() and self.root['sid'] == self.root['pgid'] == child.pid
              and self.root['uid'] == os.geteuid(), 'FRESH_OWNED_RUNTIME_SESSION')
        self.remember(self.root, 'direct_child_fresh_session')

    def remember(self, row, proof):
        key = identity(row)
        if key not in self.records:
            check(len(self.records) < 512 and row['uid'] == os.geteuid()
                  and row['start_ticks'] >= self.root['start_ticks'], 'OWNED_DESCENDANT_COUNT_UID_BIRTH')
            self.records[key] = {'first': row, 'ownership': proof, 'pidfd': pidfd(row['pid'], row)}

    def census(self):
        table = {}
        entries = list(Path('/proc').iterdir())
        check(len(entries) <= 8192, 'PLATFORM_PROCESS_NAMESPACE_BOUND')
        for path in entries:
            if not path.name.isdecimal():
                continue
            try:
                row = process(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                check(path.stat().st_uid != os.geteuid(), 'OWNED_PROCESS_CENSUS_PERMISSION_FAILURE')
                continue
            table[row['pid']] = row
        for _ in range(32):
            added = False
            session_owned = any(identity(row) in self.records and row['sid'] == self.root['pid']
                                for row in table.values())
            for row in table.values():
                if row['uid'] != os.geteuid() or row['start_ticks'] < self.root['start_ticks']:
                    continue
                known = identity(row) in self.records
                parent = table.get(row['ppid'])
                proof = ('fresh_runtime_session' if session_owned and row['sid'] == self.root['pid'] else
                         'verified_live_parent_birth' if parent and identity(parent) in self.records else
                         'adopted_child_of_exclusive_subreaper' if row['ppid'] == os.getpid() else None)
                if not known and proof:
                    try:
                        self.remember(row, proof)
                        added = True
                    except (FileNotFoundError, ProcessLookupError):
                        pass
            if not added:
                break
        else:
            check(False, 'OWNED_PROCESS_ANCESTRY_DEPTH_BOUND')
        return [row for row in table.values() if identity(row) in self.records]

    def terminate(self):
        errors = []
        remaining = None
        try:
            for selected_signal, duration in ((signal.SIGTERM, 3), (signal.SIGKILL, 7)):
                stop = time.monotonic() + duration
                while True:
                    rows = self.census()
                    for row in rows:
                        record = self.records[identity(row)]
                        if row['state'] != 'Z':
                            signal_owned(record['pidfd'], record['first'], selected_signal)
                            self.actions.append({'pid': row['pid'], 'start_ticks': row['start_ticks'],
                                                 'signal': int(selected_signal), 'pidfd_bound': True})
                        elif row['ppid'] == os.getpid() and row['pid'] != self.child.pid:
                            try:
                                os.waitpid(row['pid'], os.WNOHANG)
                            except ChildProcessError:
                                pass
                    self.child.poll()
                    if not self.census():
                        break
                    check(len(self.actions) <= 16384, 'OWNED_CLEANUP_ACTION_BOUND')
                    if time.monotonic() >= stop:
                        break
                    time.sleep(0.05)
            remaining = self.census()
        except BaseException as error:
            errors.append(safe_error(error))
            # Incomplete census never counts as absence. Still attempt every
            # already-proved persistent pidfd before reporting cleanup HOLD.
            for record in self.records.values():
                try:
                    signal_owned(record['pidfd'], record['first'], signal.SIGKILL)
                except BaseException as additional:
                    errors.append(safe_error(additional))
        try:
            self.child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            errors.append({'error_code': 'RUNTIME_CHILD_NOT_REAPED'})
        result = {'successful': remaining == [] and not errors, 'runtime_returncode': self.child.returncode,
            'root': self.root, 'tracked': [{'first': r['first'], 'ownership': r['ownership']}
                                        for r in self.records.values()], 'termination_actions': self.actions,
            'remaining': remaining, 'errors': errors, 'unrelated_processes_signalled': 0,
            'method': 'Own subreaper, PID/start/UID/session/live-ancestor proof and persistent pidfds; no pkill or global process-group signal.'}
        for record in self.records.values():
            os.close(record['pidfd'])
        return result


def copy_exact(source, target, expected, mode=0o444):
    observed = pin(source, 4 * 1024 * 1024)
    check(observed['bytes'] == expected['bytes'] and observed['sha256'] == digest(expected['sha256']), 'SELECTED_COPY_INPUT_PIN')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    os.fchmod(fd, mode)
    incoming_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    count, hashed = 0, hashlib.sha256()
    with os.fdopen(fd, 'wb') as output, os.fdopen(incoming_fd, 'rb') as incoming:
        while chunk := incoming.read(CHUNK):
            count += len(chunk)
            check(count <= observed['bytes'], 'SELECTED_COPY_STREAM_SIZE_BOUND')
            output.write(chunk)
            hashed.update(chunk)
    result = pin(target, 4 * 1024 * 1024, readonly=True)
    check(count == observed['bytes'] and hashed.hexdigest() == observed['sha256']
          and result['bytes'] == observed['bytes'] and result['sha256'] == observed['sha256']
          and pin(source, 4 * 1024 * 1024) == observed, 'SELECTED_COPY_BOOKEND')
    return result


def acquire(wheel, destination, environment):
    url = wheel['url']
    check(type(url) is str and re.fullmatch(r'https://files\.pythonhosted\.org/packages/[a-f0-9/]+/[A-Za-z0-9_.+-]+\.whl', url)
          and type(wheel['bytes']) is int and 0 < wheel['bytes'] <= 128 * 1024 * 1024, 'FIXED_PUBLIC_WHEEL_HTTPS_URL_AND_SIZE')
    check(not destination.exists(), 'FRESH_WHEEL_DOWNLOAD_PATH')
    argv = ['/usr/bin/curl', '--disable', '--fail', '--silent', '--show-error', '--proto', '=https',
            '--proto-redir', '=https', '--max-redirs', '0', '--connect-timeout', '10', '--max-time', '60',
            '--max-filesize', str(wheel['bytes']), '--proxy', '', '--noproxy', '*',
            '--output', str(destination), '--write-out', '%{http_code} %{size_download} %{url_effective}', url]
    response = command(argv, environment).decode('ascii', errors='strict')
    check(response == '200 ' + str(wheel['bytes']) + ' ' + url, 'EXACT_WHEEL_HTTP_STATUS_SIZE_FINAL_URL')
    actual = pin(destination, 128 * 1024 * 1024)
    check(actual['bytes'] == wheel['bytes'] and actual['sha256'] == digest(wheel['sha256']), 'EXACT_WHEEL_DOWNLOAD_SHA256_SIZE')
    os.chmod(destination, 0o400)
    return actual


def install_wheel(archive, wheel, ledger, packages, installed):
    expected = {row['path']: row for row in ledger if row['distribution'] == wheel['name'] and row['version'] == wheel['version']}
    check(len(expected) == wheel['regular_members'], 'FIXED_WHEEL_MEMBER_COUNT')
    with Path(archive).open('rb') as stream:
        stream.seek(max(0, Path(archive).stat().st_size - 65557))
        tail = stream.read(65557)
    at = tail.rfind(b'PK\x05\x06')
    check(at >= 0 and len(tail) - at >= 22, 'WHEEL_EOCD_REQUIRED')
    disk, cd_disk, disk_count, total_count, cd_bytes, cd_offset, comment = struct.unpack_from('<HHHHIIH', tail, at + 4)
    check(disk == cd_disk == 0 and disk_count == total_count and 0 < total_count <= 2048
          and cd_bytes <= 2 * 1024 * 1024 and cd_offset + cd_bytes <= Path(archive).stat().st_size
          and at + 22 + comment == len(tail), 'BOUNDED_SINGLE_DISK_NONZIP64_WHEEL_DIRECTORY')
    observed, names = set(), set()
    expected_dirs = {p.as_posix() for name in expected for p in Path(name).parents if p != Path('.')}
    with zipfile.ZipFile(archive, 'r') as zipped:
        infos = zipped.infolist()
        check(len(infos) == total_count, 'WHEEL_EXACT_CENTRAL_DIRECTORY_COUNT')
        for entry in infos:
            deadline()
            check(entry.orig_filename == entry.filename, 'NO_ZIP_FILENAME_TRUNCATION_ALIAS')
            name = entry.filename[:-1] if entry.is_dir() else entry.filename
            canonical(name)
            check(name.casefold() not in names and not entry.flag_bits & 1
                  and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), 'WHEEL_UNIQUE_UNENCRYPTED_SUPPORTED_MEMBER')
            names.add(name.casefold())
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if entry.is_dir():
                check(kind in (0, stat.S_IFDIR) and entry.file_size == 0 and name in expected_dirs,
                      'WHEEL_ONLY_DECLARED_PARENT_DIRECTORIES')
                continue
            check(kind in (0, stat.S_IFREG) and name in expected and name not in observed and name not in installed,
                  'WHEEL_REGULAR_EXACT_OWNED_MEMBER_NO_COLLISION')
            wanted = expected[name]
            check(type(wanted['bytes']) is int and entry.file_size == wanted['bytes']
                  and 0 <= entry.file_size <= 192 * 1024 * 1024, 'WHEEL_FIXED_UNCOMPRESSED_SIZE')
            target = packages / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            mode = 0o555 if name == 'playwright/driver/node' else 0o444
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            os.fchmod(fd, mode)
            hashed, count = hashlib.sha256(), 0
            with os.fdopen(fd, 'wb') as outgoing, zipped.open(entry, 'r') as incoming:
                while chunk := incoming.read(CHUNK):
                    count += len(chunk)
                    check(count <= wanted['bytes'], 'WHEEL_MEMBER_STREAM_SIZE_BOUND')
                    outgoing.write(chunk)
                    hashed.update(chunk)
            check(count == wanted['bytes'] and hashed.hexdigest() == digest(wanted['sha256']), 'WHEEL_MEMBER_ACTUAL_SHA256')
            actual = pin(target, 192 * 1024 * 1024, readonly=True)
            check(actual['bytes'] == wanted['bytes'] and actual['sha256'] == wanted['sha256'], 'WHEEL_MEMBER_DISK_READBACK')
            installed[name] = actual
            observed.add(name)
    check(observed == set(expected), 'WHEEL_ALL_REGULAR_MEMBERS_PRESENT')
    return {'name': wheel['name'], 'version': wheel['version'], 'regular_members': len(observed),
            'uncompressed_bytes': sum(expected[name]['bytes'] for name in observed)}


def runtime_run(argv, environment, source, private, delivery):
    logs = {name: (delivery / ('runtime-' + name + '.log-data')).open('xb') for name in ('stdout', 'stderr')}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=source, env=environment, close_fds=True, start_new_session=True)
    children, selected, result, failure = None, selectors.DefaultSelector(), None, None
    sizes = {'stdout': 0, 'stderr': 0}
    try:
        children = Children(proc)
        for name in sizes:
            stream = getattr(proc, name)
            os.set_blocking(stream.fileno(), False)
            selected.register(stream, selectors.EVENT_READ, name)
        stop = time.monotonic() + RUNTIME_SECONDS
        while selected.get_map() or proc.poll() is None:
            deadline()
            check(time.monotonic() < stop, 'OWNED_SEVEN_FLOW_RUNTIME_TIMEOUT')
            children.census()
            for key, _ in selected.select(0.1):
                raw = os.read(key.fd, CHUNK)
                if not raw:
                    selected.unregister(key.fileobj)
                    continue
                sizes[key.data] += len(raw)
                check(sizes[key.data] <= 16 * 1024 * 1024, 'OWNED_RUNTIME_STDIO_LIMIT')
                logs[key.data].write(raw)
        result = proc.wait(timeout=5)
    except BaseException as error:
        failure = safe_error(error)
    finally:
        selected.close()
        cleanup = children.terminate() if children is not None else {'successful': False, 'error_code': 'CHILD_IDENTITY_BINDING_INCOMPLETE'}
        if children is None and proc.poll() is None:
            row = process(proc.pid)
            check(row['ppid'] == os.getpid() and row['uid'] == os.geteuid(), 'UNBOUND_RUNTIME_DIRECT_CHILD_OWNERSHIP')
            fd = pidfd(proc.pid, row)
            try:
                signal_owned(fd, row, signal.SIGKILL)
                proc.wait(timeout=5)
            finally:
                os.close(fd)
        proc.stdout.close()
        proc.stderr.close()
        for stream in logs.values():
            stream.close()
        save(delivery / 'host-process-cleanup.json', cleanup)
        save(delivery / 'host-runtime-result.json', {'successful': result == 0 and failure is None and cleanup['successful'],
            'returncode_before_cleanup': result, 'failure': failure, 'cleanup_successful': cleanup['successful'],
            'raw_logs_preserved_privately_not_exported': True,
            'raw_log_pins': [pin(delivery / ('runtime-' + name + '.log-data'), 16 * 1024 * 1024) for name in sizes]})
    cleanup['runtime_successful'] = result == 0 and failure is None and cleanup['successful']
    return cleanup


def validate_evidence(evidence):
    observed = {p.name for p in evidence.iterdir()}
    check(observed == set(RUNTIME_REPORTS) | {name + '.png' for name in CAPTURES}, 'EXACT_SEVEN_FLOW_REPORT_CAPTURE_NAMESPACE')
    summary, _ = read_json(evidence / 'desktop-browser-summary.json')
    runtime, _ = read_json(evidence / 'desktop-runtime-summary.json')
    check(summary['status'] == 'PASS' and tuple(summary['expected_ids']) == IDS
          and tuple(row['id'] for row in summary['cases']) == IDS
          and all(row['status'] == 'PASS' and row['cleanup_complete'] is True
                  and row['final_validation_passed'] is True and row['evidence_finalized'] is True for row in summary['cases'])
          and summary['cleanup_complete'] is True and summary['browser_closed'] is True and not summary['suite_errors']
          and summary['final_validation_after_context_fixture_and_browser_close'] is True,
          'ALL_ORIGINAL_SEVEN_CASE_FINAL_GATES')
    check(runtime['successful'] is True and [runtime[key] for key in ('flows', 'captures', 'verified_downloads', 'actual_download_events')] == [7, 14, 8, 8]
          and runtime['mandatory_distributions'] == 18 and runtime['regular_python_files'] == 1202,
          'EXACT_RUNTIME_7_14_8_8_18_1202')
    check(tuple(row['name'] for row in summary['captures']) == CAPTURES
          and len(summary['downloads']) == len(summary['download_events']) == 8, 'EXACT_CAPTURE_DOWNLOAD_EVENT_COUNTS')
    events, _ = read_json(evidence / 'desktop-download-events.json')
    check(events == summary['download_events'], 'GLOBAL_DOWNLOAD_EVENT_LEDGER_EQUALS_SUMMARY')
    total = 0
    for row in summary['captures']:
        actual = pin(evidence / (row['name'] + '.png'), 1536 * 1024)
        check(actual['bytes'] == row['bytes'] and actual['sha256'] == digest(row['sha256']), 'ACTUAL_CAPTURE_DISK_PIN')
        with Path(actual['path']).open('rb') as stream:
            header = stream.read(24)
        check(header[:8] == b'\x89PNG\r\n\x1a\n' and struct.unpack('>II', header[16:24]) == (1280, 900), 'ACTUAL_CAPTURE_PNG_VIEWPORT')
        total += actual['bytes']
    check(total <= 8 * 1024 * 1024, 'ACTUAL_CAPTURE_TOTAL_BYTE_LIMIT')
    sandbox, _ = read_json(evidence / 'desktop-sandbox.json')
    actual = sandbox['actual_sandbox']
    check(sandbox['owned_processes_gone'] is True and actual['explicit_chromium_sandbox'] is True
          and actual['additional_renderer_filter'] is True
          and actual['renderer_user_pid_net_namespaces_differ_from_driver'] is True,
          'ACTUAL_REQUIRED_RENDERER_SANDBOX_RECEIPT')
    for row in [actual['outer'], actual['driver'], actual['browser']] + actual['renderers']:
        check(row['no_new_privileges'] == 1 and row['uid'] == os.geteuid() > 0
              and all(int(row['capabilities'][key], 16) == 0 for key in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')),
              'ACTUAL_SANDBOX_NNP_NONROOT_CAPABILITY_FACTS')
    check(actual['renderers'] and all(row['seccomp'] == 2 and row['seccomp_filters'] > actual['browser']['seccomp_filters']
          and all(row['namespaces'][key] != actual['driver']['namespaces'][key] for key in ('user', 'pid', 'net'))
          for row in actual['renderers']), 'ACTUAL_RENDERER_KERNEL_NAMESPACES_AND_SECCOMP_FACTS')
    return {'successful': True, 'flows': 7, 'captures': 14, 'verified_downloads': 8, 'actual_download_events': 8,
            'actual_capture_bytes': total, 'visual_review_still_required': True, 'customer_readiness_claim': False}


def remove_owned_tree(root, expected_identity):
    """Only after owned process cleanup; never follow child-created links."""
    info = root.lstat()
    check((info.st_dev, info.st_ino) == expected_identity and info.st_uid == os.geteuid()
          and stat.S_ISDIR(info.st_mode) and not root.is_symlink(), 'CLEANUP_EXACT_OWNED_ROOT_IDENTITY')
    visited = [0]
    def remove(directory):
        check(directory.lstat().st_uid == os.geteuid() and not directory.is_symlink(), 'CLEANUP_OWNED_DIRECTORY')
        os.chmod(directory, 0o700)
        with os.scandir(directory) as scanner:
            entries = list(scanner)
        for entry in entries:
            visited[0] += 1
            check(visited[0] <= 20000, 'CLEANUP_ENTRY_BOUND')
            path = Path(entry.path)
            item = path.lstat()
            check(item.st_uid == os.geteuid() and item.st_dev == expected_identity[0], 'CLEANUP_ONLY_OWNED_SAME_DEVICE_ENTRY')
            if stat.S_ISDIR(item.st_mode) and not stat.S_ISLNK(item.st_mode):
                remove(path)
                path.rmdir()
            else:
                check(stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode) or stat.S_ISSOCK(item.st_mode)
                      or stat.S_ISFIFO(item.st_mode), 'CLEANUP_ONLY_OWNED_EXPECTED_FILE_LINK_OR_CHROME_IPC')
                path.unlink()
    remove(root)
    root.rmdir()
    return {'successful': not root.exists(), 'owned_entries_removed': visited[0], 'root_device': info.st_dev, 'root_inode': info.st_ino}


def emit(delivery, names, successful):
    # No raw archive, installed file or raw stdout/stderr is eligible here.
    check(type(names) is list and len(names) <= 64 and len(set(names)) == len(names), 'EXPORT_NAME_COUNT_BOUND')
    total, inventory = 0, []
    for name in names:
        check(re.fullmatch(r'[A-Za-z0-9_-]+\.(?:json|png)', name), 'FLAT_DERIVED_EXPORT_NAME')
        actual = pin(delivery / name, 4 * 1024 * 1024)
        total += actual['bytes']
        check(total <= 48 * 1024 * 1024, 'TOTAL_DERIVED_EXPORT_BOUND')
        inventory.append({'name': name, 'bytes': actual['bytes'], 'sha256': actual['sha256']})
    for row in inventory:
        fd = os.open(delivery / row['name'], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        check(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'], 'FINAL_DERIVED_EXPORT_HASH')
        payload = base64.encodebytes(raw)
        payload_sha = hashlib.sha256(payload).hexdigest()
        name = 'diagnostics/platform/' + row['name']
        token = os.urandom(16).hex()
        header = {'name': name, 'bytes': len(payload), 'sha256': payload_sha, 'encoding': 'base64',
                  'decoded_bytes': len(raw), 'decoded_sha256': row['sha256'],
                  'convention': 'SHA256 of base64 ASCII payload; length excludes separator LF'}
        print('::group::Evidence ' + name)
        print('::stop-commands::' + token)
        print('SS_CI_EVIDENCE_BEGIN ' + json.dumps(header, separators=(',', ':')))
        sys.stdout.flush()
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.write(b'\n')
        sys.stdout.buffer.flush()
        print('SS_CI_EVIDENCE_END ' + json.dumps(dict(header, delivered=True), separators=(',', ':')))
        print('::' + token + '::')
        print('::endgroup::')
    print('SS_PLATFORM_FUNCTIONAL_COMPLETE ' + json.dumps({'successful': successful, 'reports': inventory}, separators=(',', ':')), flush=True)


def main():
    global TOOL_LOG_DIRECTORY
    check(len(sys.argv) == 1 and sys.platform == 'linux' and os.geteuid() > 0 and os.getegid() > 0
          and os.uname().machine == 'x86_64'
          and sys.version.split()[0] == '3.12.16' and sys.flags.isolated and sys.flags.no_site
          and sys.flags.ignore_environment and sys.dont_write_bytecode
          and sys.base_prefix == '/opt/hostedtoolcache/Python/3.12.16/x64', 'EXACT_PREINSTALLED_PLATFORM_HOST_PYTHON')
    os.umask(0o077)
    check(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
          and os.environ.get('RUNNER_OS') == 'Linux' and os.environ.get('ImageOS') == 'ubuntu24', 'ROOT_ADMITTED_FRESH_GITHUB_UBUNTU24_PLATFORM_ONLY')
    repo, temporary = Path(os.environ['GITHUB_WORKSPACE']), Path(os.environ['RUNNER_TEMP'])
    check(repo.resolve(strict=True) == repo and temporary.resolve(strict=True) == temporary, 'CANONICAL_GITHUB_REPO_AND_TEMP')
    external = Path(os.environ['SS_PLATFORM_HOST_METADATA'])
    check(external.name == 'host-metadata.json' and external.parent.parent == temporary
          and re.fullmatch(r'stock-sleeve-platform-activation-[a-f0-9]{32}', external.parent.name)
          and external.parent.lstat().st_uid == os.geteuid() and external.parent.lstat().st_mode & 0o077 == 0,
          'EXACT_OWNED_EXTERNAL_POSTCHECKOUT_METADATA_PATH')
    check(external.lstat().st_uid == os.geteuid() and stat.S_IMODE(external.lstat().st_mode) == 0o600,
          'OWNED_PRIVATE_EXTERNAL_METADATA_FILE')
    metadata, metadata_pin = read_json(external, os.environ['SS_PLATFORM_HOST_METADATA_SHA256'])
    check(set(metadata) == {'format', 'nonce', 'source_sha', 'job_id', 'run_id', 'job_key', 'image_version', 'root_grant_sha256'}
          and metadata['format'] == 'stock-and-sleeve:platform-postcheckout-host-metadata:v1'
          and re.fullmatch(r'[a-f0-9]{32}', metadata['nonce'])
          and external.parent.name.endswith(metadata['nonce']) and re.fullmatch(r'[a-f0-9]{40}', metadata['source_sha'])
          and re.fullmatch(r'[0-9]{1,24}', metadata['job_id'])
          and metadata['run_id'] == os.environ['GITHUB_RUN_ID'] and metadata['job_key'] == os.environ['GITHUB_JOB']
          and metadata['image_version'] == os.environ['ImageVersion']
          and re.fullmatch(r'[0-9]{8}\.[0-9]+\.[0-9]+', metadata['image_version']), 'EXACT_SAME_RUN_POSTCHECKOUT_METADATA')
    grant_path = external.parent / 'root-grant.json'
    check(grant_path.lstat().st_uid == os.geteuid() and stat.S_IMODE(grant_path.lstat().st_mode) == 0o600,
          'OWNED_PRIVATE_EXTERNAL_ROOT_GRANT_FILE')
    check(metadata['root_grant_sha256'] == digest(os.environ['SS_PLATFORM_ROOT_GRANT_SHA256']), 'EXTERNALLY_ROOT_PINNED_GRANT')
    grant, grant_pin = read_json(grant_path, metadata['root_grant_sha256'])
    check(set(grant) == {'format', 'execution_hold', 'host_execution_admitted', 'source_manifest_sha256',
                        'source_review_sha256', 'runtime_source_review_sha256', 'node_api_scope_sha256', 'foundation_scope'}
          and grant['format'] == 'stock-and-sleeve:trusted-platform-host-root-grant:v1'
          and grant['execution_hold'] is None and grant['host_execution_admitted'] is True
          and grant['foundation_scope'] == 'fresh-github-ubuntu24-preinstalled-chrome-functional-only', 'ROOT_HOST_EXECUTION_GRANT_HOLD')
    for key in ('source_review_sha256', 'runtime_source_review_sha256', 'node_api_scope_sha256'):
        digest(grant[key])
    manifest, manifest_pin = read_json(repo / 'tools/desktop_label_platform_host_source.json', grant['source_manifest_sha256'])
    check(manifest['format'] == 'stock-and-sleeve:platform-host-static-source:v1'
          and manifest['selected_source_count'] == 107 and len(manifest['files']) == 11, 'FIXED_NONSELF_STATIC_SOURCE_LEDGER')
    expected_names = {'tools/' + name for name in TEMPLATES} | {'tools/desktop_label_platform_host.py', 'tools/desktop_label_platform_wheels.json'}
    check({row['path'] for row in manifest['files']} == expected_names and len(expected_names) == 11, 'EXACT_STATIC_SOURCE_NAMES')
    pins, rows_by_name = [], {}
    for row in manifest['files']:
        canonical(row['path'])
        actual = pin(repo / row['path'], 4 * 1024 * 1024)
        check(actual['bytes'] == row['bytes'] and actual['sha256'] == digest(row['sha256']), 'STATIC_SOURCE_BYTES')
        pins.append(actual)
        rows_by_name[row['path']] = row
    check(Path(__file__).resolve() == repo / 'tools/desktop_label_platform_host.py', 'ACTUAL_HOST_SOURCE_LOCATION')
    runroot = temporary / ('stock-sleeve-platform-' + metadata['nonce'])
    delivery = temporary / ('stock-sleeve-platform-evidence-' + metadata['nonce'])
    check(not runroot.exists() and not runroot.is_symlink() and not delivery.exists() and not delivery.is_symlink(), 'NONCE_PATHS_MUST_BE_FRESH')
    private_directory(runroot)
    private_directory(delivery)
    TOOL_LOG_DIRECTORY = delivery
    root_identity = (runroot.lstat().st_dev, runroot.lstat().st_ino)
    source, packages, evidence = runroot / 'source', runroot / 'packages', runroot / 'evidence'
    for path in (source, packages, evidence, runroot / 'runtime', runroot / 'tmp', runroot / 'archives', runroot / 'tool-home'):
        private_directory(path)
    tool_env = {'PATH': '/usr/bin:/bin', 'HOME': str(runroot / 'tool-home'), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_COUNT': '0'}
    successful, failure, process_cleanup, stage_pins, runtime_started = False, None, None, [], False
    source_copies, downloads, installed_receipts, installed, last_step = [], [], [], {}, 'preflight'
    try:
        for chosen_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(chosen_signal, lambda *_: check(False, 'HOST_SIGNAL_INTERRUPTION'))
        tools = [pin(Path('/usr/bin/' + name).resolve(strict=True), 32 * 1024 * 1024) for name in ('git', 'curl', 'setpriv')]
        tools.append(pin(Path(sys.executable).resolve(strict=True), 32 * 1024 * 1024))
        tools.append(pin(Path('/opt/google/chrome/chrome'), 384 * 1024 * 1024))
        check(command(['/usr/bin/git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD'], tool_env, 10).decode('ascii').strip()
              == metadata['source_sha'], 'ACTUAL_CHECKOUT_HEAD_MATCHES_EXTERNAL_METADATA')
        subreaper_receipt = subreaper()
        bootstrap_native = subreaper_receipt['pre_prctl_provider_files']
        save(delivery / 'host-bootstrap.json', {'successful': True, 'metadata': metadata, 'metadata_pin': metadata_pin,
            'root_grant': grant_pin, 'source_manifest': manifest_pin, 'host_process': process(os.getpid()),
            'tool_files': tools, 'subreaper': subreaper_receipt, 'python_version': sys.version,
            'observed_bootstrap_native_providers': bootstrap_native,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'numeric_job_id_authority': 'Root external postcheckout metadata; GITHUB_RUN_ID/job_key are separately compared. No guessed numeric job ID.',
            'bootstrap_scope': 'Trusted preinstalled Python stdlib ZIP/zlib/hash/JSON/base64/Popen/selectors/signal/pidfd/ctypes-libffi-libc plus existing curl/git/setpriv/Chrome platform. No app/vendor import during staging.'})
        source_inputs, _ = read_json(repo / 'tools/desktop_label_browser_inputs.json')
        ledger, _ = read_json(repo / 'tools/desktop_label_browser_installed_files.json')
        wheel_spec, _ = read_json(repo / 'tools/desktop_label_platform_wheels.json')
        check(len(source_inputs['sources']) == 107 and len(ledger['files']) == 1202 and len(ledger['distributions']) == 18
              and ledger['install_root'] == 'packages' and wheel_spec['count'] == len(wheel_spec['wheels']) == 18
              and wheel_spec['format'] == 'stock-and-sleeve:fixed-platform-wheel-inputs:v1'
              and wheel_spec['regular_members'] == 1202
              and sum(row['bytes'] for row in wheel_spec['wheels']) == wheel_spec['archive_bytes'] == 78114066
              and sum(row['bytes'] for row in ledger['files']) == wheel_spec['uncompressed_bytes'] == 235349482
              and {(row['name'], row['version']) for row in wheel_spec['wheels']} ==
                  {(row['name'], row['version']) for row in ledger['distributions']},
              'FIXED_SOURCE107_AND_WHEELS18_MEMBERS1202')
        names = set()
        for row in source_inputs['sources']:
            last_step = 'source-copy:' + row['path']
            name = canonical(row['path'])
            check(name.casefold() not in names, 'UNIQUE_SOURCE107_NAMES')
            names.add(name.casefold())
            source_copies.append(copy_exact(repo / name, source / name, row))
        for name in TEMPLATES:
            if name in ('desktop_label_browser_fixture.py', 'desktop_label_browser_cases.py', 'desktop_label_browser_ci.py',
                        'desktop_label_browser_admission.json', 'desktop_label_browser_platform.json'):
                continue
            source_copies.append(copy_exact(repo / ('tools/' + name), source / ('tools/' + name), rows_by_name['tools/' + name]))
        suite, _ = read_json(repo / 'tools/desktop_label_browser_admission.json')
        platform, _ = read_json(repo / 'tools/desktop_label_browser_platform.json')
        check(suite['execution_hold'] is not None and platform['trusted_platform_execution_admitted'] is False
              and platform['execution_hold'] is not None and platform['native_image_flags_inherited'] is False
              and platform['independent_source_go_sha256'] is None and platform['conditional_node_api_scope_sha256'] is None,
              'FROZEN_FALSE_PLATFORM_TEMPLATES_REQUIRED')
        suite['execution_hold'] = None
        suite_pin = save(source / 'tools/desktop_label_browser_admission.json', suite)
        os.chmod(suite_pin['path'], 0o444)
        platform['execution_hold'] = None
        platform['trusted_platform_execution_admitted'] = True
        platform['independent_source_go_sha256'] = grant['runtime_source_review_sha256']
        platform['conditional_node_api_scope_sha256'] = grant['node_api_scope_sha256']
        matched = 0
        for row in platform['runtime_source_files']:
            if row['name'] == 'desktop_label_browser_admission.json':
                row['bytes'], row['sha256'] = suite_pin['bytes'], suite_pin['sha256']
                matched += 1
        check(matched == 1, 'ONE_DEPENDENT_SUITE_ADMISSION_PIN')
        platform_pin = save(source / 'tools/desktop_label_browser_platform.json', platform)
        os.chmod(platform_pin['path'], 0o444)
        for wheel in wheel_spec['wheels']:
            last_step = 'wheel-download:' + wheel['name']
            digest(wheel['sha256'])
            archive = runroot / 'archives' / (wheel['sha256'] + '.whl-data')
            actual = acquire(wheel, archive, tool_env)
            downloads.append(dict(actual, url=wheel['url'], name=wheel['name'], version=wheel['version']))
            last_step = 'wheel-data-extraction:' + wheel['name']
            installed_receipts.append(install_wheel(archive, wheel, ledger['files'], packages, installed))
            check(pin(archive, 128 * 1024 * 1024) == actual, 'WHEEL_ARCHIVE_POST_DECODE_BOOKEND')
        check(set(installed) == {row['path'] for row in ledger['files']} and len(installed) == 1202
              and len({row['name'] for row in wheel_spec['wheels']}) == 18,
              'COMPLETE18_1202_STAGE_NAMESPACE')
        for path in sorted((p for p in packages.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            os.chmod(path, 0o555)
        stage_pins = source_copies + list(installed.values()) + [suite_pin, platform_pin]
        save(delivery / 'host-stage-inventory.json', {'successful': True, 'source_copies': source_copies,
            'downloaded_archives': downloads, 'installed_wheels': installed_receipts,
            'installed_files': list(installed.values()), 'activation_suite': suite_pin, 'activation_platform': platform_pin,
            'activation_only_after_root_grant': grant_pin['sha256'], 'source_inputs': 107,
            'raw_archives_or_vendor_bytes_exported': False})
        base_env = {'PATH': '/usr/bin:/bin', 'HOME': str(runroot / 'home'), 'USER': 'ss-ci', 'LOGNAME': 'ss-ci',
                    'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TMPDIR': str(runroot / 'tmp')}
        runtime_file = source / 'tools/desktop_label_browser_platform_runtime.py'
        child_env = dict(base_env, SS_CI_SOURCE_SHA=metadata['source_sha'], SS_PLATFORM_RUNTIME_SHA256=pin(runtime_file)['sha256'],
            SS_PLATFORM_ADMISSION_SHA256=platform_pin['sha256'], SS_DESKTOP_OWNER_NONCE=metadata['nonce'],
            SS_PLATFORM_RUNNER_TEMP=str(temporary), SS_PLATFORM_IMAGE_VERSION=metadata['image_version'], SS_PLATFORM_JOB_ID=metadata['job_id'])
        argv = ['/usr/bin/setpriv', '--no-new-privs', str(Path(sys.executable).resolve(strict=True)), '-I', '-S', '-B', str(runtime_file)]
        runtime_started = True
        last_step = 'real-seven-flow-runtime'
        process_cleanup = runtime_run(argv, child_env, source, runroot, delivery)
        check(process_cleanup['runtime_successful'], 'SEVEN_FLOW_RUNTIME_OR_OWNED_CLEANUP_FAILED')
        acceptance = validate_evidence(evidence)
        for item in stage_pins:
            check(pin(item['path'], 192 * 1024 * 1024) == item, 'FINAL_SOURCE_AND_STAGE_BOOKEND')
        for item in tools + bootstrap_native + pins + [metadata_pin, grant_pin, manifest_pin]:
            check(pin(item['path'], 384 * 1024 * 1024) == item, 'FINAL_TRUSTED_TOOL_SOURCE_METADATA_BOOKEND')
        save(delivery / 'host-functional-acceptance.json', acceptance)
        successful = True
    except BaseException as error:
        failure = safe_error(error)
    finally:
        # Preserve all original bounded JSON/PNG evidence and private raw logs
        # before removing source, wheels, packages, profiles and transient data.
        for path in sorted(evidence.iterdir(), key=lambda p: p.name):
            if path.name in RUNTIME_REPORTS or path.name in {name + '.png' for name in CAPTURES}:
                try:
                    observed = pin(path, 4 * 1024 * 1024)
                    copy_exact(path, delivery / path.name, observed, mode=0o400)
                except BaseException as error:
                    successful = False
                    failure = failure or safe_error(error)
            else:
                successful = False
                failure = failure or {'error_code': 'UNEXPECTED_PRIVATE_RUNTIME_EVIDENCE_ENTRY'}
        archive_facts = []
        for path in sorted((runroot / 'archives').iterdir(), key=lambda p: p.name):
            try:
                archive_facts.append(pin(path, 128 * 1024 * 1024))
            except BaseException as error:
                archive_facts.append(dict(safe_error(error), name=path.name))
        save(delivery / 'host-stage-progress.json', {'successful': successful, 'failure': failure,
            'last_step': last_step, 'source_copies_completed': source_copies,
            'downloads_completed': downloads, 'wheel_extractions_completed': installed_receipts,
            'original_remaining_archive_hash_facts_before_cleanup': archive_facts,
            'vendor_or_raw_archive_bytes_exported': False})
        cleaned = None
        if process_cleanup is not None and process_cleanup['successful']:
            try:
                cleaned = remove_owned_tree(runroot, root_identity)
            except BaseException as error:
                successful = False
                failure = failure or safe_error(error)
        elif not runtime_started and TOOL_CHILDREN_CLEAN:
            # No application runtime was started. All prior fixed tool children
            # are synchronously pidfd-terminated/reaped by command().
            try:
                cleaned = remove_owned_tree(runroot, root_identity)
            except BaseException as error:
                failure = failure or safe_error(error)
        else:
            successful = False
            failure = failure or {'error_code': 'OWNED_PROCESS_CLEANUP_NOT_PROVEN_NO_FILES_REMOVED'}
        save(delivery / 'host-tool-results.json', {'successful': TOOL_CHILDREN_CLEAN,
            'commands': TOOL_RESULTS, 'raw_tool_output_preserved_privately_not_exported': True})
        save(delivery / 'host-summary.json', {'successful': successful, 'failure': failure,
            'nonce': metadata['nonce'], 'source_sha': metadata['source_sha'], 'run_id': metadata['run_id'], 'job_key': metadata['job_key'],
            'external_metadata': metadata_pin, 'root_grant': grant_pin, 'temporary_cleanup': cleaned,
            'runtime_process_cleanup_proved': process_cleanup is not None and process_cleanup['successful'],
            'raw_stdout_stderr_private_only': True, 'normal_platform_network_not_egress_isolation': True,
            'custom_native_image_or_customer_readiness_claim': False})
        names = sorted(path.name for path in delivery.iterdir()
                       if path.name in HOST_REPORTS or path.name in RUNTIME_REPORTS
                       or path.name in {name + '.png' for name in CAPTURES})
        emit(delivery, names, successful)
    check(successful, 'OWNED_PLATFORM_SEVEN_FLOW_ACCEPTANCE_FAILED')


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        print(json.dumps(dict(safe_error(error), successful=False), separators=(',', ':')), flush=True)
        raise SystemExit(1)
