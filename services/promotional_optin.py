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

import os
import re
import secrets
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

# --- Hosted web opt-in (customer_web_optin) --------------------------------
# The brand and the exact disclosure the customer affirms on the hosted page.
# DISCLOSURE_VERSION must change whenever WEB_OPTIN_DISCLOSURE_TEXT changes: the
# version is snapshotted on the consent link and re-checked on submission, and
# the full text is stored on every PromotionalConsentEvent as immutable proof.
WEB_OPTIN_BRAND = "MyOrder.fun Text Specials"
WEB_OPTIN_DISCLOSURE_VERSION = "2026-09-02.v1"
WEB_OPTIN_DISCLOSURE_TEXT = (
    "Yes, I want to receive recurring promotional text messages from MyOrder.fun "
    "about sales, specials, new menu items and offers. Message frequency varies. "
    "Msg & data rates may apply. Reply STOP to unsubscribe or HELP for help. "
    "Consent is not a condition of purchase."
)
_WEB_TOKEN_SALT = "promotional-web-optin.v1"

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


def _grant_promotional_consent(contact: Contact, when: datetime, source: str) -> None:
    """Set the contact's opt-in columns so the *existing* campaign resolver
    includes them for promotional purpose. This is the only consent mutation
    this module performs, and only after a channel-specific consent event has
    been written (inbound YES, or a hosted web opt-in). ``source`` records which
    channel granted it: ``promotional_optin_sms_yes`` / ``promotional_web_optin``.
    """
    contact.sms_marketing_opt_in = True
    contact.sms_consent_status = "opted_in"
    contact.sms_opted_out = False
    contact.sms_opt_out_at = None
    contact.sms_marketing_opt_in_at = when
    contact.sms_marketing_opt_in_source = source


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


def classify_audience(company_id: int, segment_id: int | None = None,
                      *, include_solicitation_status: bool = False) -> dict:
    """Aggregate the customer segment into the operator buckets. Read-only.

    ``include_solicitation_status`` adds a per-contact ``solicitation`` block to
    every audience row and a ``delivery`` sub-count to ``counts`` (how many of
    the Needs audience have been sent / delivered / replied YES etc.). It reads
    the solicitation table but never writes anything.
    """
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

    if include_solicitation_status:
        status_map = latest_solicitation_status_map(
            company_id, [row["contact_id"] for row in audience]
        )
        delivery = {
            "not_sent": 0, "sent": 0, "delivered": 0, "failed": 0,
            "blocked": 0, "replied_yes": 0, "stopped": 0,
        }
        for row in audience:
            info = status_map.get(row["contact_id"]) or {"state": "not_sent", "label": "Not sent"}
            row["solicitation"] = info
            delivery[info["state"]] = delivery.get(info["state"], 0) + 1
        buckets["delivery"] = delivery

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

    _grant_promotional_consent(contact, received_at, "promotional_optin_sms_yes")

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


# ---------------------------------------------------------------------------
# 4. Operator send flow  (dispatches the opt-in *request* SMS — never consent)
# ---------------------------------------------------------------------------
#
# create_solicitation() records the operator-approved pending row. This section
# adds the delivery: it sends the "Reply YES to opt in" request through the
# tenant's normal conversational SMS path (services.twilio_gate enforces
# LUXIT_TWILIO_MODE, so a disabled environment records 'blocked' and sends
# nothing). Consent is still granted only by record_contextual_yes() when the
# customer actually replies YES.

_SENT_DELIVERY_STATES = {"queued", "sending", "sent", "delivered"}


def _normalize_delivery_status(raw) -> str:
    s = str(raw or "").strip().lower()
    if s in {"delivered"}:
        return "delivered"
    if s in {"failed", "undelivered"}:
        return s
    if s in {"queued", "sending", "accepted", "scheduled"}:
        return "queued"
    return "sent"


