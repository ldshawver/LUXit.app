#!/usr/bin/env python3
"""Idempotent backfill of the canonical My Order Customer segment for every
tenant (or one, with --company).

Membership only. Never writes consent / STOP / suppression / opt-in-out / DNC.
Safe to rerun: a second run reports 0 additions and 0 removals.

    python scripts/backfill_my_order_segment.py            # all companies, apply
    python scripts/backfill_my_order_segment.py --dry-run  # preview only
    python scripts/backfill_my_order_segment.py --company 1
"""
import argparse
import sys

from app import create_app
from extensions import db
from models import Company
from services.crm_automation import backfill_apply, backfill_preview


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", type=int, help="single company id")
    ap.add_argument("--dry-run", action="store_true", help="preview, write nothing")
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        ids = [args.company] if args.company else [c.id for c in Company.query.order_by(Company.id).all()]
        totals = {"companies": 0, "additions": 0, "removals": 0, "members": 0, "skipped": 0}
        for cid in ids:
            try:
                if args.dry_run:
                    p = backfill_preview(cid)
                    add, rem, mem, skip = p.get("proposed_inserts", 0), 0, p.get("already_in_segment", 0), None
                else:
                    r = backfill_apply(cid)
                    add, rem, mem, skip = r["additions"], r["removals"], r["members"], r.get("skipped")
            except Exception as exc:  # noqa: BLE001 - report, keep going
                print(f"company {cid}: ERROR {type(exc).__name__}: {exc}")
                continue
            totals["companies"] += 1
            totals["additions"] += add
            totals["removals"] += rem
            totals["members"] += mem
            totals["skipped"] += 1 if skip else 0
            print(f"company {cid}: +{add} -{rem} members={mem}" + (f" skipped={skip}" if skip else ""))
        if not args.dry_run:
            db.session.commit()
        print(f"\nTOTAL  companies={totals['companies']}  additions={totals['additions']}  "
              f"removals={totals['removals']}  members={totals['members']}  skipped={totals['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
