#!/usr/bin/env bash
# Nightly verified backup of the LUXit production database.
#
#   * pg_dump custom format (compressed, restorable table-by-table)
#   * written 0600 to BACKUP_DIR, then verified by reading its TOC back
#   * keeps RETAIN_DAYS of dumps
#   * optional off-site copy: set OFFSITE_CMD to a command that receives the
#     dump path as $1 (e.g. an rclone/aws-cli wrapper). Until then the dumps live
#     on the same disk as the database and do NOT protect against disk loss.
#
# Reads DATABASE_URL from ENV_FILE. Never prints the URL.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/luxit/prod.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/luxit}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
OFFSITE_CMD="${OFFSITE_CMD:-}"

umask 077
mkdir -p "$BACKUP_DIR"

DATABASE_URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'")"
if [ -z "$DATABASE_URL" ]; then
    echo "luxit-db-backup: DATABASE_URL missing from $ENV_FILE" >&2
    exit 2
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/luxdb-$stamp.dump"
tmp="$out.partial"

pg_dump --format=custom --compress=6 --no-owner --dbname="$DATABASE_URL" --file="$tmp"

# A dump that cannot be listed cannot be restored: fail loudly, keep nothing.
entries="$(pg_restore --list "$tmp" | grep -cv '^;')"
if [ "$entries" -lt 100 ]; then
    echo "luxit-db-backup: verification failed ($entries TOC entries)" >&2
    rm -f "$tmp"
    exit 3
fi
mv "$tmp" "$out"
sha256sum "$out" > "$out.sha256"

if [ -n "$OFFSITE_CMD" ]; then
    $OFFSITE_CMD "$out"
fi

find "$BACKUP_DIR" -maxdepth 1 -name 'luxdb-*.dump*' -mtime +"$RETAIN_DAYS" -delete

echo "luxit-db-backup: ok $(basename "$out") entries=$entries size=$(stat -c %s "$out")"