def send_solicitation(
    company_id: int,
    contact_id: int,
    *,
    actor_user_id: int | None,
    business_phone_number: str | None = None,
    body: str | None = None,
    force_resend: bool = False,
) -> dict:
    """Record-or-reuse a pending solicitation for one contact AND dispatch the
    opt-in request SMS.

    Refuses (``ok=False``) exactly where create_solicitation refuses:
    suppressed / already-promotional / no canonical phone / contact not found.

    Idempotent: if the pending solicitation was already handed to Twilio
    (``delivery_status`` in queued/sent/delivered) a second call sends nothing
    and returns ``resent=False`` unless ``force_resend`` is set.

    Never mutates consent columns. On a blocked/failed send the pending row is
    kept (so a later YES still has context) with ``delivery_status`` recorded.
    """
    from twilio_sms import _get_twilio_account, _get_or_create_conversation, sendConversationSms
    from services.phone_normalization import normalize_phone_e164
    from models import TwilioMessage, TwilioPhoneNumber

    created = create_solicitation(
        company_id, contact_id,
        business_phone_number=business_phone_number,
        actor_user_id=actor_user_id, body=body, source="operator",
    )
    if not created.get("ok"):
        return {"ok": False, "error": created.get("error"), "contact_id": contact_id}
    row = created["solicitation"]

    already_dispatched = (row.delivery_status or "") in _SENT_DELIVERY_STATES
    if already_dispatched and not force_resend:
        return {
            "ok": True, "sent": True, "resent": False, "contact_id": contact_id,
            "solicitation_id": row.id, "delivery_status": row.delivery_status,
            "message_sid": row.solicitation_message_sid,
        }

    ta = _get_twilio_account(company_id)
    if not ta:
        return {
            "ok": True, "sent": False, "blocked": False, "contact_id": contact_id,
            "solicitation_id": row.id, "delivery_status": row.delivery_status,
            "error": "twilio_not_configured",
        }

    business = (
        normalize_phone_e164(business_phone_number)
        or row.business_phone_number
        or getattr(ta, "from_phone", None)
    )
    pn = None
    if business:
        pn = TwilioPhoneNumber.query.filter_by(
            company_id=company_id, phone_number=business, is_active=True
        ).first()

    conv = _get_or_create_conversation(
        company_id, row.canonical_phone, business or "",
        phone_number_id=pn.id if pn else None, create_contact=False,
    )
    if conv is not None and not conv.contact_id:
        conv.contact_id = contact_id
    db.session.flush()

    text = body or row.solicitation_body or SUGGESTED_SOLICITATION_COPY
    send = sendConversationSms(
        conv.id, text, twilio_account=ta,
        effect_type="promotional_optin_solicitation", bypass_outbox=True,
    )

    now = _now()
    row.last_status_at = now
    if not row.solicitation_body:
        row.solicitation_body = text
    if business and not row.business_phone_number:
        row.business_phone_number = business

    if send.get("success"):
        row.solicitation_message_sid = send.get("sid") or row.solicitation_message_sid
        row.delivery_status = _normalize_delivery_status(send.get("provider_status"))
        row.sent_at = row.sent_at or now
        row.send_error = None
        _audit(company_id, actor_user_id, "promotional_solicitation_sent", row.id, {
            "contact_id": contact_id, "canonical_phone": row.canonical_phone,
            "business_phone_number": row.business_phone_number,
            "message_sid": row.solicitation_message_sid,
            "delivery_status": row.delivery_status,
        })
        return {
            "ok": True, "sent": True, "resent": already_dispatched,
            "contact_id": contact_id, "solicitation_id": row.id,
            "delivery_status": row.delivery_status,
            "message_sid": row.solicitation_message_sid,
        }

    # A system-level block (outbound Twilio disabled, or the tenant's Phone/PWA
    # SMS licence inactive) is not a per-message delivery failure — mark it
    # 'blocked' so the operator sees a config problem, not a bad number, and
    # keep the pending row so a later YES still resolves.
    blocked = (
        str(send.get("error_code") or "") == "TwilioSendBlockedError"
        or bool(send.get("license_blocked"))
        or "disabled" in str(send.get("error") or "").lower()
    )
    row.delivery_status = "blocked" if blocked else "failed"
    row.send_error = send.get("error")
    _audit(company_id, actor_user_id,
           "promotional_solicitation_send_blocked" if blocked else "promotional_solicitation_send_failed",
           row.id, {
               "contact_id": contact_id, "error": send.get("error"),
               "error_code": send.get("error_code"),
           })
    return {
        "ok": True, "sent": False, "blocked": blocked, "contact_id": contact_id,
        "solicitation_id": row.id, "delivery_status": row.delivery_status,
        "error": send.get("error"),
    }


