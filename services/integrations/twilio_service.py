"""
Twilio integration service.

Wraps the existing per-company TwilioAccount model/blueprint with a
platform-level health check and a safe SMS-send helper that enforces
opt-in rules before touching the Twilio API.
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STOP_WORDS  = {"stop", "unsubscribe", "cancel", "end", "quit", "optout"}
START_WORDS = {"start", "subscribe", "yes"}

_UNICODE_REPLACEMENTS = str.maketrans({
    "\u2026": "...", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-",   "\u2014": "--", "\u2022": "*", "\u00a0": " ",
    "\u2122": "(TM)", "\u00ae": "(R)", "\u00a9": "(C)",
})

def _sanitize_body(text: str) -> str:
    if not text:
        return text
    text = text.translate(_UNICODE_REPLACEMENTS)
    return text.encode("latin-1", errors="replace").decode("latin-1")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def health_check() -> dict:
    """Return status dict for the Twilio integration."""
    sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    phone = os.environ.get("TWILIO_PHONE_NUMBER", "")

    if not (sid and token and phone):
        return {"status": "missing_config", "detail": "Twilio credentials not configured"}

    try:
        from twilio.rest import Client
        client = Client(sid, token)
        # Lightweight call — just fetch account to verify credentials
        acct = client.api.accounts(sid).fetch()
        return {"status": "connected", "account_name": acct.friendly_name}
    except ImportError:
        return {"status": "error", "detail": "twilio package not installed"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


# ---------------------------------------------------------------------------
# SMS sending with consent enforcement
# ---------------------------------------------------------------------------

def send_sms(to_number: str, body: str, company_id: int | None = None,
             from_number: str | None = None) -> dict:
    """
    Send an SMS, enforcing opt-in rules.

    Returns {"ok": True, "sid": "..."} or {"ok": False, "reason": "..."}.
    """
    if not to_number or not body:
        return {"ok": False, "reason": "missing to_number or body"}

    # --- Consent check against Contact table ---
    try:
        from models import Contact
        contact = Contact.query.filter_by(phone=to_number, company_id=company_id).first()
        if contact:
            tags = (contact.tags or "").lower()
            if "do_not_text" in tags:
                return {"ok": False, "reason": "contact tagged do_not_text"}
            if not contact.is_subscribed:
                return {"ok": False, "reason": "contact not subscribed"}
    except Exception as exc:
        logger.warning("Twilio consent check error: %s", exc)

    # --- Opt-in check against TwilioConversation / TwilioMessage ---
    try:
        from models import TwilioConversation
        convo = TwilioConversation.query.filter_by(
            from_number=to_number, company_id=company_id
        ).first()
        if convo and convo.is_opted_out:
            return {"ok": False, "reason": "sms_opt_in is false"}
    except Exception as exc:
        logger.warning("Twilio conversation check error: %s", exc)

    # --- Send ---
    sid   = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_phone = from_number or os.environ.get("TWILIO_PHONE_NUMBER", "")

    if not (sid and token and from_phone):
        return {"ok": False, "reason": "Twilio credentials not configured"}

    try:
        from twilio.rest import Client
        body = _sanitize_body(body)
        client = Client(sid, token)
        msg = client.messages.create(body=body, from_=from_phone, to=to_number)
        _log_event(company_id, "sms_sent", {"to": to_number, "sid": msg.sid})
        return {"ok": True, "sid": msg.sid}
    except Exception as exc:
        _log_error(company_id, "send_sms", str(exc))
        return {"ok": False, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Inbound SMS handling
# ---------------------------------------------------------------------------

def handle_inbound(from_number: str, body: str, to_number: str,
                   company_id: int | None = None) -> dict:
    """
    Process an inbound SMS: handle STOP/START, create lead, build reply.
    Returns {"reply": str | None, "action": str}.
    """
    word = body.strip().lower()

    if word in STOP_WORDS:
        _set_opt_in(from_number, company_id, False, f"STOP keyword: {body[:50]}")
        _log_event(company_id, "sms_opt_out", {"phone": from_number, "keyword": word})
        return {"reply": _opt_out_message(company_id), "action": "opt_out"}

    if word in START_WORDS:
        _set_opt_in(from_number, company_id, True, f"START keyword: {body[:50]}")
        _log_event(company_id, "sms_opt_in", {"phone": from_number, "keyword": word})
        return {"reply": _opt_in_message(company_id), "action": "opt_in"}

    if word == "help":
        return {"reply": _help_message(company_id), "action": "help"}

    # First-contact lead creation
    is_new = _ensure_lead(from_number, company_id, body)
    action = "new_lead" if is_new else "message_received"
    _log_event(company_id, action, {"phone": from_number})
    return {"reply": _first_contact_message(company_id) if is_new else None, "action": action}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _set_opt_in(phone: str, company_id: int | None, opt_in: bool, source: str):
    try:
        from extensions import db
        from models import TwilioConversation
        convo = TwilioConversation.query.filter_by(
            from_number=phone, company_id=company_id
        ).first()
        if convo:
            convo.is_opted_out = not opt_in
            if opt_in:
                convo.sms_opt_in_at = datetime.now(timezone.utc)
            else:
                convo.sms_opt_out_at = datetime.now(timezone.utc)
            db.session.commit()
    except Exception as exc:
        logger.warning("_set_opt_in error: %s", exc)


def _ensure_lead(phone: str, company_id: int | None, first_msg: str) -> bool:
    """Create contact + conversation if phone is unknown. Returns True if created."""
    try:
        from extensions import db
        from models import TwilioConversation, Contact
        existing = TwilioConversation.query.filter_by(
            from_number=phone, company_id=company_id
        ).first()
        if existing:
            return False
        # Create contact
        contact = Contact(
            phone=phone,
            company_id=company_id,
            tags="sms_opt_in,new_lead",
            source="inbound_sms",
            is_subscribed=True,
        )
        db.session.add(contact)
        db.session.flush()
        # Create conversation
        convo = TwilioConversation(
            company_id=company_id,
            from_number=phone,
            is_opted_out=False,
            is_first_contact=True,
            lead_captured=True,
            sms_opt_in_at=datetime.now(timezone.utc),
        )
        db.session.add(convo)
        db.session.commit()
        return True
    except Exception as exc:
        logger.error("_ensure_lead error: %s", exc)
        try:
            from extensions import db
            db.session.rollback()
        except Exception:
            pass
        return False


def _opt_out_message(company_id):
    return "You have been unsubscribed. Reply START to resubscribe."


def _opt_in_message(company_id):
    return "You have been subscribed. Reply STOP to unsubscribe."


def _help_message(company_id):
    return "Reply STOP to unsubscribe or START to resubscribe. For support visit luxit.app"


def _first_contact_message(company_id):
    return "Thanks for reaching out! We'll be in touch shortly."


def _log_event(company_id, event_type, payload):
    try:
        from extensions import db
        from models import IntegrationEvent
        ev = IntegrationEvent(
            company_id=company_id,
            provider="twilio",
            event_type=event_type,
            payload_json=json.dumps(payload),
            status="processed",
        )
        db.session.add(ev)
        db.session.commit()
    except Exception as exc:
        logger.debug("_log_event failed: %s", exc)


def _log_error(company_id, endpoint, error_msg):
    try:
        from extensions import db
        from models import IntegrationErrorLog
        el = IntegrationErrorLog(
            company_id=company_id,
            provider="twilio",
            endpoint=endpoint,
            error_message=error_msg[:500],
        )
        db.session.add(el)
        db.session.commit()
    except Exception as exc:
        logger.debug("_log_error failed: %s", exc)
