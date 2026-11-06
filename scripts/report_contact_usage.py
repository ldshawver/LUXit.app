#!/usr/bin/env python3
"""Daily contact-overage report to Stripe (Billing Meter events).

For every active workspace with a Stripe customer: recount billable contacts,
store the count/overage, and send the overage as a meter event when the
subscription carries the metered contacts price. Dry run by default (counts
only, nothing sent to Stripe, nothing written).

    python scripts/report_contact_usage.py            # dry run
    python scripts/report_contact_usage.py --apply    # send + store

Install: deploy/billing/luxit-contact-usage.{service,timer} (daily 03:30 UTC).
Same-day re-runs are deduplicated by Stripe (identifier per company per day).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    from app import create_app
    from extensions import db
    from models import Company
    from services.stripe_billing import (compute_contacts_overage, count_billable_contacts,
                                         included_contacts_for_tier, report_contact_usage)

    app = create_app()
    failures = 0
    with app.app_context():
        companies = (Company.query.filter(Company.is_active.is_(True), Company.stripe_customer_id.isnot(None))
                     .order_by(Company.id).all())
        for company in companies:
            if not args.apply:
                used = count_billable_contacts(company.id)
                included = company.included_contacts or included_contacts_for_tier(
                    company.billing_tier or company.subscription_tier)
                print(json.dumps({"company_id": company.id, "dry_run": True, "contacts_used": used,
                                  "overage": compute_contacts_overage(used, included),
                                  "metered": bool(company.stripe_contact_usage_subscription_item_id)}))
                continue
            try:
                result = report_contact_usage(company)
            except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
                db.session.rollback()
                failures += 1
                result = {"error": type(exc).__name__, "detail": str(exc)[:200]}
            print(json.dumps({"company_id": company.id, **result}, default=str))
        print(json.dumps({"companies": len(companies), "failures": failures, "applied": args.apply}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
