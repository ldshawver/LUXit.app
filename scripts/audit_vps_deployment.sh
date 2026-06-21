#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/var/www/LUXit.app}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-lux-email-bot.service}"
PORT="${PORT:-8001}"
DATABASE_URL="${DATABASE_URL:?DATABASE_URL is required}"
COOKIE_FILE="${COOKIE_FILE:-/tmp/luxit.cookies}"
BASE_URL="${BASE_URL:-http://127.0.0.1:${PORT}}"

cd "$REPO_DIR"

echo "== Git deployment parity =="
git fetch "$REMOTE" "$BRANCH" --prune
HEAD_SHA="$(git rev-parse HEAD)"
ORIGIN_SHA="$(git rev-parse "${REMOTE}/${BRANCH}")"
echo "HEAD=${HEAD_SHA}"
echo "${REMOTE}/${BRANCH}=${ORIGIN_SHA}"
test "$HEAD_SHA" = "$ORIGIN_SHA"

echo "== Apply forward migrations only (rollback files excluded) =="
while IFS= read -r -d '' migration; do
  echo "Applying ${migration}"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$migration"
done < <(find migrations -maxdepth 1 -type f -name '*.sql' ! -iname '*rollback*' ! -iname '*revert*' -print0 | sort -z)

echo "== Restart and verify service on port ${PORT} =="
sudo systemctl restart "$SERVICE"
sudo systemctl is-active --quiet "$SERVICE"
if command -v ss >/dev/null 2>&1; then
  ss -ltnp | grep ":${PORT}"
else
  netstat -ltnp | grep ":${PORT}"
fi
curl -fsS "${BASE_URL}/auth/login" >/dev/null

echo "== Live PWA DOM/CSS cache-busting proof =="
if [[ -s "$COOKIE_FILE" ]]; then
  curl -fsS -b "$COOKIE_FILE" "${BASE_URL}/app/inbox" -o /tmp/luxit-pwa-inbox.html
  grep -E 'data-pwa-version|/static/sw\.js\?v=|manifest\.json\?v=' /tmp/luxit-pwa-inbox.html
  grep -E 'Push setup incomplete|VAPID_PUBLIC_KEY|data-vapid-missing' /tmp/luxit-pwa-inbox.html
  curl -fsS -b "$COOKIE_FILE" "${BASE_URL}/app/calls" -o /tmp/luxit-pwa-calls.html
  grep -E 'font-size:1\.45rem|\.nav span\{font-size|--pwa-card-bg|--pwa-primary-soft' /tmp/luxit-pwa-calls.html
else
  echo "SKIP DOM proof: COOKIE_FILE=${COOKIE_FILE} missing; create it from an authenticated admin/mobile-inbox session."
fi

echo "== PWA service worker version proof =="
curl -fsS "${BASE_URL}/static/sw.js?v=$(git rev-parse --short HEAD)" | grep -E "SW_VERSION|luxit-inbox-|url\\.pathname\\.startsWith\\('/app/'\\)"

echo "== Recent service logs must be clean =="
journalctl -u "$SERVICE" --since "20 minutes ago" --no-pager | egrep -i 'UndefinedColumn|InFailedSqlTransaction|ProgrammingError|Traceback| 500 |not enabled on server' && exit 1 || true

echo "DEPLOY_AUDIT_OK=1"
