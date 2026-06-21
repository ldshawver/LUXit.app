#!/usr/bin/env bash
set -euo pipefail

CONTRACT_ID="${CONTRACT_ID:-735551c2-ec6c-41e6-976d-1eef4e13bfa5}"
BROKEN_PATH="/app/contractor-hub/contracts/"
DOCUMENSO_PUBLIC_URL="${DOCUMENSO_PUBLIC_URL:-${DOCUMENSO_BASE_URL:-https://document.luxit.app}}"
OUT_DIR="${OUT_DIR:-./documenso-live-audit-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT_DIR"

log() { printf '\n== %s ==\n' "$*" | tee -a "$OUT_DIR/summary.txt"; }
run() {
  local name="$1"; shift
  log "$name"
  set +e
  "$@" >"$OUT_DIR/$name.out" 2>"$OUT_DIR/$name.err"
  local code=$?
  set -e
  printf 'exit_code=%s\n' "$code" | tee -a "$OUT_DIR/summary.txt"
  sed -n '1,120p' "$OUT_DIR/$name.out" | tee -a "$OUT_DIR/summary.txt"
  if [ "$code" -ne 0 ]; then sed -n '1,80p' "$OUT_DIR/$name.err" | tee -a "$OUT_DIR/summary.txt"; fi
  return 0
}

redact_env() {
  sed -E 's/(SECRET|TOKEN|KEY|PASSWORD|PASS|PRIVATE|AUTH|CREDENTIAL|SALT)=.*/\1=<redacted>/I'
}

log "audit_context"
{
  echo "contract_id=$CONTRACT_ID"
  echo "documenso_public_url=$DOCUMENSO_PUBLIC_URL"
  date -u '+utc=%Y-%m-%dT%H:%M:%SZ'
  uname -a || true
} | tee -a "$OUT_DIR/summary.txt"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker command not found. Run this script on the Documenso VPS." | tee -a "$OUT_DIR/summary.txt"
  exit 2
fi

run docker_ps docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

docker ps --format '{{.Names}} {{.Image}}' \
  | awk 'tolower($0) ~ /documenso|document/ {print $1}' > "$OUT_DIR/documenso_containers.txt"

if [ ! -s "$OUT_DIR/documenso_containers.txt" ]; then
  echo "ERROR: no container with image/name matching documenso|document found." | tee -a "$OUT_DIR/summary.txt"
  exit 3
fi

while IFS= read -r container; do
  [ -n "$container" ] || continue
  safe_name="${container//[^A-Za-z0-9_.-]/_}"

  log "container_$safe_name"
  echo "$container" | tee -a "$OUT_DIR/summary.txt"

  docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sort \
    | grep -E '^(APP_URL|NEXT_PUBLIC_WEBAPP_URL|NEXTAUTH_URL|WEBAPP_URL|DOCUMENSO_|MAIL_|SMTP_|WEBHOOK_|API_|NEXT_PUBLIC_|PUBLIC_)=' \
    | redact_env > "$OUT_DIR/${safe_name}_runtime_env.redacted.txt" || true
  sed -n '1,200p' "$OUT_DIR/${safe_name}_runtime_env.redacted.txt" | tee -a "$OUT_DIR/summary.txt"

  run "${safe_name}_logs_contract" sh -c "docker logs --since 168h '$container' 2>&1 | grep -Ei '$CONTRACT_ID|signing|email|webhook|redirect|return|metadata|document' | tail -n 300"
  run "${safe_name}_search_broken_path" docker exec "$container" sh -lc "grep -R --line-number '$BROKEN_PATH' /app /data /config 2>/dev/null || true"
done < "$OUT_DIR/documenso_containers.txt"

# Best-effort Documenso Postgres inspection. This is read-only and uses the
# Postgres container's own POSTGRES_USER/POSTGRES_DB environment when present.
docker ps --format '{{.Names}} {{.Image}}' \
  | awk 'tolower($0) ~ /postgres|postgis|pgvector/ {print $1}' > "$OUT_DIR/postgres_containers.txt" || true

while IFS= read -r pg_container; do
  [ -n "$pg_container" ] || continue
  pg_safe="${pg_container//[^A-Za-z0-9_.-]/_}"
  log "postgres_$pg_safe"

  pg_env="$OUT_DIR/${pg_safe}_postgres_env.redacted.txt"
  docker inspect "$pg_container" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sort \
    | grep -E '^(POSTGRES_DB|POSTGRES_USER|DATABASE_URL|PGDATABASE|PGUSER)=' \
    | redact_env > "$pg_env" || true
  sed -n '1,80p' "$pg_env" | tee -a "$OUT_DIR/summary.txt"

  pg_user="$(docker inspect "$pg_container" --format '{{range .Config.Env}}{{println .}}{{end}}' | awk -F= '$1=="POSTGRES_USER" {print $2; exit}')"
  pg_db="$(docker inspect "$pg_container" --format '{{range .Config.Env}}{{println .}}{{end}}' | awk -F= '$1=="POSTGRES_DB" {print $2; exit}')"
  pg_user="${pg_user:-postgres}"
  pg_db="${pg_db:-$pg_user}"

  run "${pg_safe}_tables" docker exec "$pg_container" psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -Atc "select schemaname||'.'||tablename from pg_tables where schemaname not in ('pg_catalog','information_schema') order by 1;"
  run "${pg_safe}_relevant_columns" docker exec "$pg_container" psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -Atc "select table_schema||'.'||table_name||'.'||column_name from information_schema.columns where table_schema not in ('pg_catalog','information_schema') and (column_name ilike '%metadata%' or column_name ilike '%redirect%' or column_name ilike '%return%' or column_name ilike '%sign%' or column_name ilike '%webhook%' or column_name ilike '%status%' or column_name ilike '%email%' or column_name ilike '%document%') order by 1;"

  cat > "$OUT_DIR/${pg_safe}_search.sql" <<SQL
