"""UNRUN fixed Debian DATA profile. No application or vendor ELF execution.

Only an independently reviewed Root admission may start this source. The
immutable Python foundation and declared native stdlib codecs are distinct
from the browser/OS payloads, which remain inert private .data files.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import sysconfig
import time


PROFILE = 'debian-archives'
CODE = Path('/ci-code')
INPUT = Path('/ci-input')
EVIDENCE = Path('/ci-evidence')
RAW = EVIDENCE / 'raw'
REPORTS = EVIDENCE / 'reports'
BASE = 'python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'
CONFIG = 'sha256:78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db'
CHUNK = 65536
STARTED = time.monotonic()
CODE_NAMES = frozenset(('debian_data.py', 'strict_debian_tar_data.py', 'ci_elf_data.py',
                        'admission.json', 'input-spec.json', 'source-manifest.json'))
CODECS = ('_lzma', '_bz2', 'zlib', 'binascii', 'grp')
PROVIDERS = frozenset(('liblzma.so.5', 'libbz2.so.1.0', 'libz.so.1', 'libc.so.6',
                      'ld-linux-x86-64.so.2', 'libpthread.so.0', 'libdl.so.2', 'libm.so.6'))
FIXED_INPUTS = {
    'chromium': ('deb', 91684936, '9325777fb842eafa3f992209ee6f1b69bad76c3311a88efd4d7772cec22e3795'),
    'chromium-common': ('deb', 31731400, '29d9a33123bcc5aba47746eb89a72d973bf7a737e3820425c7496904f7235fbd'),
    'chromium-sandbox': ('deb', 161728, '5a265f12abaefe0c700ab6b4e5956b7e1b8f713720aaa253d7a3319631969898'),
    'security-index': ('xz-index', 309332, '1caca5883330e8e422b31d243606b2ed3ef9225cc7946599acc7559b7f6d9740'),
    'updates-index': ('xz-index', 1108, 'eea860c5dea11680e64299adb2736c2367c4235763a66f2b70ca61ce1b51978a'),
    'debian-source': ('xz-source-tar', 508920, '1374b79c15a37b72c38d643d153d865567a61855a98414e726accfbeddbd30bf'),
}
INDEX_PINS = {
    'security-index': (2249761, 'dae32ddcd3aa2b9f334e5383fbe473b69f6536e9a7c5791080bf840bf91faa54'),
    'updates-index': (2351, '7f1dd99bbd1a0416d39499892ec12ff38d80637029114d234c38972bba8ae041'),
}
REGISTRY = {}
DERIVED = {}
NATIVE = []
TOTAL_RAW = 0
IMAGE_NATIVE = {}
MODULES_PREDECODE = None
INTERPRETER_PIN = None
PWD_CLASSIFICATION = None


class ProfileFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ProfileFailure(code)


def deadline():
    require(time.monotonic() - STARTED <= 300, 'DATA_PROFILE_DEADLINE')


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_nlink,
            info.st_mtime_ns, info.st_ctime_ns)


def hash_file(path, maximum, readonly=False):
    path = Path(path)
    require(path.is_absolute() and all(not parent.is_symlink() for parent in (path, *path.parents)),
            'NO_SYMLINK_ANCESTOR')
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
            'REGULAR_SINGLE_LINK_BOUNDED_FILE')
    require(not readonly or info.st_mode & 0o222 == 0, 'SOURCE_INPUT_NOT_READONLY')
    hashed, count = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        require(stamp(os.fstat(fd)) == stamp(info), 'OPEN_FILE_IDENTITY')
        while chunk := os.read(fd, CHUNK):
            deadline()
            count += len(chunk)
            require(count <= maximum, 'FILE_READ_BOUND')
            hashed.update(chunk)
        require(stamp(os.fstat(fd)) == stamp(info) == stamp(path.lstat())
                and count == info.st_size, 'FILE_CHANGED_DURING_HASH')
    finally:
        os.close(fd)
    return {'path': str(path), 'bytes': count, 'sha256': hashed.hexdigest(),
            'device': info.st_dev, 'inode': info.st_ino}


def duplicate_guard(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, 'DUPLICATE_JSON_KEY')
        result[name] = value
    return result


def load_json(path):
    pin = hash_file(path, 1048576, readonly=True)
    value = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=duplicate_guard,
        parse_constant=lambda _: (_ for _ in ()).throw(ProfileFailure('NONFINITE_JSON')))
    require(type(value) is dict, 'JSON_OBJECT_REQUIRED')
    return value, pin


def load_owned_module(name, expected_sha):
    require(name in ('ci_elf_data', 'strict_debian_tar_data'), 'FIXED_SOURCE_MODULE')
    path = CODE / (name + '.py')
    require(hash_file(path, 1048576, readonly=True)['sha256'] == expected_sha,
            'REVIEWED_OWNED_MODULE_PIN')
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, 'OWNED_MODULE_SPEC')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def raw_output(key, source, expected_bytes, expected_sha=None):
    global TOTAL_RAW
    require(type(expected_bytes) is int and 0 <= expected_bytes <= 384 * 1024 * 1024,
            'RAW_MEMBER_BOUND')
    require(TOTAL_RAW + expected_bytes <= 1024 * 1024 * 1024, 'TOTAL_RAW_BOUND')
    name = hashlib.sha256(key.encode('utf-8', errors='strict')).hexdigest() + '.data'
    path = RAW / name
    require(name not in REGISTRY, 'RAW_OUTPUT_KEY_REUSED')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    hashed, count, prefix = hashlib.sha256(), 0, b''
    with os.fdopen(fd, 'wb') as output:
        while chunk := source.read(CHUNK):
            deadline()
            count += len(chunk)
            require(count <= expected_bytes, 'RAW_STREAM_LENGTH_BOUND')
            if not prefix:
                prefix = chunk[:16]
            output.write(chunk)
            hashed.update(chunk)
    require(count == expected_bytes and (expected_sha is None or hashed.hexdigest() == expected_sha),
            'RAW_STREAM_EXACT_PIN')
    pin = hash_file(path, 384 * 1024 * 1024)
    require(pin['sha256'] == hashed.hexdigest() and pin['bytes'] == count, 'RAW_READBACK_PIN')
    REGISTRY[name] = dict(pin, key=key, prefix_hex=prefix[:8].hex(), elf=prefix.startswith(b'\x7fELF'))
    TOTAL_RAW += count
    return REGISTRY[name]


def save_report(name, value):
    require(name in ('bootstrap-receipt.json', 'data-inventory.json') or
            re.fullmatch(r'elf-[a-f0-9]{64}\.json', name), 'DERIVED_REPORT_NAME')
    require(name not in DERIVED, 'DERIVED_REPORT_REUSED')
    path = REPORTS / name
    encoded = json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    count = 0
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        for part in encoded.iterencode(value):
            deadline()
            raw = part.encode('ascii')
            count += len(raw)
            require(count <= 33554432, 'DERIVED_REPORT_BOUND')
            stream.write(raw)
    DERIVED[name] = path
    return hash_file(path, 33554432)


def elf_report(elf_module, pin, logical):
    name = 'elf-' + pin['sha256'] + '.json'
    if name not in DERIVED:
        identity = {key: pin[key] for key in ('bytes', 'sha256', 'device', 'inode')}
        facts = elf_module.inspect_elf(pin['path'], identity)
        save_report(name, facts)
    NATIVE.append({'logical_path': logical, 'sha256': pin['sha256'], 'bytes': pin['bytes'],
                   'report': name, 'opaque_file': Path(pin['path']).name})
    return name


def module_inventory(baseline_native=None):
    """Actual trusted-base/owned module facts; no archive or foreign import.

    The immutable interpreter and already-loaded foundation are a separately
    explicit Root trust decision. Later extension origins must have been read
    as data before import, or be an unchanged native foundation module.
    """
    rows = []
    require(len(sys.modules) <= 1024, 'STDLIB_MODULE_COUNT_BOUND')
    for name, module in sorted(tuple(sys.modules.items())):
        require(type(name) is str and len(name) <= 256 and module is not None,
                'STDLIB_MODULE_NAME_OR_STATE')
        spec = getattr(module, '__spec__', None)
        origin = getattr(spec, 'origin', None)
        path_value = getattr(module, '__file__', None)
        kind = 'built-in' if origin == 'built-in' or name in sys.builtin_module_names else \
               'frozen' if origin == 'frozen' else 'file'
        row = {'module': name, 'kind': kind, 'spec_origin': origin}
        if path_value is None:
            require(kind in ('built-in', 'frozen'), 'UNDECLARED_MODULE_WITHOUT_FILE')
        else:
            require(type(path_value) is str, 'MODULE_FILE_PATH_TYPE')
            path = Path(path_value).resolve(strict=True)
            require('site-packages' not in path.parts and
                    (path.is_relative_to('/usr/local/lib/python3.12') or path.is_relative_to(CODE)),
                    'MODULE_ORIGIN_OUTSIDE_DECLARED_IMAGE_OR_OWNED_SOURCE')
            pin = hash_file(path, 16777216 if path.suffix == '.so' else 2097152)
            row['file'] = pin
            if path.suffix == '.so':
                require(path.parent == Path('/usr/local/lib/python3.12/lib-dynload'),
                        'EXACT_NATIVE_STDLIB_DIRECTORY')
                if baseline_native is not None:
                    require(IMAGE_NATIVE.get(str(path)) == pin or baseline_native.get(name) == pin,
                            'NEW_NATIVE_MODULE_NOT_READ_BEFORE_IMPORT')
        cached = getattr(module, '__cached__', None)
        if cached is not None:
            require(type(cached) is str, 'MODULE_CACHED_PATH_TYPE')
            cached_path = Path(cached)
            require(cached_path.is_absolute() and cached_path.suffix == '.pyc' and
                    (cached_path.is_relative_to('/usr/local/lib/python3.12') or
                     cached_path.is_relative_to(CODE)), 'DECLARED_BASE_OR_OWNED_CACHED_MODULE_PATH')
            require(not cached_path.is_relative_to(CODE) or not cached_path.exists(),
                    'OWNED_BYTECODE_ABSENT_WITH_B_FLAG')
            row['cached_path'] = str(cached_path)
            row['cached_file'] = hash_file(cached_path, 2097152) if cached_path.exists() else None
        rows.append(row)
    return rows


def bootstrap():
    global MODULES_PREDECODE, INTERPRETER_PIN, PWD_CLASSIFICATION
    require(len(sys.argv) == 1 and sys.platform == 'linux' and sys.version.split()[0] == '3.12.16'
            and os.uname().machine == 'x86_64' and os.geteuid() == os.getegid() == 10001
            and sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            'EXACT_OWNED_PYTHON_FOUNDATION')
    require(all(not os.environ.get(name) for name in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH',
            'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT')), 'NO_LOADER_OR_PYTHON_OVERRIDE')
    require(all(name not in sys.modules for name in (*CODECS, 'lzma', 'bz2', 'gzip', 'tarfile', 'base64', 'pwd')),
            'NO_CODECS_BEFORE_BOOTSTRAP_SCOPE')
    require(os.environ.get('SS_DATA_PROFILE_ID') == PROFILE, 'EXACT_PROFILE_ID')
    for key, pattern in (('SS_DATA_SOURCE_SHA', r'[a-f0-9]{40}'),
            ('SS_DATA_PROFILE_SHA256', r'[a-f0-9]{64}'),
            ('SS_DATA_SOURCE_MANIFEST_SHA256', r'[a-f0-9]{64}'),
            ('SS_DATA_INPUT_SPEC_SHA256', r'[a-f0-9]{64}')):
        require(re.fullmatch(pattern, os.environ.get(key, '')), 'EXACT_HOST_RECEIPT_PIN')
    admission, admission_pin = load_json(CODE / 'admission.json')
    require(admission['profile_id'] == PROFILE and admission['execution_hold'] is None
            and admission['base_image'] == BASE and admission['base_image_config'] == CONFIG,
            'ROOT_PROFILE_EXECUTION_HOLD')
    scope = admission['bootstrap_scope']
    require(scope['admitted'] is True and scope['policy'] == 'EXACT_IMMUTABLE_PYTHON_IMAGE_DECLARED_STDLIB'
            and re.fullmatch(r'[a-f0-9]{64}', scope['source_go_sha256']), 'ROOT_NATIVE_BOOTSTRAP_SCOPE_HOLD')
    manifest, manifest_pin = load_json(CODE / 'source-manifest.json')
    require(manifest['profile_id'] == PROFILE and manifest_pin['sha256'] ==
            os.environ['SS_DATA_SOURCE_MANIFEST_SHA256'] and type(manifest['files']) is list
            and len(manifest['files']) == 5, 'SOURCE_MANIFEST_SCHEMA_PIN')
    sources, named_sources = [], set()
    for row in manifest['files']:
        require(set(row) == {'name', 'bytes', 'sha256'} and row['name'] in CODE_NAMES - {'source-manifest.json'}
                and row['name'] not in named_sources and type(row['bytes']) is int
                and re.fullmatch(r'[a-f0-9]{64}', row['sha256']), 'SOURCE_ROW_SCHEMA')
        named_sources.add(row['name'])
        actual = hash_file(CODE / row['name'], 1048576, readonly=True)
        require(actual['bytes'] == row['bytes'] and actual['sha256'] == row['sha256'], 'ACTUAL_COPIED_SOURCE_PIN')
        sources.append(actual)
    require(named_sources | {'source-manifest.json'} == CODE_NAMES and
            {path.name for path in CODE.iterdir()} == CODE_NAMES, 'EXACT_SIX_COPIED_CODE_FILES')
    inputs, inputs_pin = load_json(CODE / 'input-spec.json')
    require(inputs['profile_id'] == PROFILE and inputs_pin['sha256'] == os.environ['SS_DATA_INPUT_SPEC_SHA256']
            and len(inputs['inputs']) == 6 and inputs['total_bytes'] == 124397424, 'INPUT_SPEC_SCHEMA_PIN')
    raw_inputs, by_id = [], {}
    for row in inputs['inputs']:
        require(row['id'] in FIXED_INPUTS and row['id'] not in by_id, 'EXACT_INPUT_ID')
        kind, size, sha = FIXED_INPUTS[row['id']]
        require(row['kind'] == kind and type(row['bytes']) is int and row['bytes'] == size
                and row['sha256'] == sha and row['name'] == sha + '.blob-data', 'FIXED_RAW_INPUT_SPEC')
        actual = hash_file(INPUT / row['name'], 134217728, readonly=True)
        require(actual['bytes'] == size and actual['sha256'] == sha, 'ACTUAL_RAW_INPUT_PIN')
        raw_inputs.append(actual)
        by_id[row['id']] = dict(row, path=actual['path'])
    require(set(by_id) == set(FIXED_INPUTS) and {path.name for path in INPUT.iterdir()} ==
            {row['name'] for row in inputs['inputs']}, 'COMPLETE_FIXED_RAW_INPUT_SET')
    status = Path('/proc/self/status').read_text(encoding='ascii')
    require('\nNoNewPrivs:\t1\n' in status and '\nSeccomp:\t2\n' in status
            and re.search(r'(?m)^CapEff:\s+0+$', status)
            and sorted(path.name for path in Path('/sys/class/net').iterdir()) == ['lo'],
            'ACTUAL_PRIVATE_PROCESS_BOUNDARY')
    require(EVIDENCE.is_dir() and not EVIDENCE.is_symlink() and not RAW.exists() and not REPORTS.exists(),
            'FRESH_PRIVATE_EVIDENCE_LAYOUT')
    RAW.mkdir(mode=0o700)
    REPORTS.mkdir(mode=0o700)
    actual_source = {Path(row['path']).name: row['sha256'] for row in sources}
    require(actual_source['ci_elf_data.py'] == '5c226678de6e5f066a9294aa34d755745243d7339c308827fdb0383a33b86cca'
            and actual_source['strict_debian_tar_data.py'] ==
            'fc7e5249ad90d016f88c09baebd86daed0fb2e1b7e21dadf254971e479f4d19a', 'EXACT_REVIEWED_SHARED_MODULES')
    elf_module = load_owned_module('ci_elf_data', actual_source['ci_elf_data.py'])
    codec_dir = Path('/usr/local/lib/python3.12/lib-dynload')
    require(Path(sysconfig.get_config_var('DESTSHARED')).resolve() == codec_dir,
            'EXACT_STDLIB_CODEC_DIRECTORY')
    pwd_spec = importlib.util.find_spec('pwd')
    require(pwd_spec is not None, 'PWD_RUNTIME_SPEC_REQUIRED')
    if pwd_spec.origin == 'built-in':
        PWD_CLASSIFICATION = {'module': 'pwd', 'kind': 'actual-runtime-built-in',
                              'spec_origin': 'built-in', 'extension_preprobe_required': False}
    else:
        require(type(pwd_spec.origin) is str and Path(pwd_spec.origin).parent == codec_dir
                and Path(pwd_spec.origin).name == 'pwd.cpython-312-x86_64-linux-gnu.so',
                'PWD_RUNTIME_ORIGIN_UNSUPPORTED')
        PWD_CLASSIFICATION = {'module': 'pwd', 'kind': 'actual-runtime-extension',
                              'spec_origin': pwd_spec.origin, 'extension_preprobe_required': True}
    queue = [(name, codec_dir / (name + '.cpython-312-x86_64-linux-gnu.so')) for name in CODECS]
    if PWD_CLASSIFICATION['extension_preprobe_required']:
        queue.append(('pwd', Path(PWD_CLASSIFICATION['spec_origin'])))
    native, seen = [], set()
    while queue:
        logical, path = queue.pop(0)
        resolved = path.resolve(strict=True)
        require(any(resolved.is_relative_to(root) for root in
                    (Path('/usr/local/lib'), Path('/usr/lib'), Path('/lib'))), 'DECLARED_IMAGE_NATIVE_PATH')
        if str(resolved) in seen:
            continue
        require(len(seen) < 20, 'NATIVE_BOOTSTRAP_GRAPH_BOUND')
        seen.add(str(resolved))
        original = hash_file(resolved, 16777216)
        with resolved.open('rb') as stream:
            copied = raw_output('stdlib:' + str(resolved), stream, original['bytes'], original['sha256'])
        report_name = elf_report(elf_module, copied, str(resolved))
        report = json.loads((REPORTS / report_name).read_text(encoding='ascii'), object_pairs_hook=duplicate_guard)
        require(not report['rpath'] and not report['runpath']
                and all(name in PROVIDERS for name in report['needed']), 'DECLARED_CODEC_PROVIDER_GRAPH')
        native.append(dict(original, logical=logical, report=report_name))
        IMAGE_NATIVE[str(resolved)] = original
        for needed in report['needed']:
            candidates = {str((Path(directory) / needed).resolve(strict=True)) for directory in
                ('/usr/lib/x86_64-linux-gnu', '/lib/x86_64-linux-gnu', '/usr/local/lib')
                if (Path(directory) / needed).exists()}
            require(len(candidates) == 1, 'UNAMBIGUOUS_IMAGE_NATIVE_PROVIDER')
            queue.append((needed, Path(candidates.pop())))
    require(all(name not in sys.modules for name in (*CODECS, 'lzma', 'bz2', 'gzip', 'tarfile', 'base64', 'pwd')),
            'DECLARED_STDLIB_STILL_UNIMPORTED_BEFORE_PREPROBE_COMPLETE')
    interpreter_path = Path('/proc/self/exe').resolve(strict=True)
    require(interpreter_path == Path('/usr/local/bin/python3.12')
            and Path(sys.executable).resolve(strict=True) == interpreter_path,
            'EXACT_TRUSTED_INTERPRETER_PATH')
    INTERPRETER_PIN = hash_file(interpreter_path, 33554432)
    foundation = module_inventory()
    baseline_native = {row['module']: row['file'] for row in foundation
                       if 'file' in row and row['file']['path'].endswith('.so')}
    strict = load_owned_module('strict_debian_tar_data',
        'fc7e5249ad90d016f88c09baebd86daed0fb2e1b7e21dadf254971e479f4d19a')
    # Explicitly load the complete known decoder/export transitive stdlib set
    # AFTER its extension bytes/providers were read, but BEFORE archive/corpus
    # decoding. Native modules not preprobed or in the trusted foundation fail.
    import base64
    import gzip
    import lzma
    MODULES_PREDECODE = module_inventory(baseline_native)
    require(all(name in sys.modules for name in (*CODECS, 'pwd', 'tarfile', 'base64', 'gzip', 'lzma')),
            'COMPLETE_DECLARED_STDLIB_IMPORT_SET')
    pwd_origin = getattr(getattr(sys.modules['pwd'], '__spec__', None), 'origin', None)
    require(pwd_origin == PWD_CLASSIFICATION['spec_origin'], 'PWD_ACTUAL_IMPORT_CLASSIFICATION')
    receipt = {'profile_id': PROFILE, 'successful': True, 'status': 'BOOTSTRAP_FACTS_BEFORE_ARCHIVE_DECODE',
        'source_sha': os.environ['SS_DATA_SOURCE_SHA'], 'profile_sha256': os.environ['SS_DATA_PROFILE_SHA256'],
        'python': sys.version, 'uid': os.geteuid(), 'gid': os.getegid(), 'admission': admission_pin,
        'trusted_interpreter': INTERPRETER_PIN,
        'startup_flags': {'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site,
            'dont_write_bytecode': sys.dont_write_bytecode, 'ignore_environment': sys.flags.ignore_environment,
            'safe_path': sys.flags.safe_path, 'argv_count': len(sys.argv)},
        'pwd_runtime_classification': PWD_CLASSIFICATION,
        'source_manifest': manifest_pin, 'sources': sources, 'input_spec': inputs_pin, 'inputs': raw_inputs,
        'native_stdlib_data_read_before_import': native, 'foundation_module_inventory': foundation,
        'complete_module_inventory_before_archive_decode': MODULES_PREDECODE,
        'declared_stdlib_imports_complete': True, 'archive_decoding_started': False,
        'vendor_native_execution': False,
        'readonly_sources_inputs': True, 'no_new_privileges': True, 'seccomp': 2,
        'network_interfaces': ['lo'], 'raw_publication': False, 'signature_authenticated': False}
    save_report('bootstrap-receipt.json', receipt)
    return admission, by_id, sources + [manifest_pin], raw_inputs, elf_module, strict


def exact(stream, size):
    require(type(size) is int and 0 <= size <= CHUNK, 'EXACT_READ_BOUND')
    value = bytearray()
    while len(value) < size:
        part = stream.read(size - len(value))
        require(bool(part), 'TRUNCATED_INPUT')
        value.extend(part)
    return bytes(value)


def ar_members(path):
    members, seen = [], set()
    total = Path(path).stat().st_size
    with Path(path).open('rb') as stream:
        require(exact(stream, 8) == b'!<arch>\n', 'AR_MAGIC')
        while stream.tell() < total:
            deadline()
            require(len(members) < 3, 'AR_MEMBER_COUNT')
            header_offset = stream.tell()
            header = exact(stream, 60)
            require(header[58:] == b'`\n', 'AR_HEADER_END')
            name = header[:16].decode('ascii').rstrip(' ').removesuffix('/')
            size_text = header[48:58].decode('ascii').strip(' ')
            require(name in ('debian-binary', 'control.tar.xz', 'data.tar.xz')
                    and name not in seen and re.fullmatch(r'[0-9]{1,10}', size_text), 'AR_MEMBER_FORM')
            seen.add(name)
            size, offset = int(size_text), stream.tell()
            require(size <= total - offset, 'AR_MEMBER_BOUND')
            hashed, count, prefix = hashlib.sha256(), 0, b''
            while count < size:
                chunk = exact(stream, min(CHUNK, size - count))
                deadline()
                if not prefix:
                    prefix = chunk[:8]
                hashed.update(chunk)
                count += len(chunk)
            require((name == 'debian-binary' and size == 4 and prefix == b'2.0\n')
                    or (name != 'debian-binary' and prefix.startswith(b'\xfd7zXZ\x00')), 'AR_MEMBER_MAGIC')
            if size % 2:
                require(exact(stream, 1) == b'\n', 'AR_PADDING')
            members.append({'name': name, 'header_offset': header_offset, 'offset': offset,
                            'bytes': size, 'sha256': hashed.hexdigest()})
        require(stream.tell() == total and seen ==
                {'debian-binary', 'control.tar.xz', 'data.tar.xz'}, 'AR_COMPLETE_COVERAGE')
    return members


class Slice:
    def __init__(self, stream, offset, size):
        self.stream, self.remaining = stream, size
        stream.seek(offset)

    def read(self, size):
        require(0 < size <= CHUNK, 'SLICE_READ_BOUND')
        value = exact(self.stream, min(size, self.remaining))
        self.remaining -= len(value)
        return value


class BoundedXZ(io.RawIOBase):
    def __init__(self, source, maximum, lzma_module):
        super().__init__()
        self.source, self.maximum = source, maximum
        self.decoder = lzma_module.LZMADecompressor(format=lzma_module.FORMAT_XZ,
                                                   memlimit=64 * 1024 * 1024)
        self.count, self.hashed, self.finished = 0, hashlib.sha256(), False

    def readable(self):
        return True

    def readinto(self, output):
        deadline()
        if not output:
            return 0
        while not self.decoder.eof:
            compressed = self.source.read(CHUNK) if self.decoder.needs_input else b''
            require(compressed or not self.decoder.needs_input, 'XZ_TRUNCATED')
            value = self.decoder.decompress(compressed, max_length=min(len(output), CHUNK))
            if value:
                self.count += len(value)
                require(self.count <= self.maximum, 'XZ_EXPANSION_BOUND')
                self.hashed.update(value)
                output[:len(value)] = value
                return len(value)
        require(not self.decoder.unused_data and self.source.remaining == 0, 'XZ_TRAILING_OR_CONCATENATED')
        self.finished = True
        return 0


def inspect_tar(path, offset, size, label, strict, lzma_module, tarfile_module):
    strict.begin_tar_dataset()
    rows, seen = [], set()
    with Path(path).open('rb') as stream:
        decoded = BoundedXZ(Slice(stream, offset, size), 768 * 1024 * 1024, lzma_module)
        buffered = io.BufferedReader(decoded, buffer_size=CHUNK)
        try:
            with tarfile_module.open(fileobj=buffered, mode='r|', bufsize=CHUNK,
                                     tarinfo=strict.BoundedTarInfo) as archive:
                for member in archive:
                    deadline()
                    require(len(rows) < 10000 and member.size <= strict.MAX_MEMBER and not member.sparse,
                            'TAR_MEMBER_RESOURCE_BOUND')
                    name = strict.canonical_member(member.name, member.isdir())
                    require(name not in seen, 'DUPLICATE_TAR_CANONICAL_PATH')
                    seen.add(name)
                    require(member.isfile() or member.isdir() or member.issym() or member.islnk(),
                            'UNSUPPORTED_TAR_MEMBER')
                    require(len(json.dumps(member.pax_headers)) <= 65536, 'PAX_DATA_BOUND')
                    row = {'path': name, 'bytes': member.size, 'mode_recorded_not_applied': member.mode,
                        'uid_recorded_not_applied': member.uid, 'gid_recorded_not_applied': member.gid,
                        'type': 'regular' if member.isfile() else 'directory' if member.isdir() else
                                'symlink' if member.issym() else 'hardlink', 'pax_headers': member.pax_headers}
                    if member.issym() or member.islnk():
                        require(len(member.linkname) <= 2048 and '\x00' not in member.linkname
                                and '\\' not in member.linkname, 'TAR_LINK_DATA_FORM')
                        row['link_target_recorded_not_created'] = member.linkname
                    if member.isfile():
                        source = archive.extractfile(member)
                        require(source is not None, 'TAR_REGULAR_STREAM')
                        with source:
                            pin = raw_output(label + '\n' + name, source, member.size)
                        row.update(sha256=pin['sha256'], opaque_file=Path(pin['path']).name, elf=pin['elf'])
                    rows.append(row)
                require(len(json.dumps(archive.pax_headers)) <= 65536, 'GLOBAL_PAX_DATA_BOUND')
                ending = strict.validate_tar_ending(archive, decoded)
            require(decoded.finished, 'XZ_COMPLETE_EOF_REQUIRED')
            return {'label': label, 'members': rows, 'expanded_bytes': decoded.count,
                    'expanded_sha256': decoded.hashed.hexdigest(), 'validated_tar_ending': ending}
        finally:
            buffered.close()


def decoded_index(row, lzma_module):
    expected_bytes, expected_sha = INDEX_PINS[row['id']]
    with Path(row['path']).open('rb') as stream:
        decoded = BoundedXZ(Slice(stream, 0, row['bytes']), expected_bytes, lzma_module)
        buffered = io.BufferedReader(decoded, buffer_size=CHUNK)
        try:
            pin = raw_output(row['id'], buffered, expected_bytes, expected_sha)
            require(decoded.finished and decoded.count == expected_bytes
                    and decoded.hashed.hexdigest() == expected_sha, 'EXPANDED_INDEX_COMPLETE_PIN')
        finally:
            buffered.close()
    text = Path(pin['path']).read_text(encoding='utf-8', errors='strict')
    return {'id': row['id'], 'bytes': expected_bytes, 'sha256': expected_sha,
            'opaque_file': Path(pin['path']).name, 'text': text}


def selected_texts(datasets):
    result = []
    for dataset in datasets:
        for row in dataset['members']:
            if row['type'] != 'regular':
                continue
            wanted = dataset['label'].endswith(':control.tar.xz') or row['path'] in (
                'debian/rules', 'debian/control', 'debian/copyright') or bool(
                    re.search(r'(?i)(copyright|licen[cs]e|notice|copying)', row['path']))
            wanted = wanted or (dataset['label'] == 'debian-source' and
                (row['path'].endswith(('.mk', '.gn', '.gni', '.install')) or
                 row['path'].startswith('debian/rules')))
            if wanted:
                require(row['bytes'] <= 1048576, 'NOTICE_OR_SOURCE_TEXT_BOUND')
                path = RAW / row['opaque_file']
                text = path.read_text(encoding='utf-8', errors='strict')
                result.append({'dataset': dataset['label'], 'path': row['path'],
                    'bytes': row['bytes'], 'sha256': row['sha256'], 'text': text})
    require(bool(result), 'NOTICE_AND_CONTROL_TEXT_REQUIRED')
    return result


def analyze(admission, inputs, sources, input_pins, elf_module, strict, corpus):
    # These imports occur only after source/input/bootstrap gates and exact
    # native extension/provider byte facts have been captured.
    import lzma
    import tarfile
    datasets, archives, indexes = [], [], []
    for name in ('chromium', 'chromium-common', 'chromium-sandbox'):
        row = inputs[name]
        members = ar_members(row['path'])
        archives.append({'id': name, 'bytes': row['bytes'], 'sha256': row['sha256'], 'ar_members': members})
        for member in members:
            if member['name'] != 'debian-binary':
                datasets.append(inspect_tar(row['path'], member['offset'], member['bytes'],
                    name + ':' + member['name'], strict, lzma, tarfile))
    source = inputs['debian-source']
    datasets.append(inspect_tar(source['path'], 0, source['bytes'], 'debian-source', strict, lzma, tarfile))
    for name in ('security-index', 'updates-index'):
        indexes.append(decoded_index(inputs[name], lzma))
    for dataset in datasets:
        for row in dataset['members']:
            if row.get('elf'):
                pin = REGISTRY[row['opaque_file']]
                elf_report(elf_module, pin, dataset['label'] + ':' + row['path'])
    native = [row for row in NATIVE if not row['logical_path'].startswith('/usr/')]
    require(native, 'FULL_BROWSER_NATIVE_FACTS_REQUIRED')
    texts = selected_texts(datasets)
    rule_text = [row for row in texts if row['dataset'] == 'debian-source' and row['path'] == 'debian/rules']
    require(len(rule_text) == 1, 'ACTUAL_DEBIAN_BUILD_RULES_REQUIRED')
    build_lines = [line for line in rule_text[0]['text'].splitlines()
                   if re.search(r'(?i)(widevine|is_chrome_branded|is_official_build|proprietary_codecs)', line)]
    inventory = {'profile_id': PROFILE, 'successful': True, 'status': 'COMPLETE_PINNED_DATA_FACTS_NOT_RUNTIME_ADMISSION',
        'archives': archives, 'tar_datasets': datasets, 'expanded_indexes': indexes,
        'selected_control_notice_source_text': texts, 'source_build_flag_lines': build_lines,
        'all_native_fact_references': NATIVE, 'browser_native_files': len(native),
        'raw_regular_files': len(REGISTRY), 'raw_regular_bytes': TOTAL_RAW,
        'widevine_named_paths': [{'dataset': row['label'], 'path': member['path']}
            for row in datasets for member in row['members'] if 'widevine' in member['path'].lower()],
        'loaded_declared_native_stdlib': [{'module': name, 'path': str(Path(sys.modules[name].__file__).resolve())}
            for name in CODECS if name in sys.modules],
        'parser_corpus': corpus,
        'raw_publication': False, 'native_vendor_execution': False, 'maintainer_execution': False,
        'links_created_or_vendor_modes_applied': False, 'signature_authenticated': False,
        'limit': 'Complete bounded byte facts only; provider/OS/font/crypto/license/advisory closure and all native UI tests remain separate.'}
    save_report('data-inventory.json', inventory)
    return inventory


def final_rehash(sources, input_pins):
    for expected in sources + input_pins:
        current = hash_file(expected['path'], 134217728, readonly=True)
        require(all(current[key] == expected[key] for key in ('bytes', 'sha256', 'device', 'inode')),
                'FINAL_SOURCE_OR_INPUT_CHANGED')
    require({path.name for path in RAW.iterdir()} == set(REGISTRY), 'EXACT_PRIVATE_RAW_OUTPUT_SET')
    for expected in REGISTRY.values():
        current = hash_file(expected['path'], 384 * 1024 * 1024)
        require(all(current[key] == expected[key] for key in ('bytes', 'sha256', 'device', 'inode')),
                'FINAL_PRIVATE_OUTPUT_CHANGED')
    expected_reports = set(DERIVED) | {'output-manifest.json'}
    require({path.name for path in REPORTS.iterdir()} == expected_reports, 'EXACT_DERIVED_OUTPUT_SET')
    for name in expected_reports:
        hash_file(REPORTS / name, 33554432)
    require({path.name for path in CODE.iterdir()} == CODE_NAMES, 'FINAL_CODE_PATH_SET')
    for expected in IMAGE_NATIVE.values():
        current = hash_file(expected['path'], 16777216)
        require(all(current[key] == expected[key] for key in ('bytes', 'sha256', 'device', 'inode')),
                'FINAL_IMAGE_NATIVE_BYTES_CHANGED')
    for name in CODECS:
        require(name in sys.modules and getattr(sys.modules[name], '__file__', None), 'DECLARED_CODEC_NOT_LOADED')
        origin = str(Path(sys.modules[name].__file__).resolve(strict=True))
        require(origin in IMAGE_NATIVE, 'LOADED_CODEC_ORIGIN_NOT_PROBED')
    current_modules = module_inventory({row['module']: row['file'] for row in MODULES_PREDECODE
        if 'file' in row and row['file']['path'].endswith('.so')})
    require(current_modules == MODULES_PREDECODE, 'FINAL_COMPLETE_MODULE_ORIGIN_OR_BYTES_CHANGED')
    current_interpreter = hash_file(INTERPRETER_PIN['path'], 33554432)
    require(current_interpreter == INTERPRETER_PIN, 'FINAL_TRUSTED_INTERPRETER_CHANGED')
    return True


def section_elf(specifications):
    """Synthetic section-data fixtures only; never a loadable executable."""
    names = bytearray(b'\x00')
    name_offsets = {}
    for name in ['.shstrtab'] + [row['name'] for row in specifications]:
        name_offsets[name] = len(names)
        names.extend(name.encode('ascii') + b'\x00')
    rows = [{'name': '.shstrtab', 'type': 3, 'data': bytes(names)}] + specifications
    indexes = {row['name']: index + 1 for index, row in enumerate(rows)}
    body, headers, locations = bytearray(64), [bytes(64)], {}
    for row in rows:
        if 'alias' in row:
            target, delta, size = row['alias']
            offset = locations[target][0] + delta
        else:
            body.extend(bytes((-len(body)) % 8))
            offset, size = len(body), len(row['data'])
            body.extend(row['data'])
        locations[row['name']] = (offset, size)
        headers.append(struct.pack('<IIQQQQIIQQ', name_offsets[row['name']], row['type'], 0, 0,
            offset, size, indexes[row['link']] if row.get('link') else 0,
            row.get('info', 0), 1, row.get('entry_bytes', 0)))
    body.extend(bytes((-len(body)) % 8))
    table_offset = len(body)
    body.extend(b''.join(headers))
    struct.pack_into('<16sHHIQQQIHHHHHH', body, 0, b'\x7fELF\x02\x01\x01' + bytes(9),
                     3, 62, 1, 0, 0, table_offset, 0, 64, 0, 0, 64, len(headers), 1)
    return bytes(body)


def parser_corpus(elf_module, strict):
    """Contract-level malformed/edge inputs, all inert and privately owned."""
    import tarfile
    rows = []

    class Decoded(io.RawIOBase):
        def __init__(self, value):
            self.value, self.offset, self.count, self.finished = value, 0, 0, False

        def readable(self):
            return True

        def readinto(self, target):
            if self.offset == len(self.value):
                self.finished = True
                return 0
            count = min(len(target), len(self.value) - self.offset, CHUNK)
            target[:count] = self.value[self.offset:self.offset + count]
            self.offset += count
            self.count += count
            return count

    def tar_check(value):
        strict.begin_tar_dataset()
        decoded = Decoded(value)
        buffered = io.BufferedReader(decoded, buffer_size=CHUNK)
        try:
            with tarfile.open(fileobj=buffered, mode='r|', bufsize=CHUNK,
                              tarinfo=strict.BoundedTarInfo) as archive:
                for member in archive:
                    strict.canonical_member(member.name, member.isdir())
                return strict.validate_tar_ending(archive, decoded)
        finally:
            buffered.close()

    def rejected(label, action, expected):
        try:
            action()
        except Exception as error:
            require(type(error).__name__ in ('DataFailure', 'ElfDataError', 'ProfileFailure')
                    and str(error) in expected, 'CORPUS_WRONG_FAILURE_KIND')
            rows.append({'id': label, 'status': 'PASS', 'result': 'typed_rejection', 'code': str(error)})
        else:
            raise ProfileFailure('CORPUS_REJECTION_MISSING')

    header = tarfile.TarInfo('owned.txt')
    header.size, header.mode, header.uid, header.gid, header.mtime = 0, 0o600, 10001, 10001, 0
    raw_header = header.tobuf(format=tarfile.USTAR_FORMAT)
    proof = tar_check(raw_header + bytes(1024))
    require(proof['second_zero_block_present'] and proof['complete_aligned_zero_trailer'], 'CORPUS_VALID_TAR')
    rows.append({'id': 'tar_valid_two_zero_blocks', 'status': 'PASS', 'result': 'complete_termination'})
    rejected('tar_malformed_later_header', lambda: tar_check(raw_header + b'!' * 512 + bytes(1024)),
             {'TAR_MALFORMED_TRUNCATED_OR_MISSING_HEADER'})
    rejected('tar_truncated_later_header', lambda: tar_check(raw_header + b'!' * 128),
             {'TAR_MALFORMED_TRUNCATED_OR_MISSING_HEADER'})
    rejected('tar_missing_terminal', lambda: tar_check(raw_header),
             {'TAR_MALFORMED_TRUNCATED_OR_MISSING_HEADER'})
    rejected('tar_only_one_zero_block', lambda: tar_check(raw_header + bytes(512)),
             {'TAR_SECOND_ZERO_BLOCK_REQUIRED'})
    rejected('tar_nonzero_read_ahead_trailer', lambda: tar_check(raw_header + bytes(512) + b'!' + bytes(511)),
             {'TAR_NONZERO_TRAILER'})
    solaris = tarfile.TarInfo('solaris-pax')
    solaris.type, solaris.size = b'X', 65537
    rejected('tar_solaris_pax_early_rejection', lambda: tar_check(solaris.tobuf(format=tarfile.USTAR_FORMAT)),
             {'TAR_UNSUPPORTED_RAW_TYPE'})
    pax = tarfile.TarInfo('pax')
    pax.type, pax.size = tarfile.XHDTYPE, 65537
    rejected('tar_supported_pax_size_bound', lambda: tar_check(pax.tobuf(format=tarfile.USTAR_FORMAT)),
             {'TAR_EXTENDED_METADATA_BOUND'})

    def elf_case(label, value, expected=None, verify=None):
        before_fds = len(list(Path('/proc/self/fd').iterdir()))
        pin = raw_output('corpus:' + label, io.BytesIO(value), len(value))
        identity = {key: pin[key] for key in ('bytes', 'sha256', 'device', 'inode')}
        try:
            if expected:
                rejected(label, lambda: elf_module.inspect_elf(pin['path'], identity), expected)
            else:
                facts = elf_module.inspect_elf(pin['path'], identity)
                require(verify is None or verify(facts), 'CORPUS_POSITIVE_FACTS_MISMATCH')
                rows.append({'id': label, 'status': 'PASS', 'result': 'bounded_data_facts'})
            require(len(list(Path('/proc/self/fd').iterdir())) == before_fds, 'CORPUS_DESCRIPTOR_LEAK')
        finally:
            global TOTAL_RAW
            actual = hash_file(pin['path'], 384 * 1024 * 1024)
            require(all(actual[key] == pin[key] for key in ('bytes', 'sha256', 'device', 'inode')),
                    'OWNED_CORPUS_FIXTURE_CHANGED')
            Path(pin['path']).unlink()
            del REGISTRY[Path(pin['path']).name]
            TOTAL_RAW -= pin['bytes']

    elf_case('elf_static_section_facts', section_elf([
        {'name': '.go.buildinfo', 'type': 1, 'data': b'owned-section-data'}]), verify=lambda facts:
        facts['dynamic_symbol_entry_count'] == 0 and any(row['name'] == '.go.buildinfo' for row in facts['sections']))
    elf_case('elf_bad_magic', b'not-ELF' + bytes(64), {'ELF64_LE_AMD64_HEADER'})
    def identity_rejections():
        global TOTAL_RAW
        value = section_elf([{'name': '.owned', 'type': 1, 'data': b'owned'}])
        pin = raw_output('corpus:identity', io.BytesIO(value), len(value))
        identity = {key: pin[key] for key in ('bytes', 'sha256', 'device', 'inode')}
        before = len(list(Path('/proc/self/fd').iterdir()))
        try:
            rejected('elf_boolean_identity_rejected', lambda: elf_module.inspect_elf(pin['path'],
                dict(identity, bytes=True)), {'ELF_IDENTITY_FIELDS'})
            rejected('elf_constructor_inode_failure_closes_fd', lambda: elf_module.inspect_elf(pin['path'],
                dict(identity, inode=identity['inode'] + 1)), {'ELF_OPEN_IDENTITY'})
            require(len(list(Path('/proc/self/fd').iterdir())) == before, 'CORPUS_CONSTRUCTOR_FD_LEAK')
        finally:
            actual = hash_file(pin['path'], 384 * 1024 * 1024)
            require(all(actual[key] == pin[key] for key in ('bytes', 'sha256', 'device', 'inode')),
                    'OWNED_CORPUS_IDENTITY_FIXTURE_CHANGED')
            Path(pin['path']).unlink()
            del REGISTRY[Path(pin['path']).name]
            TOTAL_RAW -= pin['bytes']
    identity_rejections()
    dynsym = bytes(24) + struct.pack('<IBBHQQ', 1, 0x11, 0, 2, 0, 1)
    elf_case('elf_raw_versym_zero_candidate', section_elf([
        {'name': '.dynstr', 'type': 3, 'data': b'\x00sample\x00'},
        {'name': '.dynsym', 'type': 11, 'data': dynsym, 'link': '.dynstr', 'entry_bytes': 24},
        {'name': '.gnu.version', 'type': 0x6fffffff, 'data': bytes(4), 'link': '.dynsym'}]),
        verify=lambda facts: len(facts['defined_dynamic_symbols']) == 1 and
            facts['defined_dynamic_symbols'][0]['version_index'] == 0 and
            facts['defined_dynamic_symbols'][0]['unversioned'] is True and
            facts['defined_dynamic_symbols'][0]['binding_visibility_candidate'] is True)
    version_blocks = []
    for index in range(35):
        auxiliary = b''.join(struct.pack('<II', 0, 8 if item < 4095 else 0) for item in range(4096))
        block_size = 20 + len(auxiliary)
        version_blocks.append(struct.pack('<HHHHIII', 1, 0, index + 1, 4096, 0, 20,
            block_size if index < 34 else 0) + auxiliary)
    elf_case('elf_empty_version_parent_budget', section_elf([
        {'name': '.dynstr', 'type': 3, 'data': b'\x00'},
        {'name': '.gnu.version_d', 'type': 0x6ffffffd, 'data': b''.join(version_blocks),
         'link': '.dynstr', 'info': 35}]), {'ELF_METADATA_ENTRY_AGGREGATE_BOUND'})
    aliases = [{'name': '.large', 'type': 1, 'data': bytes(32 * 1024 * 1024 + 2)}]
    for index in range(3):
        aliases.append({'name': '.str' + str(index), 'type': 3,
                        'alias': ('.large', index, 32 * 1024 * 1024)})
        aliases.append({'name': '.need' + str(index), 'type': 0x6ffffffe,
            'data': struct.pack('<HHIII', 1, 1, 0, 16, 0) + struct.pack('<IHHII', 0, 0, index + 2, 0, 0),
            'link': '.str' + str(index), 'info': 1})
    elf_case('elf_overlapping_string_cache_budget', section_elf(aliases), {'ELF_AGGREGATE_STRING_CACHE_BOUND'})
    # Additive version-header/auxiliary-layout regression vectors. The old15
    # vectors above remain literal byte-equal. These are synthetic data only.
    version_strings = b'\x00libA.so\x00libB.so\x00VER_A\x00VER_B\x00'

    def version_fixture(kind, value, records=2):
        return section_elf([
            {'name': '.dynstr', 'type': 3, 'data': version_strings},
            {'name': '.gnu.version_r' if kind == 0x6ffffffe else '.gnu.version_d',
             'type': kind, 'data': value, 'link': '.dynstr', 'info': records}])

    # LLVM's need writer packs the two16-byte headers before both16-byte
    # auxiliaries: the first vn_next16 precedes its own vn_aux32.
    packed_need = (
        struct.pack('<HHIII', 1, 1, 1, 32, 16) +
        struct.pack('<HHIII', 1, 1, 9, 32, 0) +
        struct.pack('<IHHII', 0, 0, 2, 17, 0) +
        struct.pack('<IHHII', 0, 0, 3, 23, 0))
    packed_def = (
        struct.pack('<HHHHIII', 1, 0, 1, 1, 0, 40, 20) +
        struct.pack('<HHHHIII', 1, 0, 2, 1, 0, 28, 0) +
        struct.pack('<II', 17, 0) + struct.pack('<II', 23, 0))

    def need_facts(facts):
        return facts['required_symbol_versions'] == [
            {'provider': 'libA.so', 'version': 'VER_A', 'flags': 0,
             'hash': 0, 'index_raw': 2, 'index': 2},
            {'provider': 'libB.so', 'version': 'VER_B', 'flags': 0,
             'hash': 0, 'index_raw': 3, 'index': 3}]

    def def_facts(facts):
        return facts['defined_version_records'] == [
            {'index_raw': 1, 'index': 1, 'flags': 0, 'hash': 0,
             'name': 'VER_A', 'parents': []},
            {'index_raw': 2, 'index': 2, 'flags': 0, 'hash': 0,
             'name': 'VER_B', 'parents': []}]

    elf_case('elf_packed_verneed_headers_before_auxiliaries',
             version_fixture(0x6ffffffe, packed_need), verify=need_facts)
    elf_case('elf_packed_verdef_headers_before_auxiliaries',
             version_fixture(0x6ffffffd, packed_def), verify=def_facts)
    interleaved_need = (
        struct.pack('<HHIII', 1, 1, 1, 16, 32) +
        struct.pack('<IHHII', 0, 0, 2, 17, 0) +
        struct.pack('<HHIII', 1, 1, 9, 16, 0) +
        struct.pack('<IHHII', 0, 0, 3, 23, 0))
    elf_case('elf_interleaved_verneed_layout_still_supported',
             version_fixture(0x6ffffffe, interleaved_need), verify=need_facts)

    def changed_field(value, offset, format, replacement):
        changed = bytearray(value)
        struct.pack_into(format, changed, offset, replacement)
        return bytes(changed)

    elf_case('elf_verneed_next_shorter_than_header_denied',
             version_fixture(0x6ffffffe, changed_field(packed_need, 12, '<I', 8)),
             {'ELF_VERSION_RECORD_PROGRESS'})
    elf_case('elf_verdef_next_shorter_than_header_denied',
             version_fixture(0x6ffffffd, changed_field(packed_def, 16, '<I', 16)),
             {'ELF_VERSION_RECORD_PROGRESS'})
    elf_case('elf_verneed_next_full_header_out_of_range_denied',
             version_fixture(0x6ffffffe, changed_field(packed_need, 12, '<I', 56)),
             {'ELF_VERSION_RECORD_PROGRESS'})
    elf_case('elf_verdef_next_full_header_out_of_range_denied',
             version_fixture(0x6ffffffd, changed_field(packed_def, 16, '<I', 48)),
             {'ELF_VERSION_RECORD_PROGRESS'})
    elf_case('elf_verneed_backward_unsigned_offset_denied',
             version_fixture(0x6ffffffe, changed_field(packed_need, 12, '<I', 0xfffffff0)),
             {'ELF_VERSION_RECORD_PROGRESS'})
    short_aux = changed_field(packed_need, 2, '<H', 2)
    short_aux = changed_field(short_aux, 44, '<I', 8)
    elf_case('elf_packed_verneed_short_auxiliary_progress_denied',
             version_fixture(0x6ffffffe, short_aux), {'ELF_VERNEED_AUX_CHAIN'})
    elf_case('elf_packed_verneed_duplicate_index_denied',
             version_fixture(0x6ffffffe, changed_field(packed_need, 54, '<H', 2)),
             {'ELF_REQUIRED_VERSION_INDEX_UNIQUENESS'})
    elf_case('elf_packed_verdef_auxiliary_out_of_range_denied',
             version_fixture(0x6ffffffd, changed_field(packed_def, 12, '<I', 52)),
             {'ELF_VERDEF_AUX_BOUND'})
    elf_case('elf_packed_verneed_early_termination_count_denied',
             version_fixture(0x6ffffffe, changed_field(packed_need, 12, '<I', 0)),
             {'ELF_VERSION_SECTION_COUNT'})

    require(len(rows) == 27 and all(row['status'] == 'PASS' for row in rows), 'EXACT_CORPUS_SCOPE')
    return {'status': 'PASS', 'native_execution': False, 'vectors': rows,
            'owned_synthetic_files_removed_after_identity_check': True}


def main():
    admission, inputs, sources, input_pins, elf_module, strict = bootstrap()
    marker = {'profile_id': PROFILE, 'source_sha': os.environ['SS_DATA_SOURCE_SHA']}
    sys.stdout.buffer.write(b'SS_DATA_JSON_STREAM ' +
        json.dumps(marker, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n')
    sys.stdout.buffer.flush()
    corpus = parser_corpus(elf_module, strict)
    inventory = analyze(admission, inputs, sources, input_pins, elf_module, strict, corpus)
    export_derived(PROFILE, dict(DERIVED), lambda: final_rehash(sources, input_pins), {
        'inventory': {'profile_id': PROFILE, 'raw_files_private_only': len(REGISTRY),
            'raw_bytes_private_only': TOTAL_RAW, 'native_fact_reports': len(NATIVE),
            'corpus_vectors': len(corpus['vectors']), 'native_vendor_execution': False,
            'signature_authenticated': False, 'browser_runtime_admitted': False},
        'limits': {'derived_bytes_per_report': 33554432, 'derived_bytes_total': 134217728,
                   'gzip_bytes_total': 16777216, 'derived_reports': 1024}})


# Byte-exact common exporter R2; independently reviewed source, not execution admission.
def export_derived(profile_id, reports, rehash_all, manifest_fields=None):
    """SOURCE-ONLY common DATA export; embed byte-exact after independent review.

    No .data/archive/native payloads, arbitrary output roots, user URLs or new
    authority. The fixed profile owns the explicit report selection and its
    source/input/private-output rehash callback. All checks precede first frame.
    """
    import base64
    import gzip
    import hashlib
    import json
    import os
    from pathlib import Path
    import re
    import stat
    import sys

    RAW_FILE = 32 * 1024 * 1024
    RAW_TOTAL = 128 * 1024 * 1024
    GZIP_TOTAL = 16 * 1024 * 1024
    FILES = 1024
    FACT_NODES = 256
    FACT_KEYS_PER_GROUP = 64
    FACT_STRING_CHARACTERS = 512
    FACT_ENCODED_BYTES = 16 * 1024

    def check(value, code):
        if not value:
            raise ValueError('DATA_EXPORT_' + code)

    def duplicate_free(pairs):
        value = {}
        for key, item in pairs:
            check(key not in value, 'DUPLICATE_JSON_KEY')
            value[key] = item
        return value

    def finite(_):
        raise ValueError('DATA_EXPORT_NONFINITE_JSON')

    def snapshot(path, remaining=RAW_FILE):
        check(type(path) is Path or isinstance(path, Path), 'PATH_TYPE')
        check(path.is_absolute() and path.parent in (Path('/ci-evidence'), Path('/ci-evidence/reports')),
              'FIXED_REPORT_PARENT')
        for ancestor in (path, *path.parents):
            check(not ancestor.is_symlink(), 'SYMLINK')
        before = path.lstat()
        check(type(remaining) is int and 0 <= remaining <= RAW_TOTAL, 'REMAINING_RAW_BUDGET')
        check(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.getuid()
              and 0 < before.st_size <= min(RAW_FILE, remaining), 'REGULAR_OWNED_BOUNDED_FILE')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            check((opened.st_dev, opened.st_ino, opened.st_size) ==
                  (before.st_dev, before.st_ino, before.st_size), 'OPEN_IDENTITY')
            raw = stream.read(RAW_FILE + 1)
            after = os.fstat(stream.fileno())
        check(len(raw) == before.st_size and
              (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
              (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'FILE_CHANGED')
        current = path.lstat()
        check((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) ==
              (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'PATH_REPLACED')
        try:
            parsed = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=duplicate_free,
                                parse_constant=finite)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError('DATA_EXPORT_INVALID_UTF8_JSON') from error
        check(type(parsed) is dict, 'JSON_ROOT_OBJECT')
        return raw, {'decoded_bytes': len(raw), 'decoded_sha256': hashlib.sha256(raw).hexdigest(),
                     'device': after.st_dev, 'inode': after.st_ino}

    check(profile_id in ('debian-archives', 'postgres-rootfs'), 'FIXED_PROFILE')
    check(type(reports) is dict and 2 <= len(reports) < FILES and callable(rehash_all), 'REPORT_MAP')
    check({'bootstrap-receipt.json', 'data-inventory.json'} <= set(reports)
          and 'output-manifest.json' not in reports, 'COMMON_NAMES_AND_SELF_EXCLUSION')
    check(all(type(name) is str and re.fullmatch(r'[a-z][a-z0-9-]{0,95}\.json', name)
              for name in reports), 'FLAT_REVIEWED_NAMES')
    check(all(isinstance(path, Path) and path.name == name for name, path in reports.items()), 'NAME_PATH_BINDING')
    parents = {path.parent for path in reports.values()}
    check(len(parents) == 1 and next(iter(parents)) in (Path('/ci-evidence'), Path('/ci-evidence/reports')),
          'ONE_FIXED_REPORT_DIRECTORY')
    parent = next(iter(parents))
    fields = {} if manifest_fields is None else manifest_fields
    check(type(fields) is dict and len(fields) <= 2 and set(fields) <= {'inventory', 'limits'}
          and all(type(value) is dict for value in fields.values()), 'PROFILE_FACTS_ONLY')
    # The only caller facts are two bounded dictionaries of scalar values. No
    # arbitrary nested containers, recursive traversal, floats or unbounded ints.
    # Charge a conservative ensure_ascii=True JSON size before serialization.
    fact_nodes, fact_encoded_bound = 1, 2
    for group_name, group in fields.items():
        check(len(group) <= FACT_KEYS_PER_GROUP, 'FACT_GROUP_KEY_BOUND')
        fact_nodes += 1
        fact_encoded_bound += len(group_name) + 7
        for key, value in group.items():
            check(type(key) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key), 'FACT_KEY_FORM')
            fact_nodes += 1
            fact_encoded_bound += len(key) + 4
            if type(value) is str:
                check(len(value) <= FACT_STRING_CHARACTERS, 'FACT_STRING_BOUND')
                try:
                    value.encode('utf-8', errors='strict')
                except UnicodeError as error:
                    raise ValueError('DATA_EXPORT_FACT_STRING_UTF8') from error
                fact_encoded_bound += 12 * len(value) + 2
            elif type(value) is int:
                check(-(2 ** 63) <= value <= 2 ** 63 - 1, 'FACT_INTEGER_BOUND')
                fact_encoded_bound += 20
            elif value is None or type(value) is bool:
                fact_encoded_bound += 5
            else:
                raise ValueError('DATA_EXPORT_FACT_SCALAR_TYPE_AND_FIXED_DEPTH')
            check(fact_nodes <= FACT_NODES and fact_encoded_bound <= FACT_ENCODED_BYTES,
                  'FACT_NODE_OR_ENCODED_BOUND')

    raw_by_name, observed, total = {}, {}, 0
    for name, path in sorted(reports.items()):
        # Reserve against the aggregate before this report is read/allocated.
        raw, row = snapshot(path, RAW_TOTAL - total)
        total += len(raw)
        check(total <= RAW_TOTAL, 'TOTAL_RAW_BOUND')
        raw_by_name[name], observed[name] = raw, row
    manifest = {'format': 'stock-and-sleeve:owned-data-derived-export-manifest:v1',
                'profile_id': profile_id, 'successful': True,
                'derived_exports': [{'name': name, 'decoded_bytes': observed[name]['decoded_bytes'],
                                     'decoded_sha256': observed[name]['decoded_sha256']} for name in sorted(observed)],
                'self_excluded': True, 'raw_opaque_payload_exported': False,
                'profile_inventory': fields.get('inventory', {}), 'profile_limits': fields.get('limits', {}),
                'transport_limits': {'decoded_bytes_per_file': RAW_FILE, 'decoded_bytes_total': RAW_TOTAL,
                                     'gzip_bytes_total': GZIP_TOTAL, 'files_including_manifest': FILES},
                'profile_fact_limits': {'fixed_container_depth': 2, 'nodes': FACT_NODES,
                                        'keys_per_group': FACT_KEYS_PER_GROUP,
                                        'string_characters': FACT_STRING_CHARACTERS,
                                        'conservative_encoded_bytes': FACT_ENCODED_BYTES}}
    try:
        manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                   ensure_ascii=True, allow_nan=False) + '\n').encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError('DATA_EXPORT_MANIFEST_FACTS_JSON') from error
    check(0 < len(manifest_raw) <= RAW_FILE and total + len(manifest_raw) <= RAW_TOTAL, 'MANIFEST_SIZE')
    manifest_path = parent / 'output-manifest.json'
    descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), 'MANIFEST_IDENTITY')
        stream.write(manifest_raw)
        stream.flush()
        os.fsync(stream.fileno())
    reread, row = snapshot(manifest_path)
    check(reread == manifest_raw, 'MANIFEST_READBACK')
    raw_by_name['output-manifest.json'], observed['output-manifest.json'] = reread, row
    check(len(raw_by_name) <= FILES, 'FINAL_REPORT_COUNT')

    prepared, gzip_bytes, payload_bytes = [], 0, 0
    for name, raw in sorted(raw_by_name.items()):
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        gzip_bytes += len(compressed)
        check(gzip_bytes <= GZIP_TOTAL, 'TOTAL_GZIP_BOUND')
        payload = base64.encodebytes(compressed)
        lines = payload.splitlines()
        check(payload.endswith(b'\n') and lines and all(len(line) == 76 for line in lines[:-1])
              and 1 <= len(lines[-1]) <= 76 and base64.decodebytes(payload) == compressed,
              'CANONICAL_76_LF_BASE64')
        payload_bytes += len(payload)
        header = {'name': name, 'encoding': 'gzip+base64',
                  'decoded_bytes': len(raw), 'decoded_sha256': hashlib.sha256(raw).hexdigest(),
                  'compressed_bytes': len(compressed), 'compressed_sha256': hashlib.sha256(compressed).hexdigest(),
                  'payload_bytes': len(payload), 'payload_sha256': hashlib.sha256(payload).hexdigest()}
        prepared.append((header, payload))
    # The source profile independently checks ALL source/input/opaque output and
    # derived files, including this one exact new manifest. It must return True.
    check(rehash_all() is True, 'PROFILE_FINAL_REHASH')
    for name, prior in observed.items():
        path = manifest_path if name == 'output-manifest.json' else reports[name]
        raw, current = snapshot(path)
        check(current == prior and raw == raw_by_name[name], 'REPORT_FINAL_BOOKEND')

    output = sys.stdout.buffer

    def write_all(raw):
        view = memoryview(raw)
        while view:
            count = output.write(view)
            check(type(count) is int and 0 < count <= len(view), 'STDOUT_SHORT_WRITE')
            view = view[count:]

    for header, payload in prepared:
        write_all(b'SS_DATA_JSON_BEGIN ' + json.dumps(header, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n')
        write_all(payload)
        write_all(b'SS_DATA_JSON_END ' + json.dumps({'name': header['name']}, separators=(',', ':')).encode('ascii') + b'\n')
    write_all(b'SS_DATA_JSON_COMPLETE ' + json.dumps({'profile_id': profile_id,
              'reports': len(prepared), 'successful': True}, separators=(',', ':')).encode('ascii') + b'\n')
    output.flush()
    return {'profile_id': profile_id, 'reports': len(prepared), 'decoded_bytes': total + len(manifest_raw),
            'gzip_bytes': gzip_bytes, 'payload_bytes': payload_bytes,
            'manifest_decoded_sha256': observed['output-manifest.json']['decoded_sha256'],
            'all_source_input_private_output_rehash_callback': True,
            'all_reports_bookended': True, 'raw_opaque_payload_exported': False}


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', code):
            code = 'UNEXPECTED_TYPED_PROFILE_FAILURE'
        failure = {'profile_id': PROFILE, 'successful': False,
                   'error_type': type(error).__name__, 'error_code': code,
                   'raw_payload_or_exception_detail_printed': False}
        sys.stderr.write('SS_DATA_PROFILE_FAILURE ' +
            json.dumps(failure, sort_keys=True, separators=(',', ':')) + '\n')
        raise SystemExit(1)
