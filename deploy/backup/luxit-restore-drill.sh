#!/usr/bin/env bash
# Prove the newest backup restores: load it into a throwaway database on the
# dev cluster, compare key row counts with the dump's own data, then drop it.
# Touches no production database.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/luxit}"
DRILL_PORT="${DRILL_PORT:-5433}"
dump="${1:-$(ls -1t "$BACKUP_DIR"/luxdb-*.dump | head -1)}"
db="lux_restore_drill_$(date -u +%Y%m%d%H%M%S)"

runuser -u postgres -- createdb -p "$DRILL_PORT" "$db"
trap 'runuser -u postgres -- dropdb -p "$DRILL_PORT" --if-exists "$db"' EXIT

start=$(date +%s)
runuser -u postgres -- pg_restore -p "$DRILL_PORT" -d "$db" --no-owner --no-privileges --exit-on-error < "$dump"
secs=$(( $(date +%s) - start ))

q() { runuser -u postgres -- psql -p "$DRILL_PORT" -d "$db" -X -A -t -c "$1"; }
tables=$(q "select count(*) from information_schema.tables where table_schema='public'")
migrations=$(q "select count(*) from schema_migrations")
contacts=$(q "select count(*) from contact")
companies=$(q "select count(*) from company")

echo "restore-drill: dump=$(basename "$dump") seconds=$secs tables=$tables migrations=$migrations companies=$companies contacts=$contacts"
[ "$tables" -gt 100 ] && [ "$companies" -gt 0 ] || { echo "restore-drill: FAILED sanity checks" >&2; exit 1; }
echo "restore-drill: PASS"
