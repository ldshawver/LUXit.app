#!/usr/bin/env python3
"""Encrypt existing SocialMediaAccount plaintext tokens without logging secrets.

Usage:
  python scripts/backfill_social_tokens.py --dry-run
  python scripts/backfill_social_tokens.py --apply

Requires ENCRYPTION_MASTER_KEY so re-runs are stable across app restarts.
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _is_encrypted(value: str, vault) -> bool:
    if not value:
        return False
    try:
        vault.decrypt(value)
        return True
    except Exception:
        return False


def _scan_or_apply(db, SocialMediaAccount, vault, apply: bool = False) -> dict:
    counts = {
        "accounts_seen": 0,
        "access_token_plaintext": 0,
        "refresh_token_plaintext": 0,
        "access_token_already_encrypted": 0,
        "refresh_token_already_encrypted": 0,
        "accounts_updated": 0,
    }
    accounts = SocialMediaAccount.query.order_by(SocialMediaAccount.id.asc()).all()
    for account in accounts:
        counts["accounts_seen"] += 1
        changed = False

        raw_access = account._access_token
        if raw_access:
            if _is_encrypted(raw_access, vault):
                counts["access_token_already_encrypted"] += 1
            else:
                counts["access_token_plaintext"] += 1
                if apply:
                    account.access_token = raw_access
                    changed = True

        raw_refresh = account._refresh_token
        if raw_refresh:
            if _is_encrypted(raw_refresh, vault):
                counts["refresh_token_already_encrypted"] += 1
            else:
                counts["refresh_token_plaintext"] += 1
                if apply:
                    account.refresh_token = raw_refresh
                    changed = True

        if changed:
            counts["accounts_updated"] += 1

    if apply:
        db.session.commit()
    else:
        db.session.rollback()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill encrypted SocialMediaAccount token storage")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report counts only; do not write")
    mode.add_argument("--apply", action="store_true", help="encrypt plaintext token columns in-place")
    args = parser.parse_args()

    if not os.environ.get("ENCRYPTION_MASTER_KEY"):
        print("ERROR: ENCRYPTION_MASTER_KEY must be set before social token backfill.", file=sys.stderr)
        return 2

    from app import create_app
    from extensions import db
    from models import SocialMediaAccount
    from services.secret_vault import vault

    app = create_app()
    with app.app_context():
        counts = _scan_or_apply(db, SocialMediaAccount, vault, apply=args.apply)

    print("Social token backfill summary (counts only; no secret values):")
    for key in sorted(counts):
        print(f"{key}={counts[key]}")
    print("mode=apply" if args.apply else "mode=dry_run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
