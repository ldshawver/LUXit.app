"""One tenant-scoped canonical contact and safe display-name resolver."""
from __future__ import annotations

import re
from dataclasses import dataclass
from sqlalchemy import func, or_

from extensions import db
from models import Contact, ContactEmailAddress, GoogleContactLookup
from services.phone_normalization import normalize_phone_e164

PLACEHOLDERS = {"pending identity", "unknown", "unknown contact", "n/a", "na", "none", "caller", "new contact", "new caller", "name needed"}
SOURCE_RANK = {"customer_confirmed": 600, "pwa_verified": 600, "manual": 500, "user": 500,
               "google_contacts": 400, "google": 400, "ios_contacts": 300, "icloud_import": 300,
               "trusted_import": 200, "canonical": 100}


def safe_name(value: str | None, phone: str | None = None) -> str | None:
    value = " ".join(str(value or "").split()).strip()
    if not value or value.casefold() in PLACEHOLDERS or "@" in value:
        return None
    if not re.search(r"[A-Za-z]", value):
        return None
    digits = re.sub(r"\D", "", value)
    phone_digits = re.sub(r"\D", "", phone or "")
    if (digits and len(digits) >= 7 and not re.search(r"[A-Za-z]", value)) or (phone_digits and digits == phone_digits):
        return None
    return value


def mask_phone(phone: str | None) -> str | None:
    normalized = normalize_phone_e164(phone)
    return f"••• ••• {normalized[-4:]}" if normalized else None


@dataclass(frozen=True)
class ContactResolution:
    canonical_contact_id: int | None
    first_name: str | None
    last_name: str | None
    safe_display_name: str
    masked_phone: str | None
    name_source: str | None
    verification_level: str
    identity_status: str
    conflict_state: str

    def as_dict(self):
        return self.__dict__.copy()


def _contact_name(contact: Contact) -> str | None:
    return safe_name(contact.display_name, contact.normalized_phone or contact.phone) or safe_name(contact.name, contact.normalized_phone or contact.phone) or safe_name(f"{contact.first_name or ''} {contact.last_name or ''}", contact.normalized_phone or contact.phone)


def resolve_contact_identity(company_id: int, *, phone=None, email=None, contact_id=None, allow_enrichment=False) -> ContactResolution:
    q = Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True))
    candidates = []
    if contact_id is not None:
        candidates = q.filter(Contact.id == contact_id).all()
    norm_phone = normalize_phone_e164(phone)
    if not candidates and norm_phone:
        candidates = q.filter(Contact.normalized_phone == norm_phone).all()
        if not candidates:
            candidates = [row for row in q.all() if normalize_phone_e164(row.phone) == norm_phone]
            for row in candidates:
                row.normalized_phone = norm_phone
    norm_email = (email or "").strip().casefold()
    if not candidates and norm_email:
        candidates = q.filter(or_(Contact.normalized_email == norm_email, func.lower(Contact.email) == norm_email)).all()
        linked_ids = [r.contact_id for r in ContactEmailAddress.query.filter_by(company_id=company_id, normalized_value=norm_email).all()]
        if linked_ids:
            candidates += [c for c in q.filter(Contact.id.in_(linked_ids)).all() if c not in candidates]
    if len(candidates) > 1:
        return ContactResolution(None, None, None, "Name needed", mask_phone(norm_phone or phone), None, "unverified", "conflict", "multiple_contacts")
    contact = candidates[0] if candidates else None
    if not contact:
        return ContactResolution(None, None, None, "Name needed", mask_phone(phone), None, "unverified", "name_needed", "none")

    current = _contact_name(contact)
    source = contact.name_source or "canonical"
    if source == "user" and not contact.name_verified_at and contact.source in {"google", "google_contacts"}:
        source = "google_contacts"
    evidence = []
    if current:
        evidence.append((SOURCE_RANK.get(source, 100), current, source))
    lookups = GoogleContactLookup.query.filter_by(company_id=company_id, normalized_phone=contact.normalized_phone).all() if contact.normalized_phone else []
    google_names = {safe_name(r.display_name, contact.normalized_phone) for r in lookups if not r.is_ambiguous}
    google_names.discard(None)
    if any(r.is_ambiguous for r in lookups) or len(google_names) > 1:
        contact.google_match_status = "ambiguous"
        contact.identity_conflict_status = "ambiguous"
        return ContactResolution(contact.id, contact.first_name, contact.last_name, current or "Name needed", mask_phone(contact.normalized_phone or contact.phone), source if current else None, contact.name_verification_level or "unverified", contact.identity_status, "google_ambiguous")
    if google_names:
        evidence.append((400, next(iter(google_names)), "google_contacts"))
    ios = q.filter(Contact.normalized_phone == contact.normalized_phone, Contact.source_provider.in_(["ios_contacts", "icloud_import"])).all() if contact.normalized_phone else []
    ios_names = {_contact_name(row) for row in ios}; ios_names.discard(None)
    if len(ios_names) > 1:
        contact.identity_conflict_status = "ambiguous"
        return ContactResolution(contact.id, contact.first_name, contact.last_name, current or "Name needed", mask_phone(contact.normalized_phone or contact.phone), source if current else None, contact.name_verification_level or "unverified", contact.identity_status, "ios_ambiguous")
    if ios_names:
        evidence.append((300, next(iter(ios_names)), "ios_contacts"))
    evidence.sort(key=lambda row: row[0], reverse=True)
    best = evidence[0] if evidence else None
    if best and allow_enrichment and (not current or best[0] > SOURCE_RANK.get(source, 100)):
        contact.display_name = contact.name = best[1]
        parts = best[1].split(None, 1)
        contact.first_name, contact.last_name = parts[0], parts[1] if len(parts) > 1 else contact.last_name
        contact.name_source = best[2]
        contact.name_verification_level = "trusted"
        if contact.identity_status == "pending_identity":
            contact.identity_status = "confirmed"
        current, source = best[1], best[2]
    return ContactResolution(contact.id, contact.first_name, contact.last_name, current or "Name needed", mask_phone(contact.normalized_phone or contact.phone), source if current else None, contact.name_verification_level or "unverified", contact.identity_status, contact.identity_conflict_status or "none")


def resolve_pending_contacts(company_id: int) -> dict[str, int]:
    """Safe tenant batch used after sync/import and by an external scheduled worker."""
    counts = {"examined": 0, "updated": 0, "conflicted": 0, "name_needed": 0}
    rows = Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True)).all()
    for contact in rows:
        counts["examined"] += 1
        before = _contact_name(contact)
        result = resolve_contact_identity(company_id, contact_id=contact.id, allow_enrichment=True)
        if result.conflict_state != "none": counts["conflicted"] += 1
        elif result.safe_display_name == "Name needed": counts["name_needed"] += 1
        elif not before: counts["updated"] += 1
    db.session.flush()
    return counts
