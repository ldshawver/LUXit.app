#!/usr/bin/env python3
"""Scheduled-worker entrypoint for tenant-scoped contact identity resolution."""
import argparse

from app import create_app
from extensions import db
from models import Company
from services.contact_resolver import resolve_pending_contacts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int)
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        ids = [args.company_id] if args.company_id else [row.id for row in Company.query.filter_by(is_active=True).all()]
        for company_id in ids:
            print(company_id, resolve_pending_contacts(company_id))
        db.session.commit()


if __name__ == "__main__":
    main()
