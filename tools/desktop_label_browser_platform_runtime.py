"""UNRUN seven-flow functional CI on a separately trusted fresh GitHub VM.

No custom-image/native/distribution flag is inherited or enabled. The platform
interpreter, OS, fonts and preinstalled official Chrome are an explicit test
foundation; the exact owned18 package bytes and local Node pipes remain pinned.
Root must review/admit this source and its new conditional Node/API scope first.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'
RUNROOT = ROOT.parent
PACKAGES = RUNROOT / 'packages'
BROWSER = Path('/opt/google/chrome/chrome')
NODE = PACKAGES / 'playwright/driver/node'
CLI = PACKAGES / 'playwright/driver/package/cli.js'
EVIDENCE = RUNROOT / 'evidence'
CHUNK = 65536
STARTED = time.monotonic()
FIXED_ENV = {'PATH': '/usr/bin:/bin', 'HOME': str(RUNROOT / 'home'),
             'USER': 'ss-ci', 'LOGNAME': 'ss-ci', 'LANG': 'C.UTF-8',
             'LC_ALL': 'C.UTF-8', 'TMPDIR': str(RUNROOT / 'tmp')}
HOST_ENV = {'SS_CI_SOURCE_SHA': r'[a-f0-9]{40}',
            'SS_PLATFORM_RUNTIME_SHA256': r'[a-f0-9]{64}',
            'SS_PLATFORM_ADMISSION_SHA256': r'[a-f0-9]{64}',
            'SS_DESKTOP_OWNER_NONCE': r'[a-f0-9]{32}',
            'SS_PLATFORM_RUNNER_TEMP': r'/[A-Za-z0-9_./-]{1,240}',
            'SS_PLATFORM_IMAGE_VERSION': r'[0-9]{8}\.[0-9]+\.[0-9]+',
            'SS_PLATFORM_JOB_ID': r'[0-9]{1,24}'}
NODE_PIN = {'bytes': 150348872,
            'sha256': 'd931c649850ffa7155b8ceffb400dc916ef325866e509cc52ae36e33e8fbb9a9'}
CLI_PIN = {'bytes': 867,
           'sha256': '7c46f799363eee3e0bb7a7d7c870a92316a1601bf6fda780b656df4209542aa9'}


class RuntimeFailure(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise RuntimeFailure(code)


def deadline():
    require(time.monotonic() - STARTED <= 900, 'DESKTOP_RUNTIME_DEADLINE')


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_RUNTIME_JSON_KEY')
        result[key] = value
    return result


def pin_file(path, maximum, readonly=True):
    path = Path(path)
    require(path.is_absolute() and not any(parent.is_symlink() for parent in (path, *path.parents)),
            'RUNTIME_PATH_OR_SYMLINK')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and 0 <= before.st_size <= maximum and (not readonly or before.st_mode & 0o222 == 0),
            'RUNTIME_REGULAR_READONLY_BOUNDED_FILE')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    count, hashed = 0, hashlib.sha256()
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_nlink,
                value.st_mtime_ns, value.st_ctime_ns)
    try:
        require(identity(os.fstat(descriptor)) == identity(before), 'RUNTIME_OPEN_IDENTITY')
        while chunk := os.read(descriptor, CHUNK):
            deadline()
            count += len(chunk)
            require(count <= maximum, 'RUNTIME_FILE_READ_BOUND')
            hashed.update(chunk)
        require(identity(os.fstat(descriptor)) == identity(before) == identity(path.lstat())
                and count == before.st_size, 'RUNTIME_FILE_CHANGED')
    finally:
        os.close(descriptor)
    return {'path': str(path), 'bytes': count, 'sha256': hashed.hexdigest(),
            'device': before.st_dev, 'inode': before.st_ino}


def pinned_json(path, expected=None):
    pin = pin_file(path, 2 * 1024 * 1024)
    require(expected is None or pin['sha256'] == expected, 'RUNTIME_JSON_EXTERNAL_PIN')
    raw = Path(path).read_bytes()
    require(len(raw) == pin['bytes'] and hashlib.sha256(raw).hexdigest() == pin['sha256'],
            'RUNTIME_JSON_READBACK')
    value = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(RuntimeFailure('NONFINITE_RUNTIME_JSON')))
    require(type(value) is dict, 'RUNTIME_JSON_OBJECT')
    return value, pin


def save(name, value):
    require(name in ('desktop-runtime-driver.json', 'desktop-runtime-summary.json', 'platform-foundation.json', 'platform-native-mappings.json'), 'FIXED_RUNTIME_REPORT_NAME')
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                     allow_nan=False).encode('ascii')
    require(len(raw) <= 4 * 1024 * 1024, 'RUNTIME_REPORT_BOUND')
    descriptor = os.open(EVIDENCE / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)


def closed_environment(platform):
    require(set(os.environ) == set(FIXED_ENV) | set(HOST_ENV), 'EXACT_SCRUBBED_PLATFORM_CHILD_ENVIRONMENT')
    require(all(os.environ.get(key) == value for key, value in FIXED_ENV.items()),
            'FIXED_PRIVATE_PLATFORM_ENVIRONMENT')
    for key, pattern in HOST_ENV.items():
        require(re.fullmatch(pattern, os.environ.get(key, '')), 'FIXED_PLATFORM_HOST_RECEIPT_VALUE')
    require(platform['foundation_scope'] == 'fresh-github-ubuntu24-preinstalled-chrome-functional-only',
            'EXPLICIT_TRUSTED_PLATFORM_SCOPE')
    return dict(os.environ)

def verify_source(native):
    inputs, source_pin = pinned_json(TOOLS / 'desktop_label_browser_inputs.json',
                                    native['source_inputs_sha256'])
    rows = inputs['sources']
    require(type(rows) is list and len(rows) == 107, 'EXACT_107_BROWSER_INPUTS')
    names, observed = set(), []
    for row in rows:
        name = row['path']
        require(type(name) is str and len(name) <= 256 and not Path(name).is_absolute()
                and '\\' not in name and ':' not in name and all(part not in ('', '.', '..') for part in name.split('/'))
                and name.casefold() not in names, 'SOURCE_UNIQUE_CANONICAL_PATH')
        names.add(name.casefold())
        actual = pin_file(ROOT / name, 4 * 1024 * 1024)
        require(type(row['bytes']) is int and actual['bytes'] == row['bytes']
                and actual['sha256'] == row['sha256'], 'ACTUAL_107_SOURCE_BYTES')
        observed.append(actual)
    runtime_files = native['runtime_source_files']
    require(type(runtime_files) is list and 3 <= len(runtime_files) <= 16, 'EXPLICIT_EXTRA_RUNTIME_SOURCE_SCOPE')
    extra_names = set()
    for row in runtime_files:
        require(type(row['name']) is str and re.fullmatch(r'[a-z][a-z0-9_\.-]{1,100}', row['name'])
                and row['name'] not in extra_names, 'EXTRA_RUNTIME_FILE_NAME')
        extra_names.add(row['name'])
        actual = pin_file(TOOLS / row['name'], 2 * 1024 * 1024)
        require(type(row['bytes']) is int and actual['bytes'] == row['bytes']
                and actual['sha256'] == row['sha256'], 'EXTRA_RUNTIME_SOURCE_PIN')
        observed.append(actual)
    require({'desktop_label_browser_platform_runtime.py', 'desktop_label_browser_platform_sandbox.py',
             'desktop_label_browser_installed_files.json', 'desktop_label_browser_admission.json'} <= extra_names, 'MANDATORY_RUNTIME_SOURCE_FILES')
    return source_pin, observed


def verify_python_payload(native):
    installed, installed_pin = pinned_json(TOOLS / 'desktop_label_browser_installed_files.json',
                                         native['installed_files_sha256'])
    rows = installed['files']
    require(installed['install_root'] == 'packages' and len(rows) == 1202 and len(installed['distributions']) == 18,
            'EXACT_18_DISTRIBUTIONS_1202_REGULAR_FILES')
    expected, casefold = {}, set()
    for row in rows:
        name = row['path']
        require(type(name) is str and len(name) <= 512 and not Path(name).is_absolute()
                and '\\' not in name and ':' not in name and all(part not in ('', '.', '..') for part in name.split('/'))
                and name.casefold() not in casefold, 'OWNED_PACKAGE_PATH')
        require(not name.endswith(('.pth', '.pyc', '.pyo', '.egg-link')) and
                Path(name).name not in ('sitecustomize.py', 'usercustomize.py') and '__pycache__' not in Path(name).parts,
                'NO_AUTOLOAD_OR_UNREVIEWED_BYTECODE')
        casefold.add(name.casefold())
        expected[name] = row
    actual_names, actual_directories, pins = set(), set(), []
    expected_directories = {parent.as_posix() for name in expected for parent in Path(name).parents
                            if parent != Path('.')}
    entry_count = 0
    for path in PACKAGES.rglob('*'):
        entry_count += 1
        require(entry_count <= 4096, 'OWNED_PACKAGE_TREE_ENTRY_BOUND')
        info = path.lstat()
        require(not path.is_symlink() and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)),
                'NO_INSTALLED_PACKAGE_LINK_OR_SPECIAL_FILE')
        if stat.S_ISDIR(info.st_mode):
            require(info.st_mode & 0o222 == 0, 'READONLY_OWNED_PACKAGE_DIRECTORY')
            directory = path.relative_to(PACKAGES).as_posix()
            require(directory in expected_directories, 'NO_UNDECLARED_PACKAGE_DIRECTORY')
            actual_directories.add(directory)
            continue
        name = path.relative_to(PACKAGES).as_posix()
        require(name in expected and name not in actual_names, 'EXACT_OWNED_PACKAGE_FILE_SET')
        actual_names.add(name)
        pin = pin_file(path, 192 * 1024 * 1024)
        require(type(expected[name]['bytes']) is int and pin['bytes'] == expected[name]['bytes']
                and pin['sha256'] == expected[name]['sha256'], 'OWNED_PACKAGE_FILE_BYTE_PIN')
        pins.append(pin)
    require(actual_names == set(expected) and actual_directories == expected_directories,
            'COMPLETE_OWNED_PACKAGE_FILE_AND_DIRECTORY_CLOSURE')
    normalize = lambda value: re.sub(r'[-_.]+', '-', value).lower()
    packages = {normalize(row['name']): row['version'] for row in installed['distributions']}
    distributions = list(importlib.metadata.distributions(path=[str(PACKAGES)]))
    observed = {normalize(row.metadata['Name']): row.version for row in distributions}
    require(len(distributions) == len(observed) == len(packages) == 18 and observed == packages
            and packages.get('playwright') == '1.65.2' and packages.get('greenlet') == '3.5.6'
            and 'pyee' not in packages, 'ACTUAL_MANDATORY_18_METADATA')
    return installed_pin, pins


def proc_bytes(pid, name, maximum):
    require(type(pid) is int and pid > 0 and name in ('stat', 'status', 'cmdline', 'environ'),
            'FIXED_OWNED_PROC_READ')
    with (Path('/proc') / str(pid) / name).open('rb') as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, 'OWNED_PROC_DATA_BOUND')
    return raw


def process_identity(pid):
    raw = proc_bytes(pid, 'stat', 8192).decode('ascii', errors='strict')
    tail = raw.rsplit(') ', 1)[1].split()
    fields = {}
    for line in proc_bytes(pid, 'status', 16384).decode('ascii', errors='strict').splitlines():
        key, _, value = line.partition(':')
        fields[key] = value.strip()
    return {'pid': pid, 'start_ticks': tail[19], 'ppid': int(tail[1]),
        'effective_uid': int(fields['Uid'].split()[1]), 'effective_gid': int(fields['Gid'].split()[1]),
        'no_new_privileges': int(fields['NoNewPrivs']), 'seccomp': int(fields['Seccomp']),
        'cap_eff': fields['CapEff'],
        'capabilities': {name: fields[name] for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')}}


class DriverGuard:
    def __init__(self, environment):
        self.environment = dict(environment, PW_LANG_NAME='python', PW_LANG_NAME_VERSION='3.12',
                                PW_CLI_DISPLAY_VERSION='1.65.2')
        self.argv = [str(NODE), str(CLI), 'run-driver']
        self.spawns = []
        self.process = None
        self.first_identity = None
        self.observations = []

    def audit(self, event, args):
        if event == 'subprocess.Popen':
            require(len(args) == 4, 'ACTUAL_DRIVER_AUDIT_SCHEMA')
            executable, argv, cwd, environment = args
            require(not self.spawns and executable == str(NODE) and list(argv) == self.argv
                    and cwd is None and environment == self.environment,
                    'UNEXPECTED_PYTHON_CHILD_OR_DRIVER_ARGV_ENV')
            self.spawns.append({'executable': executable, 'argv': list(argv), 'cwd': None,
                                'environment_keys': sorted(environment), 'actual_audit_event': True})
        elif event in ('os.exec', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'pty.spawn'):
            raise RuntimeFailure('UNAPPROVED_ADDITIONAL_PYTHON_PROCESS')

    def bind(self, manager):
        client = manager._client
        transport = client._transport
        require(type(transport).__name__ == 'PipeTransport'
                and type(transport).__module__ == 'playwright._impl.transport', 'ACTUAL_PINNED_PIPE_TRANSPORT')
        self.process = transport._proc
        require(self.process.returncode is None and len(self.spawns) == 1, 'EXACT_ONE_RUNNING_DRIVER')
        return self.observe('after_sdk_start')

    def observe(self, stage):
        deadline()
        require(self.process is not None and self.process.returncode is None, 'OWNED_DRIVER_STILL_RUNNING')
        pid = self.process.pid
        before = process_identity(pid)
        require(before['ppid'] == os.getpid() and before['effective_uid'] == os.geteuid() > 0 and before['effective_gid'] == os.getegid() > 0
                and before['no_new_privileges'] == 1 and before['seccomp'] in (0, 2)
                and all(int(before['capabilities'][name], 16) == 0
                        for name in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')), 'ACTUAL_DRIVER_OWNED_PROCESS_BOUNDARY')
        require(self.first_identity is None or before == self.first_identity, 'DRIVER_PROCESS_IDENTITY_CHANGED')
        self.first_identity = before
        directory = Path('/proc') / str(pid)
        executable = (directory / 'exe').resolve(strict=True)
        require(executable == NODE, 'ACTUAL_DRIVER_EXE_PATH')
        cwd = (directory / 'cwd').resolve(strict=True)
        require(cwd == ROOT, 'ACTUAL_DRIVER_OWNED_WORKING_DIRECTORY')
        pin = pin_file(executable, 192 * 1024 * 1024)
        require(all(pin[key] == NODE_PIN[key] for key in NODE_PIN), 'ACTUAL_DRIVER_EXE_HASH')
        raw_argv = proc_bytes(pid, 'cmdline', 8192)
        require(raw_argv.endswith(b'\0') and raw_argv[:-1].split(b'\0') ==
                [item.encode('utf-8') for item in self.argv], 'ACTUAL_DRIVER_PROC_ARGV')
        raw_environment = proc_bytes(pid, 'environ', 32768)
        require(raw_environment.endswith(b'\0'), 'ACTUAL_DRIVER_ENV_TERMINATION')
        environment = {}
        for item in raw_environment[:-1].split(b'\0'):
            key, separator, value = item.partition(b'=')
            key, value = key.decode('utf-8', errors='strict'), value.decode('utf-8', errors='strict')
            require(separator and key not in environment, 'ACTUAL_DRIVER_ENV_DUPLICATE')
            environment[key] = value
        require(environment == self.environment, 'ACTUAL_DRIVER_PROC_ENVIRONMENT')
        pipes = []
        for index in (0, 1):
            pipe = self.process._transport.get_pipe_transport(index).get_extra_info('pipe')
            target = os.readlink(directory / 'fd' / str(index))
            parent_target = os.readlink(Path('/proc/self/fd') / str(pipe.fileno()))
            require(re.fullmatch(r'pipe:\[[0-9]+\]', target) and target == parent_target,
                    'ACTUAL_DRIVER_STDIO_PIPE_PAIR')
            pipes.append({'driver_fd': index, 'target': target, 'parent_fd': pipe.fileno()})
        stderr = os.readlink(directory / 'fd/2')
        require(stderr == os.readlink(Path('/proc/self/fd') / str(sys.stderr.fileno())),
                'ACTUAL_DRIVER_STDERR_OWNED_PARENT')
        descriptors = list((directory / 'fd').iterdir())
        require(len(descriptors) <= 128, 'DRIVER_DESCRIPTOR_BOUND')
        socket_descriptors = [path.name for path in descriptors if os.readlink(path).startswith('socket:')]
        require(not socket_descriptors, 'NODE_SOCKET_PRESENT_AT_OBSERVATION')
        require(process_identity(pid) == before, 'DRIVER_PROC_READ_BOOKEND')
        row = {'stage': stage, 'process': before, 'executable': pin, 'argv': self.argv, 'cwd': str(cwd),
               'actual_stdio_pipe_pairs': pipes, 'stderr_matches_parent': True,
               'environment_keys': sorted(environment),
               'environment_sha256': hashlib.sha256(json.dumps(environment, sort_keys=True,
                   separators=(',', ':')).encode()).hexdigest(), 'socket_descriptors_at_observation': [],
               'limit': 'Bounded observations, not continuous syscall/network tracing or general Node/OpenSSL clearance.'}
        self.observations.append(row)
        return row

    def stopped(self):
        require(self.process is not None and self.process.returncode == 0, 'DRIVER_EXIT_NOT_ZERO_OR_NOT_REAPED')
        try:
            identity = process_identity(self.first_identity['pid'])
        except (FileNotFoundError, ProcessLookupError):
            return True
        require(identity['start_ticks'] != self.first_identity['start_ticks'], 'OWNED_DRIVER_PROCESS_REMAINS')
        return True


def platform_file(path, maximum=384 * 1024 * 1024):
    """Observe a trusted platform file, including its actual mutable permissions."""
    requested = Path(path)
    require(requested.is_absolute() and '..' not in requested.parts, 'PLATFORM_FILE_ABSOLUTE_PATH')
    resolved = requested.resolve(strict=True)
    allowed = (Path('/opt/hostedtoolcache/Python'), Path('/opt/google/chrome'),
               Path('/usr'), Path('/etc'), Path('/var/lib/dpkg'), Path('/var/cache/fontconfig'), PACKAGES)
    require(any(resolved.is_relative_to(root) for root in allowed), 'PLATFORM_FOUNDATION_FILE_ROOT')
    actual = pin_file(resolved, maximum, readonly=False)
    info = resolved.lstat()
    require(info.st_uid in (0, os.geteuid()), 'PLATFORM_FOUNDATION_EXPECTED_FILE_OWNER')
    return {'requested_path': str(requested), 'file': actual, 'uid': info.st_uid, 'gid': info.st_gid,
            'mode': stat.S_IMODE(info.st_mode), 'original_path_is_symlink': requested.is_symlink(),
            'trusted_platform_fact_not_immutable_distribution_pin': True}


def foundation_files():
    candidates = [Path('/etc/os-release'), Path('/var/lib/dpkg/status'),
                  Path('/etc/apparmor.d/chrome'), Path('/usr/bin/setpriv'),
                  Path(sys.executable), BROWSER]
    roots = (Path('/usr/share/fonts'), Path('/usr/local/share/fonts'), Path('/etc/fonts'),
             Path('/usr/share/fontconfig'), Path('/var/cache/fontconfig'))
    require(roots[0].is_dir() and roots[2].is_dir(), 'PLATFORM_INSTALLED_FONTS_AND_CONFIG_REQUIRED')
    scanned = 0
    for root in roots:
        if not root.exists():
            continue
        require(root.is_dir() and not root.is_symlink(), 'PLATFORM_FIXED_FOUNDATION_DIRECTORY')
        for path in root.rglob('*'):
            scanned += 1
            require(scanned <= 8192, 'PLATFORM_FOUNDATION_NAMESPACE_BOUND')
            if path.is_symlink():
                require(path.resolve(strict=True).is_file(), 'PLATFORM_ONLY_FOUNDATION_FILE_LINKS')
                candidates.append(path)
            elif path.is_file():
                candidates.append(path)
            else:
                require(path.is_dir(), 'PLATFORM_NO_FOUNDATION_SPECIAL_FILE')
    require(len(candidates) <= 4096 and len({str(path) for path in candidates}) == len(candidates),
            'PLATFORM_FOUNDATION_FILE_COUNT')
    result, total = [], 0
    for path in sorted(candidates, key=str):
        row = platform_file(path)
        total += row['file']['bytes']
        require(total <= 2 * 1024 * 1024 * 1024, 'PLATFORM_FOUNDATION_TOTAL_BYTE_BOUND')
        result.append(row)
    return result


def mapped_native_providers(pids):
    """Hash executable file-backed mappings; no ELF load/decode or ABI verdict."""
    require(type(pids) is list and 1 <= len(pids) <= 128 and len(set(pids)) == len(pids),
            'PLATFORM_MAP_PROCESS_SELECTION_BOUND')
    paths, observations = set(), []
    for pid in pids:
        before = process_identity(pid)
        with (Path('/proc') / str(pid) / 'maps').open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, 'PLATFORM_PROC_MAPS_BOUND')
        rows = raw.decode('utf-8', errors='strict').splitlines()
        require(len(rows) <= 8192, 'PLATFORM_PROC_MAP_ROWS_BOUND')
        selected = set()
        for line in rows:
            fields = line.split(None, 5)
            require(len(fields) >= 5, 'PLATFORM_PROC_MAPS_ROW_SCHEMA')
            if 'x' not in fields[1] or len(fields) != 6 or fields[5].startswith('['):
                continue
            require(fields[5].startswith('/') and not fields[5].endswith(' (deleted)')
                    and '\\' not in fields[5], 'PLATFORM_EXECUTABLE_PROVIDER_PATH')
            selected.add(fields[5])
        require(process_identity(pid) == before, 'PLATFORM_MAP_PROCESS_BOOKEND')
        paths.update(selected)
        observations.append({'process': before, 'mapped_executable_paths': sorted(selected),
            'maps_sha256': hashlib.sha256(raw).hexdigest(),
            'limit': 'A bounded executable-mapping snapshot, not complete loader history or continuous observation.'})
    require(len(paths) <= 512, 'PLATFORM_MAPPED_PROVIDER_COUNT')
    providers, total = [], 0
    for path in sorted(paths):
        row = platform_file(Path(path))
        total += row['file']['bytes']
        require(total <= 4 * 1024 * 1024 * 1024, 'PLATFORM_MAPPED_PROVIDER_BYTE_BOUND')
        providers.append(row)
    return {'observations': observations, 'files': providers,
            'abi_or_advisory_clearance_claim': False}


def recheck_platform_files(rows):
    for expected in rows:
        actual = platform_file(Path(expected['requested_path']))
        require(actual == expected, 'FINAL_OBSERVED_PLATFORM_FILE_CHANGED')


def exact_source_namespace(platform):
    expected = {row['path'] for row in pinned_json(TOOLS / 'desktop_label_browser_inputs.json')[0]['sources']}
    expected.update('tools/' + row['name'] for row in platform['runtime_source_files'])
    expected.update(('tools/desktop_label_browser_inputs.json', 'tools/desktop_label_browser_platform.json'))
    observed, count = set(), 0
    for path in ROOT.rglob('*'):
        count += 1
        require(count <= 2048, 'PLATFORM_SOURCE_NAMESPACE_ENTRY_BOUND')
        info = path.lstat()
        require(not path.is_symlink() and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)),
                'PLATFORM_SOURCE_NO_LINK_OR_SPECIAL_FILE')
        if stat.S_ISREG(info.st_mode):
            require(info.st_uid == os.geteuid() and info.st_mode & 0o222 == 0,
                    'PLATFORM_OWNED_READONLY_SOURCE_FILE')
            observed.add(path.relative_to(ROOT).as_posix())
    require(observed == expected, 'PLATFORM_COMPLETE_SOURCE_NAMESPACE_NO_CACHE_OR_EXTRA_CODE')


def main():
    require(len(sys.argv) == 1 and sys.platform == 'linux' and sys.version.split()[0] == '3.12.16'
            and Path.cwd() == ROOT and ROOT.name == 'source' and os.geteuid() > 0 and os.getegid() > 0
            and sys.flags.isolated and sys.flags.no_site and sys.flags.ignore_environment
            and sys.dont_write_bytecode, 'EXACT_TRUSTED_PLATFORM_PYTHON_ENTRY')
    platform, platform_pin = pinned_json(TOOLS / 'desktop_label_browser_platform.json',
                                        os.environ.get('SS_PLATFORM_ADMISSION_SHA256'))
    require(platform['format'] == 'stock-and-sleeve:trusted-platform-desktop-ui:v1'
            and platform['execution_hold'] is None and platform['trusted_platform_execution_admitted'] is True
            and re.fullmatch(r'[a-f0-9]{64}', platform['independent_source_go_sha256'])
            and re.fullmatch(r'[a-f0-9]{64}', platform['conditional_node_api_scope_sha256'])
            and platform['native_image_flags_inherited'] is False,
            'SEPARATE_PLATFORM_SOURCE_AND_NODE_API_EXECUTION_HOLD')
    runtime_pin = pin_file(Path(__file__).resolve(), 1048576)
    require(runtime_pin['sha256'] == os.environ.get('SS_PLATFORM_RUNTIME_SHA256'),
            'EXTERNAL_ACTUAL_PLATFORM_RUNTIME_SOURCE_PIN')
    environment = closed_environment(platform)
    runner_temp = Path(environment['SS_PLATFORM_RUNNER_TEMP'])
    require(runner_temp.is_absolute() and runner_temp.resolve(strict=True) == runner_temp
            and RUNROOT.parent == runner_temp
            and RUNROOT.name == 'stock-sleeve-platform-' + environment['SS_DESKTOP_OWNER_NONCE'],
            'FRESH_NONCE_OWNED_RUNNER_TEMP_ROOT')
    for path in (RUNROOT, ROOT, PACKAGES, EVIDENCE, RUNROOT / 'runtime', RUNROOT / 'tmp'):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and not path.is_symlink() and info.st_uid == os.geteuid()
                and info.st_mode & 0o077 == 0, 'PLATFORM_PRIVATE_OWNED_DIRECTORY')
    require(not list(EVIDENCE.iterdir()) and not list((RUNROOT / 'runtime').iterdir())
            and not list((RUNROOT / 'tmp').iterdir()), 'FRESH_EMPTY_PLATFORM_RUNTIME_AND_EVIDENCE')
    self_identity = process_identity(os.getpid())
    require(self_identity['no_new_privileges'] == 1 and self_identity['seccomp'] in (0, 2)
            and all(int(self_identity['capabilities'][name], 16) == 0
                    for name in ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')),
            'ACTUAL_PLATFORM_NONROOT_NNP_NO_EFFECTIVE_CAPABILITIES')
    require(Path('/opt/hostedtoolcache/Python/3.12.16/x64') == Path(sys.base_prefix)
            and Path(sys.executable).resolve(strict=True).is_relative_to(Path(sys.base_prefix)),
            'ACTUAL_PREINSTALLED_TOOLCACHE_PYTHON_31216')
    source_pin, sources = verify_source(platform)
    exact_source_namespace(platform)
    suite, suite_pin = pinned_json(TOOLS / 'desktop_label_browser_admission.json')
    require(suite['execution_hold'] is None and suite['required_linux_launcher'] is True
            and suite['version'] == 1 and suite['source_count'] == 107
            and suite['source_inputs_sha256'] == source_pin['sha256']
            and suite['expected_ids'] == ['DUI01', 'DUI02', 'DUI03', 'DUI04', 'DUI05', 'DUI06', 'DUI07'],
            'SEPARATE_PLATFORM_SEVEN_FLOW_SOURCE_EXECUTION_HOLD')
    installed_pin, installed = verify_python_payload(platform)
    for path, expected in ((NODE, NODE_PIN), (CLI, CLI_PIN)):
        actual = pin_file(path, 192 * 1024 * 1024)
        require(all(actual[key] == expected[key] for key in expected), 'EXACT_OWNED_VENDOR_NODE_AND_CLI')
    browser = pin_file(BROWSER, 384 * 1024 * 1024, readonly=False)
    require(platform['browser_path'] == str(BROWSER), 'EXACT_INSTALLED_OFFICIAL_CHROME_LOCATION')
    home = Path(FIXED_ENV['HOME'])
    require(not home.exists() and not home.is_symlink(), 'FRESH_PRIVATE_PLATFORM_HOME')
    home.mkdir(mode=0o700)
    foundation = foundation_files()
    with Path('/sys/module/apparmor/parameters/enabled').open('rb') as stream:
        apparmor_enabled = stream.read(17)
    require(apparmor_enabled.strip() == b'Y', 'PLATFORM_DEFAULT_APPARMOR_ENABLED')
    sysctls = {}
    for path in ('/proc/sys/kernel/apparmor_restrict_unprivileged_userns',
                 '/proc/sys/user/max_user_namespaces'):
        with Path(path).open('rb') as stream:
            raw = stream.read(65)
        require(len(raw) <= 64 and raw.strip().isdigit(), 'PLATFORM_EXISTING_USERNS_POLICY_VALUE')
        sysctls[path] = int(raw.strip())
    require(sysctls['/proc/sys/kernel/apparmor_restrict_unprivileged_userns'] == 1
            and sysctls['/proc/sys/user/max_user_namespaces'] > 0,
            'PLATFORM_RESTRICTED_USERNS_POLICY_REMAINS_ENABLED')
    save('platform-foundation.json', {'successful': True, 'source_sha': environment['SS_CI_SOURCE_SHA'],
        'scope': platform['foundation_scope'], 'runner_image_version': environment['SS_PLATFORM_IMAGE_VERSION'],
        'job_id': environment['SS_PLATFORM_JOB_ID'], 'python_version': sys.version,
        'python_executable': platform_file(Path(sys.executable)),
        'python_flags': {key: bool(getattr(sys.flags, key)) for key in
                         ('isolated', 'no_site', 'ignore_environment', 'safe_path', 'dont_write_bytecode')},
        'actual_process': self_identity, 'apparmor_enabled': True, 'observed_userns_sysctls': sysctls,
        'files': foundation, 'native_image_acceptance_claim': False,
        'limit': 'Explicitly trusted fresh GitHub VM foundation. Actual hashes/modes are observations; these are not an immutable distributable OS/font graph, no-network isolation, or a complete advisory verdict.'})
    # Fresh source/package trees have no .pyc/.pth/customizer. The cached stdlib
    # interpreter and its ordinary platform bootstrap are separately trusted.
    sys.path[:0] = [str(PACKAGES), str(ROOT)]
    guard = DriverGuard(environment)
    sys.addaudithook(guard.audit)
    from playwright.sync_api import sync_playwright
    from tools.desktop_label_browser_ci import run_browser_suite
    manager, playwright, chromium = sync_playwright(), None, None
    successful, driver_cleanup, cleanup_errors = False, False, []
    providers = None
    try:
        playwright = manager.start()
        initial = guard.bind(manager)
        chromium = playwright.chromium.launch(executable_path=str(BROWSER), headless=True,
            chromium_sandbox=True, timeout=30000, env=dict(FIXED_ENV))
        require(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+', chromium.version),
                'OBSERVED_OFFICIAL_CHROME_VERSION')
        observed_version = chromium.version
        require(re.fullmatch(platform['browser_version_pattern'], observed_version),
                'ADMITTED_PREINSTALLED_BROWSER_VERSION_RANGE')
        session = chromium.new_browser_cdp_session()
        try:
            info = session.send('SystemInfo.getProcessInfo')['processInfo']
        finally:
            session.detach()
        require(type(info) is list and len(info) <= 128, 'PLATFORM_PROVIDER_PROCESS_INFO_BOUND')
        main = [int(row['id']) for row in info if row['type'] == 'browser']
        require(len(main) == 1, 'PLATFORM_SINGLE_PROVIDER_BROWSER')
        providers = mapped_native_providers([os.getpid(), guard.process.pid, main[0]])
        save('platform-native-mappings.json', providers)
        run_browser_suite(chromium, {'browser': browser, 'driver': initial,
                                    'platform_admission_sha256': platform_pin['sha256']})
        guard.observe('after_all7_cases_and_browser_close')
        successful = True
    finally:
        try:
            if chromium is not None and chromium.is_connected():
                chromium.close()
        except Exception as error:
            cleanup_errors.append({'code': 'PLATFORM_BROWSER_CLOSE_FAILED', 'error_type': type(error).__name__})
        try:
            manager.__exit__()
        except Exception as error:
            cleanup_errors.append({'code': 'PLATFORM_DRIVER_MANAGER_CLOSE_FAILED', 'error_type': type(error).__name__})
        try:
            if guard.process is not None and guard.first_identity is not None:
                driver_cleanup = guard.stopped()
        except Exception as error:
            cleanup_errors.append({'code': 'PLATFORM_DRIVER_NOT_REAPED', 'error_type': type(error).__name__})
        save('desktop-runtime-driver.json', {'successful': successful and driver_cleanup and not cleanup_errors,
            'source_sha': environment['SS_CI_SOURCE_SHA'], 'platform_admission': platform_pin,
            'spawn_audit': guard.spawns, 'observations': guard.observations,
            'driver_process_reaped_and_gone': driver_cleanup, 'cleanup_errors': cleanup_errors,
            'separate_outer_owned_process_group_cleanup_required': True})
    for expected in sources + installed + [runtime_pin, source_pin, installed_pin, platform_pin, suite_pin]:
        require(pin_file(expected['path'], 384 * 1024 * 1024) == expected, 'FINAL_OWNED_SOURCE_AND_PACKAGE_BYTES_CHANGED')
    require(pin_file(BROWSER, 384 * 1024 * 1024, readonly=False) == browser, 'FINAL_PLATFORM_CHROME_BYTES_CHANGED')
    recheck_platform_files(foundation)
    require(providers is not None, 'PLATFORM_NATIVE_PROVIDER_OBSERVATIONS_REQUIRED')
    recheck_platform_files(providers['files'])
    exact_source_namespace(platform)
    require(dict(os.environ) == environment and successful and driver_cleanup and not cleanup_errors,
            'FINAL_PLATFORM_ENV_SUITE_OR_CLEANUP_FAILED')
    save('desktop-runtime-summary.json', {'successful': True, 'source_sha': environment['SS_CI_SOURCE_SHA'],
        'flows': 7, 'captures': 14, 'verified_downloads': 8, 'actual_download_events': 8,
        'playwright': '1.65.2', 'greenlet': '3.5.6', 'mandatory_distributions': 18,
        'regular_python_files': 1202, 'node_version_source_pin': '26.13.1',
        'browser': browser, 'browser_version': observed_version,
        'node_pipe_observations_only': True, 'network_egress_isolation_claim': False,
        'outer_owned_process_group_cleanup_required': True,
        'no_live_square_windows_hardware_native_image_or_consumer_ready_claim': True})


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        value = str(error) if isinstance(error, RuntimeFailure) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(error)) else 'PLATFORM_RUNTIME_FAILED'
        print(json.dumps({'successful': False, 'error_type': type(error).__name__, 'error_code': value},
                         separators=(',', ':')), flush=True)
        raise SystemExit(1)
