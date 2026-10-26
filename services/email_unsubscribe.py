"""Signed, non-expiring email unsubscribe links (CAN-SPAM opt-out mechanism).

The link carries a capability token bound to one contact in one tenant; it must
keep working for at least 30 days after a send, so it has no expiry. Applying
it goes through the canonical apply_email_opt_out() so every send gate and badge
sees the same evidence.
"""
from __future__ import annotations

import os
from datetime import datetime

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from extensions import db
from services.contact_consent import apply_email_opt_out, has_explicit_email_opt_out

_SALT = "luxit-email-unsubscribe-v1"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.secret_key, salt=_SALT)


def make_token(contact) -> str:
    return _serializer().dumps({"c": int(contact.id), "co": int(contact.company_id or 0)})


def unsubscribe_url(contact) -> str:
    base = (os.environ.get("PUBLIC_BASE_URL") or "https://luxit.app").rstrip("/")
    return f"{base}/email/unsubscribe/{make_token(contact)}"


def resolve_token(token: str):
    """Return the contact the token names, or None if forged/unknown/moved."""
    from models import Contact
    try:
        data = _serializer().loads(token)
    except BadSignature:
        return None
    contact = db.session.get(Contact, data.get("c"))
    if contact is None or int(contact.company_id or 0) != int(data.get("co") or -1):
        return None
    return contact


def apply_unsubscribe(contact, *, source: str, request_ip: str | None = None) -> bool:
    """Opt the contact out of email. Returns False if they already were."""
    from models import MarketingAuditLog
    already = has_explicit_email_opt_out(contact)
    if not already:
        apply_email_opt_out(contact)
        contact.marketing_preferences_source = source
        contact.marketing_preferences_updated_at = datetime.utcnow()
        db.session.add(MarketingAuditLog(
            company_id=contact.company_id,
            entity_type="contact",
            entity_id=contact.id,
            action="email_unsubscribed_via_link",
            details={"source": source, "ip": request_ip},
        ))
        db.session.commit()
    return not already
