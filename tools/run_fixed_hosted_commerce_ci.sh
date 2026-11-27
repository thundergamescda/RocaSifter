#!/usr/bin/env bash
# AUTHOR-ONLY candidate. Run only after review/admission on GitHub ubuntu-24.04.
# A workflow-level timeout is still required. No Windows runtime is invoked.
set +x
set -Eeuo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'This harness requires the reviewed Linux CI host.'
for command in docker openssl timeout git realpath mktemp sha256sum sed base64 date wc python3; do
  command -v "$command" >/dev/null || die "Missing required CI tool: $command"
done
[[ -z "${DATABASE_URL+x}" ]] || die 'An external DATABASE_URL is not accepted.'
[[ "${SS_CI_SOURCE_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || die 'SS_CI_SOURCE_SHA must be the public checkout commit.'
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" ]] || die 'RUNNER_TEMP must be an existing owned CI temporary directory.'
[[ -O "$RUNNER_TEMP" ]] || die 'RUNNER_TEMP must be owned by the current CI user.'
build_seconds="${HOSTED_CI_BUILD_TIMEOUT_SECONDS:-600}"
test_seconds="${HOSTED_CI_TEST_TIMEOUT_SECONDS:-1200}"
[[ "$build_seconds" =~ ^[0-9]{1,4}$ && "$test_seconds" =~ ^[0-9]{1,4}$ ]] || die 'Invalid CI timeout.'
((10#$build_seconds >= 30 && 10#$build_seconds <= 600 &&
  10#$test_seconds >= 30 && 10#$test_seconds <= 3600)) || die 'CI timeout is outside the reviewed bounds.'
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_CI_SOURCE_SHA" ]] || die 'Checkout HEAD differs from SS_CI_SOURCE_SHA.'
[[ -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || die 'CI source checkout must be clean.'
# Strict stdlib JSON gate; SOURCE_ONLY_UNADMITTED refuses before any resources.
control_root="$source_root/tools/fixed_hosted_ci"
[[ -f "$control_root/admission.json" && ! -L "$control_root/admission.json" &&
   -f "$control_root/context-manifest.json" && ! -L "$control_root/context-manifest.json" ]] || die 'Fixed CI control data is required.'
context_sha256="$(python3 -I -S -B "$control_root/fixed_hosted_ci_context.py" --host-admission)" || die 'No reviewed fixed CI execution admission.'
[[ "$context_sha256" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid fixed CI context identity.'
host_wrapper_sha256="$(sha256sum -- "${BASH_SOURCE[0]}")"; host_wrapper_sha256="${host_wrapper_sha256%% *}"
admission_sha256="$(sha256sum -- "$control_root/admission.json")"; admission_sha256="${admission_sha256%% *}"
[[ "$host_wrapper_sha256" =~ ^[0-9a-f]{64}$ && "$admission_sha256" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid fixed CI control pins.'
temp_root="$(realpath -- "$RUNNER_TEMP")"
run_dir="$(mktemp -d "$temp_root/stock-sleeve-hosted-ci-XXXXXXXXXXXX")"
[[ "$(realpath -- "$run_dir")" == "$temp_root/"* ]] || die 'Invalid owned output directory.'
run_key="$(openssl rand -hex 16)"
[[ "$run_key" =~ ^[0-9a-f]{32}$ ]] || die 'Failed to create owned resource identity.'
owner_label='org.stockandsleeve.hosted-ci.owner'
role_label='org.stockandsleeve.hosted-ci.role'
network_name="ss-hosted-ci-$run_key"
runner_tag="ss-hosted-ci-runner:$run_key"
db_password=''
database_url=''
python_digest=''
python_id=''
postgres_digest=''
postgres_id=''
runner_id=''
network_id=''
db_id=''
test_id=''
test_exit='null'
cleanup_complete=true
capture_complete=true
delivery_complete=true
test_stopped=false
network_verified=false
declare -A capture_status=(
  [test_stop]=null [test_inspect]=null [test_logs]=null [test_sanitize]=null
  [test_raw_cleanup]=null [evidence_copy]=null [postgres_dir]=null [postgres_copy]=null
  [db_logs]=null [db_sanitize]=null [db_raw_cleanup]=null
)
mkdir "$run_dir/evidence"
# Host-only boundary: every docker subprocess resolves this private shim. Its
# interpreter starts with an empty environment; the actual Docker CLI receives
# only fixed local-engine/config/build values, never ambient context/auth.
docker_binary="$(realpath -- "$(command -v docker)")"
[[ "$docker_binary" =~ ^/[A-Za-z0-9_./-]+$ && "$docker_binary" != *..* &&
   -f "$docker_binary" && -x "$docker_binary" && ! -L "$docker_binary" ]] || die 'Invalid trusted Docker CLI path.'
mkdir -m 700 "$run_dir/docker-config" "$run_dir/docker-home" "$run_dir/docker-bin" "$run_dir/host-boundary-receipts"
printf '{"auths":{}}\n' > "$run_dir/docker-config/config.json"
chmod 400 "$run_dir/docker-config/config.json"
fixed_host_path='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
{
  printf '#!/usr/bin/env -S -i /bin/bash --noprofile --norc\nset -Eeuo pipefail\n'
  printf 'for argument in "$@"; do\n  case "$argument" in --host|--host=*|-H|-H*|--context|--context=*|--config|--config=*|--tls|--tls=*|--tlsverify|--tlsverify=*) exit 97 ;; esac\ndone\n'
  printf 'exec /usr/bin/env -i PATH=%q HOME=%q DOCKER_CONFIG=%q DOCKER_HOST=%q DOCKER_BUILDKIT=1 BUILDKIT_PROGRESS=plain %q "$@"\n' \
    "$fixed_host_path" "$run_dir/docker-home" "$run_dir/docker-config" \
    'unix:///var/run/docker.sock' "$docker_binary"
} > "$run_dir/docker-bin/docker"
chmod 500 "$run_dir/docker-bin/docker"
export PATH="$run_dir/docker-bin:$fixed_host_path"
engine_ready=false
boundary_helper="$run_dir/owned_host_boundary.py"
cat > "$boundary_helper" <<'SS_HOST_BOUNDARY_PY_SOURCE'
"""SOURCE ONLY host Docker metadata guard. No application/vendor imports."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import tempfile

OWNER = 'org.stockandsleeve.hosted-ci.owner'
ROLE = 'org.stockandsleeve.hosted-ci.role'
FIXED_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
LIMIT = 1048576


def require(value, code):
    if not value:
        raise ValueError(code)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_BOUNDARY_JSON')
        result[key] = value
    return result


def raw(path, maximum=LIMIT):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and not path.is_symlink()
            and before.st_nlink == 1 and 0 <= before.st_size <= maximum, 'BOUNDARY_FILE_FORM')
    value = path.read_bytes()
    after = path.lstat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            and len(value) == before.st_size, 'BOUNDARY_FILE_CHANGED')
    return value


def load(path):
    return json.loads(raw(path).decode('utf-8', errors='strict'),
                      object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, 'NONFINITE_BOUNDARY_JSON'))


def save(path, value):
    encoded = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    require(len(encoded) <= LIMIT and not path.exists(), 'BOUNDARY_OUTPUT_FRESH_BOUND')
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def fields(values):
    require(type(values) is list and len(values) <= 64, 'CONTAINER_ENV_BOUND')
    result = {}
    for row in values:
        require(type(row) is str and len(row) <= 32768 and '=' in row, 'CONTAINER_ENV_FORM')
        key, value = row.split('=', 1)
        require(re.fullmatch('[A-Z][A-Z0-9_]*', key) and key not in result, 'CONTAINER_ENV_KEY')
        result[key] = value
    return result


def main():
    require(sys.platform == 'linux' and sys.flags.isolated and sys.flags.no_site
            and sys.flags.ignore_environment and sys.dont_write_bytecode, 'HOST_BOUNDARY_STARTUP')
    resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('engine', 'created', 'snapshot', 'final'))
    for name in ('run', 'owner', 'source-sha', 'docker-binary'):
        parser.add_argument('--' + name, required=True)
    for name in ('network', 'db', 'test', 'postgres', 'runner'):
        parser.add_argument('--' + name, default='')
    args = parser.parse_args()
    require(re.fullmatch('[a-f0-9]{32}', args.owner)
            and re.fullmatch('[a-f0-9]{40}', args.source_sha), 'BOUNDARY_OWNER_SOURCE')
    run = Path(args.run)
    require(run.is_absolute() and run.resolve(strict=True) == run and not run.is_symlink()
            and run.stat().st_uid == os.getuid() and stat.S_IMODE(run.stat().st_mode) == 0o700,
            'BOUNDARY_RUN_OWNERSHIP')
    binary = Path(args.docker_binary)
    require(re.fullmatch('/[A-Za-z0-9_./-]+', str(binary)) and binary.resolve(strict=True) == binary
            and binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK),
            'BOUNDARY_DOCKER_BINARY')
    config = run / 'docker-config'
    home = run / 'docker-home'
    private_receipts = run / 'host-boundary-receipts'
    for directory in (config, home, private_receipts):
        require(directory.is_dir() and not directory.is_symlink()
                and directory.stat().st_uid == os.getuid()
                and stat.S_IMODE(directory.stat().st_mode) == 0o700, 'BOUNDARY_DOCKER_PRIVATE_DIRECTORY')
    require(load(config / 'config.json') == {'auths': {}}, 'BOUNDARY_EMPTY_AUTH_CONFIG')
    environment = {'PATH': FIXED_PATH, 'HOME': str(home), 'DOCKER_CONFIG': str(config),
                   'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_BUILDKIT': '1',
                   'BUILDKIT_PROGRESS': 'plain'}

    def docker(argv, parse=False):
        require(type(argv) is list and len(argv) <= 16, 'BOUNDARY_DOCKER_ARGS')
        # Temporary file plus inherited FSIZE/AS/NOFILE bounds prevents unbounded
        # subprocess stdout allocation. Raw inspect env/host paths stay private.
        with tempfile.TemporaryFile(dir=run) as output:
            subprocess.run([str(binary), *argv], env=environment, stdin=subprocess.DEVNULL,
                           stdout=output, stderr=subprocess.DEVNULL, check=True, timeout=15)
            output.seek(0)
            value = output.read(LIMIT + 1)
        require(len(value) <= LIMIT, 'BOUNDARY_DOCKER_OUTPUT_BOUND')
        text = value.decode('utf-8', errors='strict')
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: require(False, 'NONFINITE_BOUNDARY_JSON')) if parse else text

    def names(argv):
        result = docker(argv).splitlines()
        require(len(result) <= 4096 and len(set(result)) == len(result)
                and all(re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', name)
                        for name in result), 'BOUNDARY_DOCKER_NAME_LIST')
        return result

    baseline_file = run / 'initial-volume-names.json'
    if args.mode == 'engine':
        socket = Path('/var/run/docker.sock')
        require(socket.resolve(strict=True) == Path('/run/docker.sock')
                and stat.S_ISSOCK(socket.stat().st_mode), 'BOUNDARY_LOCAL_SOCKET')
        require(docker(['context', 'show']).strip() == 'default', 'BOUNDARY_DEFAULT_CONTEXT')
        context = docker(['context', 'inspect', 'default'], True)
        require(type(context) is list and len(context) == 1
                and context[0]['Endpoints']['docker']['Host'] == environment['DOCKER_HOST'],
                'BOUNDARY_LOCAL_CONTEXT_ENDPOINT')
        info = docker(['info', '--format', '{{json .}}'], True)
        require(info['OSType'] == 'linux' and info['Architecture'] in ('x86_64', 'amd64'),
                'BOUNDARY_ENGINE_PLATFORM')
        volumes = names(['volume', 'ls', '--quiet'])
        save(baseline_file, {'owner': args.owner, 'volumes': volumes})
        save(private_receipts / 'host-docker-boundary.json',
             {'format': 'stock-and-sleeve:owned-host-docker-boundary:v1', 'successful': True,
              'source_sha': args.source_sha, 'owner': args.owner,
              'endpoint': environment['DOCKER_HOST'], 'context': 'default',
              'fresh_empty_auth_config': True, 'docker_environment_keys': sorted(environment),
              'cli_sha256': hashlib.sha256(raw(binary, 134217728)).hexdigest(),
              'server_version': info['ServerVersion'], 'os': info['OSType'],
              'architecture': info['Architecture'], 'initial_volume_count': len(volumes),
              'actual_standard_disposable_CI_Docker_not_DATA_profile': True})
        return

    baseline = load(baseline_file)
    require(baseline['owner'] == args.owner, 'BOUNDARY_VOLUME_BASELINE_OWNER')
    initial_volumes = set(baseline['volumes'])

    def collect_mounts(container):
        volume_rows = []
        mounts = container.get('Mounts', [])
        require(type(mounts) is list and len(mounts) <= 8, 'BOUNDARY_MOUNT_COUNT')
        for mount in mounts:
            kind = mount.get('Type')
            require(kind in ('tmpfs', 'volume'), 'BOUNDARY_HOST_BIND_OR_SOCKET_MOUNT')
            require(mount.get('Destination') == '/var/lib/postgresql'
                    and container['Config']['Labels'].get(ROLE) == 'db', 'BOUNDARY_MOUNT_DESTINATION')
            if kind == 'volume':
                name = mount.get('Name', '')
                require(re.fullmatch('[a-f0-9]{64}', name) and name not in initial_volumes,
                        'BOUNDARY_NEW_ANONYMOUS_VOLUME_REQUIRED')
                volume_rows.append({'name': name, 'destination': mount['Destination'],
                                    'container_id': container['Id'], 'anonymous': True})
        return volume_rows

    if args.mode == 'created':
        for value in (args.network, args.db, args.test):
            require(re.fullmatch('[a-f0-9]{64}', value), 'BOUNDARY_RESOURCE_ID')
        for value in (args.postgres, args.runner):
            require(re.fullmatch('sha256:[a-f0-9]{64}', value), 'BOUNDARY_IMAGE_ID')
        network = docker(['network', 'inspect', args.network], True)[0]
        require(network['Id'] == args.network and network['Internal'] is True
                and network['Driver'] == 'bridge' and network['Scope'] == 'local'
                and network['Labels'].get(OWNER) == args.owner
                and network['Labels'].get(ROLE) == 'network', 'BOUNDARY_INTERNAL_OWNED_NETWORK')
        known = {args.db, args.test}
        require(set(network.get('Containers', {})) <= known, 'BOUNDARY_UNOWNED_NETWORK_MEMBER')
        receipts, volumes = [], []
        for role, identifier, image_id in (('db', args.db, args.postgres), ('test', args.test, args.runner)):
            container = docker(['inspect', '--type', 'container', identifier], True)[0]
            host = container['HostConfig']
            require(container['Id'] == identifier and container['Image'] == image_id
                    and container['Config']['Labels'].get(OWNER) == args.owner
                    and container['Config']['Labels'].get(ROLE) == role
                    and container['State']['Running'] is False, 'BOUNDARY_OWNED_CREATED_CONTAINER')
            require(host.get('Privileged') is False and not host.get('Binds')
                    and not host.get('Devices') and not host.get('DeviceRequests')
                    and not host.get('VolumesFrom') and not host.get('Links')
                    and not host.get('ExtraHosts')
                    and not host.get('PortBindings') and host.get('PublishAllPorts') is False
                    and host.get('NetworkMode') == args.network
                    and all(host.get(key, '') not in ('host',) for key in
                            ('PidMode', 'IpcMode', 'UTSMode', 'UsernsMode'))
                    and not host.get('PidMode', '').startswith('container:')
                    and not host.get('IpcMode', '').startswith('container:'),
                    'BOUNDARY_PRIVILEGED_HOST_NAMESPACE_PORT_OR_MOUNT')
            attachments = container['NetworkSettings'].get('Networks', {})
            require(len(attachments) <= 1
                    and all(row.get('NetworkID', '') in ('', args.network)
                            for row in attachments.values())
                    and all(not binding for binding in
                            (container['NetworkSettings'].get('Ports') or {}).values()),
                    'BOUNDARY_ONLY_OWNED_NETWORK')
            expected_memory = 2147483648 if role == 'db' else 4294967296
            require(host['Memory'] == expected_memory and host['NanoCpus'] == 2000000000,
                    'BOUNDARY_MEMORY_CPU_LIMIT')
            require(container['Config']['User'] == ('0:0' if role == 'db' else '10001:10001'),
                    'BOUNDARY_EXPECTED_CONTAINER_USER')
            if role == 'test':
                require(set(host.get('CapDrop') or []) == {'ALL'}
                        and not host.get('CapAdd')
                        and any(value in ('no-new-privileges', 'no-new-privileges:true', 'no-new-privileges=true')
                                for value in host.get('SecurityOpt') or []), 'BOUNDARY_TEST_CAPS_NNP')
                require(not host.get('Tmpfs'), 'BOUNDARY_TEST_UNDECLARED_TMPFS')
            else:
                require(not host.get('CapAdd') and not host.get('CapDrop'),
                        'BOUNDARY_PG_DEFAULT_CAPS_ONLY')
                require(host.get('Tmpfs') in (
                    {'/var/lib/postgresql': 'rw,nosuid,nodev,size=2g'},
                    {'/var/lib/postgresql': 'rw,nosuid,nodev,size=2147483648'}),
                        'BOUNDARY_PG_DECLARED_TMPFS')
            # Compare complete environment to the exact image defaults plus the
            # locally generated fixture env-file; publish only keys/boolean.
            image = docker(['image', 'inspect', image_id], True)[0]
            defaults = fields(image['Config'].get('Env') or [])
            fixture = fields(raw(run / ('db.env' if role == 'db' else 'test.env'), 65536)
                             .decode('ascii', errors='strict').splitlines())
            expected_keys = {'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB', 'POSTGRES_INITDB_ARGS'} if role == 'db' else {
                'DATABASE_URL', 'SS_CI_SOURCE_SHA', 'PYTHONDONTWRITEBYTECODE',
                'PYTHONUNBUFFERED', 'PSYCOPG_IMPL', 'SS_FIXED_HOSTED_INTERNAL_NETWORK',
                'SS_FIXED_HOSTED_CONTEXT_SHA256'}
            require(set(fixture) == expected_keys, 'BOUNDARY_FIXTURE_ENV_KEYS')
            expected_env = dict(defaults, **fixture)
            require(fields(container['Config'].get('Env') or []) == expected_env,
                    'BOUNDARY_COMPLETE_EXPECTED_CONTAINER_ENV')
            password = fields(raw(run / 'db.env', 65536).decode('ascii', errors='strict').splitlines())['POSTGRES_PASSWORD']
            require(re.fullmatch('[a-f0-9]{48}', password), 'BOUNDARY_FICTIONAL_CREDENTIAL')
            if role == 'db':
                require(fixture['POSTGRES_USER'] == 'postgres'
                        and fixture['POSTGRES_DB'] == 'ss_ci_' + args.owner[:16]
                        and fixture['POSTGRES_INITDB_ARGS'] == '--auth-host=scram-sha-256',
                        'BOUNDARY_FICTIONAL_DATABASE')
            else:
                expected_url = 'postgresql://postgres:' + password + '@db:5432/ss_ci_' + args.owner[:16] + '?sslmode=require'
                require(fixture['DATABASE_URL'] == expected_url
                        and fixture['SS_CI_SOURCE_SHA'] == args.source_sha
                        and fixture['PSYCOPG_IMPL'] == 'binary'
                        and fixture['SS_FIXED_HOSTED_INTERNAL_NETWORK'] == '1',
                        'BOUNDARY_FICTIONAL_INTERNAL_DATABASE_URL')
            volumes.extend(collect_mounts(container))
            receipts.append({'id': identifier, 'role': role, 'image_id': image_id,
                             'memory_bytes': host['Memory'], 'nano_cpus': host['NanoCpus'],
                             'user': container['Config']['User'], 'privileged': False,
                             'no_explicit_host_pid_ipc_uts_or_shared_pid_ipc_options_binds_socket_ports': True,
                             'actual_userns_mode': host.get('UsernsMode'),
                             'actual_cgroupns_mode': host.get('CgroupnsMode'),
                             'actual_kernel_namespace_separation_measured': False,
                             'default_user_cgroup_namespace_modes_trusted_for_CI': True,
                             'network_mode': host['NetworkMode'],
                             'fixture_environment_keys': sorted(fixture),
                             'complete_environment_matches_image_plus_fixture': True,
                             'pg_root_default_caps_explicitly_trusted': role == 'db',
                             'read_only_root_implemented': bool(host.get('ReadonlyRootfs')),
                             'pids_limit_actual': host.get('PidsLimit')})
        save(run / 'owned-volume-observations.json', {'owner': args.owner, 'volumes': volumes})
        save(private_receipts / 'actual-container-boundaries.json',
             {'format': 'stock-and-sleeve:owned-hosted-container-boundaries:v1',
              'successful': True, 'source_sha': args.source_sha, 'owner': args.owner,
              'network_id': args.network, 'internal': True, 'containers': receipts,
              'anonymous_volumes': volumes, 'initial_volume_count': len(initial_volumes),
              'not_a_production_or_DATA_hardening_claim': True})
        return

    if args.mode == 'snapshot':
        identifiers = names(['ps', '-aq', '--no-trunc', '--filter', 'label=' + OWNER + '=' + args.owner])
        volumes = []
        for identifier in identifiers:
            require(re.fullmatch('[a-f0-9]{64}', identifier), 'BOUNDARY_SNAPSHOT_CONTAINER_ID')
            value = docker(['inspect', '--type', 'container', identifier], True)[0]
            require(value['Id'] == identifier and value['Config']['Labels'].get(OWNER) == args.owner
                    and value['Config']['Labels'].get(ROLE) in ('db', 'test'), 'BOUNDARY_SNAPSHOT_OWNERSHIP')
            volumes.extend(collect_mounts(value))
        save(run / 'cleanup-volume-snapshot.json', {'owner': args.owner, 'volumes': volumes})
        return

    containers = names(['ps', '-aq', '--no-trunc', '--filter', 'label=' + OWNER + '=' + args.owner])
    networks = names(['network', 'ls', '--quiet', '--no-trunc', '--filter', 'label=' + OWNER + '=' + args.owner])
    images = names(['image', 'ls', '--quiet', '--no-trunc', '--filter', 'label=' + OWNER + '=' + args.owner])
    current_volumes = set(names(['volume', 'ls', '--quiet']))
    created_complete = (run / 'owned-resource-creation-complete').is_file()
    mutation_started = (run / 'owned-resource-mutation-started').is_file()
    safe = (not containers and not networks and not images
            and current_volumes == initial_volumes
            and (not mutation_started or created_complete))
    save(private_receipts / 'owned-resource-cleanup.json',
         {'format': 'stock-and-sleeve:owned-hosted-final-resource-absence:v1',
          'successful': safe, 'source_sha': args.source_sha, 'owner': args.owner,
          'remaining_owned_container_ids': containers, 'remaining_owned_network_ids': networks,
          'remaining_owned_image_ids': images,
          'initial_volume_count': len(initial_volumes),
          'final_volume_count': len(current_volumes),
          'new_or_missing_volume_count': len(current_volumes ^ initial_volumes),
          'new_or_missing_volume_name_sha256': sorted(hashlib.sha256(value.encode('utf-8')).hexdigest()
                                                    for value in current_volumes ^ initial_volumes),
          'volume_namespace_restored': current_volumes == initial_volumes,
          'resource_creation_fully_acknowledged': created_complete,
          'mutation_started': mutation_started,
          'inspection_errors_are_not_absence': True,
          'unknown_resources_not_deleted': True})
    # Container evidence has already been copied. A container-created colliding
    # filename fails exclusive publication instead of replacing a host receipt.
    for name in ('host-docker-boundary.json', 'actual-container-boundaries.json',
                 'owned-resource-cleanup.json'):
        receipt = private_receipts / name
        if receipt.exists():
            save(run / 'evidence' / name, load(receipt))
    require(safe, 'BOUNDARY_RESOURCES_REMAIN_OR_CREATION_UNCERTAIN')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        code = str(error)
        if not re.fullmatch('[A-Z][A-Z0-9_]{0,127}', code):
            code = 'BOUNDARY_REDACTED_FAILURE'
        print(json.dumps({'successful': False, 'code': code}, separators=(',', ':')))
        raise SystemExit(1)

SS_HOST_BOUNDARY_PY_SOURCE
chmod 400 "$boundary_helper"
boundary_helper_sha256='569522adf684c0e6073590f19054a78809aa3384b0f5ceb3d86f5a4f45b8cc79'
boundary() {
  local mode="$1" observed ignored_suffix
  [[ "$mode" == engine || "$mode" == created || "$mode" == snapshot || "$mode" == final ]] || return 1
  read -r observed ignored_suffix < <(sha256sum -- "$boundary_helper") || return 1
  [[ "$observed" == "$boundary_helper_sha256" ]] || return 1
  timeout --signal=TERM --kill-after=10s 100s python3 -I -S -B "$boundary_helper" "$mode" \
    --run "$run_dir" --owner "$run_key" --source-sha "$SS_CI_SOURCE_SHA" \
    --docker-binary "$docker_binary" --network "$network_id" --db "$db_id" --test "$test_id" \
    --postgres "$postgres_id" --runner "$runner_id" > "$run_dir/host-boundary-$mode.log" 2>&1
}
required_evidence=(
  hosted-ci-summary.json postgres-summary.json postgres-tests.log
  unit-identity-summary.json unit-identity-tests.log
  unit-provider-ids-summary.json unit-provider-ids-tests.log
  unit-square-status-summary.json unit-square-status-tests.log
  unit-revocation-guards-summary.json unit-revocation-guards-tests.log
  unit-cleanup-square-summary.json unit-cleanup-square-tests.log
  unit-staff-recovery-startup-summary.json unit-staff-recovery-startup-tests.log
  unit-capture-privacy-summary.json unit-capture-privacy-tests.log
  unit-square-sku-lookup-summary.json unit-square-sku-lookup-tests.log
  unit-label-schema-summary.json
  unit-label-schema-tests.log
  copied-source-before.json copied-source-after.json tls-client-probe.json
  pip-freeze.txt pip-bootstrap-report.json pip-install-report.json
  fixed-context-audit.json fixed-provenance-guards.json fixed-preimport-proof.json
  fixed-driver-runtime.json fixed-entry-summary.json
  fixed-pg-child/fixed-context-audit.json fixed-pg-child/fixed-provenance-guards.json
  fixed-pg-child/fixed-preimport-proof.json fixed-pg-child/fixed-driver-runtime.json
  fixed-pg-child/fixed-entry-summary.json
  fixed-gunicorn-smoke.json
  fixed-gunicorn-child/fixed-context-audit.json fixed-gunicorn-child/fixed-provenance-guards.json
  fixed-gunicorn-child/fixed-preimport-proof.json fixed-gunicorn-child/fixed-driver-runtime.json
  fixed-gunicorn-child/fixed-entry-summary.json
  host-docker-boundary.json actual-container-boundaries.json owned-resource-cleanup.json
)

sanitize() {
  if [[ -n "$db_password" ]]; then
    sed -e "s|$database_url|[redacted fixture database]|g" \
        -e "s|$db_password|[redacted fixture password]|g" "$1"
  else
    cat -- "$1"
  fi
}

transport_path="$source_root/tools/ci_evidence_transport.sh"
[[ -f "$transport_path" && ! -L "$transport_path" && -r "$transport_path" ]] ||
  die 'The exact checkout evidence transport helper is required.'
source "$transport_path"

owned_container() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]] || return 1
  local found
  found="$(timeout 10s docker inspect --type container \
    --format "{{.Id}}|{{index .Config.Labels \"$owner_label\"}}|{{index .Config.Labels \"$role_label\"}}" "$1" 2>/dev/null)" || return 1
  [[ "$found" == "$1|$run_key|db" || "$found" == "$1|$run_key|test" ]]
}

capture() {
  local id="$1" role="$2" code
  [[ "$role" == test || "$role" == db ]] || return 1
  [[ -n "$id" ]] && owned_container "$id" || return 1
  timeout 15s docker logs "$id" > "$run_dir/$role.raw.log" 2>&1
  capture_status[${role}_logs]="$?"
  sanitize "$run_dir/$role.raw.log" > "$run_dir/$role.log"
  capture_status[${role}_sanitize]="$?"
  rm -f -- "$run_dir/$role.raw.log"
  capture_status[${role}_raw_cleanup]="$?"
  if [[ "$role" == test ]]; then
    [[ "$test_stopped" == true ]] || return 1
    timeout 20s docker cp "$id:/ci-evidence/." "$run_dir/evidence/" > "$run_dir/copy-evidence.log" 2>&1
    capture_status[evidence_copy]="$?"
    mkdir -p "$run_dir/evidence/postgres-runner"
    code="$?"
    capture_status[postgres_dir]="$code"
    if ((code == 0)); then
      timeout 20s docker cp "$id:/workspace/Testing/oauth-receiving/." "$run_dir/evidence/postgres-runner/" \
        > "$run_dir/copy-postgres.log" 2>&1
      capture_status[postgres_copy]="$?"
    fi
  fi
  [[ "${capture_status[${role}_logs]}" == 0 &&
     "${capture_status[${role}_sanitize]}" == 0 &&
     "${capture_status[${role}_raw_cleanup]}" == 0 ]]
}

write_harness_summary() {
  printf '{"scope":"Linux hosted API unit and SQL fixture acceptance only; not Windows/native/signing acceptance","source_sha":"%s","owner":"%s","python_tag":"python:3.12.16-slim","python_digest":"%s","python_image_id":"%s","postgres_tag":"postgres:18.7","postgres_digest":"%s","postgres_image_id":"%s","runner_image_id":"%s","runtime_network_internal":%s,"test_container_exit_code":%s,"cleanup_complete":%s,"capture_complete":%s,"capture_status":{"test_stop":%s,"test_inspect":%s,"test_logs":%s,"test_sanitize":%s,"test_raw_cleanup":%s,"evidence_copy":%s,"postgres_dir":%s,"postgres_copy":%s,"db_logs":%s,"db_sanitize":%s,"db_raw_cleanup":%s},"delivery_complete":%s,"harness_exit_code":%s,"fixed_context_sha256":"%s","fixed_hostwrapper_sha256":"%s","admission_sha256":"%s"}\n' \
    "$SS_CI_SOURCE_SHA" "$run_key" "$python_digest" "$python_id" "$postgres_digest" "$postgres_id" \
    "$runner_id" "$network_verified" "$test_exit" "$cleanup_complete" "$capture_complete" \
    "${capture_status[test_stop]}" "${capture_status[test_inspect]}" \
    "${capture_status[test_logs]}" "${capture_status[test_sanitize]}" \
    "${capture_status[test_raw_cleanup]}" "${capture_status[evidence_copy]}" \
    "${capture_status[postgres_dir]}" "${capture_status[postgres_copy]}" \
    "${capture_status[db_logs]}" "${capture_status[db_sanitize]}" \
    "${capture_status[db_raw_cleanup]}" "$delivery_complete" "$1" "$context_sha256" "$host_wrapper_sha256" "$admission_sha256" > "$run_dir/harness-summary.json"
}

finish() {
  local status="$1" id found code name
  trap - EXIT INT TERM
  set +e
  # No Docker cleanup/query is issued unless the fresh local binding passed.
  if [[ "$engine_ready" == true ]]; then
    boundary snapshot || cleanup_complete=false
  # First stop only the verified owned test container, preserving failure files.
  if [[ -n "$test_id" ]] && owned_container "$test_id"; then
    timeout 25s docker stop --time 10 "$test_id" > "$run_dir/stop-test.log" 2>&1
    capture_status[test_stop]="$?"
    found="$(timeout 10s docker inspect --format '{{.State.Running}}|{{.State.ExitCode}}' "$test_id" 2>/dev/null)"
    capture_status[test_inspect]="$?"
    code="${found#false|}"
    if [[ "${capture_status[test_inspect]}" == 0 && "$found" == "false|$code" &&
          "$code" =~ ^[0-9]{1,3}$ ]]; then
      test_stopped=true
      test_exit="$code"
    fi
  fi
  capture "$test_id" test || capture_complete=false
  capture "$db_id" db || capture_complete=false
  for name in "${!capture_status[@]}"; do
    [[ "${capture_status[$name]}" == 0 ]] || capture_complete=false
  done
  # Reconcile only this unpredictable owner label after interrupted create calls.
  if ! timeout 10s docker ps -aq --no-trunc --filter "label=$owner_label=$run_key" \
      > "$run_dir/owned-containers.txt" 2>> "$run_dir/cleanup.log"; then cleanup_complete=false; fi
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    if owned_container "$id"; then
      timeout 25s docker rm --force --volumes "$id" >> "$run_dir/cleanup.log" 2>&1 || cleanup_complete=false
    else cleanup_complete=false
    fi
  done < "$run_dir/owned-containers.txt"
  if ! timeout 10s docker network ls --no-trunc --quiet --filter "label=$owner_label=$run_key" \
      > "$run_dir/owned-networks.txt" 2>> "$run_dir/cleanup.log"; then cleanup_complete=false; fi
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    found="$(timeout 10s docker network inspect --format \
      "{{.Id}}|{{index .Labels \"$owner_label\"}}|{{index .Labels \"$role_label\"}}" "$id" 2>/dev/null)"
    if [[ "$found" == "$id|$run_key|network" ]]; then
      timeout 15s docker network rm "$id" >> "$run_dir/cleanup.log" 2>&1 || cleanup_complete=false
    else cleanup_complete=false
    fi
  done < "$run_dir/owned-networks.txt"
  if ! timeout 10s docker image ls --no-trunc --quiet --filter "label=$owner_label=$run_key" \
      > "$run_dir/owned-images.txt" 2>> "$run_dir/cleanup.log"; then cleanup_complete=false; fi
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    found="$(timeout 10s docker image inspect --format \
      "{{.Id}}|{{index .Config.Labels \"$owner_label\"}}|{{index .Config.Labels \"$role_label\"}}" "$id" 2>/dev/null)"
    if [[ "$id" =~ ^sha256:[0-9a-f]{64}$ && "$found" == "$id|$run_key|runner" ]]; then
      timeout 20s docker image rm "$id" >> "$run_dir/cleanup.log" 2>&1 || cleanup_complete=false
    else cleanup_complete=false
    fi
  done < "$run_dir/owned-images.txt"
    # Successful fresh queries, not inspect-errors-as-absence. Preserve any
    # uncertainty from interrupted resource creation instead of false cleanup.
    boundary final || cleanup_complete=false
  else
    cleanup_complete=false
  fi
  [[ "$cleanup_complete" == true ]] || status=1
  [[ "$capture_complete" == true ]] || status=1
  [[ "$test_exit" == 0 ]] || status=1
  if ((status == 0)); then
    for name in "${required_evidence[@]}"; do
      [[ -s "$run_dir/evidence/$name" ]] || status=1
    done
  fi
  rm -f -- "$run_dir/db.env" "$run_dir/test.env" "$run_dir/hosted-ci-server.key" || status=1
  for name in "${required_evidence[@]}"; do
    emit_file "evidence/$name" "$run_dir/evidence/$name" || { status=1; delivery_complete=false; }
  done
  if ((status != 0)); then
    for name in runner.stdout.log runner.stderr.log; do
      if [[ -e "$run_dir/evidence/$name" || -L "$run_dir/evidence/$name" ]]; then
        emit_file "evidence/$name" "$run_dir/evidence/$name" || { status=1; delivery_complete=false; }
      fi
    done
    for name in pull-python.log pull-postgres.log build.log readiness.log db.log test.log \
      tls-generation.log start-db.log start-test.log stop-test.log copy-evidence.log \
      copy-postgres.log cleanup.log test-wait-exit.txt owned-containers.txt \
      owned-networks.txt owned-images.txt host-boundary-engine.log host-boundary-created.log \
      host-boundary-snapshot.log host-boundary-final.log; do
      if [[ -e "$run_dir/$name" || -L "$run_dir/$name" ]]; then
        emit_file "diagnostics/$name" "$run_dir/$name" || { status=1; delivery_complete=false; }
      fi
    done
  fi
  printf 'Owned evidence directory: %s\n' "$run_dir" || { status=1; delivery_complete=false; }
  if write_harness_summary "$status"; then
    if ! emit_file harness-summary.json "$run_dir/harness-summary.json"; then
      status=1
      delivery_complete=false
      write_harness_summary "$status" || status=1
    fi
  else
    status=1
    delivery_complete=false
    emit_problem harness-summary.json summary_write_failed || status=1
  fi
  exit "$status"
}
trap 'finish "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

boundary engine || die 'Fresh local Docker boundary failed.'
engine_ready=true

db_password="$(openssl rand -hex 24)"
[[ "$db_password" =~ ^[0-9a-f]{48}$ ]] || die 'Failed to generate fixture credential.'
database_name="ss_ci_${run_key:0:16}"
database_url="postgresql://postgres:$db_password@db:5432/$database_name?sslmode=require"
printf 'POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=%s\nPOSTGRES_INITDB_ARGS=--auth-host=scram-sha-256\n' \
  "$db_password" "$database_name" > "$run_dir/db.env"
printf 'DATABASE_URL=%s\nSS_CI_SOURCE_SHA=%s\nPYTHONDONTWRITEBYTECODE=1\nPYTHONUNBUFFERED=1\nPSYCOPG_IMPL=binary\nSS_FIXED_HOSTED_INTERNAL_NETWORK=1\nSS_FIXED_HOSTED_CONTEXT_SHA256=%s\n' \
  "$database_url" "$SS_CI_SOURCE_SHA" "$context_sha256" > "$run_dir/test.env"
timeout 20s openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -subj '/CN=db' -addext 'subjectAltName=DNS:db' \
  -keyout "$run_dir/hosted-ci-server.key" -out "$run_dir/hosted-ci-server.crt" > "$run_dir/tls-generation.log" 2>&1
chmod 600 "$run_dir/hosted-ci-server.key"

# Dependency fetches finish before the private runtime network exists.
build_deadline=$((SECONDS + 10#$build_seconds))
build_step() {
  local remaining=$((build_deadline - SECONDS))
  if ((remaining <= 0)); then
    printf 'Shared image pull/build deadline expired.\n' >&2 || return 1
    return 124
  fi
  timeout --signal=TERM --kill-after=10s "$remaining" "$@"
}
build_step docker pull python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b > "$run_dir/pull-python.log" 2>&1
build_step docker pull postgres@sha256:ec003ee60c23d320d1b81868f255a6fc724ac06d8f1ced4468532f7d8593f357 > "$run_dir/pull-postgres.log" 2>&1
python_id="$(build_step docker image inspect --format '{{.Id}}' python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b)"
python_digest="$(build_step docker image inspect --format '{{index .RepoDigests 0}}' python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b)"
postgres_id="$(build_step docker image inspect --format '{{.Id}}' postgres@sha256:ec003ee60c23d320d1b81868f255a6fc724ac06d8f1ced4468532f7d8593f357)"
postgres_digest="$(build_step docker image inspect --format '{{index .RepoDigests 0}}' postgres@sha256:ec003ee60c23d320d1b81868f255a6fc724ac06d8f1ced4468532f7d8593f357)"
[[ "$python_id" =~ ^sha256:[0-9a-f]{64}$ && "$postgres_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'Invalid resolved image IDs.'
[[ "$python_digest" =~ ^(docker.io/library/)?python@sha256:[0-9a-f]{64}$ &&
   "$postgres_digest" =~ ^(docker.io/library/)?postgres@sha256:[0-9a-f]{64}$ ]] || die 'Missing official image digests.'
[[ "$python_id" == sha256:78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db &&
   "$postgres_id" == sha256:22fac36945da9a4145a11bc9a6198ebc00975b8323c8d454740e477c4e84593a ]] || die 'Immutable platform image config mismatch.'
[[ "$python_digest" == python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b ||
   "$python_digest" == docker.io/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b ]] || die 'Python digest mismatch.'
[[ "$postgres_digest" == postgres@sha256:ec003ee60c23d320d1b81868f255a6fc724ac06d8f1ced4468532f7d8593f357 ||
   "$postgres_digest" == docker.io/library/postgres@sha256:ec003ee60c23d320d1b81868f255a6fc724ac06d8f1ced4468532f7d8593f357 ]] || die 'PostgreSQL digest mismatch.'
printf 'started\n' > "$run_dir/owned-resource-mutation-started"
build_step docker build --pull=false --build-arg "PYTHON_IMAGE=$python_digest" --build-arg "SS_CI_SOURCE_SHA=$SS_CI_SOURCE_SHA" --build-arg "SS_FIXED_HOSTED_CONTEXT_SHA256=$context_sha256" \
  --label "$owner_label=$run_key" --label "$role_label=runner" \
  --tag "$runner_tag" --file "$source_root/tools/fixed_hosted_ci/Dockerfile" "$source_root" > "$run_dir/build.log" 2>&1
runner_id="$(build_step docker image inspect --format '{{.Id}}' "$runner_tag")"
[[ "$runner_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'Invalid owned runner image ID.'
network_id="$(timeout 20s docker network create --internal --label "$owner_label=$run_key" \
  --label "$role_label=network" "$network_name")"
[[ "$network_id" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid owned network ID.'
[[ "$(docker network inspect --format '{{.Internal}}' "$network_id")" == true ]] || die 'Runtime network is not internal.'
network_verified=true

db_id="$(timeout 20s docker create --name "ss-hosted-db-$run_key" --network "$network_id" \
  --network-alias db --label "$owner_label=$run_key" --label "$role_label=db" \
  --env-file "$run_dir/db.env" --user 0:0 --memory 2g --cpus 2 --shm-size 256m \
  --tmpfs /var/lib/postgresql:rw,nosuid,nodev,size=2g \
  --entrypoint /bin/sh "$postgres_id" -ceu \
  'chown postgres:postgres /tmp/hosted-ci-server.key /tmp/hosted-ci-server.crt
   chmod 600 /tmp/hosted-ci-server.key
   chmod 644 /tmp/hosted-ci-server.crt
   exec /usr/local/bin/docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/hosted-ci-server.crt -c ssl_key_file=/tmp/hosted-ci-server.key')"
owned_container "$db_id" || die 'Database ownership check failed.'
timeout 20s docker cp "$run_dir/hosted-ci-server.key" "$db_id:/tmp/hosted-ci-server.key"
timeout 20s docker cp "$run_dir/hosted-ci-server.crt" "$db_id:/tmp/hosted-ci-server.crt"
test_id="$(timeout 20s docker create --name "ss-hosted-test-$run_key" --network "$network_id" \
  --label "$owner_label=$run_key" --label "$role_label=test" \
  --env-file "$run_dir/test.env" --memory 4g --cpus 2 --cap-drop ALL \
  --security-opt no-new-privileges --init "$runner_id")"
owned_container "$test_id" || die 'Test container ownership check failed.'
boundary created || die 'Actual owned container boundary failed.'
printf 'complete\n' > "$run_dir/owned-resource-creation-complete"
timeout 20s docker start "$db_id" > "$run_dir/start-db.log" 2>&1
ready=false
readiness_deadline=$((SECONDS + 90))
while ((SECONDS < readiness_deadline)); do
  if timeout 5s docker exec "$db_id" pg_isready -h 127.0.0.1 -U postgres -d "$database_name" \
       >> "$run_dir/readiness.log" 2>&1; then ready=true; break; fi
  sleep 1
done
[[ "$ready" == true ]] || die 'Owned database did not become ready within 90 seconds.'
timeout 20s docker start "$test_id" > "$run_dir/start-test.log" 2>&1
timeout --signal=TERM --kill-after=10s "$test_seconds" docker wait "$test_id" > "$run_dir/test-wait-exit.txt"
test_exit="$(cat "$run_dir/test-wait-exit.txt")"
[[ "$test_exit" =~ ^[0-9]{1,3}$ ]] || die 'Test container did not provide a native exit status.'
[[ "$test_exit" == 0 ]] || die 'Linux hosted SQL fixture acceptance failed.'
exit 0
