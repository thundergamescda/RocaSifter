#!/usr/bin/env bash
# Source-only candidate. One fresh-runner/base-layer diagnostic, not R3 replay.
set +x
set -Eeuo pipefail
umask 077
die() { printf '%s\n' "$1" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'Linux CI is required.'
for command in docker openssl timeout git realpath mktemp sha256sum base64 wc cat rm chmod cp cmp tr sed grep jq; do
  command -v "$command" >/dev/null || die 'A required CI utility is missing.'
done
[[ "${SS_CI_SOURCE_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || die 'An exact source commit is required.'
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" && ! -L "$RUNNER_TEMP" && -O "$RUNNER_TEMP" ]] || die 'An owned CI temporary directory is required.'
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_CI_SOURCE_SHA" &&
   -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || die 'The exact clean checkout is required.'
profile="$source_root/tools/hosted_browser-seccomp.json"
profile_sha='e56c2f17869890cbd1fed1b44e584af59ffca5f44cb946832b194c5662cc049f'
transport="$source_root/tools/ci_evidence_transport.sh"
transport_sha='870c8d0ef39bc5f80d9ebfdd5f02ecc41a587d431f0fadaf52d0cf0adf250206'
probe="$source_root/tools/browser_primitive_probe.sh"
for file in "$profile" "$transport" "$probe"; do
  [[ -f "$file" && ! -L "$file" && -r "$file" ]] || die 'A required regular source file is missing.'
done
found="$(sha256sum < "$profile")"
[[ "${found%% *}" == "$profile_sha" ]] || die 'The reviewed seccomp profile differs.'
found="$(sha256sum < "$transport")"
[[ "${found%% *}" == "$transport_sha" ]] || die 'The reviewed evidence transport differs.'
probe_sha="$(sha256sum < "$probe")"; probe_sha="${probe_sha%% *}"
temp_root="$(realpath -- "$RUNNER_TEMP")"
run_dir="$(mktemp -d "$temp_root/ss-browser-primitive-XXXXXXXXXXXX")"
[[ "$(realpath -- "$run_dir")" == "$temp_root/"* ]] || die 'Invalid owned output directory.'
owner="$(openssl rand -hex 16)"
[[ "$owner" =~ ^[0-9a-f]{32}$ ]] || die 'Invalid resource identity.'
owner_label='org.stockandsleeve.browser-primitive.owner'
role_label='org.stockandsleeve.browser-primitive.role'
image='python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'
expected_image_id='sha256:78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db'
image_id=''; network_id=''; container_id=''; stage=initializing
controls=false; network_internal=false; probe_complete=false; available=false
capture=true; cleanup=true; delivered=true; timed_out=false
container_exit=null; attach_exit=null; user_exit=null; combined_exit=null
deadline=$((SECONDS + 180))
: > "$run_dir/probe.stdout.txt"; : > "$run_dir/probe.stderr.txt"
sanitize() {
  # No credentials are supplied to this fixture. Still reject control/URL text
  # before the reviewed base64 framing helper transports diagnostics.
  LC_ALL=C tr -cd '\11\12\15\40-\176' < "$1" |
    sed -E 's#https?://[^[:space:]]+#[redacted-url]#gI; s/((password|secret|token|authorization|cookie|credential)[[:space:]]*[:=]).*/\1[redacted]/gI'
}
source "$transport"

step() {
  local limit="$1" remaining=$((deadline - SECONDS)); shift
  ((remaining > 0)) || return 124
  ((remaining >= limit)) || limit="$remaining"
  timeout --signal=TERM --kill-after=2s "${limit}s" "$@"
}
owned_container() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]] || return 1
  local value
  value="$(timeout 5s docker inspect --type container --format \
    "{{.Id}}|{{index .Config.Labels \"$owner_label\"}}|{{index .Config.Labels \"$role_label\"}}" "$1" 2>/dev/null)" || return 1
  [[ "$value" == "$1|$owner|probe" ]]
}
owned_network() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]] || return 1
  local value
  value="$(timeout 5s docker network inspect --format \
    "{{.Id}}|{{index .Labels \"$owner_label\"}}|{{index .Labels \"$role_label\"}}" "$1" 2>/dev/null)" || return 1
  [[ "$value" == "$1|$owner|network" ]]
}
summary() {
  printf '{"scope":"base-layer-fresh-runner-namespace-availability-only","not_r3_reproduction":true,"not_policy_attribution":true,"not_browser_or_sandbox_acceptance":true,"exit_codes_are_raw_not_signal_attribution":true,"source_sha":"%s","probe_sha256":"%s","transport_sha256":"%s","profile_sha256":"%s","image":"%s","expected_image_id":"%s","actual_image_id":"%s","stage":"%s","controls_verified":%s,"network_internal":%s,"probe_complete":%s,"both_primitives_available":%s,"user_exit_code":%s,"combined_exit_code":%s,"container_exit_code":%s,"attach_exit_code":%s,"outer_timeout_reported":%s,"capture_complete":%s,"cleanup_complete":%s,"delivery_complete":%s,"harness_exit_code":%s}\n' \
    "$SS_CI_SOURCE_SHA" "$probe_sha" "$transport_sha" "$profile_sha" "$image" "$expected_image_id" "$image_id" \
    "$stage" "$controls" "$network_internal" "$probe_complete" "$available" "$user_exit" "$combined_exit" \
    "$container_exit" "$attach_exit" "$timed_out" "$capture" "$cleanup" "$delivered" "$1" > "$run_dir/sandbox-primitive-summary.json"
}
finish() {
  local status="$1" id value name count=0
  trap - EXIT INT TERM
  set +e
  # Recover an interrupted create by this unpredictable label; never by name.
  timeout 5s docker ps -aq --no-trunc --filter "label=$owner_label=$owner" > "$run_dir/containers.txt" 2> /dev/null || cleanup=false
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    ((count += 1)); ((count <= 1)) || { cleanup=false; continue; }
    if owned_container "$id"; then
      timeout --kill-after=2s 8s docker stop --time 2 "$id" > /dev/null 2>&1 || cleanup=false
      value="$(timeout 5s docker inspect --format '{{.State.Running}}|{{.State.ExitCode}}|{{.State.OOMKilled}}' "$id" 2>/dev/null)"
      if [[ "$value" =~ ^false\|([0-9]{1,3})\|(true|false)$ ]]; then
        container_exit="${BASH_REMATCH[1]}"
        [[ "${BASH_REMATCH[2]}" == false ]] || capture=false
      else capture=false; fi
      timeout --kill-after=2s 8s docker rm --force --volumes "$id" > /dev/null 2>&1 || cleanup=false
    else cleanup=false; fi
  done < "$run_dir/containers.txt"
  count=0
  timeout 5s docker network ls -q --no-trunc --filter "label=$owner_label=$owner" > "$run_dir/networks.txt" 2> /dev/null || cleanup=false
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    ((count += 1)); ((count <= 1)) || { cleanup=false; continue; }
    if owned_network "$id"; then
      timeout 5s docker network rm "$id" > /dev/null 2>&1 || cleanup=false
    else cleanup=false; fi
  done < "$run_dir/networks.txt"
  value="$(timeout 5s docker ps -aq --no-trunc --filter "label=$owner_label=$owner" 2>/dev/null)"
  [[ "$?" == 0 && -z "$value" ]] || cleanup=false
  value="$(timeout 5s docker network ls -q --no-trunc --filter "label=$owner_label=$owner" 2>/dev/null)"
  [[ "$?" == 0 && -z "$value" ]] || cleanup=false
  # Shared immutable base image is not an owned image; never remove it.
  [[ "$cleanup" == true && "$capture" == true && "$controls" == true &&
     "$network_internal" == true && "$probe_complete" == true && "$available" == true &&
     "$container_exit" == 0 && "$attach_exit" == 0 ]] || status=1
  for name in stdout stderr; do
    emit_file "diagnostics/primitive-probe.$name.txt" "$run_dir/probe.$name.txt" || { delivered=false; status=1; }
  done
  if summary "$status"; then
    if ! emit_file evidence/sandbox-primitive-summary.json "$run_dir/sandbox-primitive-summary.json"; then
      delivered=false; status=1; summary "$status" || :
    fi
  else
    emit_problem evidence/sandbox-primitive-summary.json summary_write_failed || :
    status=1
  fi
  exit "$status"
}
trap 'finish "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stage=pull_pinned_base
step 60 docker pull "$image" > "$run_dir/pull.log" 2>&1
stage=verify_pinned_base
image_id="$(step 5 docker image inspect --format '{{.Id}}' "$image")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || { image_id=''; die 'Invalid base image ID.'; }
[[ "$image_id" == "$expected_image_id" ]] || die 'Pinned base image ID differs.'
stage=create_internal_network
network_id="$(step 10 docker network create --internal --label "$owner_label=$owner" \
  --label "$role_label=network" "ss-primitive-$owner")"
