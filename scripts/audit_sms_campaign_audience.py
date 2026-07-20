#!/usr/bin/env python3
"""Read-only SMS campaign audience audit. This command never calls Twilio."""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app
from extensions import db
from models import SMSCampaign
from services.contact_audience import canonical_tag_ids, resolve_sms_campaign_recipients

LABELS = [
    ("matching_contacts", "Matching contacts"),
    ("contacts_with_phone", "Contacts with phone"),
    ("unique_phone_numbers", "Unique normalized phone numbers"),
    ("eligible_recipients", "Eligible recipients"),
    ("missing_phone_numbers", "Missing phone"),
    ("invalid_phone_numbers", "Invalid phone"),
    ("duplicate_phone_numbers", "Duplicate phone"),
    ("opted_out_contacts", "Opted out"),
    ("missing_sms_consent", "Missing consent"),
    ("archived_or_suppressed", "Archived or suppressed"),
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", required=True, type=int)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sample-hashes", action="store_true")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        try:
            tag_ids = canonical_tag_ids(args.company_id, tag_names=[args.tag])
        except ValueError as exc:
            parser.error(f"{exc} Run the SMS audience migration before auditing legacy tag text.")
        campaign = SMSCampaign(company_id=args.company_id, segment=args.tag,
                               selected_tag_ids=tag_ids, audience_filter={"selected_tag_ids": tag_ids})
        result = resolve_sms_campaign_recipients(campaign)
        for key, label in LABELS:
            print(f"{label}: {result['counts'][key]}")
        if args.sample_hashes:
            hashes = [hashlib.sha256(phone.encode()).hexdigest()[:12] for _, phone in result["recipients"][:5]]
            print("Eligible phone SHA-256 samples:", ", ".join(hashes) or "none")
        db.session.rollback()

if __name__ == "__main__":
    main()
