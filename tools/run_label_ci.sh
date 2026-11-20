#!/usr/bin/env bash
# Source-only candidate. Invoke only after independent admission on owned CI.
set +x
set -Eeuo pipefail
umask 077
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'Reviewed Linux host required.'
[[ "${GITHUB_REPOSITORY:-}" == thundergamescda/RocaSifter &&
   "${GITHUB_REF:-}" == refs/heads/stock-sleeve-label-ci-* ]] || die 'Dedicated label CI branch required.'
[[ "${SS_LABEL_SOURCE_SHA:-}" =~ ^[a-f0-9]{40}$ ]] || die 'Exact source SHA required.'
for command in docker timeout git realpath mktemp openssl sha256sum base64 sed date wc; do
  command -v "$command" >/dev/null || die "Missing owned CI tool: $command"
done
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" && -O "$RUNNER_TEMP" ]] || die 'Owned RUNNER_TEMP required.'
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_LABEL_SOURCE_SHA" &&
   -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || die 'Exact clean checkout required.'
temp_root="$(realpath -- "$RUNNER_TEMP")"
run_dir="$(mktemp -d "$temp_root/stock-sleeve-label-ci-XXXXXXXXXXXX")"
[[ "$(realpath -- "$run_dir")" == "$temp_root/"* && ! -L "$run_dir" && -O "$run_dir" ]] || die 'Invalid owned directory.'
mkdir "$run_dir/evidence"
# The nonce parent remains0700. Only this private child is writable by container
# uid10001, allowing evidence to survive container stop without writable sources.
chmod 0777 "$run_dir/evidence"
run_key="$(openssl rand -hex 16)"
[[ "$run_key" =~ ^[a-f0-9]{32}$ ]] || die 'Owned resource key failed.'
owner_label='org.stockandsleeve.label-ci.owner'
role_label='org.stockandsleeve.label-ci.role'
base='python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'
runner_tag="ss-label-ci-$run_key"
runner_id=''
test_id=''
test_exit='null'
cleanup_complete=true
capture_complete=true
delivery_complete=true
runtime_verified=false
required=(label-summary.json source-before.json source-after.json dependency-preflight.json
          case-results.json case-events.jsonl label-tests.log guard-summary.json guard-results.json
          guard-events.jsonl guard-tests.log pip-install-report.json pip-check.txt pip-freeze.txt pip-list.json)

# All container inputs are exact source or fictional fixture data; no host env,
# credentials, customer profiles, database or provider transport is forwarded.
# Strip terminal controls before the shared bounded length/hash transport.
sanitize() { LC_ALL=C sed 's/\r$//;s/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]//g' "$1"; }
source "$source_root/tools/ci_evidence_transport.sh"

owned_container() {
  local found
  [[ "$1" =~ ^[a-f0-9]{64}$ ]] || return 1
  found="$(timeout 10s docker inspect --format \
    '{{.Id}}|{{index .Config.Labels "org.stockandsleeve.label-ci.owner"}}|{{index .Config.Labels "org.stockandsleeve.label-ci.role"}}|{{.Image}}' "$1" 2>/dev/null)" || return 1
  [[ "$found" == "$1|$run_key|test|$runner_id" ]]
}