def send_solicitation_batch(
    company_id: int,
    contact_ids: list,
    *,
    actor_user_id: int | None,
    segment_id: int | None = None,
    body: str | None = None,
    max_batch: int = 200,
) -> dict:
    """Send opt-in request SMS to a list of contacts.

    The list is re-intersected against the CURRENT ``needs_promotional_optin``
    audience for the tenant/segment, so a stale or hand-crafted client list can
    never reach a STOP/suppressed contact, an already-promotional contact, a
    duplicate phone, or a contact outside the segment. Anything not currently
    eligible is reported in ``skipped_not_eligible`` and never contacted.
    """
    eligible = {a["contact_id"] for a in needs_promotional_optin(company_id, segment_id)}
    requested = list(dict.fromkeys(int(c) for c in (contact_ids or [])))
    targets = [c for c in requested if c in eligible][:max_batch]
    skipped = [c for c in requested if c not in eligible]
    over_cap = [c for c in requested if c in eligible][max_batch:]

    results = []
    for cid in targets:
        try:
            res = send_solicitation(
                company_id, cid, actor_user_id=actor_user_id, body=body,
            )
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 — one bad contact must not abort the batch
            db.session.rollback()
            res = {"ok": False, "contact_id": cid, "error": str(exc)}
        results.append(res)

    return {
        "ok": True,
        "requested": len(requested),
        "eligible": len(targets),
        "sent": sum(1 for r in results if r.get("sent")),
        "blocked": sum(1 for r in results if r.get("blocked")),
        "failed": sum(1 for r in results if r.get("ok") and not r.get("sent") and not r.get("blocked")),
        "refused": sum(1 for r in results if not r.get("ok")),
        "skipped_not_eligible": skipped,
        "skipped_over_cap": over_cap,
        "results": results,
    }


def sync_solicitation_delivery_status(message_sid: str, status: str, error: str | None = None) -> int:
    """Apply a Twilio delivery-status callback to any solicitation whose opt-in
    request carried this MessageSid. Delivery status only — never consent."""
    if not message_sid:
        return 0
    rows = PromotionalOptInSolicitation.query.filter_by(
        solicitation_message_sid=message_sid
    ).all()
    mapped = _normalize_delivery_status(status)
    now = _now()
    for row in rows:
        row.delivery_status = mapped
        row.last_status_at = now
        if mapped in {"failed", "undelivered"}:
            row.send_error = error or status
        elif mapped == "delivered":
            row.send_error = None
    return len(rows)


def latest_solicitation_status_map(company_id: int, contact_ids: list) -> dict:
    """{contact_id: {state, label, status, delivery_status, message_sid,
    sent_at}} for the newest solicitation per contact. Read-only.

    ``state`` is the single value the operator UI colours on:
    replied_yes | stopped | delivered | sent | failed | blocked | not_sent.
    """
    ids = [int(c) for c in (contact_ids or [])]
    if not ids:
        return {}
    rows = (
        PromotionalOptInSolicitation.query
        .filter(
            PromotionalOptInSolicitation.company_id == company_id,
            PromotionalOptInSolicitation.contact_id.in_(ids),
        )
        .order_by(PromotionalOptInSolicitation.contact_id.asc(),
                  PromotionalOptInSolicitation.id.asc())
        .all()
    )
    latest: dict[int, PromotionalOptInSolicitation] = {}
    for row in rows:
        latest[row.contact_id] = row  # ascending id → last wins

    out: dict[int, dict] = {}
    for cid, row in latest.items():
        is_web = (row.source == "web_optin") or bool(row.web_token_jti)
        if row.status == "consented" and is_web:
            state, label = "consented_web", "Consented (web)"
        elif row.status == "consented":
            state, label = "replied_yes", "Replied YES"
        elif row.status == "stopped":
            state, label = "stopped", "STOP"
        elif row.status == "cancelled":
            state, label = "not_sent", "Cancelled"
        elif row.delivery_status == "delivered":
            state, label = "delivered", "Delivered"
        elif row.delivery_status in _SENT_DELIVERY_STATES:
            state, label = "sent", "Sent"
        elif row.delivery_status == "blocked":
            state, label = "blocked", "Blocked (SMS disabled)"
        elif row.delivery_status in {"failed", "undelivered"}:
            state, label = "failed", "Send failed"
        elif is_web and row.web_token_jti:
            state, label = "link", "Consent link generated"
        else:
            state, label = "not_sent", "Not sent"
        out[cid] = {
            "state": state, "label": label,
            "status": row.status, "delivery_status": row.delivery_status,
            "message_sid": row.solicitation_message_sid,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "solicitation_id": row.id,
            "web_link": bool(row.web_token_jti),
            "web_consent_at": row.web_consent_at.isoformat() if row.web_consent_at else None,
        }
    return out