\\pset tuples_only on
\\pset pager off
DO \$do\$
DECLARE
  r record;
  sql text;
  contract_id text := '$CONTRACT_ID';
  broken_path text := '$BROKEN_PATH';
BEGIN
  FOR r IN
    SELECT table_schema, table_name, column_name
    FROM information_schema.columns
    WHERE table_schema NOT IN ('pg_catalog','information_schema')
      AND data_type IN ('text','character varying','character','json','jsonb','uuid')
  LOOP
    sql := format('SELECT %L AS source, count(*)::text AS matches FROM %I.%I WHERE %I::text ILIKE %L OR %I::text ILIKE %L',
      r.table_schema||'.'||r.table_name||'.'||r.column_name,
      r.table_schema, r.table_name, r.column_name, '%'||contract_id||'%', r.column_name, '%'||broken_path||'%');
    EXECUTE 'INSERT INTO pg_temp.luxit_documenso_matches ' || sql;
  END LOOP;
END
\$do\$;
SELECT source||' matches='||matches FROM pg_temp.luxit_documenso_matches WHERE matches::int > 0 ORDER BY source;
SQL
  sed -i '1i CREATE TEMP TABLE luxit_documenso_matches(source text, matches text);' "$OUT_DIR/${pg_safe}_search.sql"
  docker cp "$OUT_DIR/${pg_safe}_search.sql" "$pg_container:/tmp/luxit_documenso_search.sql" >/dev/null 2>&1 || true
  run "${pg_safe}_contract_metadata_search" docker exec "$pg_container" psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -f /tmp/luxit_documenso_search.sql
  run "${pg_safe}_candidate_document_rows" docker exec "$pg_container" psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -Atc "select 'documents table exists: '||to_regclass('public.Document')::text union all select 'documents lowercase exists: '||to_regclass('public.document')::text union all select 'recipients exists: '||to_regclass('public.Recipient')::text union all select 'webhooks exists: '||to_regclass('public.Webhook')::text;"
done < "$OUT_DIR/postgres_containers.txt"

run compose_file_search sh -c "find / -name 'docker-compose*.yml' -o -name 'docker-compose*.yaml' -o -name 'compose*.yml' -o -name 'compose*.yaml' 2>/dev/null | xargs -r grep -nE 'APP_URL|NEXT_PUBLIC_WEBAPP_URL|NEXTAUTH_URL|WEBAPP_URL|DOCUMENSO_|WEBHOOK_|MAIL_|SMTP_|contractor-hub' | sed -E 's/(SECRET|TOKEN|KEY|PASSWORD|PASS|PRIVATE|AUTH|CREDENTIAL|SALT)=([^[:space:]]+)/\\1=<redacted>/Ig'"
run env_file_search sh -c "find / -name '.env' -o -name '*.env' 2>/dev/null | xargs -r grep -nE 'APP_URL|NEXT_PUBLIC_WEBAPP_URL|NEXTAUTH_URL|WEBAPP_URL|DOCUMENSO_|WEBHOOK_|MAIL_|SMTP_|contractor-hub' | sed -E 's/(SECRET|TOKEN|KEY|PASSWORD|PASS|PRIVATE|AUTH|CREDENTIAL|SALT)=([^[:space:]]+)/\\1=<redacted>/Ig'"
python3 - <<PY > "$OUT_DIR/public_url_check.out" 2> "$OUT_DIR/public_url_check.err" || true
import os, urllib.request
url = os.environ.get('DOCUMENSO_PUBLIC_URL') or os.environ.get('DOCUMENSO_BASE_URL') or '$DOCUMENSO_PUBLIC_URL'
url = url.rstrip('/') + '/'
req = urllib.request.Request(url, headers={'User-Agent':'luxit-documenso-live-audit/1.0'})
with urllib.request.urlopen(req, timeout=15) as resp:
    print('status=' + str(resp.status))
    print('final_url=' + resp.geturl())
    print('content_type=' + str(resp.headers.get('content-type', '')))
PY
log public_url_check
cat "$OUT_DIR/public_url_check.out" "$OUT_DIR/public_url_check.err" | tee -a "$OUT_DIR/summary.txt"

if [ -x ./scripts/documenso/verify_documenso_signing_config.py ]; then
  run validator_real_config python3 ./scripts/documenso/verify_documenso_signing_config.py --root / --documenso-public-url "$DOCUMENSO_PUBLIC_URL"
fi

log "operator_next_steps"
cat <<'EOF' | tee -a "$OUT_DIR/summary.txt"
Use the redacted runtime env and search outputs above to record before/after URL values.
For database metadata, connect to the Documenso Postgres container and query tables/columns containing metadata, redirect, return, sign, webhook, and the affected contract id.
Do not paste secrets into tickets or PRs; include only redacted host/path/status values.
EOF

printf '\nAudit bundle written to %s\n' "$OUT_DIR" | tee -a "$OUT_DIR/summary.txt"