finish() {
  local status="$1" id found name
  trap - EXIT INT TERM
  set +e
  if [[ -n "$test_id" ]] && owned_container "$test_id"; then
    timeout 25s docker stop --time 10 "$test_id" > "$run_dir/stop.log" 2>&1 || capture_complete=false
    timeout 20s docker logs "$test_id" > "$run_dir/container.log" 2>&1 || capture_complete=false
    for name in pip-install-report.json pip-check.txt pip-freeze.txt pip-list.json; do
      timeout 15s docker cp "$test_id:/opt/label-ci/$name" "$run_dir/evidence/$name" \
        >> "$run_dir/copy.log" 2>&1 || capture_complete=false
    done
  else
    capture_complete=false
  fi
  # Reconcile interrupted creates using only the unpredictable exact owner label.
  timeout 10s docker ps -aq --no-trunc --filter "label=$owner_label=$run_key" \
    > "$run_dir/owned-containers.txt" 2>> "$run_dir/cleanup.log" || cleanup_complete=false
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    if owned_container "$id"; then
      timeout 25s docker rm --force --volumes "$id" >> "$run_dir/cleanup.log" 2>&1 || cleanup_complete=false
    else cleanup_complete=false
    fi
  done < "$run_dir/owned-containers.txt"
  timeout 10s docker image ls -q --no-trunc --filter "label=$owner_label=$run_key" \
    > "$run_dir/owned-images.txt" 2>> "$run_dir/cleanup.log" || cleanup_complete=false
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    found="$(timeout 10s docker image inspect --format \
      '{{.Id}}|{{index .Config.Labels "org.stockandsleeve.label-ci.owner"}}|{{index .Config.Labels "org.stockandsleeve.label-ci.role"}}' "$id" 2>/dev/null)"
    if [[ "$id" =~ ^sha256:[a-f0-9]{64}$ && "$found" == "$id|$run_key|runner" ]]; then
      timeout 25s docker image rm --force "$id" >> "$run_dir/cleanup.log" 2>&1 || cleanup_complete=false
    else cleanup_complete=false
    fi
  done < "$run_dir/owned-images.txt"
  [[ "$cleanup_complete" == true && "$capture_complete" == true && "$test_exit" == 0 && "$runtime_verified" == true ]] || status=1
  [[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_LABEL_SOURCE_SHA" &&
     -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || status=1
  for name in "${required[@]}"; do
    [[ -f "$run_dir/evidence/$name" && ! -L "$run_dir/evidence/$name" ]] || status=1
    emit_file "evidence/$name" "$run_dir/evidence/$name" || { status=1; delivery_complete=false; }
  done
  if ((status != 0)); then
    for name in build.log pull.log container.log stop.log copy.log cleanup.log owned-containers.txt owned-images.txt; do
      [[ -f "$run_dir/$name" && ! -L "$run_dir/$name" ]] || continue
      emit_file "diagnostics/$name" "$run_dir/$name" || { status=1; delivery_complete=false; }
    done
  fi
  printf '{"scope":"Owned Linux label40 plus separate stdlib provenance6","source_sha":"%s","owner":"%s","base":"%s","runner_image_id":"%s","runtime_network":"none","runtime_verified":%s,"read_only_sources":true,"uid":10001,"test_exit_code":%s,"cleanup_complete":%s,"capture_complete":%s,"delivery_complete":%s,"harness_exit_code":%s}\n' \
    "$SS_LABEL_SOURCE_SHA" "$run_key" "$base" "$runner_id" "$runtime_verified" "$test_exit" \
    "$cleanup_complete" "$capture_complete" "$delivery_complete" "$status" > "$run_dir/label-harness-summary.json"
  if ! emit_file label-harness-summary.json "$run_dir/label-harness-summary.json"; then status=1; fi
  printf 'Owned label evidence directory: %s\n' "$run_dir"
  exit "$status"
}
trap 'finish "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Fixed bounds, no mutable test scope/timeouts, production branches or secrets.
build_deadline=$((SECONDS + 900))
build_step() {
  local remaining=$((build_deadline - SECONDS))
  ((remaining > 0)) || return 124
  timeout --signal=TERM --kill-after=10s "$remaining" "$@"
}
build_step docker pull --platform linux/amd64 "$base" > "$run_dir/pull.log" 2>&1
[[ "$(docker image inspect --format '{{.Os}}|{{.Architecture}}' "$base")" == 'linux|amd64' ]] || die 'Wrong base platform.'
build_step docker build --platform linux/amd64 --pull=false \
  --label "$owner_label=$run_key" --label "$role_label=runner" --tag "$runner_tag" \
  --file "$source_root/tools/label_ci.Dockerfile" "$source_root" > "$run_dir/build.log" 2>&1
runner_id="$(timeout 10s docker image inspect --format '{{.Id}}' "$runner_tag")"
[[ "$runner_id" =~ ^sha256:[a-f0-9]{64}$ ]] || die 'Owned image ID missing.'
test_id="$(timeout 20s docker create --name "ss-label-test-$run_key" \
  --label "$owner_label=$run_key" --label "$role_label=test" \
  --platform linux/amd64 --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges --init --pids-limit 256 \
  --memory 4g --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=1g,mode=1777 \
  --mount "type=bind,src=$run_dir/evidence,dst=/ci-evidence" \
  --env "SS_LABEL_SOURCE_SHA=$SS_LABEL_SOURCE_SHA" "$runner_id")"
owned_container "$test_id" || die 'Owned test container identity failed.'
found="$(timeout 10s docker inspect --format \
  '{{.HostConfig.NetworkMode}}|{{.HostConfig.ReadonlyRootfs}}|{{.Config.User}}|{{.HostConfig.Privileged}}|{{.HostConfig.PidMode}}' "$test_id")"
[[ "$found" == 'none|true|10001:10001|false|' ]] || die 'Runtime container boundary changed.'
runtime_verified=true
timeout 20s docker start "$test_id" > "$run_dir/start.log" 2>&1
timeout --signal=TERM --kill-after=10s 1200 docker wait "$test_id" > "$run_dir/wait-exit.txt"
test_exit="$(cat "$run_dir/wait-exit.txt")"
[[ "$test_exit" =~ ^[0-9]{1,3}$ && "$test_exit" == 0 ]] || die 'Portable label acceptance failed.'
exit 0