# ---------------------------------------------------------------------------
# 5. Hosted web opt-in  (customer_web_optin — the second consent-granting path)
# ---------------------------------------------------------------------------
#
# A signed link (or its QR) is generated per contact for an operator to share
# out-of-band: at checkout, in person, on the customer's account page, on
# printed material, or any other independently permitted channel. NO SMS is
# sent to deliver this link. When the customer opens the page and affirmatively
# checks the (initially unchecked) disclosure box and submits, promotional
# consent is granted exactly as a contextual inbound YES would grant it —
# through _grant_promotional_consent() — with an immutable PromotionalConsentEvent
# recording the exact disclosure text + version + request context.
#
# STOP still wins: a suppressed / opted-out contact who somehow submits the form
# grants nothing and the pending link is closed. Idempotent on the link's
# web_token_jti: a double submission never creates a second event or side effect.

def _web_optin_serializer():
    from itsdangerous import URLSafeSerializer
    from flask import current_app
    secret = (
        current_app.config.get("SECRET_KEY")
        or os.environ.get("SESSION_SECRET")
        or os.environ.get("SECRET_KEY")
        or "insecure-dev-key"
    )
    return URLSafeSerializer(secret, salt=_WEB_TOKEN_SALT)


def _encode_web_token(company_id: int, contact_id: int, jti: str) -> str:
    return _web_optin_serializer().dumps({"c": int(company_id), "k": int(contact_id), "j": jti})


def _decode_web_token(token: str) -> dict | None:
    from itsdangerous import BadData
    try:
        data = _web_optin_serializer().loads(token)
    except BadData:
        return None
    if not isinstance(data, dict) or not all(k in data for k in ("c", "k", "j")):
        return None
    return data


