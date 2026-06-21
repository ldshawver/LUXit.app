#!/usr/bin/env python3
"""Backfill PWA conversation contact names from CRM/Google Contact rows.

Usage:
  python scripts/backfill_conversation_contact_names.py --dry-run
  python scripts/backfill_conversation_contact_names.py --company-id 123
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description="Backfill TwilioConversation contact_name/contact_id from Contact cache")
    parser.add_argument("--company-id", type=int, default=None, help="Restrict backfill to one company_id")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without committing")
    args = parser.parse_args()

    from app import create_app
    from services.google_contacts import backfill_conversation_contact_names

    app = create_app()
    with app.app_context():
        result = backfill_conversation_contact_names(company_id=args.company_id, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
