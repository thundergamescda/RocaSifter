#!/usr/bin/env bash
# AUTHORING ONLY. No job, image or native/bootstrap execution is admitted.
set +x
set -Eeuo pipefail
umask 077
die() { printf '%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'Owned Linux CI required.'
[[ "${GITHUB_REPOSITORY:-}" == thundergamescda/RocaSifter &&
   "${GITHUB_REF:-}" == refs/heads/stock-sleeve-owned-data-* ]] || die 'Dedicated data branch required.'
[[ "${SS_DATA_SOURCE_SHA:-}" =~ ^[a-f0-9]{40}$ &&
   "${SS_DATA_PROFILE_SHA256:-}" =~ ^[a-f0-9]{64}$ &&
   "${SS_DATA_PROFILE_PATH:-}" =~ ^tools/owned_data_profiles/[a-z0-9-]+\.json$ ]] || die 'Exact reviewed source/profile required.'
for tool in docker timeout git realpath mktemp openssl sha256sum base64 sed date wc python3; do
  command -v "$tool" >/dev/null || die 'Missing owned CI tool.'
done
for variable in LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_AUTH_CONFIG DOCKER_API_VERSION BUILDKIT_HOST BUILDX_BUILDER; do
  [[ -z "${!variable:-}" ]] || die 'Ambient loader/Python/TLS override refused.'
done
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_DATA_SOURCE_SHA" &&
   -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || die 'Exact clean checkout required.'
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" && -O "$RUNNER_TEMP" ]] || die 'Owned runner temporary root required.'
temp_root="$(realpath -- "$RUNNER_TEMP")"
run_dir="$(mktemp -d "$temp_root/stock-sleeve-owned-data-XXXXXXXXXXXX")"
[[ "$(realpath -- "$run_dir")" == "$temp_root/"* && ! -L "$run_dir" && -O "$run_dir" ]] || die 'Owned run directory refused.'
mkdir "$run_dir/evidence" "$run_dir/stage" "$run_dir/docker-config"
# Use the owned CI host's local engine, with no ambient credentials or context.
export DOCKER_CONFIG="$run_dir/docker-config"
export DOCKER_HOST='unix:///var/run/docker.sock'
helper="$source_root/tools/owned_data_host.py"
transport="$source_root/tools/ci_evidence_transport.sh"
[[ -f "$helper" && ! -L "$helper" && -f "$transport" && ! -L "$transport" ]] || die 'Missing controlled host sources.'
transport_sha="$(sha256sum < "$transport")"; transport_sha="${transport_sha%% *}"
[[ "$transport_sha" == 870c8d0ef39bc5f80d9ebfdd5f02ecc41a587d431f0fadaf52d0cf0adf250206 ]] || die 'Transport source changed.'
sanitize() { cat -- "$1"; } # Only host-validated ASCII JSON envelopes reach emit_file.
# shellcheck source=/dev/null
source "$transport"
owner="$(openssl rand -hex 16)"
[[ "$owner" =~ ^[a-f0-9]{32}$ ]] || die 'Owner nonce failed.'
owner_label='org.stockandsleeve.owned-data.owner'
role_label='org.stockandsleeve.owned-data.role'
base='python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'
image_tag="ss-owned-data-$owner"
image_id=''; container_id=''; process_exit='null'
runtime_verified=false; data_complete=false; capture_complete=false
cleanup_complete=true; delivery_complete=true; primary_status=1
resources_started=false
failure_phase='preparation'; failure_code='HOST_OPERATION_FAILED'
bounded() { local seconds="$1"; shift; timeout --signal=TERM --kill-after=10s "$seconds" "$@"; }
host() {
  local operation="$1" seconds=30
  case "$operation" in
    field|exit-code) seconds=15 ;;
    check-image|check-container|failure-details) seconds=30 ;;
    check-copy|collect) seconds=120 ;;
    *) return 1 ;;
  esac
  bounded "$seconds" python3 -I -S -B "$helper" "$@" 2> "$run_dir/helper-$operation.log"
}
owned_container() {
  local id="$1" actual
  [[ "$id" =~ ^[a-f0-9]{64}$ ]] || return 1
  actual="$(bounded 20 docker container inspect --format '{{index .Config.Labels "org.stockandsleeve.owned-data.owner"}}|{{index .Config.Labels "org.stockandsleeve.owned-data.role"}}' "$id" 2>/dev/null)" || return 1
  [[ "$actual" == "$owner|process" ]]
}
owned_image() {
  local id="$1" actual
  [[ "$id" =~ ^sha256:[a-f0-9]{64}$ ]] || return 1
  actual="$(bounded 20 docker image inspect --format '{{index .Config.Labels "org.stockandsleeve.owned-data.owner"}}|{{index .Config.Labels "org.stockandsleeve.owned-data.role"}}' "$id" 2>/dev/null)" || return 1
  [[ "$actual" == "$owner|image" ]]
}
finish() {
  local incoming=$? id current='' status=1 log_name log_bytes log_sha separator=''
  trap - EXIT INT TERM
  set +e
  # A create interruption may lose stdout/cidfile. Reconcile only this nonce/role.
  if [[ "$resources_started" == true ]]; then
    bounded 20 docker ps -aq --no-trunc --filter "label=$owner_label=$owner" --filter "label=$role_label=process" > "$run_dir/owned-containers.txt" 2>/dev/null
    if [[ "$?" -ne 0 ]]; then cleanup_complete=false; fi
  else
    : > "$run_dir/owned-containers.txt"
  fi
  while IFS= read -r id; do
    [[ -z "$id" ]] && continue
    if ! owned_container "$id"; then cleanup_complete=false; continue; fi
    if [[ -n "$container_id" && "$id" != "$container_id" ]]; then cleanup_complete=false; fi
    bounded 20 docker stop --time 5 "$id" > /dev/null 2>&1
    current="$(bounded 20 docker container inspect --format '{{.State.Running}}' "$id" 2>/dev/null)"
    if [[ "$current" != false ]]; then cleanup_complete=false; continue; fi
    if [[ "$id" == "$container_id" ]]; then
      bounded 30 docker logs "$id" > "$run_dir/container.log" 2>&1
      if [[ "$?" -eq 0 ]]; then
        host collect --control "$run_dir/control.json" --log "$run_dir/container.log" --out "$run_dir/evidence" > "$run_dir/collect.log" 2>&1
        if [[ "$?" -eq 0 ]]; then
          capture_complete=true
          [[ "$process_exit" == 0 && "$runtime_verified" == true && "$primary_status" -eq 0 ]] && data_complete=true
        fi
      fi
    fi
    if ! owned_container "$id" || ! bounded 20 docker rm "$id" > /dev/null 2>&1; then cleanup_complete=false; fi
  done < "$run_dir/owned-containers.txt"
  # Reconcile even a lost build ID by this nonce/role. An inspect error never
  # proves absence, and the immutable base has neither of these owned labels.
  if [[ "$resources_started" == true ]]; then
    if ! bounded 20 docker image ls -aq --no-trunc --filter "label=$owner_label=$owner" --filter "label=$role_label=image" > "$run_dir/owned-images.txt" 2>/dev/null; then cleanup_complete=false; fi
    while IFS= read -r id; do
      [[ -z "$id" ]] && continue
      if ! owned_image "$id" || ! bounded 30 docker image rm "$id" > /dev/null 2>&1; then cleanup_complete=false; fi
    done < "$run_dir/owned-images.txt"
    # Both successful fresh enumerations and empty results are required after
    # successful removal. Daemon/timeout/transport errors remain uncertainty.
    if ! bounded 20 docker ps -aq --no-trunc --filter "label=$owner_label=$owner" --filter "label=$role_label=process" > "$run_dir/remaining-containers.txt" 2>/dev/null; then cleanup_complete=false; fi
    if ! bounded 20 docker image ls -aq --no-trunc --filter "label=$owner_label=$owner" --filter "label=$role_label=image" > "$run_dir/remaining-images.txt" 2>/dev/null; then cleanup_complete=false; fi
    [[ ! -s "$run_dir/remaining-containers.txt" && ! -s "$run_dir/remaining-images.txt" ]] || cleanup_complete=false
  fi
  [[ "$incoming" -eq 0 && "$primary_status" -eq 0 && "$process_exit" == 0 &&
     "$runtime_verified" == true && "$data_complete" == true && "$capture_complete" == true &&
     "$cleanup_complete" == true ]] && status=0
  if ! host failure-details --run "$run_dir" --out "$run_dir/evidence/failure-details.json" > "$run_dir/failure-details.log" 2>&1; then
    capture_complete=false; status=1
  fi
  # Retain diagnostic identity without publishing arbitrary logs or signed URLs.
  [[ "$status" -eq 0 ]] && failure_code='NONE'
  printf '{"format":"stock-and-sleeve:owned-data-failure-receipt:v1","phase":"%s","code":"%s","incoming_exit":%s,"logs":[' "$failure_phase" "$failure_code" "$incoming" > "$run_dir/evidence/failure-receipt.json"
  for log_name in prepare.log pull.log build.log create.log start.log wait.log collect.log container.log helper-field.log helper-check-image.log helper-check-container.log helper-check-copy.log helper-exit-code.log helper-collect.log failure-details.log helper-failure-details.log; do
    [[ -f "$run_dir/$log_name" && ! -L "$run_dir/$log_name" ]] || continue
    log_bytes="$(wc -c < "$run_dir/$log_name")"
    log_sha="$(bounded 20 sha256sum < "$run_dir/$log_name")"; log_sha="${log_sha%% *}"
    if [[ ! "$log_bytes" =~ ^[0-9]+$ || ! "$log_sha" =~ ^[a-f0-9]{64}$ ]]; then capture_complete=false; status=1; continue; fi
    printf '%s{"name":"%s","bytes":%s,"sha256":"%s"}' "$separator" "$log_name" "$log_bytes" "$log_sha" >> "$run_dir/evidence/failure-receipt.json"
    separator=','
  done
  printf '],"raw_logs_published":false}\n' >> "$run_dir/evidence/failure-receipt.json"
  # No raw container/pull/build log, signed URL, input or opaque output is emitted.
  if [[ -f "$run_dir/evidence/export-files.txt" && ! -L "$run_dir/evidence/export-files.txt" ]]; then
    while IFS= read -r current; do
      [[ "$current" =~ ^[a-z0-9][a-z0-9.-]{0,127}\.json$ && "$current" != *..* ]] || { delivery_complete=false; status=1; continue; }
      emit_file "evidence/$current" "$run_dir/evidence/$current" || { delivery_complete=false; status=1; }
    done < "$run_dir/evidence/export-files.txt"
  else delivery_complete=false; status=1; fi
  if [[ -f "$run_dir/evidence/failure-details.json" && ! -L "$run_dir/evidence/failure-details.json" ]]; then
    emit_file evidence/failure-details.json "$run_dir/evidence/failure-details.json" || { delivery_complete=false; status=1; }
  else delivery_complete=false; status=1; fi
  emit_file evidence/failure-receipt.json "$run_dir/evidence/failure-receipt.json" || { delivery_complete=false; status=1; }
  [[ "$delivery_complete" == true ]] || status=1
  printf '{"format":"stock-and-sleeve:owned-data-host-summary:v1","source_sha":"%s","profile_sha256":"%s","owner":"%s","process_exit":%s,"runtime_verified":%s,"data_complete":%s,"capture_complete":%s,"cleanup_complete":%s,"delivery_complete":%s,"host_exit":%s,"raw_payload_published":false,"application_acceptance":false}\n' \
    "$SS_DATA_SOURCE_SHA" "$SS_DATA_PROFILE_SHA256" "$owner" "$process_exit" "$runtime_verified" "$data_complete" "$capture_complete" "$cleanup_complete" "$delivery_complete" "$status" > "$run_dir/harness-summary.json"
  emit_file harness-summary.json "$run_dir/harness-summary.json" || status=1
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Preparation refuses false admission before any HTTP request or Docker operation.
bounded 600 python3 -I -S -B "$helper" prepare --profile "$source_root/$SS_DATA_PROFILE_PATH" \
  --profile-sha "$SS_DATA_PROFILE_SHA256" --source-sha "$SS_DATA_SOURCE_SHA" --repo "$source_root" \
  --run "$run_dir" > "$run_dir/prepare.log" 2>&1
entry="$(host field --control "$run_dir/control.json" --name entrypoint)"
source_manifest_sha="$(host field --control "$run_dir/control.json" --name source_manifest_sha256)"
input_spec_sha="$(host field --control "$run_dir/control.json" --name input_spec_sha256)"
profile_id="$(host field --control "$run_dir/control.json" --name profile_id)"
[[ "$entry" == /ci-code/debian_data.py || "$entry" == /ci-code/pg_oci_data.py ]] || die 'Fixed entrypoint required.'
[[ "$source_manifest_sha" =~ ^[a-f0-9]{64}$ && "$input_spec_sha" =~ ^[a-f0-9]{64}$ ]] || die 'Actual source/input manifest pins required.'
[[ "$profile_id" == debian-archives || "$profile_id" == postgres-rootfs ]] || die 'Fixed profile required.'
resources_started=true
failure_phase='pull'
bounded 180 docker pull --platform linux/amd64 "$base" > "$run_dir/pull.log" 2>&1
bounded 20 docker image inspect "$base" > "$run_dir/base-image.json"
failure_phase='build'
bounded 180 docker build --network=none --pull=false --build-arg "SS_DATA_OWNER=$owner" \
  --label "$owner_label=$owner" --label "$role_label=image" --iidfile "$run_dir/image-id" \
  -t "$image_tag" "$run_dir/stage" > "$run_dir/build.log" 2>&1
image_id="$(cat -- "$run_dir/image-id")"
owned_image "$image_id" || die 'Owned derived image required.'
bounded 20 docker image inspect "$image_id" > "$run_dir/derived-image.json"
failure_phase='image-check'
host check-image --control "$run_dir/control.json" --base "$run_dir/base-image.json" \
  --image "$run_dir/derived-image.json" --owner "$owner"
failure_phase='create'
bounded 30 docker create --cidfile "$run_dir/container-id" --network none --read-only --user 10001:10001 \
  --cap-drop ALL --security-opt no-new-privileges=true --pids-limit 128 --memory 4g --memory-swap 4g --cpus 2 \
  --log-driver json-file --log-opt max-size=128m --log-opt max-file=1 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777 \
  --tmpfs /ci-evidence:rw,nosuid,nodev,noexec,size=2147483648,mode=0700,uid=10001,gid=10001 \
  --label "$owner_label=$owner" --label "$role_label=process" \
  --env "SS_DATA_SOURCE_SHA=$SS_DATA_SOURCE_SHA" --env "SS_DATA_PROFILE_SHA256=$SS_DATA_PROFILE_SHA256" \
  --env "SS_DATA_SOURCE_MANIFEST_SHA256=$source_manifest_sha" --env "SS_DATA_INPUT_SPEC_SHA256=$input_spec_sha" \
  --env "SS_DATA_PROFILE_ID=$profile_id" --entrypoint python "$image_id" -I -S -B "$entry" \
  > "$run_dir/create.log" 2>&1
container_id="$(cat -- "$run_dir/container-id")"
owned_container "$container_id" || die 'Owned data process required.'
bounded 20 docker container inspect "$container_id" > "$run_dir/container-before.json"
failure_phase='container-check'
host check-container --control "$run_dir/control.json" --inspect "$run_dir/container-before.json" --owner "$owner"
# These are ordinary immutable image files, not tmpfs outputs. No process started.
failure_phase='copy-check'
[[ ! -e "$run_dir/copy-code" && ! -e "$run_dir/copy-input" ]] || die 'Fresh image-copy targets required.'
# Copy each directory itself to a nonexistent target so its actual image mode
# is retained. Copying only '/.' into a precreated0700 target loses that fact.
bounded 90 docker cp "$container_id:/ci-code" "$run_dir/copy-code"
bounded 120 docker cp "$container_id:/ci-input" "$run_dir/copy-input"
host check-copy --control "$run_dir/control.json" --code "$run_dir/copy-code" --input "$run_dir/copy-input" \
  --out "$run_dir/evidence/actual-copy-proof.json"
runtime_verified=true
failure_phase='start'
bounded 20 docker start "$container_id" > "$run_dir/start.log" 2>&1
failure_phase='wait'
set +e
bounded 215 docker wait "$container_id" > "$run_dir/wait.log" 2>&1
start_status=$?
set -e
bounded 20 docker container inspect "$container_id" > "$run_dir/container-after.json"
process_exit="$(host exit-code --inspect "$run_dir/container-after.json" --owner "$owner")"
[[ "$process_exit" =~ ^[0-9]{1,3}$ ]] || die 'Typed process exit required.'
[[ "$start_status" -eq 0 && "$process_exit" == 0 ]] || die 'Data process failed or exceeded its deadline.'
failure_phase='finalization'
primary_status=0
