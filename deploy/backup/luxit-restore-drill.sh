#!/usr/bin/env bash
# Prove a backup restores, in complete isolation: decrypt it, start a
# throwaway PostgreSQL cluster in a temp directory (unix socket only, no TCP
# listener, not registered with pg_ctlcluster), restore, check sanity row
# counts, then destroy the cluster. Touches no existing database.
#
#   restore-drill.sh [dump.gpg|dump]   default: newest luxdb-*.dump.gpg
#   DRILL_GNUPGHOME  keyring holding the PRIVATE backup key (bring it from
#                    offline storage for the drill; do not leave it on the host)
#   DRILL_LOG        append a one-line result (default $BACKUP_DIR/restore-drill.log)
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/luxit}"
PGBIN="${PGBIN:-/usr/lib/postgresql/17/bin}"
DRILL_LOG="${DRILL_LOG:-$BACKUP_DIR/restore-drill.log}"
MIN_TABLES="${MIN_TABLES:-100}"
dump="${1:-$(ls -1t "$BACKUP_DIR"/luxdb-*.dump.gpg | head -1)}"

work="$(mktemp -d /var/tmp/luxit-restore-drill.XXXXXX)"
chown postgres: "$work"; chmod 700 "$work"
as_pg() { runuser -u postgres -- "$@"; }
cleanup() {
    as_pg "$PGBIN/pg_ctl" -D "$work/data" -m immediate stop > /dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT

if [ -f "$dump.sha256" ]; then
    (cd "$(dirname "$dump")" && sha256sum --check --quiet "$(basename "$dump").sha256")
fi
plain="$work/restore.dump"
case "$dump" in
    *.gpg) GNUPGHOME="${DRILL_GNUPGHOME:?DRILL_GNUPGHOME (private key) required for .gpg}" \
               gpg --batch --quiet --output "$plain" --decrypt "$dump" ;;
    *)     cp "$dump" "$plain" ;;
esac
chown postgres: "$plain"

as_pg "$PGBIN/initdb" -D "$work/data" -A trust -U postgres > /dev/null
as_pg "$PGBIN/pg_ctl" -D "$work/data" -l "$work/pg.log" -w \
    -o "-c listen_addresses='' -c unix_socket_directories=$work -c fsync=off" start > /dev/null
as_pg "$PGBIN/createdb" -h "$work" drill

start=$(date +%s)
as_pg "$PGBIN/pg_restore" -h "$work" -d drill --no-owner --no-privileges --exit-on-error "$plain"
secs=$(( $(date +%s) - start ))

q() { as_pg "$PGBIN/psql" -h "$work" -d drill -X -A -t -c "$1"; }
tables=$(q "select count(*) from information_schema.tables where table_schema='public'")
migrations=$(q "select count(*) from schema_migrations")
companies=$(q "select count(*) from company")
contacts=$(q "select count(*) from contact")
users=$(q "select count(*) from \"user\"")

line="$(date -u +%FT%TZ) dump=$(basename "$dump") seconds=$secs tables=$tables migrations=$migrations companies=$companies users=$users contacts=$contacts"
if [ "$tables" -ge "$MIN_TABLES" ] && [ "$companies" -gt 0 ] && [ "$migrations" -gt 0 ]; then
    echo "$line result=PASS" | tee -a "$DRILL_LOG"
else
    echo "$line result=FAIL" | tee -a "$DRILL_LOG" >&2
    exit 1
fi
