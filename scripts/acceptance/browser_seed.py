#!/usr/bin/env python3
"""Seed one disposable owner + workspace into an isolated rig database
(isolated_browser_rig.sh). Refuses anything that is not a rig socket."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    if "host=/var/tmp/" not in os.environ.get("DATABASE_URL", ""):
        sys.exit("refusing: not an isolated rig database")
    from werkzeug.security import generate_password_hash

    from app import create_app
    from extensions import db
    from models import Company, User, UserCompanyAccess

    app = create_app()
    with app.app_context():
        company = Company(name="Browser Rig Co")
        db.session.add(company)
        db.session.flush()
        user = User(username="rig-owner", email="rig-owner@example.invalid",
                    password_hash=generate_password_hash(os.environ["RIG_PASSWORD"]),
                    default_company_id=company.id)
        db.session.add(user)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="owner", is_default=True))
        db.session.commit()
        print(f"seeded company={company.id} user={user.id}")


if __name__ == "__main__":
    main()
