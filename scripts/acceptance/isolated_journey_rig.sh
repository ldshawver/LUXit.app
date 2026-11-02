#!/bin/bash
# Run scripts/acceptance/staging_customer_journey.py against an isolated
# instance of a checkout (default: this repo), touching nothing shared:
#   - throwaway PostgreSQL cluster in /var/tmp (unix socket only, destroyed on exit)
#   - schema + migration ledger copied read-only from staging (pg_dump, no rows)
#   - pending migrations applied with the canonical runner (proves ordering)
#   - gunicorn + the journey run in a new network namespace (loopback only),
#     with a fresh session secret and encryption key; Twilio mode disabled
# Usage (root): RC=/path/to/checkout scripts/acceptance/isolated_journey_rig.sh [journey args]
set -euo pipefail
RC=${RC:-$(cd "$(dirname "$0")/../.." && pwd)}
STAGING_SECRET_ENV=${STAGING_SECRET_ENV:-/etc/luxit-staging/secret.env}
JOURNEY="$(cd "$(dirname "$0")" && pwd)/staging_customer_journey.py"
PY=${PY:-/srv/lux-email-bot-staging/.venv/bin/python}
W=/var/tmp/luxit-rc-journey-rig
PGBIN=/usr/lib/postgresql/17/bin
as_pg() { runuser -u postgres -- "$@"; }
stop() { as_pg $PGBIN/pg_ctl -D $W/data -m immediate stop >/dev/null 2>&1 || true; rm -rf $W; }
trap stop EXIT
rm -rf $W; mkdir -p $W; chown postgres: $W; chmod 700 $W
as_pg $PGBIN/initdb -D $W/data -A trust -U postgres >/dev/null
as_pg $PGBIN/pg_ctl -D $W/data -l $W/pg.log -w -o "-c listen_addresses='' -c unix_socket_directories=$W -c fsync=off" start >/dev/null
as_pg $PGBIN/createdb -h $W rig
STG=$(grep '^DATABASE_URL=' $STAGING_SECRET_ENV | cut -d= -f2- | tr -d "\"'")
pg_dump --schema-only --no-owner --no-privileges "$STG" > $W/schema.sql
pg_dump --data-only --table=schema_migrations "$STG" > $W/ledger.sql
as_pg $PGBIN/psql -h $W -d rig -X -q -v ON_ERROR_STOP=1 -f $W/schema.sql >/dev/null
as_pg $PGBIN/psql -h $W -d rig -X -q -v ON_ERROR_STOP=1 -f $W/ledger.sql >/dev/null
DB="postgresql://postgres@/rig?host=$W"
echo "ledger before: $(as_pg $PGBIN/psql -h $W -d rig -XAtc 'select count(*) from schema_migrations')"
(cd $RC && $PY scripts/apply_migrations.py "$DB" migrations --deployment-id rig-journey | tail -3)
echo "ledger after: $(as_pg $PGBIN/psql -h $W -d rig -XAtc 'select count(*) from schema_migrations')"
SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '/+=')
FKEY=$($PY -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
printf 'SESSION_SECRET=%s\n' "$SECRET" > $W/secret.env
chmod 600 $W/secret.env
SP=${RIG_OUT:-/var/tmp}
systemd-run --scope -q -p MemoryMax=1200M unshare -n bash -c "
ip link set lo up
cd $RC
env -i HOME=/root PATH=$(dirname $PY):/usr/bin:/bin LANG=C.UTF-8 \
  FLASK_ENV=production APP_ENV=staging POSTHOG_DISABLED=true TUYA_NOTIFICATION_ENABLED=false \
  LUXIT_TWILIO_MODE=disabled LUXIT_TWILIO_ENVIRONMENT=staging APP_BASE_URL=https://staging.luxit.app \
  DATABASE_URL='$DB' SESSION_SECRET='$SECRET' ENCRYPTION_MASTER_KEY='$FKEY' SELF_SERVE_SIGNUP_ENABLED=1 \
  DIAGNOSTICS_LOG_DIR=$W \
  $(dirname $PY)/gunicorn --workers 1 --worker-class gthread --threads 4 \
  --bind 127.0.0.1:8099 --timeout 60 --error-logfile $W/gunicorn.log app:app &
GPID=\$!
for i in \$(seq 1 60); do curl -s -o /dev/null http://127.0.0.1:8099/health && break; sleep 1; done
$PY $JOURNEY \
  --base http://127.0.0.1:8099 --host staging.luxit.app --db '$DB' --secret-env $W/secret.env \
  \"\$@\"; rc=\$?
kill \$GPID; wait \$GPID 2>/dev/null
grep -E 'Traceback|ERROR|CSRF|WARNING' $W/gunicorn.log | tail -20 > $SP/luxit-rc-journey-errors.txt || true
exit \$rc
" bash "$@"
