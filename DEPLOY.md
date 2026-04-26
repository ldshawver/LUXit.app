# LUXit.app — Deployment Guide

## Stack

| Component | Value |
|-----------|-------|
| App path | `/root/lux-email-bot` |
| Python venv | `/root/lux-email-bot/.venv` |
| Gunicorn bind | `127.0.0.1:8001` |
| Entry point | `app:app` |
| Workers | 3 |
| systemd service | `luxit` |
| Nginx → Gunicorn | `proxy_pass http://127.0.0.1:8001` |
| Environment file | `/root/lux-email-bot/.env` |

---

## First-time VPS setup

Run once to create the systemd service, fix Nginx, and link git:

```bash
cd /root/lux-email-bot
bash vps_setup_service.sh
```

This script:
- Initialises git if needed and links to GitHub
- Creates/activates the Python venv and installs `requirements.txt`
- Writes `/etc/systemd/system/luxit.service`
- Kills any stale Gunicorn on port 8000
- Fixes all Nginx `proxy_pass` entries to point to `8001`
- Enables, starts, and verifies the service

---

## Daily deployment (Git pull → restart)

```bash
cd /root/lux-email-bot
bash deploy.sh          # deploys main branch
bash deploy.sh staging  # deploys a specific branch
```

`deploy.sh` does:
1. `git fetch + pull --ff-only origin <branch>`
2. `pip install -r requirements.txt`
3. Python syntax check on key files
4. `systemctl restart luxit`
5. `curl` health check on `https://luxit.app/`

---

## systemd service

**File:** `/etc/systemd/system/luxit.service`

```ini
[Unit]
Description=LUXit Marketing Platform
After=network.target

[Service]
User=root
WorkingDirectory=/root/lux-email-bot
EnvironmentFile=/root/lux-email-bot/.env
ExecStart=/root/lux-email-bot/.venv/bin/gunicorn \
    --workers 3 \
    --bind 127.0.0.1:8001 \
    --timeout 120 \
    --log-level info \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Commands:**
```bash
sudo systemctl start luxit
sudo systemctl stop luxit
sudo systemctl restart luxit
sudo systemctl status luxit
sudo journalctl -u luxit -n 80 --no-pager
sudo journalctl -u luxit -f            # live tail
```

---

## Nginx

All `proxy_pass` entries for `luxit.app` must point to `http://127.0.0.1:8001`.

**Check:**
```bash
sudo nginx -T | grep -n "proxy_pass.*800"
```

Expected output (only 8001, no 8001):
```
proxy_pass http://127.0.0.1:8001;
```

**Fix if 8000 appears:**
```bash
sudo sed -i 's|proxy_pass http://127\.0\.0\.1:8000;|proxy_pass http://127.0.0.1:8001;|g' \
    /etc/nginx/sites-enabled/* /etc/nginx/sites-available/*
sudo nginx -t && sudo systemctl reload nginx
```

---

## Verification checklist

```bash
# 1. Only one Gunicorn, on 8001
ps aux | grep gunicorn | grep -v grep

# 2. Nginx pointing to 8001
sudo nginx -T | grep -n "proxy_pass.*800"

# 3. Site responds
curl -I https://luxit.app/

# 4. Recent logs (no errors)
sudo journalctl -u luxit -n 80 --no-pager
```

---

## Log files

| Log | Location |
|-----|----------|
| systemd journal | `journalctl -u luxit` |
| Gunicorn access | `/var/log/luxit-access.log` |
| Gunicorn errors | `/var/log/luxit-error.log` |

---

## .env — never committed to git

The `.env` file lives only on the VPS at `/root/lux-email-bot/.env`.  
`deploy.sh` never touches it — `git pull` will not overwrite it (it is in `.gitignore`).

Key variables required:
```
DATABASE_URL=
SECRET_KEY=
ENCRYPTION_MASTER_KEY=
TWILIO_ACCOUNT_SID=      # optional — also set via /twilio/settings UI
TWILIO_AUTH_TOKEN=        # optional — also set via /twilio/settings UI
```

---

## GitHub as source of truth

```
Replit (edit) → git push → GitHub → ssh VPS → bash deploy.sh
```

To push from Replit to GitHub, use the git panel or:
```bash
git add -A && git commit -m "your message" && git push origin main
```

Then on VPS:
```bash
cd /root/lux-email-bot && bash deploy.sh
```