owned_network "$network_id" || die 'Owned network identity differs.'
[[ "$(step 5 docker network inspect --format '{{.Internal}}' "$network_id")" == true ]] || die 'Network is not internal.'
network_internal=true
stage=create_probe
container_id="$(step 10 docker create --name "ss-primitive-$owner" \
  --label "$owner_label=$owner" --label "$role_label=probe" --network "$network_id" \
  --user 10001:10001 --memory 4g --cpus 2 --pids-limit 512 --ipc private --shm-size 512m \
  --cap-drop ALL --security-opt no-new-privileges=true --security-opt "seccomp=$profile" \
  --init --entrypoint /bin/sh --env LC_ALL=C --workdir / "$image_id" /ci-probe.sh)"
owned_container "$container_id" || die 'Owned container identity differs.'
stage=verify_controls
value="$(step 5 docker inspect --format '{{.Config.User}}|{{.HostConfig.Privileged}}|{{.HostConfig.IpcMode}}|{{.HostConfig.ShmSize}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{json .HostConfig.CapDrop}}|{{.HostConfig.Init}}|{{.HostConfig.NetworkMode}}|{{len .Mounts}}|{{len .NetworkSettings.Networks}}|{{json .Config.Entrypoint}}|{{json .Config.Cmd}}' "$container_id")"
[[ "$value" == "10001:10001|false|private|536870912|4294967296|2000000000|512|[\"ALL\"]|true|$network_id|0|1|[\"/bin/sh\"]|[\"/ci-probe.sh\"]" ]] || die 'Required runtime controls differ.'
value="$(step 5 docker inspect --format '{{json .HostConfig.CapAdd}}' "$container_id")"
[[ "$value" == null || "$value" == '[]' ]] || die 'Added capabilities are forbidden.'
# Docker CLI parseSecurityOpts reads and compacts the profile into seccomp=JSON.
# Inspect the array as JSON, require exactly NNP + seccomp, then compare the
# complete canonical profile (including all rule arrays) to the pinned file.
# Existing host jq is mandatory; no package installation or pathname fallback.
( ulimit -f 128 || exit 1
  step 5 docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$container_id" > "$run_dir/security-options.json" || exit 1
  step 5 jq -e '
    if type != "array" then false
    elif length != 2 then false
    elif any(.[]; type != "string") then false
    else
      ([.[] | select(. == "no-new-privileges" or . == "no-new-privileges=true")] | length) == 1 and
      ([.[] | select(startswith("seccomp="))] | length) == 1
    end' "$run_dir/security-options.json" > /dev/null || exit 1
  step 5 jq -er '.[] | select(startswith("seccomp=")) | ltrimstr("seccomp=")' \
    "$run_dir/security-options.json" > "$run_dir/actual-seccomp.json" || exit 1
  step 5 jq -cSe 'if type == "object" then . else error("profile_not_object") end' \
    "$run_dir/actual-seccomp.json" > "$run_dir/actual-seccomp.canonical.json" || exit 1
  step 5 jq -cSe 'if type == "object" then . else error("profile_not_object") end' \
    "$profile" > "$run_dir/expected-seccomp.canonical.json" || exit 1
) || die 'Security-option JSON or pinned profile validation failed.'
for file in security-options.json actual-seccomp.json actual-seccomp.canonical.json expected-seccomp.canonical.json; do
  size="$(wc -c < "$run_dir/$file")"
  [[ "$size" =~ ^[0-9]+$ && "$size" -gt 0 && "$size" -le 131072 ]] || die 'Security-option data exceeded its bound.'