def web_optin_link_base_url() -> str:
    base = (
        os.environ.get("APP_BASE_URL")
        or os.environ.get("LUXIT_TWILIO_WEBHOOK_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if base:
        return base
    try:
        from flask import request
        if request:
            return request.url_root.rstrip("/")
    except Exception:
        pass
    return ""


def _resolve_web_token(token: str):
    """(solicitation, contact, payload) for a valid link, else (None, None, reason)."""
    payload = _decode_web_token(token)
    if not payload:
        return None, None, "invalid_token"
    row = (
        PromotionalOptInSolicitation.query
        .filter_by(web_token_jti=payload["j"])
        .first()
    )
    if not row:
        return None, None, "unknown_link"
    # Tamper / cross-tenant: the signed body must match the stored row exactly.
    if row.company_id != payload["c"] or row.contact_id != payload["k"]:
        return None, None, "token_mismatch"
    contact = Contact.query.filter_by(id=row.contact_id, company_id=row.company_id).first()
    if not contact:
        return None, None, "contact_not_found"
    # The canonical phone must still resolve to the same number the link was
    # minted for (a number change since minting invalidates the link).
    current = _canonical_phone(contact)
    if not current or current != row.canonical_phone:
        return None, None, "phone_changed"
    return row, contact, payload


def generate_web_optin_link(
    company_id: int,
    contact_id: int,
    *,
    actor_user_id: int | None = None,
    business_phone_number: str | None = None,
) -> dict:
    """Create-or-reuse a hosted opt-in link for one contact. Idempotent: a
    second call returns the existing link. Refuses exactly where
    create_solicitation refuses (suppressed / already-promotional / no phone).
    Sends nothing.
    """
    created = create_solicitation(
        company_id, contact_id,
        business_phone_number=business_phone_number,
        actor_user_id=actor_user_id,
        body=SUGGESTED_SOLICITATION_COPY,
        source="web_optin",
    )
    if not created.get("ok"):
        return {"ok": False, "error": created.get("error")}
    row = created["solicitation"]

    minted = False
    if not row.web_token_jti:
        row.web_token_jti = secrets.token_urlsafe(24)
        row.web_link_disclosure_version = WEB_OPTIN_DISCLOSURE_VERSION
        row.web_link_created_at = _now()
        row.web_link_created_by_user_id = actor_user_id
        minted = True
        db.session.flush()
        _audit(company_id, actor_user_id, "promotional_web_optin_link_created", row.id, {
            "contact_id": contact_id, "canonical_phone": row.canonical_phone,
            "disclosure_version": WEB_OPTIN_DISCLOSURE_VERSION,
        })

    token = _encode_web_token(company_id, contact_id, row.web_token_jti)
    base = web_optin_link_base_url()
    path = f"/promo-optin/{token}"
    return {
        "ok": True,
        "created": minted,
        "solicitation_id": row.id,
        "token": token,
        "path": path,
        "url": (base + path) if base else path,
        "disclosure_version": row.web_link_disclosure_version or WEB_OPTIN_DISCLOSURE_VERSION,
        "status": row.status,
    }


def get_web_optin_context(token: str) -> dict:
    """Read-only context for rendering the public consent page."""
    row, contact, payload = _resolve_web_token(token)
    if not row:
        return {"ok": False, "error": payload}
    already = bool(row.status == "consented") or has_promotional_optin(contact)
    suppressed = is_suppressed(contact) or bool(contact.sms_opted_out or contact.sms_opt_out_at)
    first = _safe_first_name(contact)
    return {
        "ok": True,
        "brand": WEB_OPTIN_BRAND,
        "disclosure_version": WEB_OPTIN_DISCLOSURE_VERSION,
        "disclosure_text": WEB_OPTIN_DISCLOSURE_TEXT,
        "first_name": first,
        "phone_hint": "•••• " + (row.canonical_phone or "")[-4:],
        "already_consented": already,
        "suppressed": suppressed,
        "status": row.status,
    }


def _safe_first_name(contact: Contact) -> str | None:
    fn = (getattr(contact, "first_name", "") or "").strip()
    if not fn:
        return None
    level = getattr(contact, "name_verification_level", "") or ""
    if level not in ("verified", "confirmed", "customer_confirmed"):
        return None
    if not re.match(r"^[A-Za-z][A-Za-z'.\-À-ɏ ]{0,39}$", fn):
        return None
    return fn


def record_web_optin(
    token: str,
    *,
    disclosure_version: str | None,
    consent_ip: str | None = None,
    user_agent: str | None = None,
    page_url: str | None = None,
    received_at: datetime | None = None,
) -> dict:
    """Grant promotional consent from an affirmative hosted-page submission.

    Idempotent on the link's ``web_token_jti``. STOP precedence: a suppressed /
    opted-out contact grants nothing and the link is closed. Rejects a stale
    disclosure version (the page was cached before a disclosure change).
    """
    received_at = received_at or _now()
    row, contact, payload = _resolve_web_token(token)
    if not row:
        return {"ok": False, "error": payload}

    jti = row.web_token_jti

    existing = PromotionalConsentEvent.query.filter_by(web_token_jti=jti).first()
    if existing:
        return {
            "ok": True, "granted": False, "duplicate": True,
            "event_id": existing.id, "solicitation_id": existing.solicitation_id,
        }

    # The exact disclosure the customer affirmed must be the one we still stand
    # behind. A mismatch means a cached/stale page — refuse, don't silently
    # record consent to text the customer never saw.
    want = disclosure_version or row.web_link_disclosure_version or WEB_OPTIN_DISCLOSURE_VERSION
    if want != WEB_OPTIN_DISCLOSURE_VERSION:
        return {"ok": False, "error": "stale_disclosure",
                "current_disclosure_version": WEB_OPTIN_DISCLOSURE_VERSION}

    # STOP always wins.
    if is_suppressed(contact) or contact.sms_opted_out or contact.sms_opt_out_at:
        if row.status == "pending":
            row.status = "stopped"
            row.closed_at = received_at
            row.closed_reason = "suppressed_at_web_optin"
            _audit(row.company_id, None, "promotional_solicitation_closed", row.id, {
                "contact_id": contact.id, "reason": "suppressed_at_web_optin",
            })
        return {"ok": True, "granted": False, "suppressed": True}

    if row.status == "consented" or has_promotional_optin(contact):
        # Already promotional through another path; nothing to grant, no error.
        return {"ok": True, "granted": False, "already": True,
                "solicitation_id": row.id}

    canonical = _canonical_phone(contact) or row.canonical_phone
    context = {
        k: v for k, v in {
            "ip": consent_ip, "user_agent": (user_agent or "")[:400],
            "page_url": page_url, "channel": "customer_web_optin",
        }.items() if v
    }
    event = PromotionalConsentEvent(
        company_id=row.company_id,
        contact_id=contact.id,
        solicitation_id=row.id,
        canonical_phone=canonical,
        business_phone_number=row.business_phone_number,
        inbound_message_sid=None,
        web_token_jti=jti,
        solicited_at=row.web_link_created_at or row.solicited_at,
        consent_purpose="promotional",
        consent_source="customer_web_optin",
        disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION,
        disclosure_text=WEB_OPTIN_DISCLOSURE_TEXT,
        consent_context=context or None,
        consented_at=received_at,
    )
    try:
        with db.session.begin_nested():
            db.session.add(event)
            db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = PromotionalConsentEvent.query.filter_by(web_token_jti=jti).first()
        return {
            "ok": True, "granted": False, "duplicate": True,
            "event_id": getattr(existing, "id", None),
            "solicitation_id": getattr(existing, "solicitation_id", row.id),
        }

    row.status = "consented"
    row.consented_at = received_at
    row.web_consent_at = received_at

    _grant_promotional_consent(contact, received_at, "promotional_web_optin")

    _audit(row.company_id, None, "promotional_consent_granted", event.id, {
        "contact_id": contact.id,
        "solicitation_id": row.id,
        "canonical_phone": canonical,
        "consent_purpose": "promotional",
        "consent_source": "customer_web_optin",
        "disclosure_version": WEB_OPTIN_DISCLOSURE_VERSION,
        "consented_at": received_at.isoformat(),
    })

    confirmation_sent = _maybe_send_web_confirmation(row.company_id, contact, row)

    return {
        "ok": True, "granted": True, "duplicate": False,
        "event_id": event.id, "solicitation_id": row.id,
        "confirmation_sent": confirmation_sent,
    }


def _web_confirmation_enabled(company_id: int) -> bool:
    """True only when the deployment has explicitly turned on the post-opt-in
    confirmation SMS AND the tenant has a registered messaging campaign
    (an active Twilio account with a Messaging Service SID). Off by default
    everywhere, so a deploy sends nothing until it is deliberately configured.
    """
    flag = str(os.environ.get("PROMO_OPTIN_WEB_CONFIRMATION_SMS", "")).strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        from twilio_sms import _get_twilio_account
        ta = _get_twilio_account(company_id)
        return bool(ta and getattr(ta, "messaging_service_sid", None))
    except Exception:
        return False


def _maybe_send_web_confirmation(company_id: int, contact: Contact, row) -> bool:
    if not _web_confirmation_enabled(company_id):
        return False
    try:
        from twilio_sms import (
            _get_twilio_account, _get_or_create_conversation, sendConversationSms,
        )
        ta = _get_twilio_account(company_id)
        business = row.business_phone_number or getattr(ta, "from_phone", None)
        conv = _get_or_create_conversation(
            company_id, row.canonical_phone, business or "", create_contact=False,
        )
        if conv is not None and not conv.contact_id:
            conv.contact_id = contact.id
        db.session.flush()
        send = sendConversationSms(
            conv.id, PROMO_OPTIN_CONFIRMATION, twilio_account=ta,
            effect_type="promotional_optin_web_confirmation", bypass_outbox=True,
        )
        return bool(send.get("success"))
    except Exception:
        return False
