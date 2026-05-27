# DEPLOYMENT READINESS REPORT
**Date:** 2026-05-02

---

## CURRENT DEPLOYMENT STATUS

| Item | Status | Notes |
|------|--------|-------|
| GitHub as source of truth | ✅ | Replit syncs to GitHub |
| VPS pulls from GitHub | ✅ | `git pull` on VPS |
| Single app process | ✅ | systemd manages one gunicorn |
| Nginx → port 8001 | ✅ | Confirmed in VPS setup |
| HTTPS/TLS | ✅ | Let's Encrypt via certbot |
| Production URL | ✅ | https://luxit.app |
| Database not committed | ⚠️ Verify `.gitignore` |
| `.env` not committed | ⚠️ Verify `.gitignore` |
| Patch scripts ignored | ⚠️ `vps_*.py` should be in `.gitignore` |

---

## DEPLOYMENT PROCESS

**Source of truth:** Replit workspace → GitHub `main` branch → VPS pull

**Standard deploy:**
```bash
cd /root/lux-email-bot
git pull
.venv/bin/pip install -r requirements.txt -q
.venv/bin/python3 scripts/migrate_db.py
systemctl restart lux-email-bot
```

---

## .GITIGNORE AUDIT

Verify `/root/lux-email-bot/.gitignore` contains:
```
.env
*.db
*.sqlite
*.sqlite3
lux.db
__pycache__/
*.pyc
.venv/
*.log
vps_patch*.py
attached_assets/
instance/
```

---

## KNOWN ISSUES

| Issue | Priority | Fix |
|-------|----------|-----|
| `.gitignore` not verified | P1 | Audit and update |
| VPS gunicorn port: 8001 (VPS) vs 5000 (Replit) | Noted | Different configs, OK |
| `WTF_CSRF_ENABLED=not is_replit` disables CSRF in dev | P2 | Acceptable, document |
| Manual deployment required | P2 | Consider GitHub Actions webhook |
| Single worker (`--workers 1`) | P2 | OK for current load, scale when needed |

---

## PRODUCTION CHECKLIST

- [ ] Valid OpenAI API key set in `.env`
- [ ] Stripe keys set in `.env`  
- [ ] `STRIPE_WEBHOOK_SECRET` set in `.env`
- [ ] Twilio credentials in company settings
- [ ] `FERNET_KEY` backed up securely
- [ ] SSL certificate auto-renewal tested (`certbot renew --dry-run`)
- [ ] Database backup scheduled (cron `cp lux.db lux.db.bak`)
- [ ] Monitoring/alerting on service failures (`systemctl` restart policy: `Restart=always`)
- [ ] nginx HSTS header added
- [ ] `/auth/debug-session` disabled or IP-restricted

---

## SCALABILITY NOTES

Current: SQLite + single gunicorn worker. Suitable for < 50 concurrent users.

When scaling:
1. Migrate to PostgreSQL (`DATABASE_URL=postgresql://...`)
2. Increase gunicorn workers (`--workers 4`)
3. Add Redis for session storage
4. Move to managed hosting (Railway, Render, or dedicated VPS)
