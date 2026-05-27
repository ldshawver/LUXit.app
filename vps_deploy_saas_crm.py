#!/usr/bin/env python3
"""
VPS deployment script for SaaS Command Center.

Run on VPS:
    cd /root/lux-email-bot
    git pull
    python3 vps_deploy_saas_crm.py
"""
import subprocess, sys, os

ROOT = "/root/lux-email-bot"
os.chdir(ROOT)

OK  = lambda s: print(f"\033[32m✓\033[0m  {s}")
ERR = lambda s: print(f"\033[31m✗\033[0m  {s}")
HDR = lambda s: print(f"\n\033[35m{'─'*55}\n  {s}\n{'─'*55}\033[0m")

# ── 1. Git pull ───────────────────────────────────────────────
HDR("1 / 4  Git pull")
result = subprocess.run(["git", "pull"], capture_output=True, text=True)
print(result.stdout)
if result.returncode != 0:
    ERR(f"git pull failed:\n{result.stderr}")
    sys.exit(1)
OK("git pull complete")

# ── 2. Install any new dependencies ──────────────────────────
HDR("2 / 4  pip install (requests already present — just verifying)")
result = subprocess.run(
    [".venv/bin/pip", "install", "requests", "--quiet"],
    capture_output=True, text=True
)
if result.returncode == 0:
    OK("dependencies OK")
else:
    ERR(result.stderr[:300])

# ── 3. Run DB migration ───────────────────────────────────────
HDR("3 / 4  Database migration")
result = subprocess.run(
    [".venv/bin/python3", "scripts/migrate_db.py"],
    capture_output=True, text=True
)
print(result.stdout)
if result.returncode != 0:
    ERR(f"Migration error:\n{result.stderr[:500]}")
    sys.exit(1)
OK("Migration complete")

# ── 4. Restart service ────────────────────────────────────────
HDR("4 / 4  Restart lux-email-bot.service")
result = subprocess.run(
    ["systemctl", "restart", "lux-email-bot"],
    capture_output=True, text=True
)
if result.returncode == 0:
    OK("Service restarted")
else:
    ERR(f"Restart failed:\n{result.stderr}")
    sys.exit(1)

# ── Done ──────────────────────────────────────────────────────
print("\n\033[32m" + "="*55)
print("  SaaS Command Center deployed successfully!")
print("  → https://luxit.app/saas")
print("="*55 + "\033[0m\n")
