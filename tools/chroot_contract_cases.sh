#!/bin/sh
# Exactly two observations per container; never a browser/application launcher.
set -u
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
case "${1:-}" in original|experimental) profile=$1 ;; *) exit 78 ;; esac
[ "$#" -eq 1 ] || exit 78
fail() { printf 'contract_setup=%s\n' "$1" >&2; exit 78; }
for tool in id readlink stat sha256sum dpkg-query unshare; do
  command -v "$tool" >/dev/null 2>&1 || fail required_tool_missing
done
[ "$(id -u)" = 10001 ] && [ "$(id -g)" = 10001 ] || fail outer_ids_mismatch
[ -f /usr/bin/unshare ] && [ ! -L /usr/bin/unshare ] && [ -x /usr/bin/unshare ] &&
  [ ! -u /usr/bin/unshare ] && [ ! -g /usr/bin/unshare ] &&
  [ "$(stat -c '%u:%g' /usr/bin/unshare)" = 0:0 ] || fail unshare_file_rejected
version=$(dpkg-query -W -f='${Version}' util-linux 2>/dev/null) || fail package_missing
case "$version" in 2.41.5|2.41.5-*) ;; *) fail package_version_mismatch ;; esac
[ "$(/usr/bin/unshare --version 2>/dev/null)" = 'unshare from util-linux 2.41.5' ] || fail unshare_version_mismatch
found=$(sha256sum < /usr/bin/unshare) || fail unshare_hash_failed
[ "${found%% *}" = e7d3961bd874d5a4fe531efe6feb61b358b8000bd8c5fe91c5f1bdd9af81a002 ] || fail unshare_hash_mismatch
parent_ns=$(readlink /proc/self/ns/user) || fail parent_namespace_unavailable
# C validates the exact namespace identity syntax and its own relationship.
/ci-contract-probe "$profile" outer "$parent_ns"
outer_exit=$?
printf 'contract_outer_exit=%s\n' "$outer_exit" >&2
[ "$outer_exit" -eq 0 ] || exit 78
# Namespace-local root only: one exact 0->10001 UID/GID map, setgroups=deny.
# No map-auto/ranges/helpers/keep-caps/kill-child/mounts/persistence or retry.
/usr/bin/unshare --user --map-root-user -- /ci-contract-probe "$profile" inner "$parent_ns"
inner_exit=$?
printf 'contract_inner_exit=%s\n' "$inner_exit" >&2
[ "$inner_exit" -eq 0 ] || exit 78
