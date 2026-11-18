#!/bin/sh
# Finite OS-primitive diagnostic only. No application/browser/Python execution.
set -u
umask 077
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

metadata() {
  printf 'metadata_begin=%s\n' "$1"
  # The shell opens /proc/self; no environment or arbitrary process enumeration.
  if [ -r /proc/self/status ]; then
    while IFS= read -r line; do
      case "$line" in
        Uid:*|Gid:*|NSpid:*|CapInh:*|CapPrm:*|CapEff:*|CapBnd:*|CapAmb:*|NoNewPrivs:*|Seccomp:*|Seccomp_filters:*)
          printf 'status %.256s\n' "$line" ;;
      esac
    done < /proc/self/status
  else
    printf 'status unavailable\n'
  fi
  for path in /proc/self/uid_map /proc/self/gid_map /proc/self/setgroups \
      /proc/self/attr/current /proc/sys/user/max_user_namespaces \
      /proc/sys/kernel/unprivileged_userns_clone \
      /proc/sys/kernel/apparmor_restrict_unprivileged_userns; do
    printf 'file=%s\n' "$path"
    if [ -r "$path" ]; then
      count=0
      # Four bounded rows suffice for these fixed metadata files.
      while IFS= read -r line || [ -n "$line" ]; do
        count=$((count + 1))
        [ "$count" -le 4 ] || { printf 'truncated\n'; break; }
        printf 'value %.256s\n' "$line"
      done < "$path"
    else
      printf 'unavailable\n'
    fi
  done
  for path in /proc/self/ns/user /proc/self/ns/pid /proc/self/ns/pid_for_children /proc/self/ns/net; do
    printf 'namespace=%s\n' "$path"
    readlink -- "$path" || printf 'unavailable\n'
  done
  printf 'metadata_end=%s\n' "$1"
}

case "${1:-}" in
  child-user|child-combined)
    [ "$#" -eq 1 ] || exit 78
    [ "$(id -u)" = 10001 ] && [ "$(id -g)" = 10001 ] || exit 78
    metadata "$1"
    exit 0 ;;
  '') [ "$#" -eq 0 ] || exit 78 ;;
  *) exit 78 ;;
esac

printf 'scope=base-layer-fresh-runner-namespace-availability-only\n'
for tool in id readlink stat dpkg-query sha256sum unshare; do
  command -v "$tool" >/dev/null 2>&1 || { printf 'preflight_error=required_tool_missing\n'; exit 78; }
done
[ "$(id -u)" = 10001 ] && [ "$(id -g)" = 10001 ] || {
  printf 'preflight_error=uid_gid_mismatch\n'; exit 78;
}
nnp=false; filter=false; caps=0
[ -r /proc/self/status ] || { printf 'preflight_error=process_status_unavailable\n'; exit 78; }
while read -r field value rest; do
  case "$field" in
    NoNewPrivs:) [ "$value" = 1 ] && nnp=true ;;
    Seccomp:) [ "$value" = 2 ] && filter=true ;;
    CapInh:|CapPrm:|CapEff:|CapBnd:|CapAmb:)
      case "$value" in ''|*[!0]*) printf 'preflight_error=nonzero_capability_set\n'; exit 78 ;; esac
      caps=$((caps + 1)) ;;
  esac
done < /proc/self/status
[ "$nnp" = true ] && [ "$filter" = true ] && [ "$caps" -eq 5 ] || {
  printf 'preflight_error=process_security_state_mismatch\n'; exit 78;
}
[ -f /usr/bin/unshare ] && [ -x /usr/bin/unshare ] && [ ! -L /usr/bin/unshare ] &&
  [ ! -u /usr/bin/unshare ] && [ ! -g /usr/bin/unshare ] &&
  [ "$(stat -c '%u:%g' /usr/bin/unshare)" = 0:0 ] || {
    printf 'preflight_error=unshare_file_identity_rejected\n'; exit 78;
  }
package_version=$(dpkg-query -W -f='${Version}' util-linux 2>/dev/null) || {
  printf 'preflight_error=util_linux_package_missing\n'; exit 78;
}
case "$package_version" in
  2.41.5|2.41.5-*) ;;
  *) printf 'preflight_error=util_linux_version_mismatch\n'; exit 78 ;;
esac
version=$(/usr/bin/unshare --version 2>/dev/null) || exit 78
[ "$version" = 'unshare from util-linux 2.41.5' ] || {
  printf 'preflight_error=unshare_version_mismatch\n'; exit 78;
}
printf 'util_linux_version=2.41.5\n'
printf 'unshare_identity='
sha256sum /usr/bin/unshare || exit 78
metadata parent

# Exactly two subchecks; no retries, extra capabilities, mappings helpers,
# mount/proc remount, persistent namespaces, network traffic or kill-child.
# --kill-child can introduce pidfd_open, absent from the reviewed profile.
printf 'subcheck_begin=user\n'
/usr/bin/unshare --user --map-current-user -- /bin/sh /ci-probe.sh child-user
user_exit=$?
printf 'subcheck_end=user exit_code=%s\n' "$user_exit"
printf 'subcheck_begin=combined\n'
/usr/bin/unshare --user --map-current-user --pid --net --fork -- /bin/sh /ci-probe.sh child-combined
combined_exit=$?
printf 'subcheck_end=combined exit_code=%s\n' "$combined_exit"
printf 'SS_PRIMITIVE_RESULT user_exit=%s combined_exit=%s\n' "$user_exit" "$combined_exit"
[ "$user_exit" -eq 0 ] && [ "$combined_exit" -eq 0 ]
