#!/usr/bin/env bash
# AUTHOR-ONLY candidate. Run only after review/admission on GitHub ubuntu-24.04.
# A workflow-level timeout is still required. No Windows runtime is invoked.
set +x
set -Eeuo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'This harness requires the reviewed Linux CI host.'
for command in docker openssl timeout git realpath mktemp sha256sum sed base64 date wc; do
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
  copied-source-before.json copied-source-after.json tls-client-probe.json
  pip-freeze.txt pip-bootstrap-report.json pip-install-report.json
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
  printf '{"scope":"Linux hosted API unit and SQL fixture acceptance only; not Windows/native/signing acceptance","source_sha":"%s","owner":"%s","python_tag":"python:3.12.16-slim","python_digest":"%s","python_image_id":"%s","postgres_tag":"postgres:18.6","postgres_digest":"%s","postgres_image_id":"%s","runner_image_id":"%s","runtime_network_internal":%s,"test_container_exit_code":%s,"cleanup_complete":%s,"capture_complete":%s,"capture_status":{"test_stop":%s,"test_inspect":%s,"test_logs":%s,"test_sanitize":%s,"test_raw_cleanup":%s,"evidence_copy":%s,"postgres_dir":%s,"postgres_copy":%s,"db_logs":%s,"db_sanitize":%s,"db_raw_cleanup":%s},"delivery_complete":%s,"harness_exit_code":%s}\n' \
    "$SS_CI_SOURCE_SHA" "$run_key" "$python_digest" "$python_id" "$postgres_digest" "$postgres_id" \
    "$runner_id" "$network_verified" "$test_exit" "$cleanup_complete" "$capture_complete" \
    "${capture_status[test_stop]}" "${capture_status[test_inspect]}" \
    "${capture_status[test_logs]}" "${capture_status[test_sanitize]}" \
    "${capture_status[test_raw_cleanup]}" "${capture_status[evidence_copy]}" \
    "${capture_status[postgres_dir]}" "${capture_status[postgres_copy]}" \
    "${capture_status[db_logs]}" "${capture_status[db_sanitize]}" \
    "${capture_status[db_raw_cleanup]}" "$delivery_complete" "$1" > "$run_dir/harness-summary.json"
}

finish() {
  local status="$1" id found code name
  trap - EXIT INT TERM
  set +e
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
      owned-networks.txt owned-images.txt; do
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

db_password="$(openssl rand -hex 24)"
[[ "$db_password" =~ ^[0-9a-f]{48}$ ]] || die 'Failed to generate fixture credential.'
database_name="ss_ci_${run_key:0:16}"
database_url="postgresql://postgres:$db_password@db:5432/$database_name?sslmode=require"
printf 'POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=%s\nPOSTGRES_INITDB_ARGS=--auth-host=scram-sha-256\n' \
  "$db_password" "$database_name" > "$run_dir/db.env"
printf 'DATABASE_URL=%s\nSS_CI_SOURCE_SHA=%s\nPYTHONDONTWRITEBYTECODE=1\nPYTHONUNBUFFERED=1\n' \
  "$database_url" "$SS_CI_SOURCE_SHA" > "$run_dir/test.env"
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
build_step docker pull python:3.12.16-slim > "$run_dir/pull-python.log" 2>&1
build_step docker pull postgres:18.6 > "$run_dir/pull-postgres.log" 2>&1
python_id="$(build_step docker image inspect --format '{{.Id}}' python:3.12.16-slim)"
python_digest="$(build_step docker image inspect --format '{{index .RepoDigests 0}}' python:3.12.16-slim)"
postgres_id="$(build_step docker image inspect --format '{{.Id}}' postgres:18.6)"
postgres_digest="$(build_step docker image inspect --format '{{index .RepoDigests 0}}' postgres:18.6)"
[[ "$python_id" =~ ^sha256:[0-9a-f]{64}$ && "$postgres_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'Invalid resolved image IDs.'
[[ "$python_digest" =~ ^(docker.io/library/)?python@sha256:[0-9a-f]{64}$ &&
   "$postgres_digest" =~ ^(docker.io/library/)?postgres@sha256:[0-9a-f]{64}$ ]] || die 'Missing official image digests.'
build_step docker build --pull=false --build-arg "PYTHON_IMAGE=$python_digest" \
  --label "$owner_label=$run_key" --label "$role_label=runner" \
  --tag "$runner_tag" --file "$source_root/tools/hosted_ci.Dockerfile" "$source_root" > "$run_dir/build.log" 2>&1
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
