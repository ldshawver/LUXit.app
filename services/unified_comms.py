"""Unified communications (M4): business identities, email history, one
consent-enforcing send path, and caller context for LUXit Phone.

Nothing here talks to a provider directly. SMS goes through the existing
idempotent conversation send (``sms_send_idempotency.send_with_idempotency``
→ ``twilio_sms.sendConversationSms``), which owns sender resolution, STOP
handling and licensing. Email sending is off unless
``LUXIT_TENANT_EMAIL_SEND_ENABLED`` is set, because today's mail transport
is the platform's own mailbox (see T-16/T-25).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from sqlalchemy import func, or_

from extensions import db

logger = logging.getLogger(__name__)

CHANNELS = ("sms", "email")
PURPOSES = ("conversational", "transactional")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_SMS_LEN = 1600


class CommsError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _norm_email(value):
    from email.utils import parseaddr
    addr = parseaddr(value or "")[1].strip().lower()
    return addr if _EMAIL_RE.match(addr) else ""


def _norm_phone(value):
    from services.phone_normalization import normalize_phone_e164
    return normalize_phone_e164(value) if value else ""


# ------------------------------------------------------------- identities

def identity_dict(identity):
    from models import BusinessIdentityNumber, TwilioPhoneNumber
    numbers = (db.session.query(TwilioPhoneNumber.id, TwilioPhoneNumber.phone_number, TwilioPhoneNumber.friendly_name)
               .join(BusinessIdentityNumber, BusinessIdentityNumber.phone_number_id == TwilioPhoneNumber.id)
               .filter(BusinessIdentityNumber.identity_id == identity.id,
                       BusinessIdentityNumber.company_id == identity.company_id).all())
    return {
        "id": identity.id, "name": identity.name, "email_from_name": identity.email_from_name,
        "email_from_address": identity.email_from_address, "signature": identity.signature,
        "is_default": identity.is_default, "is_active": identity.is_active,
        "numbers": [{"id": n[0], "phone_number": n[1], "friendly_name": n[2]} for n in numbers],
    }


def save_identity(company_id, data, identity=None):
    """Create or update an identity; ``number_ids`` replaces its numbers."""
    from models import BusinessIdentity, BusinessIdentityNumber, TwilioPhoneNumber
    name = (data.get("name") if "name" in data or identity is None else identity.name) or ""
    name = name.strip()
    if not name:
        raise CommsError("invalid", "Enter a name for this identity.")
    address = data.get("email_from_address", identity.email_from_address if identity else None)
    if address:
        address = _norm_email(address)
        if not address:
            raise CommsError("invalid", "Enter a valid sender email address.")
    if identity is None:
        identity = BusinessIdentity(company_id=company_id)
        db.session.add(identity)
    identity.name = name[:120]
    identity.email_from_address = address or None
    if "email_from_name" in data:
        identity.email_from_name = (data.get("email_from_name") or "")[:120] or None
    if "signature" in data:
        identity.signature = (data.get("signature") or "")[:4000] or None
    if "is_active" in data:
        identity.is_active = bool(data.get("is_active"))
    db.session.flush()
    if data.get("is_default"):
        BusinessIdentity.query.filter(BusinessIdentity.company_id == company_id,
                                      BusinessIdentity.id != identity.id).update({"is_default": False})
        identity.is_default = True
    elif "is_default" in data:
        identity.is_default = False
    if "number_ids" in data:
        wanted = {int(n) for n in (data.get("number_ids") or [])}
        owned = {r[0] for r in db.session.query(TwilioPhoneNumber.id).filter(
            TwilioPhoneNumber.company_id == company_id, TwilioPhoneNumber.id.in_(wanted or {0}))}
        if wanted - owned:
            raise CommsError("invalid", "A phone number doesn't belong to this workspace.")
        taken = (BusinessIdentityNumber.query
                 .filter(BusinessIdentityNumber.phone_number_id.in_(wanted or {0}),
                         BusinessIdentityNumber.identity_id != identity.id).all())
        if taken:
            raise CommsError("conflict", "A phone number already belongs to another identity.")
        BusinessIdentityNumber.query.filter(BusinessIdentityNumber.identity_id == identity.id,
                                            ~BusinessIdentityNumber.phone_number_id.in_(wanted or {0})
                                            ).delete(synchronize_session=False)
        have = {r.phone_number_id for r in BusinessIdentityNumber.query.filter_by(identity_id=identity.id)}
        for pid in wanted - have:
            db.session.add(BusinessIdentityNumber(company_id=company_id, identity_id=identity.id, phone_number_id=pid))
    db.session.flush()
    return identity


def identity_for_number(company_id, business_number):
    """The identity that owns a business number, else the workspace default."""
    from models import BusinessIdentity, BusinessIdentityNumber, TwilioPhoneNumber
    number = _norm_phone(business_number) or (business_number or "")
    if number:
        row = (db.session.query(BusinessIdentity)
               .join(BusinessIdentityNumber, BusinessIdentityNumber.identity_id == BusinessIdentity.id)
               .join(TwilioPhoneNumber, TwilioPhoneNumber.id == BusinessIdentityNumber.phone_number_id)
               .filter(BusinessIdentity.company_id == company_id, BusinessIdentity.is_active.is_(True),
                       TwilioPhoneNumber.company_id == company_id, TwilioPhoneNumber.phone_number == number)
               .first())
        if row:
            return row
    return BusinessIdentity.query.filter_by(company_id=company_id, is_default=True, is_active=True).first()


def _identity_numbers(identity):
    from models import BusinessIdentityNumber, TwilioPhoneNumber
    if identity is None:
        return None
    return {r[0] for r in db.session.query(TwilioPhoneNumber.phone_number)
            .join(BusinessIdentityNumber, BusinessIdentityNumber.phone_number_id == TwilioPhoneNumber.id)
            .filter(BusinessIdentityNumber.identity_id == identity.id)}


# ---------------------------------------------------------- contact lookup

def find_contact(company_id, *, phone=None, email=None):
    """Existing canonical contact by phone or email (never creates one)."""
    from models import Contact, ContactEmailAddress, ContactPhoneNumber
    live = (Contact.company_id == company_id, Contact.merged_into_contact_id.is_(None))
    if phone:
        number = _norm_phone(phone)
        if number:
            c = (Contact.query.filter(*live, or_(Contact.phone == number, Contact.normalized_phone == number))
                 .order_by(Contact.id.asc()).first())
            if c:
                return c
            row = (db.session.query(Contact).join(ContactPhoneNumber, ContactPhoneNumber.contact_id == Contact.id)
                   .filter(*live, ContactPhoneNumber.company_id == company_id,
                           ContactPhoneNumber.normalized_value == number).order_by(Contact.id.asc()).first())
            if row:
                return row
    if email:
        addr = _norm_email(email)
        if addr:
            c = (Contact.query.filter(*live, func.lower(Contact.email) == addr).order_by(Contact.id.asc()).first())
            if c:
                return c
            return (db.session.query(Contact).join(ContactEmailAddress, ContactEmailAddress.contact_id == Contact.id)
                    .filter(*live, ContactEmailAddress.company_id == company_id,
                            func.lower(ContactEmailAddress.normalized_value) == addr)
                    .order_by(Contact.id.asc()).first())
    return None


# ----------------------------------------------------------- email history

def record_email(company_id, *, direction, from_address, to_addresses, subject=None, body_text=None,
                 provider="manual", provider_message_id=None, mailbox=None, thread_key=None, occurred_at=None,
                 identity_id=None, user_id=None, status=None, contact_id=None):
    """Store one email in history (idempotent on provider_message_id).

    Returns ``(message, created)``. The contact is the counterpart: the
    sender for inbound mail, the first matching recipient for outbound.
    """
    from models import EmailMessage
    if direction not in ("inbound", "outbound"):
        raise CommsError("invalid", "direction must be inbound or outbound")
    if provider_message_id:
        existing = EmailMessage.query.filter_by(company_id=company_id,
                                                provider_message_id=provider_message_id[:255]).first()
        if existing:
            return existing, False
    to_list = [a for a in (_norm_email(x) for x in (to_addresses or [])) if a]
    sender = _norm_email(from_address)
    if contact_id is None:
        counterpart = [sender] if direction == "inbound" else to_list
        for addr in counterpart:
            contact = find_contact(company_id, email=addr)
            if contact:
                contact_id = contact.id
                break
    msg = EmailMessage(
        company_id=company_id, contact_id=contact_id, identity_id=identity_id, user_id=user_id,
        direction=direction, mailbox=(mailbox or "")[:255] or None, from_address=sender or None,
        to_addresses=to_list, subject=(subject or "")[:500] or None, body_text=body_text,
        thread_key=(thread_key or "")[:255] or None, provider=provider,
        provider_message_id=(provider_message_id or "")[:255] or None,
        status=status or ("received" if direction == "inbound" else "sent"),
        occurred_at=occurred_at or datetime.utcnow(),
    )
    db.session.add(msg)
    db.session.flush()
    if contact_id:
        from models import Contact
        contact = db.session.get(Contact, contact_id)
        if contact is not None:
            contact.last_activity_at = max(filter(None, [contact.last_activity_at, msg.occurred_at]))
            if direction == "outbound":
                contact.last_contacted_at = max(filter(None, [contact.last_contacted_at, msg.occurred_at]))
    return msg, True


# ------------------------------------------------------------ unified send

def _block(code, message):
    raise CommsError(code, message)


def _sms_conversation(company_id, contact, identity):
    from models import TwilioConversation
    from services.contact_timeline import identity_cluster
    ids, phones, _ = identity_cluster(company_id, contact)
    q = TwilioConversation.query.filter(
        TwilioConversation.company_id == company_id,
        or_(TwilioConversation.contact_id.in_(ids),
            TwilioConversation.from_number.in_(phones or [""])))
    numbers = _identity_numbers(identity)
    if numbers is not None:
        q = q.filter(TwilioConversation.to_number.in_(numbers or {""}))
    return q.order_by(TwilioConversation.id.desc()).first()


def send_to_contact(company_id, contact, *, channel, body, user=None, identity_id=None, subject=None,
                    purpose="conversational", idempotency_key=None):
    """Send one message to a contact on the chosen channel, or refuse.

    Refusals raise ``CommsError`` with a stable ``code``: do_not_contact,
    sms_opted_out, email_opted_out, no_address, no_conversation,
    not_configured, marketing_not_allowed, invalid. Marketing never goes
    through here — campaigns own promotional consent.
    """
    from models import BusinessIdentity
    from services import contact_consent

    if contact is None or contact.company_id != company_id:
        _block("invalid", "Contact not found.")
    if channel not in CHANNELS:
        _block("invalid", "Unsupported channel.")
    if purpose not in PURPOSES:
        _block("marketing_not_allowed", "Promotional messages must be sent as a campaign.")
    body = (body or "").strip()
    if not body:
        _block("invalid", "Enter a message.")
    if getattr(contact, "do_not_contact", False):
        _block("do_not_contact", "This contact is marked do-not-contact.")
    identity = None
    if identity_id:
        identity = BusinessIdentity.query.filter_by(id=identity_id, company_id=company_id, is_active=True).first()
        if identity is None:
            _block("invalid", "Unknown identity.")

    if channel == "sms":
        if contact_consent.has_sms_opt_out(contact):
            _block("sms_opted_out", "This contact has opted out of SMS.")
        if len(body) > MAX_SMS_LEN:
            _block("invalid", "Message is too long.")
        conv = _sms_conversation(company_id, contact, identity)
        if conv is None:
            _block("no_conversation", "There's no text thread with this contact on this line yet.")
        from services.sms_send_idempotency import send_with_idempotency
        from twilio_sms import _get_twilio_account
        result = send_with_idempotency(
            company_id=company_id, user_id=getattr(user, "id", None), conversation_id=conv.id, body=body,
            twilio_account=_get_twilio_account(company_id), idempotency_key=idempotency_key,
        )
        if result.get("success"):
            contact.last_contacted_at = datetime.utcnow()
        return {"channel": "sms", "conversation_id": conv.id, **result}

    # email
    if contact_consent.has_explicit_email_opt_out(contact):
        _block("email_opted_out", "This contact has unsubscribed from email.")
    to_addr = _norm_email(contact.email)
    if not to_addr:
        _block("no_address", "This contact has no email address.")
    identity = identity or BusinessIdentity.query.filter_by(company_id=company_id, is_default=True,
                                                            is_active=True).first()
    if os.environ.get("LUXIT_TENANT_EMAIL_SEND_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        _block("not_configured", "Sending email from LUXit isn't set up for this workspace yet.")
    if identity is None or not identity.email_from_address:
        _block("not_configured", "Set a sender email address on a business identity first.")
    html = "<p>" + "</p><p>".join(_escape(p) for p in body.split("\n\n")) + "</p>"
    if identity.signature:
        html += "<p>" + _escape(identity.signature).replace("\n", "<br>") + "</p>"
    from email_service import EmailService
    ok = EmailService().send_email(to_addr, subject or f"Message from {identity.name}", html,
                                   from_email=identity.email_from_address)
    msg, _ = record_email(company_id, direction="outbound", from_address=identity.email_from_address,
                          to_addresses=[to_addr], subject=subject, body_text=body, provider="luxit",
                          identity_id=identity.id, user_id=getattr(user, "id", None),
                          status="sent" if ok else "failed", contact_id=contact.id)
    return {"channel": "email", "success": bool(ok), "email_message_id": msg.id,
            "status": "sent" if ok else "failed"}


def _escape(text):
    from markupsafe import escape
    return str(escape(text))


# ------------------------------------------------------- LUXit Phone contract

def caller_context(company_id, *, from_number, to_number=None):
    """What the phone app shows for a call: who it is, on which line, and
    what to know before answering. Never creates a contact."""
    from services.relationship_intelligence import build_briefing
    identity = identity_for_number(company_id, to_number) if to_number else None
    contact = find_contact(company_id, phone=from_number)
    out = {
        "from_number": _norm_phone(from_number) or from_number, "to_number": _norm_phone(to_number) or to_number,
        "identity": {"id": identity.id, "name": identity.name} if identity else None,
        "contact": None, "briefing": None,
    }
    if contact is None:
        return out
    briefing = build_briefing(company_id, contact.id) or {}
    out["contact"] = {"id": contact.id, "name": (briefing.get("contact") or {}).get("name"),
                      "lifecycle_stage": getattr(contact, "lifecycle_stage", None)}
    out["briefing"] = {
        "summary": briefing.get("summary"),
        "consent": briefing.get("consent"),
        "recommendations": (briefing.get("recommendations") or [])[:3],
        "open_opportunities": (briefing.get("open_opportunities") or [])[:3],
        "open_tasks": (briefing.get("open_tasks") or [])[:3],
    }
    return out
