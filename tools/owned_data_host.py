"""Source-only host control/data checks. Not an application or execution grant.

Run only on the separately admitted owned Linux CI host with -I -S -B.
No module, archive member, command or environment supplied by a profile is run.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

BASE = 'python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'
CONFIG = 'sha256:78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db'
OWNER = 'org.stockandsleeve.owned-data.owner'
ROLE = 'org.stockandsleeve.owned-data.role'
ENTRIES = {'debian-archives': 'debian_data.py', 'postgres-rootfs': 'pg_oci_data.py'}
INPUT_COUNTS = {'debian-archives': 6, 'postgres-rootfs': 14}
COMMON = {'bootstrap-receipt.json', 'data-inventory.json', 'output-manifest.json'}
HOST_NAMES = {'tools/run_owned_data_ci.sh', 'tools/owned_data_host.py', 'tools/ci_evidence_transport.sh'}
ENV_KEYS = {'SS_DATA_SOURCE_SHA', 'SS_DATA_PROFILE_SHA256', 'SS_DATA_SOURCE_MANIFEST_SHA256',
            'SS_DATA_INPUT_SPEC_SHA256', 'SS_DATA_PROFILE_ID'}
MAX_CODE = 4 * 1024 * 1024
MAX_INPUT = 128 * 1024 * 1024
MAX_INPUT_TOTAL = 256 * 1024 * 1024
MAX_LOG = 32 * 1024 * 1024
MAX_RAW = 32 * 1024 * 1024
MAX_RAW_TOTAL = 128 * 1024 * 1024
MAX_GZIP_TOTAL = 16 * 1024 * 1024
MAX_REPORTS = 1024
TMPFS = {'/tmp': 'rw,nosuid,nodev,noexec,size=67108864,mode=1777',
         '/ci-evidence': 'rw,nosuid,nodev,noexec,size=2147483648,mode=0700,uid=10001,gid=10001'}


class ControlFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ControlFailure(code)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def integer(value, minimum, maximum):
    require(type(value) is int and minimum <= value <= maximum, 'INTEGER_BOUND')
    return value


def hex_string(value, length=64):
    require(type(value) is str and re.fullmatch('[a-f0-9]{' + str(length) + '}', value), 'HEX_FORM')
    return value


def safe_name(value):
    require(type(value) is str and 1 <= len(value) <= 128
            and re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9._-]*', value) and '..' not in value, 'BASENAME_FORM')
    return value


def relative(value):
    require(type(value) is str and 1 <= len(value) <= 256 and not value.startswith('/')
            and all(re.fullmatch('[A-Za-z0-9_.-]+', p) and p not in ('.', '..')
                    for p in value.split('/')), 'RELATIVE_PATH_FORM')
    return value


def under(root, name):
    path = root / relative(name)
    require(path.resolve(strict=True) == path and root in path.parents, 'SOURCE_ANCESTRY')
    for parent in path.parents:
        if parent == root:
            break
        require(not parent.is_symlink(), 'SOURCE_ANCESTOR_LINK')
    return path


def raw(path, maximum, readonly=False):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and not path.is_symlink() and before.st_nlink == 1
            and 0 <= before.st_size <= maximum, 'REGULAR_FILE_BOUND')
    if readonly:
        require(not before.st_mode & 0o222, 'COPIED_FILE_NOT_READONLY')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        value = stream.read(maximum + 1)
    after = path.lstat()
    key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    require(len(value) == before.st_size and key(before) == key(opened) == key(after), 'FILE_IDENTITY_CHANGED')
    return value


def pairs(rows):
    result = {}
    for key, value in rows:
        require(key not in result, 'JSON_DUPLICATE_KEY')
        result[key] = value
    return result


def number(value):
    result = float(value)
    require(math.isfinite(result), 'JSON_NONFINITE')
    return result


def strict_json(value):
    return json.loads(value.decode('utf-8', errors='strict'), object_pairs_hook=pairs,
                      parse_float=number, parse_constant=lambda _: require(False, 'JSON_NONFINITE'))


def load(path, maximum=MAX_CODE):
    return strict_json(raw(path, maximum))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def save(path, value, maximum=MAX_CODE):
    value = json_bytes(value) if not isinstance(value, bytes) else value
    require(len(value) <= maximum and not path.exists(), 'OUTPUT_FRESH_BOUND')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def source_pin(root, row, maximum):
    require(type(row) is dict and set(row) == {'name', 'repo_path', 'bytes', 'sha256'}, 'CODE_PIN_SHAPE')
    name = safe_name(row['name'])
    require(row['repo_path'].startswith('tools/owned_data/'), 'CODE_NAMESPACE')
    value = raw(under(root, row['repo_path']), maximum)
    require(len(value) == integer(row['bytes'], 1, maximum) and digest(value) == hex_string(row['sha256']), 'SOURCE_PIN')
    return name, value


def pin_row(name, value):
    return {'name': name, 'bytes': len(value), 'sha256': digest(value)}


def profile(path, wanted):
    value = raw(path, MAX_CODE)
    require(digest(value) == hex_string(wanted), 'HOST_PROFILE_PIN')
    result = strict_json(value)
    fields = {'format', 'profile_id', 'execution_hold', 'expected_parent', 'base_image', 'base_image_config',
              'code_files', 'input_count', 'input_total_bytes', 'host_tools', 'required_reports', 'extra_report_names'}
    require(type(result) is dict and set(result) == fields, 'HOST_PROFILE_SCHEMA')
    require(result['format'] == 'stock-and-sleeve:owned-data-host-profile:v1'
            and result['profile_id'] in ENTRIES, 'HOST_PROFILE_KIND')
    require(result['execution_hold'] is None, 'HOST_PROFILE_EXECUTION_HOLD')
    require(result['base_image'] == BASE and result['base_image_config'] == CONFIG, 'IMMUTABLE_BASE_SCOPE')
    hex_string(result['expected_parent'], 40)
    require(type(result['code_files']) is list and len(result['code_files']) == 6
            and type(result['host_tools']) is list and len(result['host_tools']) == 3, 'CODE_SOURCE_COUNT')
    require(result['input_count'] == INPUT_COUNTS[result['profile_id']], 'INPUT_COUNT')
    integer(result['input_total_bytes'], 1, MAX_INPUT_TOTAL)
    require(type(result['required_reports']) is list and set(result['required_reports']) == COMMON
            and len(result['required_reports']) == len(COMMON), 'COMMON_REPORT_SELECTION')
    extras = result['extra_report_names']
    require(type(extras) is list and len(extras) <= 64 and len(set(extras)) == len(extras), 'EXTRA_REPORT_BOUND')
    for name in extras:
        safe_name(name)
        require(name.endswith('.json') and name not in COMMON and not name.startswith('elf-'), 'EXTRA_REPORT_FORM')
    return result


def parse_sources(repo, spec):
    expected = {ENTRIES[spec['profile_id']], 'strict_debian_tar_data.py', 'ci_elf_data.py',
                'admission.json', 'input-spec.json', 'source-manifest.json'}
    code = {}
    for row in spec['code_files']:
        name, value = source_pin(repo, row, MAX_CODE)
        require(name in expected and name not in code, 'CODE_NAMES')
        code[name] = value
    require(set(code) == expected, 'EXACT_SIX_CODE_NAMES')
    tools = set()
    for row in spec['host_tools']:
        require(type(row) is dict and set(row) == {'repo_path', 'bytes', 'sha256'}, 'HOST_TOOL_PIN_SHAPE')
        name = relative(row['repo_path'])
        require(name in HOST_NAMES and name not in tools, 'HOST_TOOL_NAMES')
        value = raw(under(repo, name), 256 * 1024)
        require(len(value) == integer(row['bytes'], 1, 256 * 1024) and digest(value) == hex_string(row['sha256']), 'HOST_TOOL_PIN')
        tools.add(name)
    require(tools == HOST_NAMES, 'HOST_TOOL_SET')
    ledger = strict_json(code['source-manifest.json'])
    require(type(ledger) is dict and set(ledger) == {'format', 'profile_id', 'files'}
            and type(ledger['format']) is str and 1 <= len(ledger['format']) <= 128
            and ledger['profile_id'] == spec['profile_id'] and type(ledger['files']) is list
            and len(ledger['files']) == 5, 'SOURCE_LEDGER_SCHEMA')
    observed = {}
    for row in ledger['files']:
        require(type(row) is dict and set(row) == {'name', 'bytes', 'sha256'}, 'SOURCE_LEDGER_ROW')
        name = safe_name(row['name'])
        require(name != 'source-manifest.json' and name in code and name not in observed, 'SOURCE_LEDGER_NAMES')
        require(row == pin_row(name, code[name]), 'SOURCE_LEDGER_PIN')
        observed[name] = row
    require(set(observed) == set(code) - {'source-manifest.json'}, 'SOURCE_LEDGER_SET')
    admission = strict_json(code['admission.json'])
    require(type(admission) is dict and admission.get('profile_id') == spec['profile_id'], 'CONTAINER_ADMISSION_KIND')
    if spec['profile_id'] == 'debian-archives':
        require(admission.get('execution_hold', 'MISSING') is None
                and admission.get('bootstrap_scope', {}).get('admitted') is True, 'CONTAINER_ADMISSION_HOLD')
        require(admission.get('base_image') == BASE and admission.get('base_image_config') == CONFIG, 'CONTAINER_BASE_SCOPE')
    else:
        require(admission.get('execution_admission') == 'REVIEWED_DATA_ONLY_ROOTFS_JOB', 'CONTAINER_ADMISSION_HOLD')
        require(admission.get('bootstrap', {}).get('image') == BASE
                and admission.get('bootstrap', {}).get('config') == CONFIG, 'CONTAINER_BASE_SCOPE')
    inputs = strict_json(code['input-spec.json'])
    input_fields = {'format', 'profile_id', 'total_bytes', 'inputs'}
    if spec['profile_id'] == 'debian-archives':
        input_fields.add('metadata_only')
    require(type(inputs) is dict and set(inputs) == input_fields
            and type(inputs['format']) is str and 1 <= len(inputs['format']) <= 128
            and (spec['profile_id'] != 'debian-archives' or inputs['metadata_only'] is True)
            and inputs['profile_id'] == spec['profile_id'] and type(inputs['inputs']) is list
            and len(inputs['inputs']) == spec['input_count'], 'INPUT_SPEC_SCHEMA')
    names, ids, total = set(), set(), 0
    for row in inputs['inputs']:
        require(type(row) is dict and {'id', 'name', 'bytes', 'sha256', 'url', 'kind'} <= set(row), 'INPUT_SPEC_ROW')
        sha = hex_string(row['sha256'])
        require(row['name'] == sha + '.blob-data' and row['name'] not in names, 'INPUT_NAME_PIN')
        require(type(row['id']) is str and re.fullmatch('[A-Za-z0-9_.-]{1,96}', row['id'])
                and row['id'] not in ids, 'INPUT_ID_FORM')
        total += integer(row['bytes'], 1, MAX_INPUT)
        require(total <= MAX_INPUT_TOTAL, 'INPUT_AGGREGATE_BOUND')
        names.add(row['name']); ids.add(row['id'])
    require(total == integer(inputs['total_bytes'], 1, MAX_INPUT_TOTAL) == spec['input_total_bytes'], 'INPUT_TOTAL_PIN')
    return code, inputs


def fetcher():
    # Host-only public byte acquisition, never used inside the network-none job.
    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    token = None

    def request(url, bearer=None):
        headers = {'Accept-Encoding': 'identity', 'Accept': 'application/octet-stream,application/json'}
        if bearer:
            require(urllib.parse.urlsplit(url).hostname == 'registry-1.docker.io', 'TOKEN_REGISTRY_ONLY')
            headers['Authorization'] = 'Bearer ' + bearer
        return opener.open(urllib.request.Request(url, headers=headers), timeout=20)

    def get_token():
        url = 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/postgres:pull'
        with request(url) as response:
            require(response.status == 200, 'PUBLIC_TOKEN_STATUS')
            value = response.read(131073)
        require(len(value) <= 131072, 'PUBLIC_TOKEN_BOUND')
        value = strict_json(value)
        result = value.get('token')
        require(type(result) is str and 0 < len(result) <= 32768 and '\n' not in result, 'PUBLIC_TOKEN_FORM')
        return result

    def acquire(row, path):
        nonlocal token
        url = row['url']
        require(type(url) is str and 1 <= len(url) <= 4096, 'PUBLIC_URL_BOUND')
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'https' and parsed.port in (None, 443) and not parsed.username
                and not parsed.password and not parsed.fragment and not parsed.query, 'PUBLIC_URL_FORM')
        registry = parsed.hostname == 'registry-1.docker.io'
        if registry:
            require(parsed.path == '/v2/library/postgres/blobs/sha256:' + row['sha256'], 'REGISTRY_DIGEST_PATH')
            if token is None:
                token = get_token()
        else:
            require((parsed.hostname == 'deb.debian.org' and parsed.path.startswith(('/debian/', '/debian-security/')))
                    or (parsed.hostname == 'security.debian.org' and parsed.path.startswith('/debian-security/')),
                    'PUBLIC_DEBIAN_ENDPOINT')
        started = time.monotonic()
        try:
            response = request(url, token if registry else None)
        except urllib.error.HTTPError as error:
            require(registry and error.code == 307, 'PUBLIC_HTTP_FAILURE')
            location = error.headers.get('Location', '')
            error.close()
            target = urllib.parse.urlsplit(location)
            expected = '/registry-v2/docker/registry/v2/blobs/sha256/' + row['sha256'][:2] + '/' + row['sha256'] + '/data'
            require(target.scheme == 'https' and target.hostname == 'production.cloudfront.docker.com'
                    and target.port in (None, 443) and not target.username and not target.password
                    and not target.fragment and target.path == expected, 'CDN_LOCATION_SCOPE')
            query = urllib.parse.parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
            require(set(query) == {'Expires', 'Key-Pair-Id', 'Signature'}
                    and all(len(v) == 1 and v[0] for v in query.values()), 'CDN_QUERY_SCOPE')
            response = request(location)  # No bearer, cookies, proxy or default credentials.
        count = 0; checksum = hashlib.sha256()
        partial = path.with_name(path.name + '.partial')
        with response:
            require(response.status == 200 and not response.headers.get('Content-Encoding'), 'PUBLIC_BLOB_STATUS_ENCODING')
            length = response.headers.get('Content-Length')
            require(length is None or length == str(row['bytes']), 'PUBLIC_BLOB_HEADER_SIZE')
            fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                while True:
                    require(time.monotonic() - started <= 180, 'PUBLIC_BLOB_DEADLINE')
                    block = response.read(65536)
                    if not block:
                        break
                    count += len(block)
                    require(count <= row['bytes'], 'PUBLIC_BLOB_RECEIVED_BOUND')
                    checksum.update(block); stream.write(block)
                stream.flush(); os.fsync(stream.fileno())
        require(count == row['bytes'] and checksum.hexdigest() == row['sha256'], 'PUBLIC_BLOB_FINAL_PIN')
        require(not path.exists(), 'PUBLIC_BLOB_TARGET_EXISTS')
        partial.rename(path)
        return {'name': row['name'], 'bytes': count, 'sha256': checksum.hexdigest()}

    return acquire


def prepare(args):
    require(sys.platform == 'linux', 'LINUX_HOST_ONLY')
    repo, run = Path(args.repo), Path(args.run)
    require(repo.resolve(strict=True) == repo and run.resolve(strict=True) == run
            and not run.is_symlink() and run.stat().st_uid == os.getuid()
            and stat.S_IMODE(run.stat().st_mode) == 0o700, 'OWNED_RUN_ROOT')
    spec = profile(Path(args.profile), args.profile_sha)
    source_sha = hex_string(args.source_sha, 40)
    commit = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                            capture_output=True, timeout=15).stdout.decode().strip()
    parents = subprocess.run(['git', '-C', str(repo), 'rev-list', '--parents', '-n', '1', 'HEAD'],
                             check=True, capture_output=True, timeout=15).stdout.decode().strip().split()
    require(commit == source_sha and parents == [source_sha, spec['expected_parent']], 'SINGLE_PUBLIC_PARENT')
    code, inputs = parse_sources(repo, spec)
    stage = run / 'stage'
    require(stage.is_dir() and not stage.is_symlink() and not list(stage.iterdir()), 'FRESH_STAGE')
    (stage / 'code').mkdir(mode=0o700); (stage / 'input').mkdir(mode=0o700)
    for name, value in code.items():
        save(stage / 'code' / name, value)
    # Source and input specs are fully validated before the first HTTP request.
    acquire = fetcher()
    observed = [acquire(row, stage / 'input' / row['name']) for row in inputs['inputs']]
    # Plain COPY preserves safe regular-file modes. Keep directory search bits;
    # uniform COPY --chmod=0444 would also remove traversal from directories.
    for directory in (stage / 'code', stage / 'input'):
        for path in directory.iterdir():
            path.chmod(0o444)
        directory.chmod(0o555)
    entry = '/ci-code/' + ENTRIES[spec['profile_id']]
    dockerfile = ('FROM ' + BASE + '\nARG SS_DATA_OWNER\n'
                  'LABEL org.stockandsleeve.owned-data.owner=$SS_DATA_OWNER org.stockandsleeve.owned-data.role=image\n'
                  'COPY code/ /ci-code/\nCOPY input/ /ci-input/\n'
                  'WORKDIR /ci-code\nUSER 10001:10001\nENTRYPOINT ["python","-I","-S","-B","' + entry + '"]\nCMD []\n')
    save(stage / 'Dockerfile', dockerfile.encode())
    rules = ['**', '!code/', 'code/**', '!input/', 'input/**']
    rules += ['!code/' + name for name in sorted(code)]
    rules += ['!input/' + row['name'] for row in sorted(inputs['inputs'], key=lambda r: r['name'])]
    save(stage / 'Dockerfile.dockerignore', ('\n'.join(rules) + '\n').encode())
    control = {'format': 'stock-and-sleeve:owned-data-host-control:v1', 'profile_id': spec['profile_id'],
               'source_sha': source_sha, 'profile_sha256': args.profile_sha, 'entrypoint': entry,
               'source_manifest_sha256': digest(code['source-manifest.json']),
               'input_spec_sha256': digest(code['input-spec.json']),
               'code': [pin_row(n, code[n]) for n in sorted(code)], 'inputs': observed,
               'required_reports': spec['required_reports'], 'extra_report_names': spec['extra_report_names']}
    save(run / 'control.json', control)
    save(run / 'evidence' / 'host-prepare-proof.json', {'successful': True, 'profile_id': spec['profile_id'],
         'source_sha': source_sha, 'profile_sha256': args.profile_sha, 'code': control['code'], 'inputs': observed,
         'raw_input_published': False, 'native_vendor_execution': False})


def control(path):
    value = load(path)
    require(value.get('format') == 'stock-and-sleeve:owned-data-host-control:v1'
            and value.get('profile_id') in ENTRIES, 'CONTROL_SCHEMA')
    return value


def one_inspect(path):
    value = load(path, 2 * 1024 * 1024)
    require(type(value) is list and len(value) == 1 and type(value[0]) is dict, 'INSPECT_SINGLE_OBJECT')
    return value[0]


def label_check(value, owner, role):
    hex_string(owner, 32)
    labels = value.get('Config', {}).get('Labels') or {}
    require(labels.get(OWNER) == owner and labels.get(ROLE) == role, 'OWNED_RESOURCE_LABELS')


def check_image(args):
    c = control(Path(args.control)); base, image = one_inspect(Path(args.base)), one_inspect(Path(args.image))
    require(base.get('Id') == CONFIG and base.get('Os') == image.get('Os') == 'linux'
            and base.get('Architecture') == image.get('Architecture') == 'amd64'
            and BASE in base.get('RepoDigests', []), 'IMMUTABLE_BASE_IMAGE')
    label_check(image, args.owner, 'image')
    require(re.fullmatch('sha256:[a-f0-9]{64}', image.get('Id', '')), 'DERIVED_IMAGE_ID')
    layers = base.get('RootFS', {}).get('Layers')
    derived = image.get('RootFS', {}).get('Layers')
    require(type(layers) is list and layers and type(derived) is list
            and derived[:len(layers)] == layers and len(derived) > len(layers), 'BASE_LAYER_PREFIX')
    cfg = image['Config']
    require(cfg.get('User') == '10001:10001' and cfg.get('WorkingDir') == '/ci-code'
            and cfg.get('Entrypoint') == ['python', '-I', '-S', '-B', c['entrypoint']]
            and cfg.get('Cmd') in (None, []) and not cfg.get('Volumes') and not cfg.get('Healthcheck')
            and not cfg.get('OnBuild') and cfg.get('Env') == base['Config'].get('Env'), 'DERIVED_IMAGE_CONFIG')
    save(Path(args.control).parent / 'image-state.json', {'image_id': image['Id'], 'owner': args.owner,
         'profile_sha256': c['profile_sha256'], 'base_env': cfg.get('Env') or [], 'successful': True})


def env_map(rows):
    require(type(rows) is list, 'ENV_ARRAY')
    result = {}
    for row in rows:
        require(type(row) is str and '=' in row, 'ENV_ROW')
        name, value = row.split('=', 1)
        require(name not in result, 'ENV_DUPLICATE')
        result[name] = value
    return result


def check_container(args):
    c = control(Path(args.control)); image = load(Path(args.control).parent / 'image-state.json')
    value = one_inspect(Path(args.inspect)); label_check(value, args.owner, 'process')
    require(image['owner'] == args.owner and image['profile_sha256'] == c['profile_sha256']
            and value.get('Image') == image['image_id'], 'EXACT_PROCESS_IMAGE')
    cfg, host = value['Config'], value['HostConfig']
    require(cfg.get('User') == '10001:10001' and value.get('Path') == 'python'
            and value.get('Args') == ['-I', '-S', '-B', c['entrypoint']]
            and cfg.get('WorkingDir') == '/ci-code' and not cfg.get('Tty') and not cfg.get('OpenStdin'), 'FIXED_PROCESS_ARGV')
    expected_env = env_map(image['base_env'])
    require(not (set(expected_env) & ENV_KEYS), 'BASE_OVERRIDES_CONTROL_ENV')
    expected_env.update({'SS_DATA_SOURCE_SHA': c['source_sha'], 'SS_DATA_PROFILE_SHA256': c['profile_sha256'],
                         'SS_DATA_SOURCE_MANIFEST_SHA256': c['source_manifest_sha256'],
                         'SS_DATA_INPUT_SPEC_SHA256': c['input_spec_sha256'], 'SS_DATA_PROFILE_ID': c['profile_id']})
    require(env_map(cfg.get('Env')) == expected_env, 'EXACT_PROCESS_ENV')
    require(host.get('NetworkMode') == 'none' and host.get('ReadonlyRootfs') is True
            and host.get('CapDrop') == ['ALL']
            and host.get('SecurityOpt') in (['no-new-privileges=true'], ['no-new-privileges:true'], ['no-new-privileges'])
            and host.get('PidsLimit') == 128 and host.get('Memory') == host.get('MemorySwap') == 4294967296
            and host.get('NanoCpus') == 2000000000 and not host.get('Privileged') and not host.get('AutoRemove')
            and not host.get('Binds') and not host.get('Devices') and not host.get('DeviceRequests')
            and not host.get('PortBindings') and not host.get('PidMode') and not host.get('IpcMode') == 'host'
            and host.get('Tmpfs') == TMPFS, 'EXACT_PROCESS_RESOURCES')
    require(host.get('LogConfig') == {'Type': 'json-file', 'Config': {'max-size': '128m', 'max-file': '1'}}, 'BOUNDED_LOG_DRIVER')
    require(all(m.get('Type') == 'tmpfs' and m.get('Destination') in TMPFS for m in value.get('Mounts', [])), 'NO_HOST_MOUNTS')
    require(value.get('State', {}).get('Running') is False, 'PROCESS_STARTED_BEFORE_AUDIT')


def copied(root, expected):
    require(root.is_dir() and not root.is_symlink(), 'COPY_DIRECTORY')
    directory_mode = stat.S_IMODE(root.stat().st_mode)
    require(directory_mode & 0o555 == 0o555 and not directory_mode & 0o022, 'COPY_DIRECTORY_READ_SEARCH')
    actual = set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'COPY_LINK')
        require(path.is_file(), 'COPY_UNEXPECTED_DIRECTORY')
        actual.add(path.relative_to(root).as_posix())
    require(actual == {row['name'] for row in expected}, 'ACTUAL_COPY_NAMES')
    result = []
    for row in expected:
        path = root / row['name']
        file_mode = stat.S_IMODE(path.lstat().st_mode)
        require(file_mode == 0o444, 'ACTUAL_COPY_FILE_MODE')
        value = raw(path, MAX_INPUT, readonly=True)
        require(pin_row(row['name'], value) == row, 'ACTUAL_COPY_PIN')
        result.append(dict(row, mode=file_mode))
    return {'directory_mode': directory_mode, 'files': result}


def check_copy(args):
    c = control(Path(args.control))
    code = copied(Path(args.code), c['code']); inputs = copied(Path(args.input), c['inputs'])
    save(Path(args.out), {'format': 'stock-and-sleeve:owned-data-actual-copy-proof:v1',
         'profile_id': c['profile_id'], 'source_sha': c['source_sha'], 'code': code, 'inputs': inputs,
         'successful': True, 'process_started': False, 'archive_decoded': False})


def exit_code(args):
    value = one_inspect(Path(args.inspect)); label_check(value, args.owner, 'process')
    state = value.get('State', {})
    require(state.get('Running') is False and not state.get('OOMKilled') and not state.get('Dead')
            and not state.get('Error'), 'FINAL_PROCESS_STATE')
    print(integer(state.get('ExitCode'), 0, 255))


def collect(args):
    import zlib
    c = control(Path(args.control)); output = Path(args.out)
    log = raw(Path(args.log), MAX_LOG)
    require(output.is_dir() and not output.is_symlink(), 'EXPORT_DIRECTORY')
    offset = 0

    def line(maximum=8192):
        nonlocal offset
        end = log.find(b'\n', offset, min(len(log), offset + maximum + 1))
        require(end >= offset and end - offset <= maximum, 'FRAME_LINE_BOUND')
        value = log[offset:end]; offset = end + 1
        return value

    first = line()
    require(first.startswith(b'SS_DATA_JSON_STREAM '), 'MISSING_STREAM_PREFIX')
    require(strict_json(first[len(b'SS_DATA_JSON_STREAM '):]) ==
            {'profile_id': c['profile_id'], 'source_sha': c['source_sha']}, 'STREAM_IDENTITY')
    reports, headers, envelopes = {}, {}, {}
    total_raw = total_gzip = 0
    completion = None
    while offset < len(log):
        marker = line()
        if marker.startswith(b'SS_DATA_JSON_COMPLETE '):
            completion = strict_json(marker[len(b'SS_DATA_JSON_COMPLETE '):])
            require(offset == len(log), 'OUTPUT_AFTER_COMPLETION')
            break
        require(marker.startswith(b'SS_DATA_JSON_BEGIN ') and len(headers) < MAX_REPORTS, 'FRAME_BEGIN_OR_COUNT')
        header = strict_json(marker[len(b'SS_DATA_JSON_BEGIN '):])
        fields = {'name', 'encoding', 'decoded_bytes', 'decoded_sha256', 'compressed_bytes',
                  'compressed_sha256', 'payload_bytes', 'payload_sha256'}
        require(type(header) is dict and set(header) == fields and header['encoding'] == 'gzip+base64', 'FRAME_SCHEMA')
        name = safe_name(header['name'])
        require(name not in headers and (name in COMMON or name in c['extra_report_names']
                or re.fullmatch('elf-[a-f0-9]{64}\.json', name)), 'DERIVED_REPORT_ALLOWLIST')
        decoded_size = integer(header['decoded_bytes'], 2, MAX_RAW)
        gzip_size = integer(header['compressed_bytes'], 20, MAX_GZIP_TOTAL)
        payload_size = integer(header['payload_bytes'], 1, MAX_LOG)
        total_raw += decoded_size; total_gzip += gzip_size
        require(total_raw <= MAX_RAW_TOTAL and total_gzip <= MAX_GZIP_TOTAL, 'EXPORT_AGGREGATE_BOUND')
        require(offset + payload_size <= len(log), 'TRUNCATED_FRAME')
        payload = log[offset:offset + payload_size]; offset += payload_size
        require(payload.endswith(b'\n') and b'\r' not in payload
                and digest(payload) == hex_string(header['payload_sha256']), 'PAYLOAD_BYTES_PIN')
        compressed = base64.b64decode(payload.replace(b'\n', b''), validate=True)
        require(base64.encodebytes(compressed) == payload and len(compressed) == gzip_size
                and digest(compressed) == hex_string(header['compressed_sha256']), 'GZIP_PAYLOAD_PIN')
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(compressed, decoded_size + 1)
        require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
                and len(decoded) == decoded_size and digest(decoded) == hex_string(header['decoded_sha256']),
                'STRICT_GZIP_JSON_PIN')
        document = strict_json(decoded)
        require(type(document) is dict, 'DERIVED_JSON_OBJECT')
        end = line()
        require(end.startswith(b'SS_DATA_JSON_END ')
                and strict_json(end[len(b'SS_DATA_JSON_END '):]) == {'name': name}, 'FRAME_END')
        headers[name] = header
        if name in COMMON:
            require(document.get('profile_id') == c['profile_id'] and document.get('successful') is True,
                    'COMMON_RECEIPT_NOT_SUCCESSFUL')
            reports[name] = document
        # Retain the compressed JSON envelope, not uncompressed native vectors.
        envelopes[name] = {'format': 'stock-and-sleeve:derived-json-gzip-envelope:v1',
                           'profile_id': c['profile_id'], 'header': header,
                           'payload_b64_lines': payload.decode('ascii').splitlines(), 'raw_payload': False}
    require(type(completion) is dict and completion == {'profile_id': c['profile_id'],
            'reports': len(headers), 'successful': True} and set(reports) == COMMON, 'COMPLETE_REPORT_SET')
    manifest = reports['output-manifest.json']
    rows = manifest.get('derived_exports')
    require(type(rows) is list and len(rows) == len(headers) - 1, 'EXPORT_REGISTRATION_COUNT')
    registered = set()
    for row in rows:
        require(type(row) is dict and set(row) == {'name', 'decoded_bytes', 'decoded_sha256'}, 'EXPORT_REGISTRATION_ROW')
        name = safe_name(row['name'])
        require(name != 'output-manifest.json' and name in headers and name not in registered, 'EXPORT_REGISTRATION_NAMES')
        require(row['decoded_bytes'] == headers[name]['decoded_bytes']
                and row['decoded_sha256'] == headers[name]['decoded_sha256'], 'EXPORT_REGISTRATION_PIN')
        registered.add(name)
    require(registered == set(headers) - {'output-manifest.json'}, 'EXPORT_REGISTRATION_SET')
    names = []
    for name in sorted(envelopes):
        target = 'data-' + name
        # Keep public runner lines bounded; one escaped base64 string would put
        # an entire multi-MiB report on a single log line. Reconstruct payload
        # as ASCII '\n'.join(payload_b64_lines) + '\n' before checking its pin.
        encoded = (json.dumps(envelopes[name], ensure_ascii=True, sort_keys=True,
                              indent=2, allow_nan=False) + '\n').encode('ascii')
        require(all(len(part) <= 1024 for part in encoded.splitlines()), 'ENVELOPE_LINE_BOUND')
        save(output / target, encoded, MAX_LOG)
        names.append(target)
    for name in ('host-prepare-proof.json', 'actual-copy-proof.json'):
        value = load(output / name)
        require(value.get('successful') is True and value.get('profile_id') == c['profile_id'], 'HOST_PROOF_REQUIRED')
        names.append(name)
    save(output / 'export-files.txt', ('\n'.join(names) + '\n').encode())


def failure_details(args):
    """Publish only bounded known typed failures, never arbitrary private logs."""
    run = Path(args.run)
    require(run.resolve(strict=True) == run and run.is_dir() and not run.is_symlink()
            and run.stat().st_uid == os.getuid(), 'FAILURE_OWNED_RUN_ROOT')
    names = ('prepare.log', 'helper-field.log', 'helper-check-image.log',
             'helper-check-container.log', 'helper-check-copy.log',
             'helper-exit-code.log', 'helper-collect.log')
    host_codes, unavailable = [], []
    for name in names:
        path = run / name
        if not path.exists():
            continue
        try:
            value = strict_json(raw(path, 4096))
            require(type(value) is dict and set(value) == {'successful', 'code'}
                    and value['successful'] is False and type(value['code']) is str
                    and re.fullmatch('[A-Z][A-Z0-9_]{1,95}', value['code']), 'TYPED_HELPER_FAILURE_SCHEMA')
        except Exception:
            if path.stat().st_size:
                unavailable.append(name)
            continue
        host_codes.append({'operation_log': name, 'code': value['code']})
    profile_id, failures = 'unavailable', []
    if (run / 'control.json').exists():
        profile_id = control(run / 'control.json')['profile_id']
    container_log = run / 'container.log'
    if container_log.exists():
        log = raw(container_log, MAX_LOG)
        # Inspect only the final bounded line; earlier frame payloads remain
        # private unless the separate successful collector accepted all pins.
        end = len(log) - 1 if log.endswith(b'\n') else len(log)
        start = log.rfind(b'\n', max(0, end - 4096), end) + 1
        line = log[start:end] if 0 <= end - start <= 4096 else b''
        prefix = b'SS_DATA_PROFILE_FAILURE '
        if line.startswith(prefix):
            try:
                value = strict_json(line[len(prefix):])
                require(type(value) is dict and set(value) == {'profile_id', 'successful', 'error_type',
                        'error_code', 'raw_payload_or_exception_detail_printed'}
                        and value['profile_id'] == profile_id and value['successful'] is False
                        and value['raw_payload_or_exception_detail_printed'] is False
                        and type(value['error_type']) is str and re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', value['error_type'])
                        and type(value['error_code']) is str and re.fullmatch('[A-Z][A-Z0-9_]{0,127}', value['error_code']),
                        'TYPED_PROFILE_FAILURE_SCHEMA')
                failures.append(value)
            except Exception:
                unavailable.append('container-final-line')
        elif line.startswith(b'DATA_FAILURE_TYPE=') and profile_id == 'postgres-rootfs':
            value = line[len(b'DATA_FAILURE_TYPE='):].decode('ascii', errors='strict')
            if re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}', value):
                failures.append({'profile_id': profile_id, 'successful': False, 'error_type': value,
                                 'error_code': 'UNAVAILABLE_TYPE_ONLY',
                                 'raw_payload_or_exception_detail_printed': False})
    save(Path(args.out), {'format': 'stock-and-sleeve:owned-data-typed-failure-details:v1',
         'profile_id': profile_id, 'host_codes': host_codes, 'profile_failures': failures,
         'unrecognized_private_diagnostics': unavailable, 'raw_logs_or_paths_or_urls_published': False,
         'changes_terminal_result': False, 'scope': 'Diagnostic collection only; does not change terminal success predicates.'})


def parser():
    result = argparse.ArgumentParser(description='Owned Linux data host checks; no application entrypoint.')
    commands = result.add_subparsers(dest='command', required=True)
    shapes = {'prepare': ('profile', 'profile-sha', 'source-sha', 'repo', 'run'),
              'field': ('control', 'name'), 'check-image': ('control', 'base', 'image', 'owner'),
              'check-container': ('control', 'inspect', 'owner'),
              'check-copy': ('control', 'code', 'input', 'out'),
              'exit-code': ('inspect', 'owner'), 'collect': ('control', 'log', 'out'),
              'failure-details': ('run', 'out')}
    for name, fields in shapes.items():
        command = commands.add_parser(name)
        for field in fields:
            command.add_argument('--' + field, required=True)
    return result


def main():
    require(sys.platform == 'linux' and sys.flags.isolated and sys.flags.no_site
            and sys.flags.ignore_environment and sys.dont_write_bytecode, 'HOST_ISOLATED_STARTUP')
    args = parser().parse_args()
    # Host-only parser bounds are independent of the contained DATA process.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1073741824, 1073741824))
    file_limit = MAX_INPUT if args.command == 'prepare' else 67108864
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    if args.command == 'field':
        require(args.name in {'entrypoint', 'source_manifest_sha256', 'input_spec_sha256', 'profile_id'}, 'FIELD_ALLOWLIST')
        print(control(Path(args.control))[args.name])
    else:
        {'prepare': prepare, 'check-image': check_image, 'check-container': check_container,
         'check-copy': check_copy, 'exit-code': exit_code, 'collect': collect,
         'failure-details': failure_details}[args.command](args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Public URLs/tokens, arbitrary exception text and raw payloads never log.
        code = str(error) if isinstance(error, ControlFailure) and re.fullmatch('[A-Z][A-Z0-9_]{1,95}', str(error)) else 'OWNED_DATA_HOST_FAILURE'
        print(json.dumps({'successful': False, 'code': code}), file=sys.stderr)
        raise SystemExit(1)
