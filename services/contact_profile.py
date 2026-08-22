"""Canonical tenant-scoped contact profile update service.

Single source of truth for editing a Contact's core identity fields (name,
phone, email, company, tags) from any authorized surface -- the desktop CRM
route and the PWA contact panel both call update_contact_fields() rather than
mutating Contact columns directly, so tenant authorization, normalization,
conflict detection, and identity-trust promotion stay consistent everywhere
instead of drifting across the several independent write paths that existed
before (routes.py, contact_intelligence.py, contact_audience.py).

Contact.notes has no backing database column despite some legacy call sites
assigning to it (it's silently lost) -- "notes" in this codebase lives on the
conversation (TwilioConversation.notes), not the contact, so it is
deliberately not handled here.
"""
from __future__ import annotations

from datetime import datetime

from email_validator import EmailNotValidError, validate_email

from extensions import db
from models import Contact, ContactEmailAddress, ContactPhoneNumber
from services.phone_normalization import normalize_phone
from services.contact_resolver import resolve_contact_identity

EDITABLE_FIELDS = ("first_name", "last_name", "phone", "email", "company", "tags")
# Sources that represent a deliberate, authorized, first-party identity claim
# (as opposed to an unverified SMS guess or a passive import row).
TRUSTED_EDIT_SOURCES = {"manual", "pwa_verified"}


class ContactConflictError(Exception):
    """Raised when a phone/email edit would collide with another active,
    same-tenant contact. Callers must not auto-merge or silently overwrite --
    surface this to the user and let the existing reviewed merge workflow
    handle it if they confirm the two records are the same person."""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def _normalize_email(value: str) -> str | None:
    try:
        return validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def _sync_contact_point(model, contact: Contact, company_id: int, normalized_value: str, original_value: str, source: str) -> None:
    model.query.filter_by(company_id=company_id, contact_id=contact.id, is_primary=True).update({"is_primary": False})
    existing = model.query.filter_by(company_id=company_id, contact_id=contact.id, normalized_value=normalized_value).first()
    verification_status = "verified" if source in TRUSTED_EDIT_SOURCES else "unverified"
    if existing:
        existing.original_value = original_value
        existing.is_primary = True
        existing.verification_status = verification_status
        existing.source = source
    else:
        db.session.add(model(
            company_id=company_id, contact_id=contact.id,
            original_value=original_value, normalized_value=normalized_value,
            is_primary=True, verification_status=verification_status, source=source,
        ))


def update_contact_fields(
    contact: Contact, *, company_id: int, fields: dict, source: str, actor_user_id: int | None = None,
) -> Contact:
    """Apply an authorized, partial update to a contact's core fields.

    `fields` may contain any subset of EDITABLE_FIELDS; only keys present are
    touched. Everything else on the contact -- consent/opt-out/suppression
    flags, segments, tags (unless explicitly included), conversation
    history, source attribution, Google linkage, merge state -- is left
    exactly as it was.

    Raises:
        PermissionError: contact does not belong to company_id.
        ValueError: a submitted phone/email is not a valid, parseable value.
        ContactConflictError: the new phone/email already belongs to another
            active contact in the same tenant.
    """
    if contact.company_id != company_id:
        raise PermissionError("contact does not belong to this tenant")

    touched_identity = False
    tags_changed = False

    if "first_name" in fields:
        contact.first_name = (fields["first_name"] or "").strip() or None
        touched_identity = True
    if "last_name" in fields:
        contact.last_name = (fields["last_name"] or "").strip() or None
        touched_identity = True
    if "company" in fields:
        contact.company = (fields["company"] or "").strip() or None
    if "tags" in fields:
        contact.tags = (fields["tags"] or "").strip() or None
        tags_changed = True

    if "phone" in fields:
        raw = (fields["phone"] or "").strip()
        if raw:
            result = normalize_phone(raw)
            if not result.is_valid or not result.normalized:
                raise ValueError(f"'{raw}' is not a valid phone number")
            conflict = Contact.query.filter(
                Contact.company_id == company_id,
                Contact.normalized_phone == result.normalized,
                Contact.id != contact.id,
                Contact.is_active.is_(True),
                Contact.merged_into_contact_id.is_(None),
            ).first()
            if conflict:
                raise ContactConflictError("phone", "This phone number is already associated with another contact.")
            contact.phone = contact.primary_phone = raw
            contact.normalized_phone = result.normalized
            _sync_contact_point(ContactPhoneNumber, contact, company_id, result.normalized, raw, source)
        else:
            contact.phone = contact.primary_phone = contact.normalized_phone = None
        touched_identity = True

    if "email" in fields:
        raw = (fields["email"] or "").strip()
        if raw:
            normalized = _normalize_email(raw)
            if not normalized:
                raise ValueError(f"'{raw}' is not a valid email address")
            conflict = Contact.query.filter(
                Contact.company_id == company_id,
                Contact.normalized_email == normalized,
                Contact.id != contact.id,
                Contact.is_active.is_(True),
                Contact.merged_into_contact_id.is_(None),
            ).first()
            if conflict:
                raise ContactConflictError("email", "This email address is already associated with another contact.")
            contact.email = contact.primary_email = raw
            contact.normalized_email = normalized
            _sync_contact_point(ContactEmailAddress, contact, company_id, normalized, raw, source)
        else:
            contact.email = contact.primary_email = contact.normalized_email = None
        touched_identity = True

    if touched_identity and source in TRUSTED_EDIT_SOURCES:
        # A deliberate authorized edit is first-party evidence -- record its
        # provenance so the identity resolver (services/contact_resolver.py)
        # ranks it correctly rather than leaving it unattributed.
        contact.name_source = source
        contact.name_provenance = {"source": source, "confidence": 100, "actor_user_id": actor_user_id}

    contact.updated_at = datetime.utcnow()
    db.session.flush()

    if tags_changed:
        from services.crm_automation import synchronize_contact_tags
        from hashlib import sha256
        tag_state = sha256((contact.tags or "").encode("utf-8")).hexdigest()
        synchronize_contact_tags(contact, source=source, event_key_prefix=f"profile:{contact.id}:{tag_state}")

    if touched_identity:
        # Re-resolve so a trusted edit that establishes first_name+phone can
        # reach at least minimum_established without waiting for another
        # inbound SMS; never forces "confirmed".
        resolve_contact_identity(company_id, contact_id=contact.id, allow_enrichment=True)

    return contact


def serialize_contact(contact: Contact) -> dict:
    return {
        "id": contact.id,
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "full_name": f"{contact.first_name or ''} {contact.last_name or ''}".strip(),
        "phone": contact.phone or "",
        "email": contact.email or "",
        "company": contact.company or "",
        "tags": contact.tags or "",
        "identity_status": contact.identity_status,
    }
