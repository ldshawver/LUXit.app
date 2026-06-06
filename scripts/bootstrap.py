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

# Validate DATABASE_URL early so the error is actionable
_db_url = os.environ.get("DATABASE_URL", "").strip()
if not _db_url:
    print(
        "\n  ❌  DATABASE_URL is not set.\n"
        "  In Replit: open Tools → Secrets and verify DATABASE_URL is present.\n"
        "  On VPS:    export DATABASE_URL=postgresql://user:pass@host:5432/dbname\n",
        file=sys.stderr,
    )
    sys.exit(1)

# Normalise postgres:// → postgresql:// for SQLAlchemy
if _db_url.startswith("postgres://"):
    os.environ["DATABASE_URL"] = _db_url.replace("postgres://", "postgresql://", 1)

print(f"[bootstrap] DATABASE_URL host: {_db_url.split('@')[-1].split('/')[0]}")

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
