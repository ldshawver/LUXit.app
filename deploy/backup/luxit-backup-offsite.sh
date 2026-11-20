#!/usr/bin/env bash
# OFFSITE_CMD for luxit-db-backup.sh: copy an encrypted dump and its checksum
# to an rclone remote, then confirm the remote copy's size matches.
#
#   OFFSITE_REMOTE   rclone destination, e.g. "b2-luxit:luxit-db-backups/prod"
#   RCLONE_CONFIG    rclone config holding that remote's credentials (0600)
#
# The payload is already gpg-encrypted to a key that is not on this host, so
# the remote only ever stores ciphertext. Use a bucket with object lock /
# versioning so a compromised host cannot delete history.
set -euo pipefail

src="${1:?encrypted dump path}"; sum="${2:?checksum path}"
OFFSITE_REMOTE="${OFFSITE_REMOTE:?OFFSITE_REMOTE not set}"
export RCLONE_CONFIG="${RCLONE_CONFIG:-/etc/luxit/backup-rclone.conf}"

case "$src" in *.gpg) ;; *) echo "offsite: refusing to upload unencrypted $src" >&2; exit 2;; esac

rclone copyto --immutable "$src" "$OFFSITE_REMOTE/$(basename "$src")"
rclone copyto --immutable "$sum" "$OFFSITE_REMOTE/$(basename "$sum")"

remote_size="$(rclone size --json "$OFFSITE_REMOTE/$(basename "$src")" | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')"
[ "$remote_size" = "$(stat -c %s "$src")" ] || { echo "offsite: size mismatch for $(basename "$src")" >&2; exit 3; }
echo "offsite: ok $(basename "$src") -> $OFFSITE_REMOTE"
