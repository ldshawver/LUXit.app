#!/bin/bash
# VPS fix: point Nginx from stale port 8001 → active Gunicorn port 8000
# Run as root from /root/lux-email-bot:
#   bash vps_fix_nginx.sh

set -e

echo "── Step 1: Kill any stale Gunicorn on 8001 ──"
sudo pkill -f "gunicorn.*127.0.0.1:8001" 2>/dev/null || echo "  (none running on 8001 — OK)"
sleep 1

echo ""
echo "── Step 2: Locate Nginx config files referencing 8001 ──"
NGINX_FILES=$(sudo grep -rl "127.0.0.1:8001" /etc/nginx/ 2>/dev/null || true)
if [ -z "$NGINX_FILES" ]; then
  echo "  No Nginx files reference 8001 — already fixed or wrong config path."
  sudo nginx -T | grep -n "proxy_pass.*800" || true
else
  echo "  Found:"
  echo "$NGINX_FILES"
fi

echo ""
echo "── Step 3: Replace 8001 → 8000 in all Nginx config files ──"
for f in $NGINX_FILES; do
  echo "  Patching: $f"
  sudo cp "$f" "${f}.bak.$(date +%s)"
  sudo sed -i 's|proxy_pass http://127\.0\.0\.1:8001;|proxy_pass http://127.0.0.1:8000;|g' "$f"
  echo "  Done: $f"
done

echo ""
echo "── Step 4: Test Nginx config ──"
sudo nginx -t

echo ""
echo "── Step 5: Reload Nginx ──"
sudo systemctl reload nginx
echo "  Nginx reloaded ✓"

echo ""
echo "── Step 6: Restart luxit service ──"
sudo systemctl restart luxit
sleep 2
echo "  luxit restarted ✓"

echo ""
echo "── Step 7: Verify ──"
echo "  --- Gunicorn processes ---"
ps aux | grep gunicorn | grep -v grep || echo "  (none found — check service)"

echo ""
echo "  --- Nginx proxy_pass lines ---"
sudo nginx -T 2>/dev/null | grep -n "proxy_pass.*800" || true

echo ""
echo "  --- luxit service status ---"
sudo systemctl is-active luxit && echo "  luxit: active ✓" || echo "  luxit: INACTIVE — check logs"

echo ""
echo "── Step 8: Quick HTTP check ──"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -L https://luxit.app/ 2>/dev/null || echo "000")
echo "  GET https://luxit.app/ → HTTP $HTTP_CODE"

echo ""
echo "══ Complete. Expected: only 8000 in Nginx, luxit active. ══"
echo "   Check journal: sudo journalctl -u luxit -n 60 --no-pager"
