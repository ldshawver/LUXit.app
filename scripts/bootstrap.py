"""
Bootstrap script — creates all tables and seeds the default company + admin user.

Run once after provisioning a fresh PostgreSQL database:
    python3 scripts/bootstrap.py

It is safe to run multiple times: existing rows are left untouched.
"""
import sys
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

# Load .env if present (local/VPS dev only; Replit uses Secrets)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)
except ImportError:
    pass

# Use the same DB resolution logic as the main app so PG* vars (Replit managed
# PostgreSQL) take priority over a potentially stale DATABASE_URL secret.
sys.path.insert(0, _PROJECT_ROOT)
from app import _resolve_db_url

try:
    _resolved = _resolve_db_url()
except RuntimeError as _e:
    print(str(_e), file=sys.stderr)
    sys.exit(1)

import urllib.parse as _up
_parsed = _up.urlparse(_resolved)
print(f"[bootstrap] PostgreSQL: {_parsed.hostname}/{(_parsed.path or '').lstrip('/')}")

from app import create_app
from extensions import db

app = create_app()

with app.app_context():
    print("[bootstrap] Creating all tables (safe no-op if already exist)...")
    db.create_all()
    print("[bootstrap] Tables OK.")

    from models import Company, User, UserCompanyAccess
    from werkzeug.security import generate_password_hash

    # ── Default company ──────────────────────────────────────────────────────
    company = Company.query.filter_by(name="LUX Marketing").first()
    if company is None:
        company = Company(
            name="LUX Marketing",
            is_active=True,
        )
        db.session.add(company)
        db.session.flush()  # assign company.id before FK reference
        print(f"[bootstrap] Created company: {company.name} (id={company.id})")
    else:
        print(f"[bootstrap] Company already exists: {company.name} (id={company.id})")

    # ── Default admin user ───────────────────────────────────────────────────
    admin_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@luxit.app")
    admin_password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "ChangeMe123!")

    user = User.query.filter_by(email=admin_email).first()
    if user is None:
        user = User(
            email=admin_email,
            username=admin_email.split("@")[0],
            password_hash=generate_password_hash(admin_password),
            is_admin=True,
            default_company_id=company.id,
        )
        db.session.add(user)
        db.session.flush()

        access = UserCompanyAccess(
            user_id=user.id,
            company_id=company.id,
            role=UserCompanyAccess.ROLE_OWNER,
            is_default=True,
            can_access_full_app=True,
        )
        db.session.add(access)

        db.session.commit()
        print(f"[bootstrap] Created admin user: {admin_email}")
        print(f"[bootstrap] ⚠️  Default password is '{admin_password}' — change it immediately!")
    else:
        db.session.commit()
        print(f"[bootstrap] Admin user already exists: {admin_email}")

    print("[bootstrap] Done. Database is ready.")
