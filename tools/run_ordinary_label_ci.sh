#!/usr/bin/env bash
# Source-only candidate. Invoke only after independent admission on owned CI.
set +x
set -Eeuo pipefail
umask 077
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'Reviewed Linux host required.'
[[ "${GITHUB_REPOSITORY:-}" == thundergamescda/RocaSifter &&
   "${GITHUB_REF:-}" == refs/heads/stock-sleeve-ordinary-label-ci-* ]] || die 'Dedicated ordinary-label CI branch required.'
[[ "${SS_ORDINARY_LABEL_SOURCE_SHA:-}" =~ ^[a-f0-9]{40}$ ]] || die 'Exact source SHA required.'
for command in docker timeout git realpath mktemp openssl sha256sum base64 sed date wc; do
  command -v "$command" >/dev/null || die "Missing owned CI tool: $command"
done
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" && -O "$RUNNER_TEMP" ]] || die 'Owned RUNNER_TEMP required.'
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_ORDINARY_LABEL_SOURCE_SHA" &&
   -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || die 'Exact clean checkout required.'
[[ "$(git -C "$source_root" cat-file -p HEAD | sed -n '/^$/q;s/^parent //p')" == 9c6eee510eaf4ab07bb73225144292e5cddd3347 ]] || die 'Reviewed single public parent required.'
temp_root="$(realpath -- "$RUNNER_TEMP")"
run_dir="$(mktemp -d "$temp_root/stock-sleeve-ordinary-label-ci-XXXXXXXXXXXX")"
[[ "$(realpath -- "$run_dir")" == "$temp_root/"* && ! -L "$run_dir" && -O "$run_dir" ]] || die 'Invalid owned directory.'
mkdir "$run_dir/evidence"
# The nonce parent remains0700. Only this private child is writable by container
# uid10001, allowing evidence to survive container stop without writable sources.
chmod 0777 "$run_dir/evidence"
run_key="$(openssl rand -hex 16)"
[[ "$run_key" =~ ^[a-f0-9]{32}$ ]] || die 'Owned resource key failed.'
owner_label='org.stockandsleeve.ordinary-label-ci.owner'
role_label='org.stockandsleeve.ordinary-label-ci.role'
base='python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'
runner_tag="ss-ordinary-label-ci-$run_key"
runner_id=''
test_id=''
test_exit='null'
cleanup_complete=true
capture_complete=true
delivery_complete=true
runtime_verified=false
context_manifest_sha=''
required=(ordinary-summary.json source-before.json source-after.json dependency-preflight.json context-audit.json
          service-summary.json service-results.json service-events.jsonl service-tests.log
          regression-summary.json regression-results.json regression-events.jsonl regression-tests.log
          guard-summary.json guard-results.json guard-events.jsonl guard-tests.log
          pip-install-report.json pip-check.txt pip-freeze.txt pip-list.json)

# All container inputs are exact source or fictional fixture data; no host env,
# credentials, customer profiles, database or provider transport is forwarded.
# Strip terminal controls before the shared bounded length/hash transport.
sanitize() { LC_ALL=C sed 's/\r$//;s/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]//g' "$1"; }
source "$source_root/tools/ci_evidence_transport.sh"

owned_container() {
  local found
  [[ "$1" =~ ^[a-f0-9]{64}$ ]] || return 1
  found="$(timeout 10s docker inspect --format \
    '{{.Id}}|{{index .Config.Labels "org.stockandsleeve.ordinary-label-ci.owner"}}|{{index .Config.Labels "org.stockandsleeve.ordinary-label-ci.role"}}|{{.Image}}' "$1" 2>/dev/null)" || return 1
  [[ "$found" == "$1|$run_key|test|$runner_id" ]]
}