done
cmp -- "$run_dir/actual-seccomp.canonical.json" "$run_dir/expected-seccomp.canonical.json" > /dev/null ||
  die 'Actual seccomp JSON differs from the exact pinned profile.'
controls=true
stage=copy_exact_probe
cp -- "$probe" "$run_dir/ci-probe.sh"; chmod 0444 "$run_dir/ci-probe.sh"
step 5 docker cp "$run_dir/ci-probe.sh" "$container_id:/ci-probe.sh"
step 5 docker cp "$container_id:/ci-probe.sh" "$run_dir/copied-probe.sh"
cmp -- "$probe" "$run_dir/copied-probe.sh" || die 'Copied probe differs.'
stage=run_two_subchecks
set +e
# Bash file-size limit bounds both attachment files even on unexpected output.
# The outer container cleanup kills the complete owned fixture on timeout.
( ulimit -f 64; timeout --signal=TERM --kill-after=2s 25s docker start --attach "$container_id" ) \
  > "$run_dir/probe.stdout.txt" 2> "$run_dir/probe.stderr.txt"
attach_exit=$?
set -e
[[ "$attach_exit" != 124 ]] || timed_out=true
for file in "$run_dir/probe.stdout.txt" "$run_dir/probe.stderr.txt"; do
  size="$(wc -c < "$file")"
  [[ "$size" =~ ^[0-9]+$ && "$size" -le 65536 ]] || { capture=false; die 'Output exceeded the diagnostic bound.'; }
done
stage=collect_result
result="$(grep -E '^SS_PRIMITIVE_RESULT user_exit=[0-9]{1,3} combined_exit=[0-9]{1,3}$' "$run_dir/probe.stdout.txt" || :)"
if [[ "$result" =~ ^SS_PRIMITIVE_RESULT\ user_exit=([0-9]{1,3})\ combined_exit=([0-9]{1,3})$ ]]; then
  user_exit="${BASH_REMATCH[1]}"; combined_exit="${BASH_REMATCH[2]}"; probe_complete=true
  [[ "$user_exit" != 0 || "$combined_exit" != 0 ]] || available=true
fi
[[ "$probe_complete" == true && "$available" == true && "$attach_exit" == 0 ]] || exit 1
stage=diagnostic_complete
