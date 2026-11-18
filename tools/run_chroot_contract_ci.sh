#!/usr/bin/env bash
# Experimental four-case OS contract; never adopts a profile for Chromium.
set +x
set -Eeuo pipefail
export LC_ALL=C
umask 077
die() { printf '%s\n' "$1" >&2; exit 1; }
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || die 'Reviewed Linux x86_64 runner required.'
for command in docker openssl timeout git realpath mktemp sha256sum base64 wc cat rm chmod cp cmp tr sed jq grep env stat head mkdir; do
  command -v "$command" >/dev/null || die 'Required existing CI utility missing; installation is forbidden.'
done
[[ -x /usr/bin/gcc && -x /usr/bin/readelf ]] || die 'Existing gcc/readelf toolchain required.'
[[ -z "${DOCKER_HOST+x}" && -z "${DOCKER_CONTEXT+x}" && -z "${DOCKER_TLS_VERIFY+x}" && -z "${DOCKER_CERT_PATH+x}" ]] || die 'Docker endpoint overrides are forbidden.'
[[ "${SS_CI_SOURCE_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || die 'Exact public source SHA required.'
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" && ! -L "$RUNNER_TEMP" && -O "$RUNNER_TEMP" ]] || die 'Owned runner temporary directory required.'
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_CI_SOURCE_SHA" && -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || die 'Exact clean checkout required.'
original="$source_root/tools/hosted_browser-seccomp.json"
experimental="$source_root/tools/chroot-contract-seccomp.json"
source_c="$source_root/tools/chroot_contract_probe.c"
cases_sh="$source_root/tools/chroot_contract_cases.sh"
transport="$source_root/tools/ci_evidence_transport.sh"
original_sha='e56c2f17869890cbd1fed1b44e584af59ffca5f44cb946832b194c5662cc049f'
experimental_sha='95221e255acfee0a4727c3cdfd266bde20b3117d548b0c8a809b3978366c4953'
c_sha='186d9874840575e9667b6c1694507cf860949cbb7d8f079c46a271180db0c56a'
cases_sha='0bfe8f45394fa39198e2f10d7f3abc0a1a4fb66b5f1511571700c96ce0d5c602'
transport_sha='870c8d0ef39bc5f80d9ebfdd5f02ecc41a587d431f0fadaf52d0cf0adf250206'
pin() {
  [[ -f "$1" && ! -L "$1" && -r "$1" ]] || return 1
  local value; value="$(sha256sum < "$1")" || return 1
  [[ "${value%% *}" == "$2" ]]
}
pin "$original" "$original_sha" && pin "$experimental" "$experimental_sha" &&
  pin "$source_c" "$c_sha" && pin "$cases_sh" "$cases_sha" && pin "$transport" "$transport_sha" || die 'Pinned contract source differs.'
temp_root="$(realpath -- "$RUNNER_TEMP")"
run_dir="$(mktemp -d "$temp_root/ss-chroot-contract-XXXXXXXXXXXX")"
[[ "$(realpath -- "$run_dir")" == "$temp_root/"* ]] || die 'Invalid owned output directory.'
owner="$(openssl rand -hex 16)"; [[ "$owner" =~ ^[0-9a-f]{32}$ ]] || die 'Invalid resource identity.'
owner_label='org.stockandsleeve.chroot-contract.owner'
role_label='org.stockandsleeve.chroot-contract.role'
image='python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b'
expected_image_id='sha256:78976a69c067520ef8be34cde307ef9f6fea6dcefa50edb57039773d241ca0db'
image_id=''; binary_sha=''; network_id=''; stage=initializing
cleanup=true; capture=false; delivered=true; profile_delta=false; matrix=false; static_verified=false
declare -A profile_exit=([original]=null [experimental]=null)
declare -A container_exit=([original]=null [experimental]=null)
declare -A attach_exit=([original]=null [experimental]=null)
declare -A controls=([original]=false [experimental]=false)
deadline=$((SECONDS + 180))
mkdir "$run_dir/compile-tmp"
cp -- "$original" "$run_dir/original-requested-profile.json"
cp -- "$experimental" "$run_dir/experimental-requested-profile.json"
for name in original experimental; do
  : > "$run_dir/$name.stdout.jsonl"; : > "$run_dir/$name.stderr.txt"
  printf 'null\n' > "$run_dir/$name-inspected-security-options.json"
  printf 'not_started\n' > "$run_dir/$name.stage"
done
printf '{"observed":false}\n' > "$run_dir/docker-server.json"
printf '{"observed":false}\n' > "$run_dir/toolchain.json"
: > "$run_dir/toolchain.log"
sanitize() {
  LC_ALL=C tr -cd '\11\12\15\40-\176' < "$1" |
    sed -E 's#https?://[^[:space:]]+#[redacted-url]#gI; s/((password|secret|token|authorization|cookie|credential)[[:space:]]*[:=]).*/\1[redacted]/gI'
}
source "$transport"
step() {
  local limit="$1" remaining=$((deadline-SECONDS)); shift
  ((remaining>0)) || return 124
  ((remaining>=limit)) || limit="$remaining"
  timeout --signal=TERM --kill-after=2s "${limit}s" "$@"
}
owned_container() {
  [[ "$1" =~ ^[0-9a-f]{64}$ && ( "$2" == original || "$2" == experimental ) ]] || return 1
  local found
  found="$(timeout 4s docker inspect --type container --format "{{.Id}}|{{index .Config.Labels \"$owner_label\"}}|{{index .Config.Labels \"$role_label\"}}" "$1" 2>/dev/null)" || return 1
  [[ "$found" == "$1|$owner|$2" ]]
}
stop_profile() {
  local role="$1" id found count=0
  timeout 4s docker ps -aq --no-trunc --filter "label=$owner_label=$owner" --filter "label=$role_label=$role" > "$run_dir/$role.ids" 2>/dev/null || return 1
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    ((count+=1)); ((count<=1)) || return 1
    owned_container "$id" "$role" || return 1
    timeout --kill-after=2s 7s docker stop --time 2 "$id" > /dev/null 2>&1 || return 1
    found="$(timeout 4s docker inspect --format '{{.State.Running}}|{{.State.ExitCode}}|{{.State.OOMKilled}}' "$id" 2>/dev/null)" || return 1
    [[ "$found" =~ ^false\|([0-9]{1,3})\|false$ ]] || return 1
    container_exit[$role]="${BASH_REMATCH[1]}"
  done < "$run_dir/$role.ids"
}
write_summary() {
  local original_stage experimental_stage
  original_stage="$(cat "$run_dir/original.stage")"; experimental_stage="$(cat "$run_dir/experimental.stage")"
  [[ "$original_stage" =~ ^[a-z_]{1,64}$ && "$experimental_stage" =~ ^[a-z_]{1,64}$ ]] || return 1
  jq -n --arg source "$SS_CI_SOURCE_SHA" --arg stage "$stage" --arg image "$image" --arg image_id "$image_id" \
    --arg original_stage "$original_stage" --arg experimental_stage "$experimental_stage" \
    --arg source_c "$c_sha" --arg binary "$binary_sha" --arg original "$original_sha" --arg experimental "$experimental_sha" \
    --argjson delta "$profile_delta" --argjson static "$static_verified" --argjson matrix "$matrix" \
    --argjson original_controls "${controls[original]}" --argjson experimental_controls "${controls[experimental]}" \
    --argjson original_profile_exit "${profile_exit[original]}" --argjson experimental_profile_exit "${profile_exit[experimental]}" \
    --argjson original_container_exit "${container_exit[original]}" --argjson experimental_container_exit "${container_exit[experimental]}" \
    --argjson original_attach_exit "${attach_exit[original]}" --argjson experimental_attach_exit "${attach_exit[experimental]}" \
    --argjson cleanup "$cleanup" --argjson capture "$capture" --argjson delivered "$delivered" --argjson exit_code "$1" \
    '{schema:1,scope:"experimental-four-case-chroot-profile-contract-only",source_sha:$source,stage:$stage,image:$image,image_id:$image_id,
      source_c_sha256:$source_c,binary_sha256:$binary,original_profile_sha256:$original,experimental_profile_sha256:$experimental,
      only_one_unconditional_chroot_rule_added:$delta,static_elf_verified:$static,matrix_passed:$matrix,
      original:{stage:$original_stage,controls_verified:$original_controls,profile_harness_exit:$original_profile_exit,container_exit:$original_container_exit,attach_exit:$original_attach_exit},
      experimental:{stage:$experimental_stage,controls_verified:$experimental_controls,profile_harness_exit:$experimental_profile_exit,container_exit:$experimental_container_exit,attach_exit:$experimental_attach_exit},
      expected_cases:[{id:"CHROOT01",profile:"original",mode:"outer",return:-1,errno:1},{id:"CHROOT02",profile:"original",mode:"inner",return:-1,errno:1},
                      {id:"CHROOT03",profile:"experimental",mode:"outer",return:-1,errno:1},{id:"CHROOT04",profile:"experimental",mode:"inner",return:0,errno:0}],
      policy_evidence:"requested bytes and complete inspected JSON equality plus process observations; installed BPF not dumped or attested",
      daemon_vendored_source_binding_verified:false,r3_reproduction:false,r3_cause_proven:false,browser_adoption:false,browser_launched:false,
      cleanup_complete:$cleanup,capture_complete:$capture,delivery_complete:$delivered,harness_exit_code:$exit_code}' > "$run_dir/contract-summary.json"
}
finish() {
  local status="$1" role id found count=0 name
  trap - EXIT INT TERM; set +e
  for role in original experimental; do stop_profile "$role" || cleanup=false; done
  timeout 4s docker ps -aq --no-trunc --filter "label=$owner_label=$owner" > "$run_dir/owned.ids" 2>/dev/null || cleanup=false
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    ((count+=1)); ((count<=2)) || { cleanup=false; continue; }
    role="$(timeout 4s docker inspect --format "{{index .Config.Labels \"$role_label\"}}" "$id" 2>/dev/null)"
    if owned_container "$id" "$role"; then timeout --kill-after=2s 7s docker rm --force --volumes "$id" > /dev/null 2>&1 || cleanup=false
    else cleanup=false; fi
  done < "$run_dir/owned.ids"
  timeout 4s docker network ls -q --no-trunc --filter "label=$owner_label=$owner" > "$run_dir/network.ids" 2>/dev/null || cleanup=false
  count=0
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    ((count+=1)); ((count<=1)) || { cleanup=false; continue; }
    [[ "$id" =~ ^[0-9a-f]{64}$ ]] || { cleanup=false; continue; }
    found="$(timeout 4s docker network inspect --format "{{.Id}}|{{index .Labels \"$owner_label\"}}|{{index .Labels \"$role_label\"}}" "$id" 2>/dev/null)"
    if [[ "$found" == "$id|$owner|network" ]]; then timeout 4s docker network rm "$id" > /dev/null 2>&1 || cleanup=false
    else cleanup=false; fi
  done < "$run_dir/network.ids"
  found="$(timeout 4s docker ps -aq --no-trunc --filter "label=$owner_label=$owner" 2>/dev/null)"
  [[ "$?" == 0 && -z "$found" ]] || cleanup=false
  found="$(timeout 4s docker network ls -q --no-trunc --filter "label=$owner_label=$owner" 2>/dev/null)"
  [[ "$?" == 0 && -z "$found" ]] || cleanup=false
  for role in original experimental; do
    [[ "${profile_exit[$role]}" == 0 && "${container_exit[$role]}" == 0 && "${attach_exit[$role]}" == 0 && "${controls[$role]}" == true ]] || status=1
  done
  [[ "$cleanup" == true && "$capture" == true && "$profile_delta" == true && "$static_verified" == true && "$matrix" == true ]] || status=1
  # Twelve fixed frames; missing observations remain explicit, never synthetic PASS.
  for name in original-requested-profile.json experimental-requested-profile.json original-inspected-security-options.json experimental-inspected-security-options.json docker-server.json toolchain.json; do
    emit_file "evidence/$name" "$run_dir/$name" || { delivered=false; status=1; }
  done
  for name in original.stdout.jsonl original.stderr.txt experimental.stdout.jsonl experimental.stderr.txt toolchain.log; do
    emit_file "diagnostics/$name" "$run_dir/$name" || { delivered=false; status=1; }
  done
  if write_summary "$status"; then
    emit_file evidence/contract-summary.json "$run_dir/contract-summary.json" || { delivered=false; status=1; write_summary "$status" || :; }
  else emit_problem evidence/contract-summary.json summary_write_failed || :; status=1; fi
  exit "$status"
}
trap 'finish "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stage=verify_one_rule_delta
step 5 jq -e --slurpfile original "$original" '
  . == ($original[0] | .syscalls = ([{names:["chroot"],action:"SCMP_ACT_ALLOW",args:[],includes:{},excludes:{}}] + .syscalls))' "$experimental" > /dev/null
profile_delta=true
stage=record_direct_docker_server
( ulimit -f 32; step 5 docker version --format '{"Version":{{json .Server.Version}},"GitCommit":{{json .Server.GitCommit}},"ApiVersion":{{json .Server.APIVersion}},"Os":{{json .Server.Os}},"Arch":{{json .Server.Arch}}' > "$run_dir/docker-server.json" )
step 5 jq -e 'type=="object" and keys==["ApiVersion","Arch","GitCommit","Os","Version"] and .Os=="linux" and .Arch=="amd64" and all(.[]; type=="string" and length>0 and length<=128)' "$run_dir/docker-server.json" > /dev/null
stage=compile_one_static_fixture
compiler_path="$(realpath /usr/bin/gcc)"; readelf_path="$(realpath /usr/bin/readelf)"
for file in "$compiler_path" "$readelf_path"; do
  [[ -f "$file" && -x "$file" && ! -u "$file" && ! -g "$file" && "$(stat -c '%u:%g' "$file")" == 0:0 ]] || die 'Toolchain identity rejected.'
done
compiler_version="$(step 5 /usr/bin/gcc -dumpfullversion -dumpversion)"
compiler_target="$(step 5 /usr/bin/gcc -dumpmachine)"
[[ "$compiler_version" =~ ^[0-9]+(\.[0-9]+){1,3}$ && "$compiler_target" == x86_64-linux-gnu ]] || die 'Unexpected compiler version/target.'
set +e
step 30 env -i PATH=/usr/bin:/bin LC_ALL=C TMPDIR="$run_dir/compile-tmp" \
  /usr/bin/gcc -std=c11 -D_GNU_SOURCE -O2 -Wall -Wextra -Werror -fno-pie -no-pie -static \
  -Wl,--build-id=none -Wl,-z,noexecstack "-DSS_SOURCE_SHA256=\"$c_sha\"" \
  "$source_c" -o "$run_dir/ci-contract-probe" 2>&1 | head -c 65536 > "$run_dir/toolchain.log"
compile_status=("${PIPESTATUS[@]}")
set -e
[[ "${compile_status[0]}" == 0 && "${compile_status[1]}" == 0 && "$(wc -c < "$run_dir/toolchain.log")" -lt 65536 ]] || die 'Single static compile failed or output exceeded its bound.'
binary_bytes="$(wc -c < "$run_dir/ci-contract-probe")"
[[ "$binary_bytes" =~ ^[0-9]+$ && "$binary_bytes" -gt 0 && "$binary_bytes" -le 4194304 ]] || die 'Compiled fixture size rejected.'
stage=verify_static_elf
step 5 /usr/bin/readelf --file-header "$run_dir/ci-contract-probe" > "$run_dir/elf-header.txt"
step 5 /usr/bin/readelf --program-headers --wide "$run_dir/ci-contract-probe" > "$run_dir/elf-program.txt"
step 5 /usr/bin/readelf --dynamic "$run_dir/ci-contract-probe" > "$run_dir/elf-dynamic.txt"
grep -Eq 'Class:[[:space:]]+ELF64' "$run_dir/elf-header.txt"
grep -Eq 'Data:[[:space:]]+2.s complement, little endian' "$run_dir/elf-header.txt"
grep -Eq 'Type:[[:space:]]+EXEC ' "$run_dir/elf-header.txt"
grep -Eq 'Machine:[[:space:]]+Advanced Micro Devices X86-64' "$run_dir/elf-header.txt"
step 5 jq -Rse 'test("(^|[[:space:]])(INTERP|DYNAMIC)([[:space:]]|$)")|not' "$run_dir/elf-program.txt" > /dev/null
step 5 jq -Rse 'contains("(NEEDED)")|not' "$run_dir/elf-dynamic.txt" > /dev/null
cat "$run_dir/elf-header.txt" "$run_dir/elf-program.txt" "$run_dir/elf-dynamic.txt" >> "$run_dir/toolchain.log"
binary_sha="$(sha256sum < "$run_dir/ci-contract-probe")"; binary_sha="${binary_sha%% *}"
compiler_sha="$(sha256sum < "$compiler_path")"; compiler_sha="${compiler_sha%% *}"
readelf_sha="$(sha256sum < "$readelf_path")"; readelf_sha="${readelf_sha%% *}"
jq -n --arg source "$c_sha" --arg binary "$binary_sha" --arg compiler "$compiler_version" --arg target "$compiler_target" \
  --arg compiler_sha "$compiler_sha" --arg readelf_sha "$readelf_sha" \
  '{observed:true,source_sha256:$source,binary_sha256:$binary,compiler_version:$compiler,compiler_target:$target,compiler_binary_sha256:$compiler_sha,readelf_binary_sha256:$readelf_sha,
    flags:["-std=c11","-D_GNU_SOURCE","-O2","-Wall","-Wextra","-Werror","-fno-pie","-no-pie","-static","-Wl,--build-id=none","-Wl,-z,noexecstack","-DSS_SOURCE_SHA256=<pinned C hash>"],
    elf:{class:"ELF64",machine:"x86_64",type:"EXEC",interpreter:false,dynamic_segment:false,needed_libraries:false},compile_count:1}' > "$run_dir/toolchain.json"
static_verified=true
stage=pull_exact_base
step 60 docker pull "$image" > "$run_dir/pull.log" 2>&1
image_id="$(step 5 docker image inspect --format '{{.Id}}' "$image")"
[[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || { image_id=''; die 'Invalid base image ID.'; }
[[ "$image_id" == "$expected_image_id" ]] || die 'Base image ID mismatch.'
network_id="$(step 8 docker network create --internal --label "$owner_label=$owner" --label "$role_label=network" "ss-chroot-contract-$owner")"
[[ "$network_id" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid network ID.'
value="$(step 4 docker network inspect --format "{{.Id}}|{{index .Labels \"$owner_label\"}}|{{index .Labels \"$role_label\"}}|{{.Internal}}" "$network_id")"
[[ "$value" == "$network_id|$owner|network|true" ]] || die 'Owned network is not internal.'
chmod 0555 "$run_dir/ci-contract-probe"
cp -- "$cases_sh" "$run_dir/ci-contract.sh"; chmod 0444 "$run_dir/ci-contract.sh"

run_profile() (
  trap - EXIT INT TERM
  set -Eeuo pipefail
  role="$1"; profile="$2"
  pstage() { printf '%s\n' "$1" > "$run_dir/$role.stage"; }
  pstage create
  id="$(step 8 docker create --name "ss-chroot-$role-$owner" --label "$owner_label=$owner" --label "$role_label=$role" \
    --network "$network_id" --user 10001:10001 --memory 4g --cpus 2 --pids-limit 512 --ipc private --shm-size 512m \
    --cap-drop ALL --security-opt no-new-privileges=true --security-opt "seccomp=$profile" --init \
    --entrypoint /bin/sh --env LC_ALL=C --workdir / "$image_id" /ci-contract.sh "$role")"
  owned_container "$id" "$role"
  pstage verify_controls
  value="$(step 4 docker inspect --format '{{.Config.User}}|{{.HostConfig.Privileged}}|{{.HostConfig.IpcMode}}|{{.HostConfig.ShmSize}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{json .HostConfig.CapDrop}}|{{.HostConfig.Init}}|{{.HostConfig.NetworkMode}}|{{len .Mounts}}|{{len .NetworkSettings.Networks}}|{{json .Config.Entrypoint}}|{{json .Config.Cmd}}' "$id")"
  [[ "$value" == "10001:10001|false|private|536870912|4294967296|2000000000|512|[\"ALL\"]|true|$network_id|0|1|[\"/bin/sh\"]|[\"/ci-contract.sh\",\"$role\"]" ]]
  value="$(step 4 docker inspect --format '{{json .HostConfig.CapAdd}}|{{json .HostConfig.GroupAdd}}' "$id")"
  [[ "$value" == 'null|null' || "$value" == '[]|[]' || "$value" == 'null|[]' || "$value" == '[]|null' ]]
  ( ulimit -f 128; step 4 docker inspect --format '{{json .HostConfig.SecurityOpt}}' "$id" > "$run_dir/$role-inspected-security-options.json" )
  step 4 jq -e 'if type!="array" then false elif length!=2 then false elif any(.[];type!="string") then false else
    ([.[]|select(.=="no-new-privileges" or .=="no-new-privileges=true")]|length)==1 and
    ([.[]|select(startswith("seccomp="))]|length)==1 end' "$run_dir/$role-inspected-security-options.json" > /dev/null
  ( ulimit -f 128
    step 4 jq -er '.[]|select(startswith("seccomp="))|ltrimstr("seccomp=")' "$run_dir/$role-inspected-security-options.json" > "$run_dir/$role.actual.json"
    step 4 jq -cSe 'if type=="object" then . else error("not_object") end' "$run_dir/$role.actual.json" > "$run_dir/$role.actual.canonical.json"
    step 4 jq -cSe . "$profile" > "$run_dir/$role.expected.canonical.json"
  )
  cmp -- "$run_dir/$role.actual.canonical.json" "$run_dir/$role.expected.canonical.json"
  printf 'true\n' > "$run_dir/$role.controls"
  pstage copy_exact_fixture
  step 4 docker cp "$run_dir/ci-contract-probe" "$id:/ci-contract-probe"
  step 4 docker cp "$run_dir/ci-contract.sh" "$id:/ci-contract.sh"
  step 4 docker cp "$id:/ci-contract-probe" "$run_dir/$role.copied-probe"
  step 4 docker cp "$id:/ci-contract.sh" "$run_dir/$role.copied-script"
  cmp -- "$run_dir/ci-contract-probe" "$run_dir/$role.copied-probe"
  cmp -- "$cases_sh" "$run_dir/$role.copied-script"
  pstage execute_two_cases
  set +e
  ( ulimit -f 64; timeout --signal=TERM --kill-after=2s 20s docker start --attach "$id" ) > "$run_dir/$role.stdout.jsonl" 2> "$run_dir/$role.stderr.txt"
  code=$?
  set -e
  printf '%s\n' "$code" > "$run_dir/$role.attach-exit"
  for file in "$run_dir/$role.stdout.jsonl" "$run_dir/$role.stderr.txt"; do
    size="$(wc -c < "$file")"; [[ "$size" =~ ^[0-9]+$ && "$size" -le 65536 ]]
  done
  pstage captured
  [[ "$code" == 0 ]]
)
stage=run_two_sequential_containers
for role in original experimental; do
  profile="$original"; [[ "$role" == original ]] || profile="$experimental"
  set +e
  run_profile "$role" "$profile" > "$run_dir/$role.control.log" 2>&1
  code=$?
  set -e
  profile_exit[$role]="$code"
  [[ ! -f "$run_dir/$role.controls" ]] || controls[$role]=true
  if [[ -f "$run_dir/$role.attach-exit" ]]; then
    value="$(cat "$run_dir/$role.attach-exit")"
    [[ "$value" =~ ^[0-9]{1,3}$ ]] || die 'Invalid attachment exit metadata.'
    attach_exit[$role]="$value"
  fi
  # No second container is started until the first is verified stopped.
  stop_profile "$role" || die 'Owned profile container did not stop cleanly.'
done
[[ "${profile_exit[original]}" != 0 || "${profile_exit[experimental]}" != 0 ]] || capture=true
stage=validate_literal_four_case_matrix
step 5 jq -s -e --arg source "$c_sha" '
  length==4 and map(.case_id)==["CHROOT01","CHROOT02","CHROOT03","CHROOT04"] and
  all(.[]; .schema==1 and .context_valid==true and .stage=="chroot_complete" and .chroot_attempted==true and .target=="/" and
    .source_sha256==$source and .nnp==1 and .seccomp==2 and .seccomp_filters>=1 and .machine=="x86_64" and .executable.file_capabilities_absent==true) and
  (.[0].profile=="original" and .[1].profile=="original" and .[2].profile=="experimental" and .[3].profile=="experimental") and
  (.[0].mode=="outer" and .[1].mode=="inner" and .[2].mode=="outer" and .[3].mode=="inner") and
  all(.[0,2]; .uid==[10001,10001,10001,10001] and .gid==[10001,10001,10001,10001] and
    all(.caps[];.=="0000000000000000") and .effective_chroot_capability==false and .user_ns==.parent_user_ns) and
  all(.[1,3]; .uid==[0,0,0,0] and .gid==[0,0,0,0] and .effective_chroot_capability==true and
    .uid_map==[[0,10001,1]] and .gid_map==[[0,10001,1]] and .setgroups=="deny" and .user_ns!=.parent_user_ns) and
  (.[0].user_ns==.[1].parent_user_ns and .[2].user_ns==.[3].parent_user_ns and .[0].user_ns==.[2].user_ns) and
  ([.[].kernel_release]|unique|length)==1 and ([.[].lsm]|unique|length)==1 and ([.[].seccomp_filters]|unique|length)==1 and
  all(.[0,1,2];.chroot_return== -1 and .chroot_errno==1) and (.[3].chroot_return==0 and .[3].chroot_errno==0)
  ' "$run_dir/original.stdout.jsonl" "$run_dir/experimental.stdout.jsonl" > /dev/null
matrix=true
stage=contract_complete
