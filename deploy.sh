#!/bin/bash
# LUXit.app — Git-to-VPS deploy script
# Run from /root/lux-email-bot as root
# Usage: bash deploy.sh [branch]   (default branch: main)

set -euo pipefail

APP_DIR="/root/lux-email-bot"
VENV="$APP_DIR/.venv"
GUNICORN="$VENV/bin/gunicorn"
SERVICE="luxit"
SITE="https://luxit.app"
BRANCH="${1:-main}"
BIND="127.0.0.1:8001"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✗${NC}  $*"; exit 1; }

echo "════════════════════════════════════════════"
echo "  LUXit Deploy  →  branch: $BRANCH"
echo "════════════════════════════════════════════"

# ── Guards ────────────────────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || fail "Must run as root: sudo bash deploy.sh"
[ -d "$APP_DIR/.git" ] || fail "$APP_DIR is not a git repo — run vps_setup_service.sh first"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
echo ""
echo "── 1. Pull latest code ──"
cd "$APP_DIR"
git fetch origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
if [ "$LOCAL" = "$REMOTE" ]; then
  ok "Already up to date ($BRANCH @ ${LOCAL:0:8})"
else
  git pull --ff-only origin "$BRANCH"
  ok "Updated to ${REMOTE:0:8} on $BRANCH"
fi

# ── 2. System build deps (Debian/Ubuntu) ─────────────────────────────────────
echo ""
echo "── 2. System dependencies ──"
if command -v apt-get &>/dev/null; then
  apt-get install -y --no-install-recommends \
      libpq-dev python3-dev build-essential gcc 2>/dev/null \
    && ok "System deps: libpq-dev gcc python3-dev" \
    || warn "apt-get install failed — psycopg2-binary may not build"
else
  warn "apt-get not found — skipping system dep install"
fi

# ── 3. Install / update Python dependencies ───────────────────────────────────
echo ""
echo "── 3. Python dependencies ──"
if [ ! -x "$GUNICORN" ]; then
  warn "Gunicorn not found — rebuilding venv"
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
if [ -f "$APP_DIR/requirements.txt" ]; then
  "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
  ok "requirements.txt installed"
else
  warn "requirements.txt not found"
fi

# ── 4. Python syntax check ────────────────────────────────────────────────────
echo ""
echo "── 4. Syntax check ──"
ERRORS=0
for f in app.py models.py routes.py twilio_sms.py legal.py; do
  [ -f "$APP_DIR/$f" ] || continue
  if python3 -m py_compile "$APP_DIR/$f" 2>/dev/null; then
    ok "$f"
  else
    warn "SYNTAX ERROR: $f"
    python3 -m py_compile "$APP_DIR/$f" || true
    ERRORS=$((ERRORS + 1))
  fi
done
[ "$ERRORS" -eq 0 ] || fail "Syntax errors — aborting deploy"

# ── 4. Confirm .env is preserved ──────────────────────────────────────────────
echo ""
echo "── 4. Environment file ──"
if [ -f "$APP_DIR/.env" ]; then
  ok ".env present ($(wc -l < "$APP_DIR/.env") lines)"
else
  warn ".env not found — app will likely fail to start"
fi

# ── 5. Kill stale Gunicorn on 8000 (if any) ───────────────────────────────────
pkill -f "gunicorn.*127\.0\.0\.1:8000" 2>/dev/null \
  && warn "Killed stale gunicorn on port 8000" || true

# ── 6. Restart service ────────────────────────────────────────────────────────
echo ""
echo "── 5. Restart luxit service ──"
systemctl daemon-reload
systemctl restart "$SERVICE"
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  ok "$SERVICE active on $BIND"
else
  echo ""
  journalctl -u "$SERVICE" -n 30 --no-pager
  fail "$SERVICE failed to start"
fi

# ── 7. Health check ───────────────────────────────────────────────────────────
echo ""
echo "── 6. Health check ──"
sleep 2
HTTP=$(curl -s -o /dev/null -w '%{http_code}' -L --max-time 15 "$SITE/" 2>/dev/null || echo "000")
if [[ "$HTTP" =~ ^(200|301|302)$ ]]; then
  ok "GET $SITE/ → HTTP $HTTP"
else
  warn "GET $SITE/ → HTTP $HTTP — check Nginx and service logs"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " Deploy complete  (branch: $BRANCH)"
echo ""
echo " Gunicorn processes:"
ps aux | grep gunicorn | grep -v grep | awk '{print "   "$11,$12,$13}' || echo "   (none)"
echo ""
echo " Nginx proxy_pass:"
nginx -T 2>/dev/null | grep "proxy_pass" | grep "800" | sed 's/^[[:space:]]*/   /' || echo "   (none found)"
echo ""
echo " Logs:  sudo journalctl -u $SERVICE -n 80 --no-pager"
echo "════════════════════════════════════════════"
