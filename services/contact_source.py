"""Helpers for consistent contact source metadata."""

from __future__ import annotations

from datetime import datetime

CONTACT_SOURCE_NEWSLETTER = "newsletter"
CONTACT_SOURCE_UPLOAD = "upload"
CONTACT_SOURCE_FACEBOOK = "facebook"
CONTACT_SOURCE_MANUAL = "manual"
CONTACT_SOURCE_SMS_INBOUND = "sms_inbound"
CONTACT_SOURCE_API_IMPORT = "api_import"

SUPPORTED_CONTACT_SOURCES = {
    CONTACT_SOURCE_NEWSLETTER,
    CONTACT_SOURCE_UPLOAD,
    CONTACT_SOURCE_FACEBOOK,
    CONTACT_SOURCE_MANUAL,
    CONTACT_SOURCE_SMS_INBOUND,
    CONTACT_SOURCE_API_IMPORT,
}


def apply_contact_source(contact, source: str, *, detail: str | None = None, user_id: int | None = None, added_at=None):
    """Apply where/when metadata to a contact.

    `detail` is intentionally free-form so future sources can say things like
    "Subscribed to newsletter", "CSV upload", "Facebook lead ad", or
    "Manual entry by Luke" while `source` stays queryable.
    """
    normalized_source = (source or "").strip().lower() or CONTACT_SOURCE_MANUAL
    contact.source = normalized_source
    contact.source_detail = detail or normalized_source
    contact.source_added_at = added_at or datetime.utcnow()
    contact.source_added_by_user_id = user_id
    return contact
