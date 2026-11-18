#!/usr/bin/env bash
# Shared host-side framing only. Caller supplies sanitize() and owned run_dir.
# Base64 is transport, not redaction: sanitize captured bytes before encoding.
# Source this exact checkout file; do not execute it as an application harness.

emit_problem() {
  [[ "$#" -eq 2 && "$1" =~ ^[-A-Za-z0-9._/]+$ &&
     "$2" =~ ^[a-zA-Z0-9_]+$ ]] || return 1
  local written=true
  printf '::group::Evidence %s\n' "$1" || written=false
  printf 'SS_CI_EVIDENCE_MISSING {"name":"%s","reason":"%s"}\n' "$1" "$2" || written=false
  printf '::endgroup::\n' || written=false
  [[ "$written" == true ]]
}

emit_file() {
  [[ "$#" -eq 2 ]] || return 1
  local name="$1" source="$2" raw_bytes sanitized payload='' bytes checksum
  local decoded_bytes='' decoded_sha256='' stop_token delivered_ok=true
  local encoded=false input_limit=67108864 size_reason=exceeds_64MiB_delivery_bound
  [[ "$name" =~ ^[-A-Za-z0-9._/]+$ ]] || return 1
  if [[ "$name" == diagnostics/* ]]; then
    encoded=true
    input_limit=50331648
    size_reason=exceeds_48MiB_diagnostic_input_bound
  fi
  if [[ ! -d "${run_dir:-}" || -L "$run_dir" || ! -O "$run_dir" ]]; then
    emit_problem "$name" invalid_owned_delivery_directory || :
    return 1
  fi
  if [[ ! -f "$source" || -L "$source" || ! -r "$source" ]]; then
    emit_problem "$name" unavailable_or_nonregular || :
    return 1
  fi
  if ! raw_bytes="$(wc -c < "$source")"; then
    emit_problem "$name" source_size_failed || :
    return 1
  fi
  if [[ ! "$raw_bytes" =~ ^[0-9]+$ ]]; then
    emit_problem "$name" source_size_failed || :
    return 1
  fi
  if ((raw_bytes > input_limit)); then
    emit_problem "$name" "$size_reason" || :
    return 1
  fi
  if ! sanitized="$(mktemp "$run_dir/delivered-XXXXXXXXXXXX")"; then
    emit_problem "$name" delivery_temporary_failed || :
    return 1
  fi
  if [[ ! -f "$sanitized" || -L "$sanitized" || ! -O "$sanitized" ]]; then
    emit_problem "$name" invalid_delivery_temporary || :
    return 1
  fi
  if ! sanitize "$source" > "$sanitized"; then
    rm -f -- "$sanitized" || :
    emit_problem "$name" sanitization_failed || :
    return 1
  fi
  if ! decoded_bytes="$(wc -c < "$sanitized")" ||
     ! decoded_sha256="$(sha256sum < "$sanitized")"; then
    rm -f -- "$sanitized" || :
    emit_problem "$name" delivery_framing_failed || :
    return 1
  fi
  decoded_sha256="${decoded_sha256%% *}"
  if [[ ! "$decoded_bytes" =~ ^[0-9]+$ ||
        ! "$decoded_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    rm -f -- "$sanitized" || :
    emit_problem "$name" delivery_framing_failed || :
    return 1
  fi
  if ((decoded_bytes > input_limit)); then
    rm -f -- "$sanitized" || :
    emit_problem "$name" "$size_reason" || :
    return 1
  fi
  payload="$sanitized"
  if [[ "$encoded" == true ]]; then
    if ! payload="$(mktemp "$run_dir/encoded-XXXXXXXXXXXX")"; then
      rm -f -- "$sanitized" || :
      emit_problem "$name" delivery_temporary_failed || :
      return 1
    fi
    if [[ ! -f "$payload" || -L "$payload" || ! -O "$payload" ]]; then
      rm -f -- "$sanitized" || :
      emit_problem "$name" invalid_delivery_temporary || :
      return 1
    fi
    # GNU wrapping includes its final LF for a nonempty encoded payload.
    if ! base64 --wrap=76 -- "$sanitized" > "$payload"; then
      rm -f -- "$payload" "$sanitized" || :
      emit_problem "$name" diagnostic_encoding_failed || :
      return 1
    fi
  fi
  if ! bytes="$(wc -c < "$payload")" ||
     ! checksum="$(sha256sum < "$payload")" ||
     ! stop_token="$(openssl rand -hex 16)"; then
    rm -f -- "$payload" "$sanitized" || :
    emit_problem "$name" delivery_framing_failed || :
    return 1
  fi
  checksum="${checksum%% *}"
  if [[ ! "$bytes" =~ ^[0-9]+$ || ! "$checksum" =~ ^[0-9a-f]{64}$ ||
        ! "$stop_token" =~ ^[0-9a-f]{32}$ ]] || ((bytes > 67108864)); then
    rm -f -- "$payload" "$sanitized" || :
    emit_problem "$name" delivery_framing_failed || :
    return 1
  fi
  if ! printf '::group::Evidence %s\n' "$name"; then
    rm -f -- "$payload" "$sanitized" || :
    return 1
  fi
  # Choose the fresh resume token only after captured output is sanitized.
  if ! printf '::stop-commands::%s\n' "$stop_token"; then
    printf '::endgroup::\n' || :
    rm -f -- "$payload" "$sanitized" || :
    return 1
  fi
  if [[ "$encoded" == true ]]; then
    printf 'SS_CI_EVIDENCE_BEGIN {"name":"%s","bytes":%s,"sha256":"%s","convention":"SHA256 of base64 ASCII payload; length excludes separator LF","encoding":"base64","decoded_bytes":%s,"decoded_sha256":"%s"}\n' \
      "$name" "$bytes" "$checksum" "$decoded_bytes" "$decoded_sha256" || delivered_ok=false
  else
    printf 'SS_CI_EVIDENCE_BEGIN {"name":"%s","bytes":%s,"sha256":"%s","convention":"SHA256 of sanitized payload; length excludes separator LF"}\n' \
      "$name" "$bytes" "$checksum" || delivered_ok=false
  fi
  if [[ "$delivered_ok" == true ]]; then
    cat -- "$payload" || delivered_ok=false
  fi
  # This separator LF remains outside the length-delimited outer payload.
  if [[ "$encoded" == true ]]; then
    printf '\nSS_CI_EVIDENCE_END {"name":"%s","bytes":%s,"sha256":"%s","delivered":%s,"encoding":"base64","decoded_bytes":%s,"decoded_sha256":"%s"}\n' \
      "$name" "$bytes" "$checksum" "$delivered_ok" "$decoded_bytes" "$decoded_sha256" || delivered_ok=false
  else
    printf '\nSS_CI_EVIDENCE_END {"name":"%s","bytes":%s,"sha256":"%s","delivered":%s}\n' \
      "$name" "$bytes" "$checksum" "$delivered_ok" || delivered_ok=false
  fi
  printf '::%s::\n' "$stop_token" || delivered_ok=false
  printf '::endgroup::\n' || delivered_ok=false
  rm -f -- "$payload" "$sanitized" || delivered_ok=false
  [[ "$delivered_ok" == true ]]
}
