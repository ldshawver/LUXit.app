#!/usr/bin/env bash
# Nightly verified, encrypted backup of the LUXit production database.
#
#   * pg_dump custom format (compressed, restorable table-by-table)
#   * verified by reading its TOC back before anything is kept
#   * encrypted with gpg to a PUBLIC key (BACKUP_GPG_RECIPIENT); the private key
#     never lives on this host, so a stolen disk or bucket yields no data
#   * keeps RETAIN_DAYS of encrypted dumps locally
#   * off-site copy: OFFSITE_CMD receives the encrypted path as $1 and its
#     .sha256 as $2 (see luxit-backup-offsite.sh). Without it the dumps live on
#     the same disk as the database and do NOT protect against disk loss.
#   * writes STATUS_FILE (key=value) that luxit-healthcheck.sh alerts on
#
# Reads DATABASE_URL from ENV_FILE. Never prints the URL.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/luxit/prod.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/luxit}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"
OFFSITE_CMD="${OFFSITE_CMD:-}"
GNUPGHOME="${GNUPGHOME:-/etc/luxit/backup-gnupg}"
BACKUP_GPG_RECIPIENT="${BACKUP_GPG_RECIPIENT:-}"
MIN_TOC_ENTRIES="${MIN_TOC_ENTRIES:-100}"
STATUS_FILE="${STATUS_FILE:-$BACKUP_DIR/last-backup.status}"
export GNUPGHOME

umask 077
mkdir -p "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
tmp="$BACKUP_DIR/.luxdb-$stamp.dump.partial"
out="$BACKUP_DIR/luxdb-$stamp.dump.gpg"

status() {  # status <ok|fail> <detail>
    printf 'result=%s\nfinished_at=%s\nfinished_epoch=%s\ndetail=%s\n' \
        "$1" "$(date -u +%FT%TZ)" "$(date +%s)" "$2" > "$STATUS_FILE.tmp"
    mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}
fail() { echo "luxit-db-backup: $2" >&2; rm -f "$tmp" "$out.partial"; status fail "$2"; exit "$1"; }
trap 'fail 9 "unexpected error at line $LINENO"' ERR

[ -n "$BACKUP_GPG_RECIPIENT" ] || fail 2 "BACKUP_GPG_RECIPIENT not set (refusing to write an unencrypted backup)"
gpg --batch --list-keys "$BACKUP_GPG_RECIPIENT" > /dev/null 2>&1 \
    || fail 2 "public key $BACKUP_GPG_RECIPIENT not in $GNUPGHOME"

if [ -z "${DATABASE_URL:-}" ]; then
    DATABASE_URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'")"
fi
[ -n "$DATABASE_URL" ] || fail 2 "DATABASE_URL missing from $ENV_FILE"

pg_dump --format=custom --compress=6 --no-owner --dbname="$DATABASE_URL" --file="$tmp"

# A dump that cannot be listed cannot be restored: fail loudly, keep nothing.
entries="$(pg_restore --list "$tmp" | grep -cv '^;' || true)"
[ "$entries" -ge "$MIN_TOC_ENTRIES" ] || fail 3 "verification failed ($entries TOC entries)"

gpg --batch --yes --trust-model always --recipient "$BACKUP_GPG_RECIPIENT" \
    --compress-algo none --output "$out.partial" --encrypt "$tmp"
rm -f "$tmp"
mv "$out.partial" "$out"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")

if [ -n "$OFFSITE_CMD" ]; then
    $OFFSITE_CMD "$out" "$out.sha256" || fail 4 "off-site copy failed for $(basename "$out")"
    offsite=yes
else
    offsite=no
fi

find "$BACKUP_DIR" -maxdepth 1 -name 'luxdb-*.dump*' -mtime +"$RETAIN_DAYS" -delete

trap - ERR
size="$(stat -c %s "$out")"
status ok "file=$(basename "$out") entries=$entries size=$size offsite=$offsite"
echo "luxit-db-backup: ok $(basename "$out") entries=$entries size=$size offsite=$offsite"
