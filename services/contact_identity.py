"""Tenant-scoped SMS identity collection and confirmation state machine."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from extensions import db
from models import Contact, ContactEmailAddress, ContactSourceEvent, GoogleContactLookup

IDENTITY_PROMPT = (
    "Thanks for contacting us. To help us identify your conversation, please "
    "reply with your first and last name and your email address."
)
_YES = {"yes", "y", "confirm", "confirmed"}
_NO = {"no", "n", "correct", "change"}


def _valid_email(value: str) -> str | None:
    try:
        return validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def extract_identity(body: str) -> tuple[str | None, str | None, str | None, list[str]]:
    """Extract an email first, then conservatively accept two human name tokens."""
    email_match = re.search(r"[^\s,;<>]+@[^\s,;<>]+", body or "")
    email = _valid_email(email_match.group(0).rstrip(".!?")) if email_match else None
    remainder = (body or "")
    if email_match:
        remainder = remainder[:email_match.start()] + " " + remainder[email_match.end():]
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]{0,79}", remainder)
    first = tokens[0].title() if len(tokens) >= 2 else None
    last = " ".join(tokens[1:]).title() if len(tokens) >= 2 else None
    missing = []
    if not first or not last:
        missing.append("first and last name")
    if not email:
        missing.append("valid email address")
    return first, last, email, missing


def apply_cached_google_match(contact: Contact) -> str:
    """Apply only one unambiguous lookup belonging to this contact's tenant."""
    if not contact.normalized_phone:
        return "unresolved"
    rows = GoogleContactLookup.query.filter_by(
        company_id=contact.company_id, normalized_phone=contact.normalized_phone
    ).all()
    candidates = {row.resource_id: row for row in rows if not row.is_ambiguous and row.resource_id}
    if any(row.is_ambiguous for row in rows) or len(candidates) > 1:
        contact.google_match_status = "ambiguous"
        contact.identity_status = "ambiguous"
        return "ambiguous"
    if len(candidates) != 1:
        return "unresolved"
    row = next(iter(candidates.values()))
    if row.display_name and contact.name_source not in {"customer_confirmed", "user", "manual"}:
        contact.display_name = row.display_name
        contact.name = contact.name or row.display_name
        parts = row.display_name.split(None, 1)
        contact.first_name = contact.first_name or parts[0]
        contact.last_name = contact.last_name or (parts[1] if len(parts) > 1 else None)
    contact.google_match_status = "matched"
    contact.google_contact_resource_id = row.resource_id
    contact.google_contact_etag = row.etag
    contact.google_matched_at = datetime.utcnow()
    contact.name_source = "google"
    contact.identity_status = "confirmed"
    return "matched"


def process_identity_message(contact: Contact, conversation, body: str, message_sid: str) -> dict:
    """Advance identity state; returns a reply body or indicates no identity reply."""
    keyword = (body or "").strip().casefold()
    if contact.identity_status == "awaiting_confirmation":
        if keyword in _YES:
            contact.first_name = contact.pending_first_name
            contact.last_name = contact.pending_last_name
            contact.display_name = f"{contact.first_name} {contact.last_name}".strip()
            contact.name = contact.display_name
            contact.primary_email = contact.pending_email
            contact.email = contact.pending_email
            contact.normalized_email = contact.pending_email
            contact.identity_status = "confirmed"
            contact.name_source = "customer_confirmed"
            contact.identity_confirmed_at = datetime.utcnow()
            contact.identity_confirmation_sid = message_sid
            existing = ContactEmailAddress.query.filter_by(
                company_id=contact.company_id, contact_id=contact.id,
                normalized_value=contact.pending_email,
            ).first()
            if not existing:
                db.session.add(ContactEmailAddress(
                    company_id=contact.company_id, contact_id=contact.id,
                    original_value=contact.pending_email, normalized_value=contact.pending_email,
                    is_primary=True, verification_status="confirmed", source="customer_confirmed",
                ))
            db.session.add(ContactSourceEvent(
                company_id=contact.company_id, contact_id=contact.id,
                source="customer_confirmed", source_detail="SMS identity confirmation",
                event_type="identity_confirmed", event_metadata={"provider": "twilio"},
            ))
            conversation.contact_name = contact.display_name
            conversation.contact_source = "customer_confirmed"
            contact.pending_first_name = contact.pending_last_name = contact.pending_email = None
            return {"reply": "Thank you. Your contact information is confirmed.", "confirmed": True}
        if keyword in _NO:
            contact.pending_first_name = contact.pending_last_name = contact.pending_email = None
            contact.identity_status = "pending_identity"
            return {"reply": "No problem. Please reply with your first and last name and a valid email address."}
        return {"reply": "Please reply YES to confirm or NO to correct your contact information."}

    if contact.identity_status in {"confirmed"} or contact.google_match_status == "matched":
        return {"reply": None}
    first, last, email, missing = extract_identity(body)
    if not missing:
        contact.pending_first_name, contact.pending_last_name, contact.pending_email = first, last, email
        contact.identity_status = "awaiting_confirmation"
        return {"reply": f"Please confirm: {first} {last}, {email}. Reply YES to confirm or NO to correct it."}
    return {"reply": None, "missing": missing}


def should_request_identity(contact: Contact, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    cooldown = timedelta(hours=int(os.getenv("IDENTITY_REQUEST_COOLDOWN_HOURS", "24")))
    limit = int(os.getenv("IDENTITY_REQUEST_ATTEMPT_LIMIT", "3"))
    return (
        contact.identity_status not in {"confirmed", "awaiting_confirmation", "declined"}
        and contact.google_match_status != "matched"
        and (contact.identity_request_count or 0) < limit
        and (not contact.identity_requested_at or contact.identity_requested_at <= now - cooldown)
        and not contact.sms_opted_out and not contact.do_not_sms and not contact.do_not_contact
    )