finish() {
  local status="$1" id found name
  trap - EXIT INT TERM
  set +e
  if [[ -n "$test_id" ]] && owned_container "$test_id"; then
    timeout 25s docker stop --time 10 "$test_id" > "$run_dir/stop.log" 2>&1 || capture_complete=false
    timeout 20s docker logs "$test_id" > "$run_dir/container.log" 2>&1 || capture_complete=false
    for name in context-audit.json pip-install-report.json pip-check.txt pip-freeze.txt pip-list.json; do
      timeout 15s docker cp "$test_id:/opt/ordinary-label-ci/$name" "$run_dir/evidence/$name" \
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
      '{{.Id}}|{{index .Config.Labels "org.stockandsleeve.ordinary-label-ci.owner"}}|{{index .Config.Labels "org.stockandsleeve.ordinary-label-ci.role"}}' "$id" 2>/dev/null)"
    if [[ "$id" =~ ^sha256:[a-f0-9]{64}$ && "$found" == "$id|$run_key|runner" ]]; then
      timeout 25s docker image rm --force "$id" >> "$run_dir/cleanup.log" 2>&1 || cleanup_complete=false
    else cleanup_complete=false
    fi
  done < "$run_dir/owned-images.txt"
  # Check actual post-removal scope rather than treating deletion requests as proof.
  timeout 10s docker ps -aq --no-trunc --filter "label=$owner_label=$run_key" \
    > "$run_dir/remaining-containers.txt" 2>> "$run_dir/cleanup.log" || cleanup_complete=false
  timeout 10s docker image ls -q --no-trunc --filter "label=$owner_label=$run_key" \
    > "$run_dir/remaining-images.txt" 2>> "$run_dir/cleanup.log" || cleanup_complete=false
  [[ ! -s "$run_dir/remaining-containers.txt" && ! -s "$run_dir/remaining-images.txt" ]] || cleanup_complete=false
  [[ "$cleanup_complete" == true && "$capture_complete" == true && "$test_exit" == 0 && "$runtime_verified" == true ]] || status=1
  [[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_ORDINARY_LABEL_SOURCE_SHA" &&
     -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || status=1
  for name in "${required[@]}"; do
    [[ -f "$run_dir/evidence/$name" && ! -L "$run_dir/evidence/$name" ]] || status=1
    emit_file "evidence/$name" "$run_dir/evidence/$name" || { status=1; delivery_complete=false; }
  done
  if ((status != 0)); then
    for name in build.log pull.log container.log stop.log copy.log cleanup.log owned-containers.txt owned-images.txt remaining-containers.txt remaining-images.txt; do
      [[ -f "$run_dir/$name" && ! -L "$run_dir/$name" ]] || continue
      emit_file "diagnostics/$name" "$run_dir/$name" || { status=1; delivery_complete=false; }
    done
  fi
  printf '{"scope":"Owned Linux current-service17 plus unchanged portable regression40 plus separate stdlib provenance6","source_sha":"%s","expected_context_manifest_sha256":"%s","owner":"%s","base":"%s","runner_image_id":"%s","runtime_network":"none","runtime_verified":%s,"read_only_sources":true,"uid":10001,"test_exit_code":%s,"cleanup_complete":%s,"capture_complete":%s,"delivery_complete":%s,"harness_exit_code":%s}\n' \
    "$SS_ORDINARY_LABEL_SOURCE_SHA" "$context_manifest_sha" "$run_key" "$base" "$runner_id" "$runtime_verified" "$test_exit" \
    "$cleanup_complete" "$capture_complete" "$delivery_complete" "$status" > "$run_dir/harness-summary.json"
  if ! emit_file harness-summary.json "$run_dir/harness-summary.json"; then status=1; fi
  printf 'Owned ordinary-label evidence directory: %s\n' "$run_dir"
  exit "$status"
}
trap 'finish "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM


# Exact expected context ledger is generated outside Docker's context from this
# already-verified clean checkout. No source text, secrets or host env enter it.
context_paths=(
  'catalog_cleanup.py'
  'catalog_physical_ownership.py'
  'credit_tracker.py'
  'hosted/__init__.py'
  'hosted/credit_authority.py'
  'hosted/credit_lots.py'
  'hosted/database.py'
  'hosted/intake.py'
  'hosted/ledger.py'
  'hosted/receiving_plan.py'
  'hosted/security.py'
  'hosted/settlement.py'
  'hosted/trades.py'
  'hosted_labels.py'
  'intake_label_contract.py'
  'intake_manifest.py'
  'intake_physical_identity.py'
  'pricing_review.py'
  'receiving_evidence.py'
  'roca_edition.json'
  'roca_to_square.py'
  'scanner_adapters.py'
  'square_variations.py'
  'station_cloud.py'
  'station_cloud_journal_cas.py'
  'station_edition.py'
  'station_financial_record.py'
  'station_hosted_receive.py'
  'station_http_json.py'
  'station_intake_claim.py'
  'station_intake_receiving_projection.py'
  'station_intake_receiving_registry.py'
  'station_intake_receiving_review.py'
  'station_intake_stock_action_codec.py'
  'station_intake_stock_action_registry.py'
  'station_intake_stock_action_transport.py'
  'station_intake_stock_actions.py'
  'station_label_legacy_worker.py'
  'station_label_publication.py'
  'station_paths.py'
  'station_pricing.py'
  'station_profile.py'
  'station_receiving_action_registry.py'
  'station_receiving_action_transport.py'
  'station_receiving_actions.py'
  'station_receiving_budget.py'
  'station_receiving_journal_cas.py'
  'station_receiving_label_plan.py'
  'station_receiving_label_publication.py'
  'station_receiving_label_render.py'
  'station_receiving_label_worker.py'
  'station_receiving_labels.py'
  'station_receiving_reference_fences.py'
  'station_receiving_reference_retirement.py'
  'station_receiving_registry.py'
  'station_receiving_restoration_proof.py'
  'station_receiving_restore.py'
  'station_recovery_review.py'
  'station_server.py'
  'station_service.py'
  'station_setup.py'
  'station_shared_trade.py'
  'station_store.py'
  'station_trade_recovery.py'
  'store_pricing.py'
  'test_hosted_receive.py'
  'test_receiving_label_plan.py'
  'test_receiving_label_render.py'
  'test_receiving_label_worker.py'
  'test_shared_trade.py'
  'test_station_cloud.py'
  'test_station_label_publication.py'
  'tools/label_ci_provenance.py'
  'tools/ordinary_label_ci.Dockerfile'
  'tools/ordinary_label_ci.Dockerfile.dockerignore'
  'tools/ordinary_label_ci_admission.json'
  'tools/ordinary_label_ci_context.py'
  'tools/ordinary_label_ci_evidence.py'
  'tools/ordinary_label_ci_requirements.txt'
  'tools/ordinary_label_ci_wheel_urls.txt'
  'tools/test_label_ci_provenance.py'
)
[[ "${#context_paths[@]}" == 81 ]] || die 'Fixed context selection failed.'
{
  printf '{"format":"stock-and-sleeve:ordinary-label-context-expected:v1","source_sha":"%s","files":[' "$SS_ORDINARY_LABEL_SOURCE_SHA"
  separator=''
  for name in "${context_paths[@]}"; do
    [[ "$name" =~ ^(hosted/|tools/)?[A-Za-z0-9_.-]+$ ]] || die 'Context name refused.'
    [[ -f "$source_root/$name" && ! -L "$source_root/$name" &&
       "$(realpath -- "$source_root/$name")" == "$source_root/$name" ]] || die 'Context regular path required.'
    bytes="$(wc -c < "$source_root/$name")"
    hash="$(sha256sum < "$source_root/$name")"
    hash="${hash%% *}"
    [[ "$bytes" =~ ^[0-9]+$ && "$hash" =~ ^[a-f0-9]{64}$ ]] || die 'Context file pin failed.'
    ((bytes <= 4194304)) || die 'Context file bound failed.'
    printf '%s{"path":"%s","bytes":%s,"sha256":"%s"}' "$separator" "$name" "$bytes" "$hash"
    separator=','
  done
  printf ']}'
} > "$run_dir/context-expected.json"
context_manifest_bytes="$(wc -c < "$run_dir/context-expected.json")"
[[ "$context_manifest_bytes" =~ ^[0-9]+$ ]] && ((context_manifest_bytes <= 65536)) || die 'Context manifest bound failed.'
context_manifest_sha="$(sha256sum < "$run_dir/context-expected.json")"
context_manifest_sha="${context_manifest_sha%% *}"
[[ "$context_manifest_sha" =~ ^[a-f0-9]{64}$ ]] || die 'Context manifest hash failed.'
context_manifest_b64="$(base64 --wrap=0 -- "$run_dir/context-expected.json")"
[[ "$context_manifest_b64" =~ ^[A-Za-z0-9+/]+={0,2}$ && ${#context_manifest_b64} -le 90000 ]] || die 'Context manifest encoding failed.'

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
  --build-arg "SS_ORDINARY_CONTEXT_EXPECTED_B64=$context_manifest_b64" \
  --label "$owner_label=$run_key" --label "$role_label=runner" --tag "$runner_tag" \
  --file "$source_root/tools/ordinary_label_ci.Dockerfile" "$source_root" > "$run_dir/build.log" 2>&1
runner_id="$(timeout 10s docker image inspect --format '{{.Id}}' "$runner_tag")"
[[ "$runner_id" =~ ^sha256:[a-f0-9]{64}$ ]] || die 'Owned image ID missing.'
test_id="$(timeout 20s docker create --name "ss-ordinary-label-test-$run_key" \
  --label "$owner_label=$run_key" --label "$role_label=test" \
  --platform linux/amd64 --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges --init --pids-limit 256 \
  --memory 4g --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=1g,mode=1777 \
  --mount "type=bind,src=$run_dir/evidence,dst=/ci-evidence" \
  --env "SS_ORDINARY_LABEL_SOURCE_SHA=$SS_ORDINARY_LABEL_SOURCE_SHA" \
  --env "SS_ORDINARY_CONTEXT_MANIFEST_SHA256=$context_manifest_sha" "$runner_id")"
owned_container "$test_id" || die 'Owned test container identity failed.'
found="$(timeout 10s docker inspect --format \
  '{{.HostConfig.NetworkMode}}|{{.HostConfig.ReadonlyRootfs}}|{{.Config.User}}|{{.HostConfig.Privileged}}|{{.HostConfig.PidMode}}' "$test_id")"
[[ "$found" == 'none|true|10001:10001|false|' ]] || die 'Runtime container boundary changed.'
runtime_verified=true
timeout 20s docker start "$test_id" > "$run_dir/start.log" 2>&1
timeout --signal=TERM --kill-after=10s 1200 docker wait "$test_id" > "$run_dir/wait-exit.txt"
test_exit="$(cat "$run_dir/wait-exit.txt")"
[[ "$test_exit" =~ ^[0-9]{1,3}$ && "$test_exit" == 0 ]] || die 'Ordinary label/service acceptance failed.'
exit 0
