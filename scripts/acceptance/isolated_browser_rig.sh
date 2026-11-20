#!/bin/bash
# Real-browser smoke (headless Chromium via Playwright) against an isolated
# instance of a checkout, touching nothing shared:
#   - throwaway PostgreSQL in /var/tmp (unix socket only, destroyed on exit),
#     schema + ledger copied read-only from staging, pending migrations applied
#   - one disposable owner/workspace seeded (browser_seed.py)
#   - gunicorn + Chromium in a new network namespace (loopback only)
# Usage (root): RC=/path/to/checkout PLAYWRIGHT_NODE_MODULES=/path/node_modules scripts/acceptance/isolated_browser_rig.sh
set -euo pipefail
RC=${RC:-$(cd "$(dirname "$0")/../.." && pwd)}
HERE="$(cd "$(dirname "$0")" && pwd)"
STAGING_SECRET_ENV=${STAGING_SECRET_ENV:-/etc/luxit-staging/secret.env}
PY=${PY:-/srv/lux-email-bot-staging/.venv/bin/python}
NM=${PLAYWRIGHT_NODE_MODULES:-/root/zappy-redesign/node_modules}
W=/var/tmp/luxit-rc-browser-rig
PGBIN=/usr/lib/postgresql/17/bin
as_pg() { runuser -u postgres -- "$@"; }
stop() { as_pg $PGBIN/pg_ctl -D $W/data -m immediate stop >/dev/null 2>&1 || true; rm -rf $W; }
trap stop EXIT
rm -rf $W; mkdir -p $W; chown postgres: $W; chmod 711 $W
as_pg $PGBIN/initdb -D $W/data -A trust -U postgres >/dev/null
as_pg $PGBIN/pg_ctl -D $W/data -l $W/pg.log -w -o "-c listen_addresses='' -c unix_socket_directories=$W -c fsync=off" start >/dev/null
as_pg $PGBIN/createdb -h $W rig
STG=$(grep '^DATABASE_URL=' $STAGING_SECRET_ENV | cut -d= -f2- | tr -d "\"'")
pg_dump --schema-only --no-owner --no-privileges "$STG" > $W/schema.sql
pg_dump --data-only --table=schema_migrations "$STG" > $W/ledger.sql
as_pg $PGBIN/psql -h $W -d rig -X -q -v ON_ERROR_STOP=1 -f $W/schema.sql >/dev/null
as_pg $PGBIN/psql -h $W -d rig -X -q -v ON_ERROR_STOP=1 -f $W/ledger.sql >/dev/null
DB="postgresql://postgres@/rig?host=$W"
(cd $RC && $PY scripts/apply_migrations.py "$DB" migrations --deployment-id rig-browser | tail -1)
SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '/+=')
FKEY=$($PY -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
PW=rig-$(head -c 12 /dev/urandom | base64 | tr -d '/+=')
ENVV="HOME=/root PATH=$(dirname $PY):/usr/bin:/bin LANG=C.UTF-8 FLASK_ENV=production APP_ENV=staging POSTHOG_DISABLED=true TUYA_NOTIFICATION_ENABLED=false LUXIT_TWILIO_MODE=disabled LUXIT_TWILIO_ENVIRONMENT=staging APP_BASE_URL=http://localhost:8099 DATABASE_URL=$DB SESSION_SECRET=$SECRET ENCRYPTION_MASTER_KEY=$FKEY DIAGNOSTICS_LOG_DIR=$W"
(cd $RC && env -i $ENVV RIG_PASSWORD=$PW $PY $HERE/browser_seed.py 2>/dev/null | tail -1)
systemd-run --scope -q -p MemoryMax=1500M unshare -n bash -c "
ip link set lo up
cd $RC
env -i $ENVV $(dirname $PY)/gunicorn --workers 1 --worker-class gthread --threads 4 \
  --bind 127.0.0.1:8099 --timeout 60 --error-logfile $W/gunicorn.log app:app &
GPID=\$!
for i in \$(seq 1 60); do curl -s -o /dev/null http://127.0.0.1:8099/health && break; sleep 1; done
cp $HERE/browser_smoke.mjs $W/smoke.mjs
NODE_PATH=$NM env -i HOME=/root PATH=/usr/bin:/bin RIG_PASSWORD=$PW RIG_BASE=http://localhost:8099 \
  bash -c 'cd $W && ln -s $NM node_modules && node smoke.mjs'; rc=\$?
kill \$GPID; wait \$GPID 2>/dev/null
grep -E 'Traceback|CSRF' $W/gunicorn.log | tail -10 || true
exit \$rc
"
