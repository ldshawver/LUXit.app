#!/bin/bash
# Purge rehearsal on an isolated copy of staging, touching nothing shared:
#   - throwaway PostgreSQL cluster in /var/tmp (unix socket only, destroyed on exit)
#   - staging schema + rows copied read-only (pg_dump), pending migrations applied
#     with the canonical runner
#   - purge_rehearsal.py closes and purges every workspace in the copy, in a
#     new network namespace, and checks FK acceptance + no cross-tenant change
# Usage (root): RC=/path/to/checkout scripts/acceptance/isolated_purge_rehearsal.sh [--company-id N ...]
set -euo pipefail
RC=${RC:-$(cd "$(dirname "$0")/../.." && pwd)}
STAGING_SECRET_ENV=${STAGING_SECRET_ENV:-/etc/luxit-staging/secret.env}
PY=${PY:-/srv/lux-email-bot-staging/.venv/bin/python}
W=/var/tmp/luxit-rc-purge-rig
PGBIN=/usr/lib/postgresql/17/bin
as_pg() { runuser -u postgres -- "$@"; }
stop() { as_pg $PGBIN/pg_ctl -D $W/data -m immediate stop >/dev/null 2>&1 || true; rm -rf $W; }
trap stop EXIT
rm -rf $W; mkdir -p $W; chown postgres: $W; chmod 700 $W
as_pg $PGBIN/initdb -D $W/data -A trust -U postgres >/dev/null
as_pg $PGBIN/pg_ctl -D $W/data -l $W/pg.log -w -o "-c listen_addresses='' -c unix_socket_directories=$W -c fsync=off" start >/dev/null
as_pg $PGBIN/createdb -h $W rig
STG=$(grep '^DATABASE_URL=' $STAGING_SECRET_ENV | cut -d= -f2- | tr -d "\"'")
pg_dump --no-owner --no-privileges "$STG" > $W/full.sql
chmod 600 $W/full.sql; chown postgres: $W/full.sql
as_pg $PGBIN/psql -h $W -d rig -X -q -v ON_ERROR_STOP=1 -f $W/full.sql >/dev/null
rm -f $W/full.sql
DB="postgresql://postgres@/rig?host=$W"
echo "ledger before: $(as_pg $PGBIN/psql -h $W -d rig -XAtc 'select count(*) from schema_migrations')"
(cd $RC && $PY scripts/apply_migrations.py "$DB" migrations --deployment-id rig-purge | tail -3)
echo "ledger after: $(as_pg $PGBIN/psql -h $W -d rig -XAtc 'select count(*) from schema_migrations')"
FKEY=$($PY -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
systemd-run --scope -q -p MemoryMax=1200M unshare -n \
  env -i HOME=/root PATH=$(dirname $PY):/usr/bin:/bin LANG=C.UTF-8 \
  FLASK_ENV=production APP_ENV=staging POSTHOG_DISABLED=true TUYA_NOTIFICATION_ENABLED=false \
  LUXIT_TWILIO_MODE=disabled LUXIT_TWILIO_ENVIRONMENT=staging \
  DATABASE_URL="$DB" SESSION_SECRET=rig-$RANDOM-$RANDOM ENCRYPTION_MASTER_KEY="$FKEY" DIAGNOSTICS_LOG_DIR=$W \
  bash -c "cd $RC && $PY scripts/acceptance/purge_rehearsal.py \"\$@\" 2>$W/err.log; rc=\$?; [ \$rc -eq 0 ] || tail -20 $W/err.log; exit \$rc" bash "$@"
