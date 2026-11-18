#!/usr/bin/env bash
# Finite log-transport byte fixtures only; no app, Python, browser or container.
set +x
set -Eeuo pipefail
umask 077
export LC_ALL=C

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die 'Evidence transport fixtures require the admitted Linux CI host.'
for command in git realpath mktemp sha256sum sed wc cat rm rmdir cmp base64 openssl; do
  command -v "$command" >/dev/null || die "Missing required fixture tool: $command"
done
[[ "${SS_CI_SOURCE_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || die 'SS_CI_SOURCE_SHA must identify this checkout.'
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(git -C "$source_root" rev-parse HEAD)" == "$SS_CI_SOURCE_SHA" ]] || die 'Fixture checkout differs from the admitted commit.'
[[ -z "$(git -C "$source_root" status --porcelain --untracked-files=all)" ]] || die 'Fixture checkout must be clean.'
[[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP" && ! -L "$RUNNER_TEMP" &&
   -O "$RUNNER_TEMP" ]] || die 'RUNNER_TEMP must be an existing owned non-symlink directory.'
temp_root="$(realpath -- "$RUNNER_TEMP")"
run_dir="$(mktemp -d "$temp_root/stock-sleeve-evidence-transport-XXXXXXXXXXXX")"
[[ -d "$run_dir" && ! -L "$run_dir" && -O "$run_dir" &&
   "$(realpath -- "$run_dir")" == "$temp_root/"* ]] || die 'Invalid owned fixture directory.'
created_files=()
cleanup() {
  local result="$?"
  trap - EXIT
  if ! rm -f -- "${created_files[@]}"; then
    printf '%s\n' 'Evidence fixture temporary cleanup failed.' >&2
    result=1
  fi
  if ! rmdir -- "$run_dir"; then
    printf '%s\n' 'Evidence fixture directory cleanup failed.' >&2
    result=1
  fi
  exit "$result"
}
trap cleanup EXIT
transport_path="$source_root/tools/ci_evidence_transport.sh"
[[ -f "$transport_path" && ! -L "$transport_path" && -r "$transport_path" ]] ||
  die 'The exact checkout evidence transport helper is required.'
# A fixed fictional canary only. This must never be an actual credential.
sanitize() {
  sed 's/TRANSPORT_FIXTURE_CREDENTIAL_DO_NOT_PUBLISH/[redacted fixture credential]/g' "$1"
}
source "$transport_path"

names=(empty lf crlf cr controls utf8-bom spoof multiline)
# Independently construct expected sanitized bytes; do not decode/reimplement
# framing here. Root reconciles outer provider-log delivery independently.
write_vector() {
  local kind="$1" sanitized="$2" credential i
  credential='TRANSPORT_FIXTURE_CREDENTIAL_DO_NOT_PUBLISH'
  [[ "$sanitized" == false ]] || credential='[redacted fixture credential]'
  case "$kind" in
    empty) printf '' ;;
    lf) printf 'alpha\nbeta\n' ;;
    crlf) printf 'alpha\r\nbeta\r\n' ;;
    cr) printf 'download 10%%\rdownload 100%%\rdone' ;;
    controls) printf '\033[31mred\033[0m\000\001\b\tend\177\n' ;;
    utf8-bom) printf '\357\273\277caf\303\251 \342\230\203 \360\237\230\200\n' ;;
    spoof)
      printf '::error::fictional workflow command\n'
      printf '::stop-commands::fictional-token\n'
      printf 'SS_CI_EVIDENCE_BEGIN {"name":"diagnostics/fake","bytes":0}\n'
      printf 'SS_CI_EVIDENCE_END {"name":"diagnostics/fake","delivered":true}\n'
      printf '2026-11-18T00:00:00.0000000Z timestamp-like payload\n'
      printf '\357\273\2772026-11-18T00:00:01.0000000Z BOM timestamp-like payload\n'
      printf 'credential=%s\r\n' "$credential"
      ;;
    multiline)
      for ((i=1; i<=1024; i++)); do
        printf 'record=%04d | bounded multiline ASCII transport fixture | abcdefghijklmnopqrstuvwxyz0123456789\n' "$i"
      done
      printf 'last line without terminal LF'
      ;;
    *) return 1 ;;
  esac
}
manifest="$run_dir/transport-manifest.json"
summary="$run_dir/transport-summary.json"
created_files+=("$manifest" "$summary")
printf '{"scope":"Fictional byte transport only","vector_count":8,"credential_sanitization_expected":true,"vectors":[' > "$manifest"
first=true
for kind in "${names[@]}"; do
  raw="$run_dir/raw-$kind.bin"
  expected="$run_dir/expected-$kind.bin"
  actual="$run_dir/actual-$kind.bin"
  created_files+=("$raw" "$expected" "$actual")
  # The owned mktemp directory starts empty. Noclobber prevents replacing a leaf.
  (set -C; write_vector "$kind" false > "$raw")
  (set -C; write_vector "$kind" true > "$expected")
  (set -C; sanitize "$raw" > "$actual")
  [[ -f "$raw" && ! -L "$raw" && -f "$expected" && ! -L "$expected" &&
     -f "$actual" && ! -L "$actual" ]] || die 'A fixture leaf is not regular.'
  cmp -s -- "$actual" "$expected" || die 'Sanitizer differs from independently generated expected bytes.'
  raw_bytes="$(wc -c < "$raw")"
  raw_sha256="$(sha256sum < "$raw")"
  raw_sha256="${raw_sha256%% *}"
  sanitized_bytes="$(wc -c < "$expected")"
  sanitized_sha256="$(sha256sum < "$expected")"
  sanitized_sha256="${sanitized_sha256%% *}"
  [[ "$raw_bytes" =~ ^[0-9]+$ && "$sanitized_bytes" =~ ^[0-9]+$ &&
     "$raw_sha256" =~ ^[0-9a-f]{64}$ &&
     "$sanitized_sha256" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid fixture identity.'
  [[ "$first" == true ]] || printf ',' >> "$manifest"
  first=false
  printf '{"name":"diagnostics/vector-%s.bin","raw_bytes":%s,"raw_sha256":"%s","sanitized_bytes":%s,"sanitized_sha256":"%s","sanitizer_matches_expected":true}' \
    "$kind" "$raw_bytes" "$raw_sha256" "$sanitized_bytes" "$sanitized_sha256" >> "$manifest"
done
printf ']}\n' >> "$manifest"
emitted=0
delivery_complete=true
for kind in "${names[@]}"; do
  if emit_file "diagnostics/vector-$kind.bin" "$run_dir/raw-$kind.bin"; then
    emitted=$((emitted + 1))
  else
    delivery_complete=false
  fi
done
if ! emit_file evidence/transport-manifest.json "$manifest"; then
  delivery_complete=false
fi
printf '{"scope":"Finite evidence transport fixtures; provider-log reconciliation still required","selected_vectors":8,"emitted_vectors":%s,"sanitizer_expected_matches":true,"delivery_complete":%s,"no_application_execution":true,"no_browser_execution":true,"no_container_execution":true}\n' \
  "$emitted" "$delivery_complete" > "$summary"
emit_file evidence/transport-summary.json "$summary" || delivery_complete=false
[[ "$delivery_complete" == true && "$emitted" -eq 8 ]]
