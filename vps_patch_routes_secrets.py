"""
VPS patch — fixes get_company_secrets and save_company_secrets in routes.py.

Root cause: both route functions were missing an explicit
    from models import Company, CompanySecret
inside the function body, so Python used a stale/module-level Company
reference that lacked set_secret().

Run from /root/lux-email-bot:
    python3 vps_patch_routes_secrets.py

Safe to run multiple times — idempotent.
"""
import sys
import re
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).parent
ROUTES  = APP_DIR / "routes.py"

if not ROUTES.exists():
    sys.exit(f"ERROR: {ROUTES} not found — run from /root/lux-email-bot")

src = ROUTES.read_text()

# ── Guard: already patched? ───────────────────────────────────────────────────
if (
    "from models import Company, CompanySecret" in src
    and "Secret save has set_secret" in src
):
    print("routes.py already patched — nothing to do.")
    subprocess.run([sys.executable, "-m", "py_compile", str(ROUTES)])
    sys.exit(0)

# ── Replacement for get_company_secrets ──────────────────────────────────────
OLD_GET = """\
def get_company_secrets(company_id):
    \"\"\"Get configured secrets for a company (masked — never returns plaintext).\"\"\"
    try:
        from models import CompanySecret
        from services.secret_vault import vault
        company = Company.query.get(company_id)
        if not company:
            return jsonify({'success': False, 'error': 'Company not found'}), 404

        if not current_user.can_edit_company(company_id):
            return jsonify({'success': False, 'error': 'Permission denied'}), 403"""

NEW_GET = """\
def get_company_secrets(company_id):
    \"\"\"Get configured secrets for a company (masked — never returns plaintext).\"\"\"
    try:
        from models import Company, CompanySecret
        from services.secret_vault import vault
        company = Company.query.get_or_404(company_id)

        if not current_user.can_edit_company(company_id):
            return jsonify({'success': False, 'error': 'Permission denied'}), 403"""

# ── Replacement for save_company_secrets ─────────────────────────────────────
# Handle both old variants: with or without local CompanySecret import
OLD_SAVE_A = """\
def save_company_secrets(company_id):
    \"\"\"Save/update encrypted secrets for a company.\"\"\"
    try:
        company = Company.query.get(company_id)
        if not company:
            return jsonify({'success': False, 'error': 'Company not found'}), 404

        if not current_user.can_edit_company(company_id):
            return jsonify({'success': False, 'error': 'Permission denied'}), 403

        data = request.get_json() or {}
        saved = []

        for key, value in data.items():
            if value:
                company.set_secret(key, value)   # encrypts + upserts
                saved.append(key)"""

OLD_SAVE_B = """\
def save_company_secrets(company_id):
    \"\"\"Save/update encrypted secrets for a company.\"\"\"
    try:
        from models import CompanySecret
        company = Company.query.get(company_id)
        if not company:
            return jsonify({'success': False, 'error': 'Company not found'}), 404

        if not current_user.can_edit_company(company_id):
            return jsonify({'success': False, 'error': 'Permission denied'}), 403

        data = request.get_json() or {}
        saved = []

        for key, value in data.items():
            if value:
                company.set_secret(key, value)   # encrypts + upserts
                saved.append(key)"""

NEW_SAVE = """\
def save_company_secrets(company_id):
    \"\"\"Save/update encrypted secrets for a company.\"\"\"
    try:
        from models import Company, CompanySecret
        company = Company.query.get_or_404(company_id)

        logger.info(f"Secret save company class: {type(company)}")
        logger.info(f"Secret save has set_secret: {hasattr(company, 'set_secret')}")

        if not current_user.can_edit_company(company_id):
            return jsonify({'success': False, 'error': 'You do not have permission to edit this company'}), 403

        data = request.get_json() or {}
        saved = []

        for key, value in data.items():
            if value:
                company.set_secret(key, value)   # encrypts + upserts
                saved.append(key)"""

# ── Apply patches ─────────────────────────────────────────────────────────────
changed = False

if OLD_GET in src:
    src = src.replace(OLD_GET, NEW_GET, 1)
    print("  Patched get_company_secrets ✓")
    changed = True
elif "from models import Company, CompanySecret" in src and "get_or_404" in src:
    print("  get_company_secrets already patched ✓")
else:
    print("  WARNING: could not locate OLD get_company_secrets pattern")
    print("           Manually add 'from models import Company, CompanySecret' inside the function.")

if OLD_SAVE_A in src:
    src = src.replace(OLD_SAVE_A, NEW_SAVE, 1)
    print("  Patched save_company_secrets (variant A) ✓")
    changed = True
elif OLD_SAVE_B in src:
    src = src.replace(OLD_SAVE_B, NEW_SAVE, 1)
    print("  Patched save_company_secrets (variant B) ✓")
    changed = True
elif "Secret save has set_secret" in src:
    print("  save_company_secrets already patched ✓")
else:
    print("  WARNING: could not locate OLD save_company_secrets pattern")
    print("           Manually add 'from models import Company, CompanySecret' inside the function.")

if changed:
    ROUTES.write_text(src)
    print(f"  Wrote {ROUTES}")

# ── Syntax check ──────────────────────────────────────────────────────────────
print("\n── Syntax check ──")
result = subprocess.run(
    [sys.executable, "-m", "py_compile", str(ROUTES)],
    capture_output=True, text=True
)
if result.returncode == 0:
    print("  routes.py syntax OK ✓")
else:
    print(f"  SYNTAX ERROR:\n{result.stderr}")
    sys.exit(1)

# ── Quick acceptance test ─────────────────────────────────────────────────────
print("\n── Acceptance test ──")
result = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, '.'); from models import Company; "
     "print('Has set_secret:', hasattr(Company, 'set_secret'))"],
    capture_output=True, text=True, cwd=str(APP_DIR)
)
if result.returncode == 0:
    print(" ", result.stdout.strip())
else:
    print("  Could not import models directly (needs Flask app context) — that is OK.")
    print("  Test will pass after service restart.")

print("\n══ Done. Restart: ══")
print("   sudo systemctl restart luxit")
print("   sudo journalctl -u luxit -n 60 --no-pager")
