"""Contact intelligence services: attribution, resolution, dedupe, Google matching, CRM helpers."""
from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parseaddr
import re

from sqlalchemy import or_, func

from extensions import db
from models import Contact, ContactEmailAddress, ContactPhoneNumber, ContactSourceEvent, ContactTask, Opportunity
from services.phone_normalization import normalize_phone

CONTROLLED_SOURCES = {
    "twilio_inbound_sms", "twilio_inbound_call", "twilio_outbound_sms", "twilio_outbound_call",
    "myorder_customer", "website_form", "manual_entry", "csv_import", "google_contacts", "api",
    "referral", "campaign", "contract", "invoice", "unknown", "legacy",
}
PLACEHOLDER_NAMES = {"unknown", "caller", "new contact", "new caller", "no name", "n/a", "na"}


def normalize_email(value: str | None) -> str:
    addr = parseaddr(value or "")[1].strip().lower()
    return addr if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr) else ""


def meaningful_name(contact: Contact | None = None, name: str | None = None) -> bool:
    raw = (name if name is not None else (getattr(contact, "name", None) or getattr(contact, "display_name", None) or "")).strip()
    if not raw or raw.lower() in PLACEHOLDER_NAMES:
        return False
    digits = re.sub(r"\D", "", raw)
    return len(digits) < 7


def apply_source_attribution(contact: Contact, source: str, *, detail: str | None = None, campaign: str | None = None,
                             url: str | None = None, referrer: str | None = None, event_type: str = "touch",
                             user_id: int | None = None, metadata: dict | None = None, at: datetime | None = None) -> ContactSourceEvent:
    now = at or datetime.utcnow()
    source = (source or "unknown").strip().lower()
    if source not in CONTROLLED_SOURCES:
        source = "api"
    if not contact.original_source:
        contact.original_source = contact.source or source
        contact.original_source_detail = contact.source_detail or detail
        contact.original_source_campaign = campaign
        contact.original_source_url = url
        contact.original_referrer = referrer
        contact.first_touch_at = contact.source_added_at or now
    contact.latest_source = source
    contact.latest_source_detail = detail
    contact.latest_source_campaign = campaign
    contact.latest_source_url = url
    contact.latest_referrer = referrer
    contact.last_touch_at = now
    contact.source = contact.source or source
    contact.source_detail = contact.source_detail or detail
    contact.source_added_at = contact.source_added_at or now
    contact.source_added_by_user_id = contact.source_added_by_user_id or user_id
    evt = ContactSourceEvent(company_id=contact.company_id, contact_id=contact.id, source=source, source_detail=detail,
                             campaign=campaign, source_url=url, referrer=referrer, event_type=event_type,
                             event_at=now, event_metadata=metadata or {}, created_by_user_id=user_id)
    db.session.add(evt)
    return evt


def sync_contact_points(contact: Contact, phone: str | None, email: str | None, source: str) -> None:
    if phone:
        res = normalize_phone(phone)
        if res.normalized:
            contact.primary_phone = contact.primary_phone or res.original
            contact.phone = contact.phone or res.original
            contact.normalized_phone = contact.normalized_phone or res.normalized
            contact.phone_extension = contact.phone_extension or res.extension
            exists = ContactPhoneNumber.query.filter_by(company_id=contact.company_id, contact_id=contact.id, normalized_value=res.normalized).first()
            if not exists:
                db.session.add(ContactPhoneNumber(company_id=contact.company_id, contact_id=contact.id, original_value=res.original, normalized_value=res.normalized, extension=res.extension, is_primary=not bool(contact.phone_numbers.count()), source=source))
    norm_email = normalize_email(email)
    if norm_email:
        contact.primary_email = contact.primary_email or email.strip()
        contact.email = contact.email or email.strip()
        contact.normalized_email = contact.normalized_email or norm_email
        exists = ContactEmailAddress.query.filter_by(company_id=contact.company_id, contact_id=contact.id, normalized_value=norm_email).first()
        if not exists:
            db.session.add(ContactEmailAddress(company_id=contact.company_id, contact_id=contact.id, original_value=email.strip(), normalized_value=norm_email, is_primary=not bool(contact.email_addresses.count()), source=source))


