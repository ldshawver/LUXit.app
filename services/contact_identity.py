"""Tenant-scoped SMS identity collection and confirmation state machine."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from extensions import db
from models import Contact, ContactEmailAddress, ContactSourceEvent
from services.contact_intelligence import normalize_email
from services.contact_resolver import resolve_contact_identity, safe_name

IDENTITY_PROMPT = (
    "Thanks for contacting us. To help us identify your conversation, please "
    "reply with your first and last name and your email address."
)
_YES = {"yes", "y", "confirm", "confirmed"}
_NO = {"no", "n", "incorrect", "change"}
APPROVED_CONTACT_TAG = "Approved Contact"
CONFIRMED_REPLY = "Thank you. Your identity has been confirmed."


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
    result = resolve_contact_identity(contact.company_id, contact_id=contact.id, allow_enrichment=True)
    return "ambiguous" if result.conflict_state != "none" else ("matched" if result.name_source in {"google_contacts", "ios_contacts"} else "unresolved")


def _add_tag(contact: Contact, tag: str) -> None:
    from services.contact_audience import add_contact_tag
    add_contact_tag(contact, tag)


def _identity_missing(contact: Contact) -> list[str]:
    missing = []
    if not safe_name(f"{contact.pending_first_name or contact.first_name or ''} {contact.pending_last_name or contact.last_name or ''}", contact.normalized_phone or contact.phone):
        missing.append("first and last name")
    if not _valid_email(contact.pending_email or contact.normalized_email or contact.primary_email or contact.email or ""):
        missing.append("valid email address")
    return missing


def confirm_pending_identity(contact: Contact, conversation, message_sid: str) -> dict:
    """Atomically prepare one canonical, unambiguous confirmation transition."""
    if contact.identity_status == "confirmed":
        return {"reply": None, "confirmed": True, "idempotent": True}
    first = (contact.pending_first_name or contact.first_name or "").strip()
    last = (contact.pending_last_name or contact.last_name or "").strip()
    email = _valid_email(contact.pending_email or contact.normalized_email or contact.primary_email or contact.email or "")
    display = safe_name(f"{first} {last}", contact.normalized_phone or contact.phone)
    if not first or not last or not email or not display:
        contact.identity_conflict_status = "invalid"
        contact.identity_requested_fields = _identity_missing(contact)
        return {"reply": "I could not verify the saved information. Please reply with your first and last name and a valid email address.", "review": True}
    phone_matches = Contact.query.filter_by(company_id=contact.company_id, normalized_phone=contact.normalized_phone, is_active=True).all()
    email_matches = Contact.query.filter(
        Contact.company_id == contact.company_id, Contact.is_active.is_(True), Contact.id != contact.id,
        db.or_(Contact.normalized_email == email, db.func.lower(Contact.email) == email),
    ).all()
    linked_matches = ContactEmailAddress.query.filter(ContactEmailAddress.company_id == contact.company_id, ContactEmailAddress.normalized_value == email, ContactEmailAddress.contact_id != contact.id).all()
    if len(phone_matches) != 1 or email_matches or linked_matches:
        contact.identity_conflict_status = "ambiguous"
        contact.approval_status = "review_required"
        return {"reply": "We could not safely confirm this identity automatically. Our team will review it.", "review": True}
    now = datetime.utcnow()
    consent_snapshot = (contact.sms_marketing_opt_in, contact.sms_consent_status, contact.sms_opted_out, contact.sms_opt_out_at, contact.do_not_sms, contact.do_not_market)
    contact.first_name, contact.last_name = first, last
    contact.display_name = contact.name = display
    contact.primary_email = contact.email = contact.normalized_email = email
    contact.identity_status = "confirmed"
    contact.identity_confirmed_at = contact.identity_verified_at = now
    contact.identity_confirmation_sid = contact.identity_last_response_sid = message_sid
    contact.identity_verification_source = contact.name_source = "customer_confirmed"
    contact.name_verification_level = "verified"
    contact.name_verified_at = now
    contact.name_provenance = {"source": "customer_confirmed_sms", "confidence": 100}
    contact.identity_conflict_status = "none"
    contact.identity_conflict_details = {}
    contact.approval_status, contact.approved_at = "approved", now
    contact.approval_source = "customer_confirmation"
    contact.approval_match_source = "customer_confirmed_sms"
    _add_tag(contact, APPROVED_CONTACT_TAG)
    existing = ContactEmailAddress.query.filter_by(company_id=contact.company_id, contact_id=contact.id, normalized_value=email).first()
    if not existing:
        db.session.add(ContactEmailAddress(company_id=contact.company_id, contact_id=contact.id, original_value=email,
            normalized_value=email, is_primary=True, verification_status="confirmed", source="customer_confirmed"))
    db.session.add(ContactSourceEvent(company_id=contact.company_id, contact_id=contact.id, source="customer_confirmed",
        source_detail="SMS identity confirmation", event_type="identity_confirmed", event_metadata={"provider": "twilio", "message_sid": message_sid}))
    conversation.contact_name, conversation.contact_source = display, "customer_confirmed"
    contact.pending_first_name = contact.pending_last_name = contact.pending_email = None
    contact.identity_requested_fields, contact.identity_request_state = [], {}
    assert consent_snapshot == (contact.sms_marketing_opt_in, contact.sms_consent_status, contact.sms_opted_out, contact.sms_opt_out_at, contact.do_not_sms, contact.do_not_market)
    return {"reply": CONFIRMED_REPLY, "confirmed": True}


def process_identity_message(contact: Contact, conversation, body: str, message_sid: str) -> dict:
    """Advance identity state; returns a reply body or indicates no identity reply."""
    keyword = (body or "").strip().casefold()
    if contact.identity_status == "awaiting_confirmation":
        if keyword in _YES:
            return confirm_pending_identity(contact, conversation, message_sid)
        if keyword in _NO:
            contact.pending_first_name = contact.pending_last_name = contact.pending_email = None
            contact.identity_status = "pending_identity"
            contact.identity_requested_fields = ["first_name", "last_name", "email"]
            return {"reply": "No problem. Please reply with your first and last name and a valid email address."}
        return {"reply": "Please reply YES to confirm or NO to correct your contact information."}

    if contact.identity_status in {"confirmed"} or contact.google_match_status == "matched":
        return {"reply": None}
    first, last, email, _ = extract_identity(body)
    if first: contact.pending_first_name = first
    if last: contact.pending_last_name = last
    if email: contact.pending_email = email
    missing = _identity_missing(contact)
    if not missing:
        contact.identity_status = "awaiting_confirmation"
        return {"reply": f"Is this correct? {contact.pending_first_name or contact.first_name} {contact.pending_last_name or contact.last_name}, {contact.pending_email or contact.email}. Reply YES to confirm or NO if incorrect."}
    contact.identity_requested_fields = missing
    return {"reply": None, "missing": missing}


def should_request_identity(contact: Contact, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    cooldown = timedelta(hours=int(os.getenv("IDENTITY_REQUEST_COOLDOWN_HOURS", "24")))
    limit = int(os.getenv("IDENTITY_REQUEST_ATTEMPT_LIMIT", "3"))
    last_request = getattr(contact, "identity_fields_requested_at", None) or contact.identity_requested_at
    return (
        contact.identity_status not in {"confirmed", "awaiting_confirmation", "declined"}
        and contact.google_match_status != "matched"
        and (contact.identity_request_count or 0) < limit
        and (not last_request or last_request <= now - cooldown)
        and not contact.sms_opted_out and not contact.do_not_sms and not contact.do_not_contact
    )
