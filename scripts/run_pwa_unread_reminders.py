#!/usr/bin/env python3
"""Run the PWA unread-message reminder job once.

Install with cron/systemd timer every minute on the VPS:
  * * * * * cd /path/to/LUXit.app && python scripts/run_pwa_unread_reminders.py

Dry-run without creating reminder rows:
  python scripts/run_pwa_unread_reminders.py --dry-run
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description="Run PWA unread-message reminders once")
    parser.add_argument("--dry-run", action="store_true", help="Count reminders that would be created without writing rows")
    args = parser.parse_args()
    from app import create_app
    from inbox_pwa import create_unread_message_reminders

    app = create_app()
    with app.app_context():
        print(json.dumps(create_unread_message_reminders(dry_run=args.dry_run), sort_keys=True))


if __name__ == "__main__":
    main()