def resolve_contact(company_id: int, *, phone: str | None = None, email: str | None = None, proposed_name: str | None = None,
                    first_name: str | None = None, last_name: str | None = None, business_name: str | None = None,
                    source: str = "unknown", detail: str | None = None, tenant_id: int | None = None,
                    user_id: int | None = None, metadata: dict | None = None) -> Contact:
    phone_result = normalize_phone(phone)
    norm_email = normalize_email(email)
    q = Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True))
    contact = None
    if phone_result.normalized:
        contact = q.filter(Contact.normalized_phone == phone_result.normalized).first()
    if not contact and norm_email:
        contact = q.filter(or_(Contact.normalized_email == norm_email, func.lower(Contact.email) == norm_email)).first()
    if not contact:
        contact = Contact(company_id=company_id, tenant_id=tenant_id or company_id, is_active=True, status="active", created_at=datetime.utcnow(), created_by_user_id=user_id)
        db.session.add(contact); db.session.flush()
    if proposed_name and not meaningful_name(contact):
        contact.name = proposed_name.strip(); contact.display_name = proposed_name.strip(); contact.name_source = "user"
    if first_name and not contact.first_name: contact.first_name = first_name.strip()
    if last_name and not contact.last_name: contact.last_name = last_name.strip()
    if business_name and not (contact.business_name or contact.company):
        contact.business_name = business_name.strip(); contact.company = business_name.strip()
    sync_contact_points(contact, phone, email, source)
    apply_source_attribution(contact, source, detail=detail, user_id=user_id, metadata=metadata)
    contact.last_activity_at = datetime.utcnow()
    db.session.flush()
    return contact


def apply_google_name_match(contact: Contact, candidates: list[dict]) -> str:
    exact = [c for c in candidates if c.get("normalized_phone") and c.get("normalized_phone") == contact.normalized_phone]
    good = [c for c in exact if meaningful_name(name=c.get("name"))]
    if not good:
        contact.google_match_status = "not_found"; return "not_found"
    resources = {c.get("resource_id") for c in good}
    if len(good) > 1 and len(resources) > 1:
        contact.google_match_status = "ambiguous"; contact.google_match_confidence = 50; return "ambiguous"
    match = good[0]
    if not meaningful_name(contact):
        contact.name = match["name"]; contact.display_name = match["name"]; contact.name_source = "google_contacts"
    contact.google_contact_resource_id = match.get("resource_id")
    contact.google_contact_etag = match.get("etag")
    contact.google_match_status = "matched"
    contact.google_match_confidence = 100
    contact.google_matched_at = datetime.utcnow()
    return "matched"


def create_follow_up_task(company_id: int, contact_id: int, title: str, *, assigned_user_id=None, due_at=None, priority="normal") -> ContactTask:
    task = ContactTask(company_id=company_id, contact_id=contact_id, title=title, assigned_user_id=assigned_user_id, due_at=due_at, priority=priority)
    db.session.add(task); return task


def create_opportunity(company_id: int, contact_id: int, name: str, *, owner_user_id=None, stage="new_lead", estimated_value=None, probability=0) -> Opportunity:
    opp = Opportunity(company_id=company_id, contact_id=contact_id, name=name, owner_user_id=owner_user_id, stage=stage, estimated_value=estimated_value, probability=probability)
    db.session.add(opp); return opp


def cleanup_audit(company_id: int) -> dict:
    q = Contact.query.filter_by(company_id=company_id, is_active=True)
    total = q.count(); invalid = missing = 0
    phones = {}; emails = {}
    for c in q.yield_per(500):
        if c.phone and not normalize_phone(c.phone).normalized: invalid += 1
        if not meaningful_name(c): missing += 1
        if c.normalized_phone: phones.setdefault(c.normalized_phone, []).append(c.id)
        if normalize_email(c.email): emails.setdefault(normalize_email(c.email), []).append(c.id)
    exact = sum(len(v)-1 for v in list(phones.values())+list(emails.values()) if len(v)>1)
    return {"company_id": company_id, "total_contacts_scanned": total, "exact_duplicates": exact, "possible_duplicates": 0, "invalid_phone_numbers": invalid, "missing_names": missing, "proposed_merges": exact, "cross_tenant_conflicts_blocked": 0}
