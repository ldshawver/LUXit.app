"""Promotional SMS opt-in workflow.

Three concerns, kept strictly separate:

  1. AUDIENCE  - "Needs Promotional Opt-In": a derived *status*, never consent.
     A customer with conversational evidence who is not already promotional-
     eligible, not STOP, not suppressed. Listing someone here changes nothing.

  2. SOLICITATION - an operator-approved request asking that customer to opt in.
     ``PromotionalOptInSolicitation`` in state ``pending`` is the *context* a
     later inbound YES needs. Creating one never mutates consent columns.
     This module never bulk-sends the solicitation.

  3. CONTEXTUAL CONSENT - an inbound YES that arrives while a pending
     solicitation exists for the same tenant / canonical phone / business
     number. Only then is promotional consent granted, a
     ``PromotionalConsentEvent`` written (idempotent on the inbound MessageSid),
     and the contact's opt-in columns set so the existing campaign resolver
     includes them. A generic YES with no pending solicitation grants nothing
     here. STOP always wins and closes any pending solicitation.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    Contact, MarketingAuditLog, PromotionalConsentEvent,
    PromotionalOptInSolicitation,
)
from services.sms_consent_purpose import (
    conversational_evidence_phone_digits, has_conversational_evidence,
    has_promotional_optin, is_suppressed,
)

PROMO_OPTIN_CONFIRMATION = (
    "MyOrder.fun: You're opted in to recurring texts about new menu items, "
    "specials and promotions. Msg frequency varies. Msg & data rates may apply. "
    "Reply STOP to opt out."
)
SUGGESTED_SOLICITATION_COPY = (
    "MyOrder.fun: Want recurring texts about new menu items, specials and "
    "promotions? Reply YES to opt in. Message frequency varies. Msg & data "
    "rates may apply. Reply STOP to opt out."
)

_YES_KEYWORDS = {"yes", "y", "yeah", "yep", "yes please", "opt in", "optin"}


def _now() -> datetime:
    return datetime.utcnow()


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _phone_key(value) -> str | None:
    d = _digits(value)
    return d[-10:] if len(d) >= 10 else None


def normalize_yes_keyword(body: str) -> str | None:
    text = " ".join(str(body or "").strip().lower().split())
    text = text.strip(".!?,")
    return text if text in _YES_KEYWORDS else None


def _canonical_phone(contact: Contact) -> str | None:
    from services.phone_normalization import normalize_phone_e164
    return normalize_phone_e164(contact.normalized_phone or contact.phone) or None


def _audit(company_id, user_id, action, entity_id, details):
    db.session.add(MarketingAuditLog(
        company_id=company_id, created_by_user_id=user_id, action=action,
        entity_type="promotional_optin", entity_id=entity_id, details=details,
    ))


# ---------------------------------------------------------------------------
# 1. Audience derivation  (status only — never writes consent)
# ---------------------------------------------------------------------------

def _segment_contacts(company_id: int, segment_id: int | None) -> list[Contact]:
    from services.contact_audience import resolve_segment_contacts
    if segment_id:
        return resolve_segment_contacts(
            company_id, None, {"selected_tag_ids": [segment_id]}
        )
    return resolve_segment_contacts(company_id, None, {})


def classify_audience(company_id: int, segment_id: int | None = None) -> dict:
    """Aggregate the customer segment into the operator buckets. Read-only."""
    contacts = _segment_contacts(company_id, segment_id)
    evidence = conversational_evidence_phone_digits(company_id)
    pending_ids = pending_contact_ids(company_id)

    buckets = {
        "my_order_customers": 0,
        "already_promotional": 0,
        "needs_promotional_optin": 0,
        "stop_suppressed": 0,
        "no_conversational_evidence": 0,
        "solicitation_pending": 0,
    }
    audience: list[dict] = []
    seen_phone: set[str] = set()
    for contact in contacts:
        if getattr(contact, "merged_into_contact_id", None):
            continue
        buckets["my_order_customers"] += 1
        phone = _canonical_phone(contact)
        supp = is_suppressed(contact)
        promo = has_promotional_optin(contact)
        conv = bool(phone) and has_conversational_evidence(
            company_id, contact, evidence_digits=evidence
        )
        if supp:
            buckets["stop_suppressed"] += 1
            continue
        if promo:
            buckets["already_promotional"] += 1
            continue
        if not phone:
            buckets["no_conversational_evidence"] += 1
            continue
        if not conv:
            buckets["no_conversational_evidence"] += 1
            continue
        key = _phone_key(phone)
        if key and key in seen_phone:
            continue
        if key:
            seen_phone.add(key)
        buckets["needs_promotional_optin"] += 1
        if contact.id in pending_ids:
            buckets["solicitation_pending"] += 1
        audience.append({
            "contact_id": contact.id,
            "name": contact.display_name or contact.name or "",
            "canonical_phone": phone,
            "solicitation_pending": contact.id in pending_ids,
        })
    return {"counts": buckets, "audience": audience}


def needs_promotional_optin(company_id: int, segment_id: int | None = None) -> list[dict]:
    return classify_audience(company_id, segment_id)["audience"]


# ---------------------------------------------------------------------------
# 2. Solicitation  (operator-approved — never writes consent, never sends here)
# ---------------------------------------------------------------------------

def pending_contact_ids(company_id: int) -> set[int]:
    rows = (
        db.session.query(PromotionalOptInSolicitation.contact_id)
        .filter(
            PromotionalOptInSolicitation.company_id == company_id,
            PromotionalOptInSolicitation.status == "pending",
        )
        .all()
    )
    return {cid for (cid,) in rows}


def get_pending_solicitation(company_id: int, contact_id: int) -> PromotionalOptInSolicitation | None:
    return (
        PromotionalOptInSolicitation.query
        .filter_by(company_id=company_id, contact_id=contact_id, status="pending")
        .order_by(PromotionalOptInSolicitation.id.desc())
        .first()
    )


def create_solicitation(
    company_id: int,
    contact_id: int,
    *,
    business_phone_number: str | None = None,
    actor_user_id: int | None = None,
    body: str | None = None,
    solicitation_message_sid: str | None = None,
    source: str = "operator",
) -> dict:
    """Record an operator-approved pending solicitation. Idempotent: a second
    call while one is already pending returns the existing row and writes
    nothing new. Does NOT touch consent columns and does NOT send an SMS.
    """
    contact = Contact.query.filter_by(id=contact_id, company_id=company_id).first()
    if not contact:
        return {"ok": False, "error": "contact_not_found"}
    if is_suppressed(contact):
        return {"ok": False, "error": "contact_suppressed"}
    if has_promotional_optin(contact):
        return {"ok": False, "error": "already_promotional"}
    phone = _canonical_phone(contact)
    if not phone:
        return {"ok": False, "error": "no_canonical_phone"}

    existing = get_pending_solicitation(company_id, contact_id)
    if existing:
        return {"ok": True, "created": False, "solicitation": existing}

    from services.phone_normalization import normalize_phone_e164
    row = PromotionalOptInSolicitation(
        company_id=company_id,
        contact_id=contact_id,
        canonical_phone=phone,
        business_phone_number=normalize_phone_e164(business_phone_number) or business_phone_number,
        status="pending",
        solicitation_body=body or SUGGESTED_SOLICITATION_COPY,
        solicitation_message_sid=solicitation_message_sid,
        solicited_at=_now(),
        solicited_by_user_id=actor_user_id,
        source=source,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        # Lost a race for the partial-unique pending index.
        existing = get_pending_solicitation(company_id, contact_id)
        if existing:
            return {"ok": True, "created": False, "solicitation": existing}
        raise
    _audit(company_id, actor_user_id, "promotional_solicitation_created", row.id, {
        "contact_id": contact_id, "canonical_phone": phone,
        "business_phone_number": row.business_phone_number, "source": source,
    })
    return {"ok": True, "created": True, "solicitation": row}


def cancel_pending_for_contact(company_id: int, contact_id: int, *, reason: str = "stop") -> int:
    """Close every pending solicitation for a contact (STOP precedence, or an
    operator withdrawing the request). Returns how many were closed."""
    rows = (
        PromotionalOptInSolicitation.query
        .filter_by(company_id=company_id, contact_id=contact_id, status="pending")
        .all()
    )
    now = _now()
    for row in rows:
        row.status = "stopped" if reason == "stop" else "cancelled"
        row.closed_at = now
        row.closed_reason = reason
    if rows:
        _audit(company_id, None, "promotional_solicitation_closed", rows[0].id, {
            "contact_id": contact_id, "reason": reason, "closed": len(rows),
        })
    return len(rows)


def cancel_pending_for_phone(company_id: int, phone: str, *, reason: str = "stop") -> int:
    from services.sms_keyword_engine import find_contact
    contact = find_contact(company_id, phone)
    if not contact:
        return 0
    return cancel_pending_for_contact(company_id, contact.id, reason=reason)


# ---------------------------------------------------------------------------
# 3. Contextual YES  (the ONLY path that grants promotional consent here)
# ---------------------------------------------------------------------------

def record_contextual_yes(
    company_id: int,
    from_phone: str,
    to_business_phone: str | None,
    inbound_message_sid: str,
    *,
    keyword: str = "yes",
    received_at: datetime | None = None,
) -> dict:
    """Grant promotional consent iff a pending solicitation exists for this
    tenant + canonical phone. Idempotent on ``inbound_message_sid``.

    Returns a dict with:
      matched        - True if a pending solicitation was found (consent granted
                       or already recorded); False => caller falls through to
                       normal keyword handling.
      granted        - True if this call created the consent event.
      duplicate      - True if the inbound SID was already recorded.
      reply          - confirmation copy to send when matched.
    """
    received_at = received_at or _now()
    from services.sms_keyword_engine import find_contact
    contact = find_contact(company_id, from_phone)
    if not contact:
        return {"matched": False, "reason": "no_contact"}

    # Idempotency: the inbound webhook may be delivered more than once.
    existing_event = PromotionalConsentEvent.query.filter_by(
        inbound_message_sid=inbound_message_sid
    ).first()
    if existing_event:
        return {
            "matched": True, "granted": False, "duplicate": True,
            "solicitation_id": existing_event.solicitation_id,
            "reply": PROMO_OPTIN_CONFIRMATION,
        }

    solicitation = get_pending_solicitation(company_id, contact.id)
    if not solicitation:
        return {"matched": False, "reason": "no_pending_solicitation"}

    # STOP precedence: a suppressed / opted-out contact never gains promotional
    # eligibility from a YES. Close the stale solicitation instead.
    if is_suppressed(contact) or contact.sms_opted_out or contact.sms_opt_out_at:
        solicitation.status = "stopped"
        solicitation.closed_at = received_at
        solicitation.closed_reason = "suppressed_at_yes"
        _audit(company_id, None, "promotional_solicitation_closed", solicitation.id, {
            "contact_id": contact.id, "reason": "suppressed_at_yes",
        })
        return {"matched": True, "granted": False, "suppressed": True, "reply": None}

    from services.phone_normalization import normalize_phone_e164
    canonical = normalize_phone_e164(contact.normalized_phone or contact.phone) or solicitation.canonical_phone
    business = normalize_phone_e164(to_business_phone) or to_business_phone or solicitation.business_phone_number

    event = PromotionalConsentEvent(
        company_id=company_id,
        contact_id=contact.id,
        solicitation_id=solicitation.id,
        canonical_phone=canonical,
        business_phone_number=business,
        inbound_message_sid=inbound_message_sid,
        solicitation_message_sid=solicitation.solicitation_message_sid,
        solicited_at=solicitation.solicited_at,
        consent_purpose="promotional",
        consent_source="sms_reply_yes",
        consent_keyword=keyword,
        consented_at=received_at,
    )
    try:
        with db.session.begin_nested():
            db.session.add(event)
            db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing_event = PromotionalConsentEvent.query.filter_by(
            inbound_message_sid=inbound_message_sid
        ).first()
        return {
            "matched": True, "granted": False, "duplicate": True,
            "solicitation_id": getattr(existing_event, "solicitation_id", solicitation.id),
            "reply": PROMO_OPTIN_CONFIRMATION,
        }

    solicitation.status = "consented"
    solicitation.consent_message_sid = inbound_message_sid
    solicitation.consented_at = received_at

    # Set the contact's opt-in columns so the *existing* campaign resolver
    # includes them for promotional purpose. This is the only consent mutation
    # this module performs, and only inside a matched contextual YES.
    contact.sms_marketing_opt_in = True
    contact.sms_consent_status = "opted_in"
    contact.sms_opted_out = False
    contact.sms_opt_out_at = None
    contact.sms_marketing_opt_in_at = received_at
    contact.sms_marketing_opt_in_source = "promotional_optin_sms_yes"

    _audit(company_id, None, "promotional_consent_granted", event.id, {
        "contact_id": contact.id,
        "solicitation_id": solicitation.id,
        "canonical_phone": canonical,
        "business_phone_number": business,
        "inbound_message_sid": inbound_message_sid,
        "solicitation_message_sid": solicitation.solicitation_message_sid,
        "solicited_at": solicitation.solicited_at.isoformat() if solicitation.solicited_at else None,
        "consented_at": received_at.isoformat(),
        "consent_purpose": "promotional",
        "consent_source": "sms_reply_yes",
    })
    return {
        "matched": True, "granted": True, "duplicate": False,
        "solicitation_id": solicitation.id, "event_id": event.id,
        "reply": PROMO_OPTIN_CONFIRMATION,
    }
