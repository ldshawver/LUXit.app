# DEPLOYMENT RUNBOOK — LUXit.app
**Last Updated:** 2026-05-02

---

## OVERVIEW

| Item | Value |
|------|-------|
| VPS Path | `/root/lux-email-bot/` |
| Service Name | `lux-email-bot.service` (systemd) |
| App Port | 8001 (gunicorn) |
| Nginx Port | 443 (proxied to 8001) |
| Python | `.venv/bin/python3` |
| Replit Dev Port | 5000 |
| Production URL | https://luxit.app |

---

## STANDARD DEPLOYMENT (Code Changes)

```bash
# 1. SSH to VPS
ssh root@<vps-ip>

# 2. Navigate to app
cd /root/lux-email-bot

# 3. Pull latest code from GitHub
git pull origin main

# 4. Install any new Python dependencies
.venv/bin/pip install -r requirements.txt

# 5. Run database migrations (safe — skips existing columns/tables)
.venv/bin/python3 scripts/migrate_db.py
# PostgreSQL deployments must also run the ledgered forward-only SQL migrations.
.venv/bin/python3 scripts/apply_migrations.py "$DATABASE_URL" migrations

# 6. Restart the service
systemctl restart lux-email-bot

# 7. Verify it started
systemctl status lux-email-bot

# 8. Monitor logs for errors
journalctl -u lux-email-bot -f
```

### Quick one-liner:
```bash
cd /root/lux-email-bot && git pull && .venv/bin/pip install -r requirements.txt -q && .venv/bin/python3 scripts/migrate_db.py && .venv/bin/python3 scripts/apply_migrations.py "$DATABASE_URL" migrations && systemctl restart lux-email-bot && journalctl -u lux-email-bot -f
```

### Using the deploy script:
```bash
cd /root/lux-email-bot && python3 vps_deploy_saas_crm.py
```

---

## FIRST-TIME / FRESH DEPLOYMENT

```bash
# 1. Clone repo
cd /root
git clone https://github.com/<org>/lux-email-bot.git
cd lux-email-bot

# 2. Create virtual environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Create .env file (never commit this)
cat > .env << 'EOF'
SECRET_KEY=<generate-with-python-secrets>
SESSION_SECRET=<same-or-different>
DATABASE_URL=sqlite:///lux.db
FERNET_KEY=<generate-with-cryptography.fernet.Fernet.generate_key>
OPENAI_API_KEY=sk-...
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
EOF

# 4. Run initial DB creation
.venv/bin/python3 -c "from app import create_app; from extensions import db; app=create_app(); app.app_context().push(); db.create_all()"

# 5. Run migrations
.venv/bin/python3 scripts/migrate_db.py

# 6. Create admin user
.venv/bin/python3 -c "
from app import create_app
from extensions import db
from models import User
import bcrypt
app = create_app()
with app.app_context():
    u = User(email='luke@adiken.com', username='admin', is_active=True)
    u.set_password('Luxit2026!')
    db.session.add(u); db.session.commit()
    print('Admin created')
"

# 7. Set up systemd service (see /etc/systemd/system/lux-email-bot.service)
systemctl daemon-reload
systemctl enable lux-email-bot
systemctl start lux-email-bot
```

---

## NGINX CONFIGURATION

```nginx
server {
    listen 443 ssl;
    server_name luxit.app www.luxit.app;

    ssl_certificate     /etc/letsencrypt/live/luxit.app/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/luxit.app/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 120s;
    }

    location /static/ {
        alias /root/lux-email-bot/static/;
        expires 7d;
    }
}

server {
    listen 80;
    server_name luxit.app www.luxit.app;
    return 301 https://$host$request_uri;
}
```

---

## SYSTEMD SERVICE FILE

`/etc/systemd/system/lux-email-bot.service`:

```ini
[Unit]
Description=LUXit Marketing Platform
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/lux-email-bot
EnvironmentFile=/root/lux-email-bot/.env
ExecStart=/root/lux-email-bot/.venv/bin/gunicorn --bind 0.0.0.0:8001 --workers 1 --timeout 120 wsgi:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## ENVIRONMENT VARIABLES REQUIRED

| Variable | Required | Notes |
|----------|----------|-------|
| `SECRET_KEY` | ✅ | Flask session secret |
| `SESSION_SECRET` | ✅ | Same as SECRET_KEY or separate |
| `DATABASE_URL` | ✅ | SQLite: `sqlite:///lux.db` |
| `FERNET_KEY` | ✅ | Encryption key for secrets |
| `OPENAI_API_KEY` | ✅ | For AI agents |
| `STRIPE_SECRET_KEY` | ⚠️ Optional | For Stripe billing |
| `STRIPE_WEBHOOK_SECRET` | ⚠️ Optional | For Stripe webhook verification |
| `TWILIO_ACCOUNT_SID` | ⚠️ Optional | Falls back to company settings |
| `TWILIO_AUTH_TOKEN` | ⚠️ Optional | Falls back to company settings |
| `SUPABASE_URL` | ⚠️ Optional | For Supabase integration |
| `SUPABASE_SERVICE_KEY` | ⚠️ Optional | For Supabase integration |
| `N8N_WEBHOOK_URL` | ⚠️ Optional | Global n8n fallback |

---

## ROLLBACK

```bash
# View recent commits
git log --oneline -10

# Rollback to specific commit
git checkout <commit-hash>
systemctl restart lux-email-bot

# Or rollback one commit
git revert HEAD
git push
# Then: git pull + restart on VPS
```

---

## HEALTH CHECK

```bash
# App status
systemctl status lux-email-bot

# Recent logs
journalctl -u lux-email-bot -n 50

# Test endpoint
curl -s https://luxit.app/health | python3 -m json.tool

# Check port
ss -tlnp | grep 8001
```

---

## .GITIGNORE VERIFICATION

Ensure these are in `.gitignore`:
```
.env
*.db
*.sqlite
*.sqlite3
lux.db
__pycache__/
*.pyc
.venv/
vps_patch*.py
attached_assets/
```
