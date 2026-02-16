# Phase 0 Runbook (Stabilization & Login Recovery)

This runbook covers the Phase 0 actions plus the **server/VPS testing commands** to validate each step.

## 1) Nginx upstream alignment (VPS)
**Goal:** Ensure Nginx proxies to Gunicorn on `127.0.0.1:5000`.

```bash
sudo rg -n "proxy_pass" /etc/nginx/sites-enabled/luxit.app
sudo sed -n '1,200p' /etc/nginx/sites-enabled/luxit.app
```

Update the upstream if needed:

```bash
sudo sed -i 's/127.0.0.1:8000/127.0.0.1:5000/g' /etc/nginx/sites-enabled/luxit.app
sudo nginx -t
sudo systemctl reload nginx
```

## 2) App factory & auth sanity (VPS)
**Goal:** Verify the single app factory and auth blueprint compile cleanly.

```bash
cd /opt/luxit
source venv/bin/activate
python -m py_compile app.py auth.py wsgi.py
```

## 3) Restart service & health check (VPS)
**Goal:** Ensure Gunicorn is healthy and responding.

```bash
sudo systemctl restart lux.service
sudo systemctl status lux.service --no-pager -l
curl -sSf http://127.0.0.1:5000/ >/dev/null
```

## 4) Login smoke check (VPS)
**Goal:** Verify login view loads and auth redirects work.

```bash
curl -sSf http://127.0.0.1:5000/auth/login | head -n 20
```

Optional: Create/reset an admin (local-only, guarded by env var).

```bash
export ALLOW_ADMIN_CREATE=true
python scripts/create_admin.py
```

## 5) Log checks (VPS)
**Goal:** Capture any remaining runtime errors.

```bash
journalctl -u lux.service -n 200 --no-pager
```

## 6) CI smoke checks (repo)
**Goal:** Ensure compile sanity in CI.

```bash
python -m py_compile app.py auth.py wsgi.py
```
