"""
Run this once on the VPS to create the initial company and link all users to it.

Usage:
  cd /root/lux-email-bot
  source .venv/bin/activate
  python scripts/create_company.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from extensions import db

def run():
    app = create_app()
    with app.app_context():
        from models import Company, User, UserCompanyAccess

        # ── 1. Check if a company already exists ────────────────────────────
        existing = Company.query.first()
        if existing:
            print(f"✓ Company already exists: '{existing.name}' (id={existing.id})")
            company = existing
        else:
            # ── 2. Create the default company ────────────────────────────────
            company = Company(
                name="LUXit Marketing",
                is_active=True,
                billing_tier="professional",
                billing_status="active",
                subscription_tier="professional",
                onboarding_status="complete",
            )
            db.session.add(company)
            db.session.flush()   # get the id before commit
            print(f"✓ Created company: '{company.name}' (id={company.id})")

        # ── 3. Link every existing user who has no company access row ────────
        users = User.query.all()
        linked = 0
        for user in users:
            acc = UserCompanyAccess.query.filter_by(
                user_id=user.id, company_id=company.id
            ).first()
            if not acc:
                role = "owner" if user.is_admin else "member"
                acc = UserCompanyAccess(
                    user_id=user.id,
                    company_id=company.id,
                    role=role,
                    is_default=True,
                    can_access_full_app=True,
                    can_access_mobile_inbox=True,
                )
                db.session.add(acc)
                linked += 1

            # Also set default_company_id if not already set
            if not user.default_company_id:
                user.default_company_id = company.id

        db.session.commit()
        print(f"✓ Linked {linked} user(s) to company (total users: {len(users)})")
        print()
        print("Done. Restart the service:")
        print("  systemctl restart lux-email-bot")

if __name__ == "__main__":
    run()
