#!/bin/bash
# LUXit.app — one-time VPS service setup
# Sets up: git repo, venv, systemd service, Nginx proxy_pass
# Run as root from /root/lux-email-bot:
#   bash vps_setup_service.sh

set -euo pipefail

APP_DIR="/root/lux-email-bot"
VENV="$APP_DIR/.venv"
SERVICE="luxit"
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
GITHUB_REPO="${LUXIT_GITHUB_REPO:-}"   # set env var or will prompt

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC}  $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "${RED}✗${NC}  $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Run as root"

echo "════════════════════════════════════════════"
echo "  LUXit VPS Service Setup"
echo "════════════════════════════════════════════"

# ── 1. Git repo check / init ──────────────────────────────────────────────────
echo ""
echo "── 1. Git repository ──"
if [ -d "$APP_DIR/.git" ]; then
  CURRENT_REMOTE=$(git -C "$APP_DIR" remote get-url origin 2>/dev/null || echo "none")
  ok "Git repo found — remote: $CURRENT_REMOTE"
else
  if [ -z "$GITHUB_REPO" ]; then
    echo -n "  Enter GitHub repo URL (e.g. git@github.com:user/luxit.git): "
    read -r GITHUB_REPO
  fi
  [ -z "$GITHUB_REPO" ] && fail "GitHub repo URL required"
  if [ -d "$APP_DIR" ] && [ "$(ls -A "$APP_DIR")" ]; then
    warn "$APP_DIR exists with files — initialising git in-place"
    git -C "$APP_DIR" init
    git -C "$APP_DIR" remote add origin "$GITHUB_REPO"
    git -C "$APP_DIR" fetch origin main
    git -C "$APP_DIR" checkout -b main --track origin/main 2>/dev/null \
      || git -C "$APP_DIR" branch --set-upstream-to=origin/main main
    ok "Git initialised and linked to $GITHUB_REPO"
  else
    git clone "$GITHUB_REPO" "$APP_DIR"
    ok "Cloned $GITHUB_REPO → $APP_DIR"
  fi
fi

# ── 2. Python venv ────────────────────────────────────────────────────────────
echo ""
echo "── 2. Python venv ──"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  ok "Created venv at $VENV"
else
  ok "Venv already exists"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
if [ -f "$APP_DIR/requirements.txt" ]; then
  "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
  ok "Dependencies installed"
fi

# ── 3. systemd service ────────────────────────────────────────────────────────
echo ""
echo "── 3. systemd service ($SERVICE_FILE) ──"
cat > "$SERVICE_FILE" << 'UNIT'
[Unit]
Description=LUXit Marketing Platform
After=network.target postgresql.service
Wants=network.target

[Service]
Type=exec
User=root
WorkingDirectory=/root/lux-email-bot
EnvironmentFile=/root/lux-email-bot/.env
ExecStart=/root/lux-email-bot/.venv/bin/gunicorn \
    --workers 3 \
    --bind 127.0.0.1:8001 \
    --timeout 120 \
    --log-level info \
    --access-logfile /var/log/luxit-access.log \
    --error-logfile /var/log/luxit-error.log \
    app:app
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

ok "Wrote $SERVICE_FILE"
touch /var/log/luxit-access.log /var/log/luxit-error.log

# ── 4. Kill any stale 8000 service ────────────────────────────────────────────
echo ""
echo "── 4. Remove stale 8000 service ──"
pkill -f "gunicorn.*127\.0\.0\.1:8000" 2>/dev/null && warn "Killed stale gunicorn on 8000" || true
# Disable any old wsgi-based service if it exists under a different name
for old in luxit-old lux-email-bot email-marketing; do
  if systemctl is-active --quiet "$old" 2>/dev/null; then
    systemctl stop "$old" && systemctl disable "$old" \
      && warn "Stopped old service: $old" || true
  fi
done

# ── 5. Enable and start luxit ─────────────────────────────────────────────────
echo ""
echo "── 5. Enable + start $SERVICE ──"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  ok "$SERVICE is active on 127.0.0.1:8001"
else
  journalctl -u "$SERVICE" -n 40 --no-pager
  fail "$SERVICE failed to start"
fi

# ── 6. Fix Nginx proxy_pass → 8001 ───────────────────────────────────────────
echo ""
echo "── 6. Nginx proxy_pass → 8001 ──"
NGINX_CHANGED=0
for cfg in /etc/nginx/sites-enabled/* /etc/nginx/sites-available/* /etc/nginx/conf.d/*.conf; do
  [ -f "$cfg" ] || continue
  if grep -q "proxy_pass http://127\.0\.0\.1:8000" "$cfg" 2>/dev/null; then
    cp "$cfg" "${cfg}.bak.$(date +%s)"
    sed -i 's|proxy_pass http://127\.0\.0\.1:8000;|proxy_pass http://127.0.0.1:8001;|g' "$cfg"
    ok "Fixed proxy_pass in $cfg"
    NGINX_CHANGED=1
  fi
done
[ "$NGINX_CHANGED" -eq 0 ] && ok "Nginx already pointing to 8001 (or no changes needed)"

nginx -t && systemctl reload nginx && ok "Nginx reloaded"

# ── 7. Verify ─────────────────────────────────────────────────────────────────
echo ""
echo "── 7. Verification ──"
echo ""
echo "  Gunicorn:"
ps aux | grep gunicorn | grep -v grep | awk '{print "    "$11,$12,$13}' || echo "    (none)"
echo ""
echo "  Nginx proxy_pass:"
nginx -T 2>/dev/null | grep "proxy_pass" | grep "800" | sed 's/^[[:space:]]*/    /' || echo "    (none found)"
echo ""
HTTP=$(curl -s -o /dev/null -w '%{http_code}' -L --max-time 15 https://luxit.app/ 2>/dev/null || echo "000")
echo "  GET https://luxit.app/ → HTTP $HTTP"

echo ""
echo "════════════════════════════════════════════"
echo " Setup complete."
echo ""
echo " Git workflow going forward:"
echo "   cd /root/lux-email-bot && bash deploy.sh"
echo ""
echo " Logs:"
echo "   sudo journalctl -u $SERVICE -n 80 --no-pager"
echo "   tail -f /var/log/luxit-error.log"
echo "════════════════════════════════════════════"
