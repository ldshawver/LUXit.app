#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="${APP_ROOT:-$(pwd)}"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
INDEX_CANDIDATES=(
  "$APP_ROOT/dist/public/index.html"
  "$APP_ROOT/dist/index.html"
  "$APP_ROOT/build/index.html"
  "$APP_ROOT/index.html"
)
found=""
for f in "${INDEX_CANDIDATES[@]}"; do
  if [[ -f "$f" ]]; then found="$f"; break; fi
done
if [[ -z "$found" ]]; then
  echo "ERROR: frontend index.html missing; checked: ${INDEX_CANDIDATES[*]}" >&2
  exit 20
fi
echo "frontend_index=$found"
listeners=$(ss -lntp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print}' || true)
count=$(printf '%s\n' "$listeners" | sed '/^$/d' | wc -l | tr -d ' ')
echo "$listeners"
if [[ "$count" -gt 1 ]]; then
  echo "ERROR: multiple listeners on $HOST:$PORT" >&2
  exit 21
fi
if command -v pm2 >/dev/null 2>&1; then pm2 list; else echo "pm2_unavailable=1"; fi
if curl -fsSI "http://$HOST:$PORT" >/tmp/paylink_headers.$$ 2>/dev/null; then cat /tmp/paylink_headers.$$; rm -f /tmp/paylink_headers.$$; fi
