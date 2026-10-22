#!/usr/bin/env python3
"""Record each number's US carrier messaging registration (A2P 10DLC / toll-free).

READ-ONLY against Twilio: lists Messaging Services, their phone numbers and
US A2P campaign registration, and toll-free verifications. Writes only the
LUXit table ``phone_number_messaging_registration`` (one row per number).

Status written:
  registered          number is in a Messaging Service with an approved/verified US A2P campaign
  pending             number is in a Messaging Service whose campaign is still in review
  not_registered      10DLC number in no Messaging Service with a campaign
  toll_free_verified  toll-free number with an approved verification
  toll_free_pending   toll-free number whose verification is pending / not submitted
  unknown             Twilio could not be queried for this number

The LUX Connect app refuses application-originated texts only for
not_registered / pending / toll_free_pending; "unknown" never blocks.

Usage:
  DATABASE_URL=... python3 scripts/check_messaging_registration.py [--company-id N] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TOLL_FREE_PREFIXES = ("+1800", "+1833", "+1844", "+1855", "+1866", "+1877", "+1888")
APPROVED_CAMPAIGN = {"VERIFIED", "APPROVED", "SUCCESS"}


def classify_numbers(client) -> dict[str, tuple[str, str | None, str]]:
    """Return {E.164: (status, messaging_service_sid, detail)} from Twilio (read-only)."""
    out: dict[str, tuple[str, str | None, str]] = {}
    for svc in client.messaging.v1.services.list(limit=200):
        campaigns = []
        try:
            campaigns = client.messaging.v1.services(svc.sid).us_app_to_person.list(limit=20)
        except Exception as exc:  # service without A2P access
            campaigns = []
            detail_err = str(exc)[:120]
        else:
            detail_err = ""
        states = {str(getattr(c, "campaign_status", "") or "").upper() for c in campaigns}
        if states & APPROVED_CAMPAIGN:
            svc_status, detail = "registered", f"campaign {'/'.join(sorted(states))}"
        elif states:
            svc_status, detail = "pending", f"campaign {'/'.join(sorted(states))}"
        else:
            svc_status, detail = "not_registered", detail_err or "no US A2P campaign on service"
        for pn in client.messaging.v1.services(svc.sid).phone_numbers.list(limit=400):
            if pn.phone_number.startswith(TOLL_FREE_PREFIXES):
                # A 10DLC campaign does not register a toll-free number; only a
                # toll-free verification does (handled below).
                continue
            prev = out.get(pn.phone_number)
            rank = {"registered": 3, "pending": 2, "not_registered": 1}
            if not prev or rank.get(svc_status, 0) > rank.get(prev[0], 0):
                out[pn.phone_number] = (svc_status, svc.sid, f"{svc.friendly_name}: {detail}"[:300])
    try:
        for v in client.messaging.v1.tollfree_verifications.list(limit=200):
            number = getattr(v, "phone_number", None) or getattr(v, "tollfree_phone_number_sid", None)
            if not number or not str(number).startswith("+"):
                continue
            st = str(getattr(v, "status", "") or "").upper()
            out[number] = ("toll_free_verified" if st == "TWILIO_APPROVED" else "toll_free_pending", None,
                           f"toll-free verification {st}"[:300])
    except Exception:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--company-id", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from twilio.rest import Client
    from app import create_app
    from extensions import db
    from models import PhoneNumberMessagingRegistration, TwilioPhoneNumber

    sid, token = os.environ.get("TWILIO_ACCOUNT_SID"), os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        print("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN required", file=sys.stderr)
        return 2
    found = classify_numbers(Client(sid, token))

    app = create_app()
    with app.app_context():
        q = TwilioPhoneNumber.query.filter_by(is_active=True)
        if args.company_id:
            q = q.filter_by(company_id=args.company_id)
        for pn in q.order_by(TwilioPhoneNumber.id).all():
            status, msid, detail = found.get(pn.phone_number, (None, None, ""))
            if status is None:
                status = "toll_free_pending" if pn.phone_number.startswith(TOLL_FREE_PREFIXES) else "not_registered"
                detail = "not in any Messaging Service" if status == "not_registered" else "no toll-free verification found"
            print(f"company={pn.company_id} number={pn.phone_number} status={status} service={msid or '-'} {detail}")
            if args.dry_run:
                continue
            row = PhoneNumberMessagingRegistration.query.filter_by(phone_number_id=pn.id).first()
            if not row:
                row = PhoneNumberMessagingRegistration(company_id=pn.company_id, phone_number_id=pn.id)
                db.session.add(row)
            row.status, row.messaging_service_sid, row.detail = status, msid, detail
            row.checked_at = datetime.utcnow()
        if not args.dry_run:
            db.session.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
