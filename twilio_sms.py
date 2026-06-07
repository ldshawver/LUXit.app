"""
Multi-tenant Twilio SMS & Call Platform — LUXit
Blueprint: twilio_bp  (url_prefix=/twilio)

Public webhook endpoints (no login):
  POST /twilio/sms/inbound   — Twilio inbound SMS webhook
  POST /twilio/sms/status    — Twilio delivery status callback
  POST /twilio/voice/inbound — Twilio inbound call webhook

Protected routes (login_required):
  GET  /twilio/inbox                      — conversation list
  GET  /twilio/inbox/<id>                 — single conversation
  POST /twilio/send                       — send outbound SMS (JSON API)
  GET  /twilio/settings                   — Twilio account settings
  POST /twilio/settings                   — save settings
  GET  /twilio/rules                      — auto-reply rules list
  POST /twilio/rules/create               — create new rule
  POST /twilio/rules/<id>/toggle          — enable/disable rule
  POST /twilio/rules/<id>/delete          — delete rule
  GET  /twilio/calls                      — call log
  GET  /twilio/analytics                  — analytics summary
  GET  /twilio/hours                      — business hours
  POST /twilio/hours                      — save business hours
"""

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import (
    Blueprint, abort, flash, jsonify, redirect,
    render_template, request, url_for
)
from flask_login import current_user, login_required

from extensions import db, csrf

logger = logging.getLogger(__name__)

twilio_bp = Blueprint("twilio", __name__, url_prefix="/twilio")


@twilio_bp.before_request
def _guard_sms_feature():
    """Allow authenticated users with full-app access through Twilio routes.
    Twilio webhook callbacks are always permitted (no auth required).
    Inbox-only users are redirected to the Mobile Inbox PWA instead."""
    from flask import request, redirect, render_template

    # Twilio server-to-server webhooks — never require auth or feature flags
    _WEBHOOK_PATHS = {
        "/twilio/sms/inbound",
        "/twilio/sms/status",
        "/twilio/voice/inbound",
        "/twilio/voice/no-answer",
        "/twilio/voice/recording",
        "/twilio/voice/status",
    }
    if request.path in _WEBHOOK_PATHS:
        return None

    if not current_user.is_authenticated:
        return None  # login_required on individual routes handles the redirect

    # Role-based gate: inbox_only users belong in the PWA, not the desktop app
    try:
        from models import UserCompanyAccess
        acc = UserCompanyAccess.query.filter_by(user_id=current_user.id).first()
        if acc and not acc.has_full_app_access():
            return redirect("/app/inbox")
    except Exception as exc:
        logger.warning("SMS access check error: %s", exc)

    return None  # allow through


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_company():
    if current_user.is_authenticated:
        return current_user.get_default_company()
    return None


def _get_twilio_account(company_id):
    from models import TwilioAccount
    return TwilioAccount.query.filter_by(company_id=company_id).first()


def _get_twilio_account_by_number(to_number: str):
    """Legacy helper — delegates to _resolve_number for backward compat."""
    _pn, ta = _resolve_number(to_number)
    return ta


def _resolve_number(to_number: str, msg_service_sid: str = ""):
    """
    Phase A: Resolve inbound Twilio webhook to (TwilioPhoneNumber, TwilioAccount).

    Lookup priority:
      1. TwilioPhoneNumber.phone_number == to_number  (multi-number DB, primary path)
      2. TwilioAccount.messaging_service_sid == msg_service_sid (messaging service)
      3. TwilioAccount.from_phone == to_number  (legacy single-number accounts)
      4. First active TwilioAccount (absolute fallback — existing behaviour)

    Returns (pn_or_None, ta_or_None).
    """
    from models import TwilioPhoneNumber, TwilioAccount

    # ── 1. Look up by phone number in new multi-number table ─────────────────
    pn = None
    if to_number:
        pn = TwilioPhoneNumber.query.filter_by(
            phone_number=to_number, is_active=True
        ).first()

    if pn:
        # Prefer the account linked directly; fall back to any account for company
        ta = (TwilioAccount.query.get(pn.twilio_account_id)
              if pn.twilio_account_id else None)
        if not ta:
            ta = TwilioAccount.query.filter_by(company_id=pn.company_id).first()
        logger.debug("_resolve_number: matched TwilioPhoneNumber id=%s company=%s",
                     pn.id, pn.company_id)
        return pn, ta

    # ── 2. Messaging Service SID ──────────────────────────────────────────────
    if msg_service_sid:
        ta = TwilioAccount.query.filter_by(
            messaging_service_sid=msg_service_sid
        ).first()
        if ta:
            logger.debug("_resolve_number: matched by MessagingServiceSid ta=%s", ta.id)
            return None, ta

    # ── 3. Legacy from_phone on TwilioAccount ────────────────────────────────
    if to_number:
        ta = TwilioAccount.query.filter_by(from_phone=to_number).first()
        if ta:
            logger.debug("_resolve_number: matched legacy from_phone ta=%s", ta.id)
            return None, ta

    # ── 4. Absolute fallback — first active account ───────────────────────────
    ta = TwilioAccount.query.filter_by(is_active=True).first()
    if ta:
        logger.debug("_resolve_number: fallback to first active TwilioAccount id=%s", ta.id)
    return None, ta


def _seed_phone_numbers_from_accounts():
    """
    Phase A migration helper: auto-create TwilioPhoneNumber rows for any
    TwilioAccount that has a from_phone but no matching TwilioPhoneNumber yet.
    Safe to call multiple times (idempotent).
    """
    from models import TwilioAccount, TwilioPhoneNumber
    try:
        accounts = TwilioAccount.query.filter(
            TwilioAccount.from_phone.isnot(None),
            TwilioAccount.from_phone != "",
        ).all()
        seeded = 0
        for ta in accounts:
            existing = TwilioPhoneNumber.query.filter_by(
                phone_number=ta.from_phone
            ).first()
            if not existing:
                pn = TwilioPhoneNumber(
                    company_id        = ta.company_id,
                    twilio_account_id = ta.id,
                    phone_number      = ta.from_phone,
                    friendly_name     = f"Primary ({ta.from_phone})",
                    app_assignment    = "luxit",
                    number_type       = "local",
                    sms_enabled       = True,
                    voice_enabled     = True,
                    sms_forward_to    = ta.sms_forward_to,
                    sms_forwarding_enabled  = bool(ta.sms_forwarding_enabled),
                    auto_reply_enabled      = True,
                    call_forward_to         = ta.call_forward_to,
                    voice_forwarding_enabled = bool(ta.voice_forwarding_enabled),
                    ring_timeout            = 25,
                    voicemail_greeting_text = ta.voicemail_greeting_text,
                    voicemail_greeting_audio_url = ta.voicemail_greeting_audio_url,
                    missed_call_text        = ta.missed_call_text,
                    after_hours_text        = ta.after_hours_text,
                    after_hours_sms_enabled = bool(ta.after_hours_sms_enabled),
                    after_hours_voicemail_enabled = bool(ta.after_hours_voicemail_enabled),
                    is_active  = True,
                    is_primary = True,
                )
                db.session.add(pn)
                seeded += 1
        if seeded:
            db.session.commit()
            logger.info("Phase A seed: created %d TwilioPhoneNumber rows from TwilioAccount.from_phone", seeded)
    except Exception as exc:
        db.session.rollback()
        logger.warning("Phase A seed error (non-fatal): %s", exc)


def _build_client(ta):
    """Create a Twilio REST client from a TwilioAccount record."""
    try:
        from twilio.rest import Client
        return Client(ta.get_account_sid(), ta.get_auth_token())
    except Exception as exc:
        logger.error("Twilio client error: %s", exc)
        return None


_LA = ZoneInfo("America/Los_Angeles")

# System-level keyword responses (always fire regardless of auto-reply rules)
_STOP_REPLY  = "You have been unsubscribed from LUX messages. Reply START to re-subscribe."
_START_REPLY = ("LUX: You are now subscribed to receive messages. Message frequency varies. "
                "Reply HELP for help or STOP to opt out. Msg & data rates may apply.")
_HELP_REPLY  = "LUX: Reply STOP to opt out. Msg frequency varies. Msg & data rates may apply."

# All recognised opt-out / opt-in keyword variants (per CTIA / Twilio guidelines)
_STOP_KEYWORDS  = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
_START_KEYWORDS = {"start", "subscribe", "join"}

TWILIO_WEBHOOK_PUBLIC_URL = os.environ.get(
    "TWILIO_WEBHOOK_PUBLIC_URL", "https://luxit.app/twilio/sms/inbound"
)


def _twiml_message(text: str):
    """Return a TwiML <Response><Message> reply."""
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'
    return xml, 200, {"Content-Type": "text/xml"}


_UNICODE_REPLACEMENTS = str.maketrans({
    "\u2026": "...",   # …  ellipsis
    "\u2019": "'",     # '  right single quotation mark
    "\u2018": "'",     # '  left single quotation mark
    "\u201c": '"',     # "  left double quotation mark
    "\u201d": '"',     # "  right double quotation mark
    "\u2013": "-",     # –  en dash
    "\u2014": "--",    # —  em dash
    "\u2022": "*",     # •  bullet
    "\u00a0": " ",     # non-breaking space
    "\u2122": "(TM)",  # ™
    "\u00ae": "(R)",   # ®
    "\u00a9": "(C)",   # ©
})


def _safe_sms_text(value):
    if value is None:
        return ""
    value = str(value)
    return (
        value.replace("…", "...")
             .replace("→", "->")
             .replace("←", "<-")
             .replace("—", "-")
             .replace("–", "-")
             .replace("“", '"')
             .replace("”", '"')
             .replace("‘", "'")
             .replace("’", "'")
             .replace("\u00a0", " ")
    )

def _sanitize_body(text: str) -> str:
    return _safe_sms_text(text).replace("\x00", "")

def _validate_twilio_signature(ta, endpoint_path: str = "/twilio/sms/inbound") -> bool:
    """
    Validate the X-Twilio-Signature header for any Twilio webhook endpoint.
    Always passes on Replit dev (no real Twilio traffic).

    By default, a signature mismatch is logged as a WARNING but the request
    is still processed.  Set TWILIO_STRICT_SIGNATURE=1 in the environment to
    reject requests with bad signatures (returns False → caller aborts 403).
    """
    is_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"))
    if is_replit:
        return True

    strict = os.environ.get("TWILIO_STRICT_SIGNATURE", "").lower() in ("1", "true", "yes")

    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        return True

    try:
        from services.provider_config import get_provider_config
        _platform_token = get_provider_config("twilio", "platform", "auth_token")
    except Exception:
        _platform_token = None
    token = (ta.get_auth_token() if ta else None) or _platform_token
    if not token:
        logger.warning("Twilio signature validation skipped: no auth token configured")
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning(
            "Twilio webhook received without X-Twilio-Signature header "
            "(path=%s strict=%s) — %s",
            endpoint_path, strict,
            "REJECTED" if strict else "accepted (set TWILIO_STRICT_SIGNATURE=1 to reject)",
        )
        return not strict  # fail-open unless strict mode

    # Build the canonical URL used for signature verification.
    # Prefer the stored webhook_base_url; fall back to TWILIO_WEBHOOK_PUBLIC_URL env.
    base = (ta.webhook_base_url.rstrip("/") if ta and ta.webhook_base_url
            else TWILIO_WEBHOOK_PUBLIC_URL.rsplit("/twilio/", 1)[0])
    url = base + endpoint_path

    validator = RequestValidator(token)
    valid = validator.validate(url, request.form, signature)
    if not valid:
        logger.warning(
            "Twilio signature mismatch (url=%s strict=%s) — %s",
            url, strict,
            "REJECTED" if strict else "accepted (set TWILIO_STRICT_SIGNATURE=1 to reject)",
        )
        return not strict  # fail-open unless strict mode
    return True


def _is_business_hours(company_id: int) -> bool:
    """
    Return True if the current local time (America/Los_Angeles) falls within
    the configured business hours for the company.
    Handles midnight-crossing schedules (e.g. open_time=11:00, close_time=01:00).
    """
    from models import BusinessHours
    now_la = datetime.now(timezone.utc).astimezone(_LA)
    day = now_la.weekday()   # 0=Mon … 6=Sun
    bh = BusinessHours.query.filter_by(company_id=company_id, day_of_week=day).first()
    if not bh or not bh.is_open:
        return False
    try:
        open_h,  open_m  = [int(x) for x in bh.open_time.split(":")]
        close_h, close_m = [int(x) for x in bh.close_time.split(":")]
        current  = now_la.hour * 60 + now_la.minute
        opens    = open_h  * 60 + open_m
        closes   = close_h * 60 + close_m
        if closes <= opens:
            # Midnight-crossing (e.g. 23:00 – 01:00 or 11:00 – 01:00 next day)
            return current >= opens or current < closes
        return opens <= current < closes
    except Exception:
        return True


def _get_or_create_conversation(company_id: int, from_number: str, to_number: str):
    from models import TwilioConversation, Contact
    conv = TwilioConversation.query.filter_by(
        company_id=company_id,
        from_number=from_number,
    ).first()
    if not conv:
        # Try to link to an existing Contact
        contact = Contact.query.filter_by(
            company_id=company_id,
            phone=from_number,
            is_active=True,
        ).first()
        conv = TwilioConversation(
            company_id=company_id,
            from_number=from_number,
            to_number=to_number,
            contact_id=contact.id if contact else None,
            contact_name=f"{contact.first_name or ''} {contact.last_name or ''}".strip() if contact else None,
            is_first_contact=True,
        )
        db.session.add(conv)
        db.session.flush()
    return conv


def _send_sms(ta, to_number: str, body: str,
              conversation_id: int = None, is_auto_reply: bool = False,
              rule_id: int = None) -> dict:
    """Send an outbound SMS and persist the TwilioMessage record."""
    from models import TwilioMessage
    client = _build_client(ta)
    if not client:
        return {"success": False, "error": "Twilio client could not be created."}
    body = _sanitize_body(body)
    try:
        kwargs = {
            "body": body,
            "to":   to_number,
        }
        if ta.messaging_service_sid:
            kwargs["messaging_service_sid"] = ta.messaging_service_sid
        elif ta.from_phone:
            kwargs["from_"] = ta.from_phone
        else:
            return {"success": False, "error": "No From number or Messaging Service SID configured."}

        msg = client.messages.create(**kwargs)

        record = TwilioMessage(
            conversation_id=conversation_id,
            company_id=ta.company_id,
            twilio_sid=msg.sid,
            direction="outbound",
            from_number=ta.from_phone or ta.messaging_service_sid,
            to_number=to_number,
            body=body,
            status=msg.status,
            is_auto_reply=is_auto_reply,
            rule_id=rule_id,
        )
        db.session.add(record)
        db.session.commit()
        logger.info("Outbound SMS sent: sid=%s to=%s", msg.sid, to_number)
        try:
            from services.posthog_client import track_event
            track_event(f"company_{ta.company_id}", 'sms_sent', {
                'company_id':    ta.company_id,
                'tenant_id':     ta.company_id,
                'is_auto_reply': is_auto_reply,
                'message_length': len(body),
                'source':        'twilio',
                'success':       True,
            })
        except Exception:
            pass
        return {"success": True, "sid": msg.sid, "status": msg.status}
    except Exception as exc:
        logger.error(
            "SMS send error: code=%s status=%s — %s",
            getattr(exc, "code", None), getattr(exc, "status", None), exc,
        )
        try:
            from services.posthog_client import track_event
            track_event(f"company_{ta.company_id}", 'sms_failed', {
                'company_id': ta.company_id,
                'tenant_id':  ta.company_id,
                'error_code': getattr(exc, "code", type(exc).__name__),
                'source':     'twilio',
                'success':    False,
            })
        except Exception:
            pass
        from services.twilio_error_handler import twilio_friendly_error
        return {"success": False, "error": twilio_friendly_error(exc)}


def _match_keywords(body: str, keywords: list, match_type: str) -> bool:
    body_lower = body.lower()
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if match_type == "keyword_exact":
            if body_lower.strip() == kw_lower:
                return True
        elif match_type == "keyword_contains":
            if kw_lower in body_lower:
                return True
        elif match_type == "regex":
            try:
                if re.search(kw, body, re.IGNORECASE):
                    return True
            except re.error:
                pass
    return False


def _is_stop_message(body: str) -> bool:
    return body.lower().strip() in _STOP_KEYWORDS


def _apply_auto_reply_rules(conv, body: str, ta) -> bool:
    """
    Evaluate auto-reply rules in priority order.
    Returns True if any rule fired and a reply was sent.

    Special handling: after_hours rules are always evaluated even when a
    lower-precedence rule (e.g. first_contact tag) has already fired, so a
    customer texting for the first time outside business hours receives the
    after-hours message rather than the generic welcome message.

    Diagnostic logging is emitted at every decision point so failures can
    be traced from application logs alone (no Twilio console needed).
    """
    from models import AutoReplyRule, Contact

    _tag = f"[auto-reply company={ta.company_id} conv={conv.id} from={conv.from_number}]"

    if not ta.automation_enabled:
        logger.info("%s automation_enabled=False — skipping all rules", _tag)
        return False

    if conv.is_opted_out:
        logger.info("%s contact is opted-out — skipping rules", _tag)
        return False

    rules_raw = (
        AutoReplyRule.query
        .filter_by(company_id=ta.company_id, is_active=True)
        .order_by(AutoReplyRule.priority.desc())
        .all()
    )

    if not rules_raw:
        logger.info(
            "%s no active auto-reply rules found for company_id=%s",
            _tag, ta.company_id,
        )
        return False

    logger.info("%s evaluating %d active rules for body=%.60r", _tag, len(rules_raw), body)

    # Always evaluate after_hours rules first, then the rest by priority.
    rules = sorted(
        rules_raw,
        key=lambda r: (0 if r.trigger_type == "after_hours" else 1, -r.priority)
    )

    now_utc     = datetime.now(timezone.utc)
    reply_sent  = False
    in_business = _is_business_hours(ta.company_id)

    logger.info("%s business_hours=%s first_contact=%s", _tag, in_business, conv.is_first_contact)

    for rule in rules:
        matched = False
        skip_reason = None

        if rule.trigger_type == "always":
            matched = True
        elif rule.trigger_type == "stop_keyword":
            matched = _is_stop_message(body)
        elif rule.trigger_type == "first_contact":
            matched = bool(conv.is_first_contact)
            if not matched:
                skip_reason = "not first_contact"
        elif rule.trigger_type == "after_hours":
            if not ta.after_hours_sms_enabled:
                matched = False
                skip_reason = "after_hours_sms_enabled=False"
            elif in_business:
                matched = False
                skip_reason = "currently in business hours"
            else:
                matched = True
        elif rule.trigger_type in ("keyword_contains", "keyword_exact", "regex"):
            matched = _match_keywords(body, rule.keywords or [], rule.trigger_type)
            if not matched:
                skip_reason = f"keywords not matched (type={rule.trigger_type})"
        else:
            skip_reason = f"unknown trigger_type={rule.trigger_type!r}"

        if not matched:
            logger.debug(
                "%s rule id=%s name=%r type=%s NOT matched — %s",
                _tag, rule.id, rule.name, rule.trigger_type, skip_reason or "condition false",
            )
            continue

        # Schedule filter
        if rule.active_days:
            if now_utc.weekday() not in rule.active_days:
                logger.debug("%s rule id=%s skipped — wrong active_day %s", _tag, rule.id, now_utc.weekday())
                continue
        if rule.active_hours_start and rule.active_hours_end:
            try:
                sh, sm = [int(x) for x in rule.active_hours_start.split(":")]
                eh, em = [int(x) for x in rule.active_hours_end.split(":")]
                cur = now_utc.hour * 60 + now_utc.minute
                if not (sh * 60 + sm <= cur < eh * 60 + em):
                    logger.debug(
                        "%s rule id=%s skipped — outside active_hours %s-%s (now=%02d:%02d UTC)",
                        _tag, rule.id, rule.active_hours_start, rule.active_hours_end,
                        now_utc.hour, now_utc.minute,
                    )
                    continue
            except Exception:
                pass

        logger.info(
            "%s rule id=%s name=%r type=%s action=%s MATCHED",
            _tag, rule.id, rule.name, rule.trigger_type, rule.action,
        )

        # Execute action
        if rule.action == "opt_out" or rule.trigger_type == "stop_keyword":
            conv.is_opted_out = True
            if rule.response:
                result = _send_sms(ta, conv.from_number, rule.response,
                                   conversation_id=conv.id, is_auto_reply=True, rule_id=rule.id)
                logger.info("%s opt-out reply sent: success=%s", _tag, result.get("success"))
            rule.match_count = (rule.match_count or 0) + 1
            db.session.commit()
            return True  # hard stop — opted out

        elif rule.action == "tag" and rule.tag_value:
            tags = list(conv.tags or [])
            if rule.tag_value not in tags:
                tags.append(rule.tag_value)
                conv.tags = tags
            rule.match_count = (rule.match_count or 0) + 1
            db.session.commit()
            if rule.response and not reply_sent:
                result = _send_sms(ta, conv.from_number, rule.response,
                                   conversation_id=conv.id, is_auto_reply=True, rule_id=rule.id)
                reply_sent = result.get("success", False)
                logger.info("%s tag rule reply: success=%s err=%s", _tag, result.get("success"), result.get("error"))
            # Continue — after_hours must still have a chance to fire

        elif rule.action == "reply" and rule.response:
            if reply_sent:
                logger.debug("%s rule id=%s skipped — reply already sent", _tag, rule.id)
                continue
            result = _send_sms(ta, conv.from_number, rule.response,
                               conversation_id=conv.id, is_auto_reply=True, rule_id=rule.id)
            rule.match_count = (rule.match_count or 0) + 1
            db.session.commit()
            reply_sent = result.get("success", False)
            logger.info(
                "%s reply rule fired: success=%s sid=%s err=%s",
                _tag, result.get("success"), result.get("sid"), result.get("error"),
            )
            if rule.trigger_type != "after_hours":
                return reply_sent

        elif rule.action == "reply" and not rule.response:
            logger.warning("%s rule id=%s action=reply but response is empty — skipped", _tag, rule.id)

    if not reply_sent:
        logger.info("%s no rule produced a reply for this message", _tag)
    return reply_sent


def _capture_lead(conv, body: str, company_id: int):
    """Auto-create a Contact record from the conversation if one doesn't exist."""
    from models import Contact
    if conv.contact_id or conv.lead_captured:
        return
    contact = Contact(
        company_id=company_id,
        phone=conv.from_number,
        first_name=conv.contact_name or "",
        source="sms_inbound",
        tags="new-lead",
        is_active=True,
        is_subscribed=True,
    )
    db.session.add(contact)
    db.session.flush()
    conv.contact_id   = contact.id
    conv.lead_captured = True
    db.session.commit()
    logger.info("Lead captured from SMS: phone=%s", conv.from_number)


def _seed_default_rules(company_id: int):
    """Create the starter auto-reply rules for a new Twilio account."""
    from models import AutoReplyRule
    existing = AutoReplyRule.query.filter_by(company_id=company_id).count()
    if existing > 0:
        return

    defaults = [
        dict(name="Pricing Inquiry",     trigger_type="keyword_contains",
             keywords=["price", "pricing", "cost", "how much"],
             response="Thanks for asking! Our pricing starts at $99/month. Reply CALL to speak with someone or visit luxit.app/pricing for details.",
             priority=10, action="reply"),
        dict(name="Help Request",        trigger_type="keyword_contains",
             keywords=["help", "support", "issue", "problem"],
             response="We're here to help! Our support team will respond shortly. For urgent issues call us directly.",
             priority=9, action="reply"),
        dict(name="Callback Request",    trigger_type="keyword_contains",
             keywords=["call", "callback", "call back", "phone"],
             response="We'd love to talk! Reply with your preferred time and we'll schedule a callback for you.",
             priority=8, action="reply"),
        dict(name="Stop / Unsubscribe",  trigger_type="stop_keyword",
             keywords=[],
             response="You've been unsubscribed. Reply START anytime to re-subscribe.",
             priority=100, action="opt_out"),
        dict(name="First Time Contact",  trigger_type="first_contact",
             keywords=[],
             response="Welcome! Thanks for reaching out. We'll be in touch soon.",
             priority=5, action="tag", tag_value="new-lead"),
        dict(name="After Hours",         trigger_type="after_hours",
             keywords=[],
             response="Thanks for reaching out to Alavont Therapeutics. You messaged us after business hours. Please reach back during business hours: 11 AM to 1 AM, Sunday-Saturday.\n\nThank you!",
             priority=50, action="reply"),
    ]
    for d in defaults:
        rule = AutoReplyRule(company_id=company_id, **d)
        db.session.add(rule)
    db.session.commit()
    logger.info("Seeded %d default auto-reply rules for company %s", len(defaults), company_id)


def _seed_default_hours(company_id: int):
    """
    Seed business hours for a new company: 11:00 AM – 1:00 AM (next day),
    America/Los_Angeles, every day of the week.
    Times are stored in local LA time; _is_business_hours() handles the
    midnight-crossing comparison correctly.
    """
    from models import BusinessHours
    existing = BusinessHours.query.filter_by(company_id=company_id).count()
    if existing > 0:
        return
    for day in range(7):
        bh = BusinessHours(
            company_id=company_id,
            day_of_week=day,
            is_open=True,
            open_time="11:00",
            close_time="01:00",   # 1 AM next day — midnight-crossing
        )
        db.session.add(bh)
    db.session.commit()
    logger.info("Seeded default business hours (11 AM–1 AM LA) for company %s", company_id)


# ---------------------------------------------------------------------------
# Public webhook endpoints
# ---------------------------------------------------------------------------

@twilio_bp.route("/sms/inbound", methods=["POST"])
@csrf.exempt
def inbound_sms():
    """
    Twilio inbound SMS webhook.
    Set this URL in your Twilio Messaging Service or phone number config.

    Flow:
      1. Identify TwilioAccount by To number / MessagingServiceSid
      2. Validate Twilio signature (skipped on Replit dev)
      3. Log inbound message
      4. Handle STOP / START / HELP system keywords (always, regardless of rules)
      5. Run auto-reply rule engine for all other messages
    """
    from models import TwilioConversation, TwilioMessage

    data            = request.form
    from_number     = data.get("From", "").strip()
    to_number       = data.get("To",   "").strip()
    body            = (data.get("Body") or "").strip()
    twilio_sid      = data.get("MessageSid", "")
    msg_service_sid = data.get("MessagingServiceSid", "")
    num_media       = int(data.get("NumMedia", 0))

    logger.info(
        "Inbound SMS: from=%s to=%s sid=%s msgsvc=%s body=%.60r",
        from_number, to_number, twilio_sid, msg_service_sid, body,
    )

    # ── 1. Find TwilioPhoneNumber + TwilioAccount (Phase A multi-number) ────
    _pn, ta = _resolve_number(to_number, msg_service_sid)
    if not ta:
        logger.warning("Inbound SMS: no TwilioAccount found for to=%s", to_number)
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    # ── 2. Validate Twilio signature ───────────────────────────────────────
    if not _validate_twilio_signature(ta, "/twilio/sms/inbound"):
        abort(403)

    # ── 2b. Owner reply relay ──────────────────────────────────────────────
    # All messages FROM the forwarding number are relay commands — never
    # treat them as customer inbound messages.
    #
    # Supported formats:
    #   reply +1XXXXXXXXXX <message>   → send to specific customer
    #   r +1XXXXXXXXXX <message>       → shorthand for specific customer
    #   r <message>                    → send to most-recent customer
    if ta.sms_forward_to and from_number == ta.sms_forward_to:
        company_name = ta.company.name if ta.company else "LUXit"

        # reply +1XXX message  OR  r +1XXX message
        relay_match = re.match(
            r'^(?:reply|r)\s+(\+?1?\d{10,15})\s+(.+)$', body, re.IGNORECASE | re.DOTALL
        )
        if relay_match:
            target_number = relay_match.group(1).strip()
            if not target_number.startswith("+"):
                target_number = "+" + target_number
            relay_body = relay_match.group(2).strip()
            if not relay_body:
                logger.warning("Owner relay: empty message body to %s — ignored", target_number)
                _send_sms(ta, ta.sms_forward_to,
                          f"[{company_name}] Reply not sent — message was empty.")
                return '<Response></Response>', 200, {"Content-Type": "text/xml"}
            target_conv = _get_or_create_conversation(ta.company_id, target_number, to_number)
            result = _send_sms(ta, target_number, relay_body, conversation_id=target_conv.id)
            logger.info(
                "Owner relay: %s → %s (success=%s err=%s)",
                from_number, target_number, result.get("success"), result.get("error")
            )
            if not result.get("success"):
                _send_sms(ta, ta.sms_forward_to,
                          f"[{company_name}] Failed to send to {target_number}: {result.get('error')}")
            return '<Response></Response>', 200, {"Content-Type": "text/xml"}

        # r <message> — reply to the most recent customer conversation
        r_match = re.match(r'^r\s+(.+)$', body.strip(), re.IGNORECASE | re.DOTALL)
        if r_match:
            from models import TwilioConversation
            relay_body = r_match.group(1).strip()
            last_conv = (
                TwilioConversation.query
                .filter_by(company_id=ta.company_id)
                .filter(TwilioConversation.from_number != ta.sms_forward_to)
                .filter(TwilioConversation.is_opted_out.isnot(True))
                .order_by(TwilioConversation.last_message_at.desc())
                .first()
            )
            if last_conv:
                result = _send_sms(ta, last_conv.from_number, relay_body,
                                   conversation_id=last_conv.id)
                logger.info(
                    "Owner 'r' relay → %s (success=%s err=%s)",
                    last_conv.from_number, result.get("success"), result.get("error"),
                )
                if not result.get("success"):
                    _send_sms(ta, ta.sms_forward_to,
                              f"[{company_name}] Failed to reply to {last_conv.from_number}: {result.get('error')}")
            else:
                logger.warning("Owner 'r' relay: no recent conversation found")
                _send_sms(ta, ta.sms_forward_to,
                          f"[{company_name}] No recent customer conversation found to reply to.")
            return '<Response></Response>', 200, {"Content-Type": "text/xml"}

        # Unrecognised command — send help back to admin
        logger.info(
            "Forwarding-number message not matched as relay command (body=%.40r); sending help.", body
        )
        help_msg = (
            f"[{company_name} SMS Commands]\n"
            f"reply +1XXXXXXXXXX your message\n"
            f"  → Reply to a specific customer\n\n"
            f"r your message\n"
            f"  → Reply to the most recent customer\n\n"
            f"r +1XXXXXXXXXX your message\n"
            f"  → Shorthand reply to specific customer"
        )
        _send_sms(ta, ta.sms_forward_to, help_msg)
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    try:
        # ── 3a. Get or create conversation thread ──────────────────────────
        conv = _get_or_create_conversation(ta.company_id, from_number, to_number)

        # Idempotency: skip if already processed
        if twilio_sid and TwilioMessage.query.filter_by(twilio_sid=twilio_sid).first():
            return '<Response></Response>', 200, {"Content-Type": "text/xml"}

        # Collect media URLs
        media_urls = [data.get(f"MediaUrl{i}") for i in range(num_media)
                      if data.get(f"MediaUrl{i}")]

        # ── 3b. Save the inbound message ───────────────────────────────────
        msg_record = TwilioMessage(
            conversation_id=conv.id,
            company_id=ta.company_id,
            twilio_sid=twilio_sid,
            direction="inbound",
            from_number=from_number,
            to_number=to_number,
            body=body,
            status="received",
            media_urls=media_urls or None,
            raw_payload=dict(data),
        )
        db.session.add(msg_record)

        # Update conversation metadata
        conv.is_read              = False
        conv.last_message_at      = datetime.utcnow()
        conv.last_message_preview = body[:200] if body else "(media)"
        conv.message_count        = (conv.message_count or 0) + 1
        db.session.commit()

        # ── 4. System-level keyword handling ──────────────────────────────
        # These always fire and return a TwiML reply immediately.
        kw = body.upper().strip()

        if kw in _STOP_KEYWORDS:
            conv.is_opted_out   = True
            conv.sms_opt_out_at = datetime.utcnow()
            db.session.commit()
            logger.info("Opt-out keyword '%s' received: opted out %s", kw, from_number)
            return _twiml_message(_STOP_REPLY)

        if kw in _START_KEYWORDS:
            conv.is_opted_out  = False
            conv.sms_opt_in_at = datetime.utcnow()
            db.session.commit()
            logger.info("Opt-in keyword '%s' received: opted in %s", kw, from_number)
            return _twiml_message(_START_REPLY)

        if kw == "HELP":
            logger.info("HELP received from %s", from_number)
            return _twiml_message(_HELP_REPLY)

        # ── 5. SMS forwarding — always runs before auto-reply so exceptions
        #       in rule processing can never suppress the forward ────────────
        if ta.sms_forward_to and ta.sms_forwarding_enabled:
            try:
                company_name = (ta.company.name if ta.company else "LUXit")
                sender_label = conv.contact_name if conv.contact_name else from_number
                if conv.contact_name:
                    sender_label = f"{conv.contact_name} ({from_number})"
                else:
                    sender_label = from_number
                msg_text = body if body else "(media message)"
                fwd_body = (
                    f"New SMS for {company_name}\n"
                    f"From: {sender_label}\n"
                    f"Message:\n{msg_text}\n\n"
                    f"Reply using:\n"
                    f"reply {from_number} your message"
                )
                _send_sms(ta, ta.sms_forward_to, fwd_body, conversation_id=conv.id)
                logger.info(
                    "SMS forwarded: customer=%s → admin=%s company=%s",
                    from_number, ta.sms_forward_to, company_name,
                )
            except Exception as fwd_exc:
                logger.warning("SMS forward failed: %s", fwd_exc)

        # ── 6. Auto-reply rule engine ──────────────────────────────────────
        try:
            # Auto-capture lead on first contact
            if conv.is_first_contact:
                _capture_lead(conv, body, ta.company_id)

            # Run rules only if not opted out
            if not conv.is_opted_out:
                _apply_auto_reply_rules(conv, body, ta)
        except Exception as rule_exc:
            logger.exception("Error in auto-reply rule engine: %s", rule_exc)

        # Clear first-contact flag after first processed message
        if conv.is_first_contact:
            conv.is_first_contact = False
            db.session.commit()

        # PostHog — sms_received (safe metadata only, no message body)
        try:
            from services.posthog_client import track_event
            has_contact = bool(conv.contact_name)
            track_event(f"company_{ta.company_id}", 'sms_received', {
                'company_id':       ta.company_id,
                'tenant_id':        ta.company_id,
                'direction':        'inbound',
                'has_contact_match': has_contact,
                'message_length':   len(body) if body else 0,
                'has_media':        num_media > 0,
                'source':           'twilio',
            })
        except Exception:
            pass

        # Fire real-time SSE event + Web Push notification to subscribed users
        try:
            from inbox_pwa import _fire_push_notification, _push_sse_event
            sender = conv.contact_name or from_number
            _push_sse_event(ta.company_id, "new_message", {
                "conversation_id":      conv.id,
                "from_number":          from_number,
                "contact_name":         conv.contact_name or "",
                "body":                 (body or "(media)")[:200],
                "has_media":            num_media > 0,
                "last_message_at":      conv.last_message_at.isoformat() if conv.last_message_at else None,
                "last_message_preview": conv.last_message_preview or "",
            })
            _fire_push_notification(ta.company_id, conv, body or "(media)")
        except Exception as push_exc:
            logger.debug("Push/SSE notification skipped: %s", push_exc)

    except Exception as exc:
        logger.exception("Error processing inbound SMS: %s", exc)

    return '<Response></Response>', 200, {"Content-Type": "text/xml"}


@twilio_bp.route("/sms/status", methods=["POST"])
@csrf.exempt
def sms_status():
    """Twilio delivery status callback — updates message status."""
    from models import TwilioMessage

    data       = request.form
    sid        = data.get("MessageSid", "")
    status     = data.get("MessageStatus", "")
    error_code = data.get("ErrorCode")
    error_msg  = data.get("ErrorMessage")

    logger.info("SMS status: sid=%s status=%s", sid, status)

    if sid:
        msg = TwilioMessage.query.filter_by(twilio_sid=sid).first()
        if msg:
            msg.status         = status
            msg.error_code     = error_code
            msg.error_message  = error_msg
            msg.updated_at     = datetime.utcnow()
            db.session.commit()
            try:
                from services.posthog_client import track_event
                track_event(f"company_{msg.company_id}", 'sms_delivery_status_updated', {
                    'company_id':    msg.company_id,
                    'tenant_id':     msg.company_id,
                    'twilio_status': status,
                    'error_code':    error_code or None,
                    'source':        'twilio',
                    'success':       status in ('delivered', 'sent', 'read'),
                })
            except Exception:
                pass

    return "", 204


@twilio_bp.route("/voice/outbound-twiml", methods=["GET", "POST"])
@csrf.exempt
def outbound_call_twiml():
    """
    TwiML served when an outbound call is answered.

    If the call was placed TO the agent (call_forward_to flow), this TwiML
    greets the agent and dials through to the customer.
    If the call was placed directly to the customer, this TwiML plays a
    greeting so the call is not silent.
    """
    to_number = request.args.get("to", "")
    caller    = request.args.get("caller", "")
    safe_to   = to_number.replace("&", "").replace("<", "").replace(">", "")
    safe_cal  = caller.replace("&", "").replace("<", "").replace(">", "")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Say voice=\"Polly.Joanna\">Connecting your call. Please hold.</Say>\n"
        f'  <Dial callerId="{safe_cal}" timeout="30">{safe_to}</Dial>\n'
        "  <Say>The call could not be connected. Goodbye.</Say>\n"
        "</Response>"
    )
    return twiml, 200, {"Content-Type": "text/xml"}


@twilio_bp.route("/voice/inbound", methods=["POST"])
@csrf.exempt
def inbound_call():
    """
    Twilio voice webhook — business-hours-aware call routing.

    Business hours  + voice_forwarding_enabled  → Dial forwarding number (25 s timeout).
      No answer → /twilio/voice/no-answer → voicemail.
    After hours + after_hours_voicemail_enabled → Voicemail greeting + Record.
    Fallback: generic voicemail.
    """
    from models import TwilioCallLog

    data        = request.form
    from_number = data.get("From", "")
    to_number   = data.get("To",   "")
    call_sid    = data.get("CallSid", "")
    call_status = data.get("CallStatus", "")
    duration    = int(data.get("CallDuration") or 0)
    caller_name = data.get("CallerName", "")

    _pn, ta = _resolve_number(to_number)

    if not ta:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>Thank you for calling. Goodbye.</Say></Response>"""
        return twiml, 200, {"Content-Type": "text/xml"}

    # Validate Twilio signature
    if not _validate_twilio_signature(ta, "/twilio/voice/inbound"):
        abort(403)

    # Log the call
    existing = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
    if not existing and call_sid:
        log = TwilioCallLog(
            company_id=ta.company_id,
            twilio_sid=call_sid,
            direction="inbound",
            from_number=from_number,
            to_number=to_number,
            status=call_status or "ringing",
            duration=duration,
            caller_name=caller_name,
            raw_payload=dict(data),
        )
        db.session.add(log)
        db.session.commit()

    # Determine routing
    in_hours = _is_business_hours(ta.company_id)

    def _voicemail_twiml():
        greeting = (ta.voicemail_greeting_text or
                    "Thank you for calling. Please leave your name and message after the tone.")
        if ta.voicemail_greeting_audio_url:
            greeting_xml = f"<Play>{ta.voicemail_greeting_audio_url}</Play>"
        else:
            safe_greeting = (greeting
                             .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            greeting_xml = f"<Say>{safe_greeting}</Say>"
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Response>\n"
            f"  {greeting_xml}\n"
            f'  <Record maxLength="180" playBeep="true"\n'
            f'          recordingStatusCallback="/twilio/voice/recording"\n'
            f'          recordingStatusCallbackMethod="POST" />\n'
            f"  <Say>We did not receive a recording. Goodbye.</Say>\n"
            f"</Response>"
        )

    if in_hours and ta.voice_forwarding_enabled and ta.call_forward_to:
        caller_id = ta.from_phone or to_number
        twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Response>\n"
            f'  <Dial callerId="{caller_id}" timeout="25"\n'
            f'        action="/twilio/voice/no-answer" method="POST">\n'
            f"    <Number>{ta.call_forward_to}</Number>\n"
            f"  </Dial>\n"
            f"</Response>"
        )
    elif not in_hours and ta.after_hours_voicemail_enabled:
        twiml = _voicemail_twiml()
    elif ta.call_forward_to and ta.voice_forwarding_enabled:
        # Always forward regardless of hours when explicitly configured
        caller_id = ta.from_phone or to_number
        twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Response>\n"
            f'  <Dial callerId="{caller_id}" timeout="25"\n'
            f'        action="/twilio/voice/no-answer" method="POST">\n'
            f"    <Number>{ta.call_forward_to}</Number>\n"
            f"  </Dial>\n"
            f"</Response>"
        )
    else:
        twiml = _voicemail_twiml()

    logger.info(
        "Voice inbound: from=%s to=%s in_hours=%s fwd=%s",
        from_number, to_number, in_hours, ta.call_forward_to,
    )
    try:
        from services.posthog_client import track_event
        track_event(f"company_{ta.company_id}", 'call_received', {
            'company_id':  ta.company_id,
            'tenant_id':   ta.company_id,
            'in_hours':    in_hours,
            'forwarded':   bool(ta.call_forward_to and ta.voice_forwarding_enabled),
            'voicemail':   not (ta.call_forward_to and ta.voice_forwarding_enabled),
            'source':      'twilio',
        })
    except Exception:
        pass
    return twiml, 200, {"Content-Type": "text/xml"}


@twilio_bp.route("/voice/no-answer", methods=["POST"])
@csrf.exempt
def voice_no_answer():
    """
    Twilio Dial action callback — fired when the forwarded call is not answered.
    Routes the caller to voicemail.
    """
    from models import TwilioCallLog

    data        = request.form
    call_sid    = data.get("CallSid", "")
    dial_status = data.get("DialCallStatus", "")
    to_number   = data.get("To", "")

    _pn, ta = _resolve_number(to_number)

    logger.info("Voice no-answer: sid=%s dial_status=%s", call_sid, dial_status)

    # Update call log
    if call_sid:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            log.status = "no-answer"
            db.session.commit()

    # Send missed-call text if configured
    from_number = data.get("From", "")
    if ta and ta.missed_call_text and from_number:
        result = _send_sms(ta, from_number, ta.missed_call_text)
        if result.get("success"):
            log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
            if log and not log.missed_text_sent:
                log.missed_text_sent = True
                db.session.commit()

    # Fall through to voicemail
    if ta:
        greeting = (ta.voicemail_greeting_text or
                    "Thank you for calling. Please leave your name and message after the tone.")
        if ta.voicemail_greeting_audio_url:
            greeting_xml = f"<Play>{ta.voicemail_greeting_audio_url}</Play>"
        else:
            safe_greeting = (greeting
                             .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            greeting_xml = f"<Say>{safe_greeting}</Say>"
    else:
        greeting_xml = "<Say>Please leave a message after the tone.</Say>"

    twiml = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Response>\n"
        f"  {greeting_xml}\n"
        f'  <Record maxLength="180" playBeep="true"\n'
        f'          recordingStatusCallback="/twilio/voice/recording"\n'
        f'          recordingStatusCallbackMethod="POST" />\n'
        f"  <Say>We did not receive a recording. Goodbye.</Say>\n"
        f"</Response>"
    )
    return twiml, 200, {"Content-Type": "text/xml"}


@twilio_bp.route("/voice/recording", methods=["POST"])
@csrf.exempt
def voice_recording():
    """
    Twilio recording status callback — logs the voicemail recording URL.
    """
    from models import TwilioCallLog

    data           = request.form
    call_sid       = data.get("CallSid", "")
    recording_url  = data.get("RecordingUrl", "")
    recording_sid  = data.get("RecordingSid", "")
    recording_dur  = data.get("RecordingDuration", "0")

    logger.info(
        "Voicemail recording: sid=%s recording=%s dur=%ss url=%s",
        call_sid, recording_sid, recording_dur, recording_url,
    )

    if call_sid and recording_url:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            notes = f"Voicemail: {recording_url} ({recording_dur}s)"
            log.notes = notes
            log.status = "voicemail"
            db.session.commit()

    return "", 204


@twilio_bp.route("/voice/status", methods=["POST"])
@csrf.exempt
def voice_status():
    """Twilio voice status callback — updates call record."""
    from models import TwilioCallLog

    data        = request.form
    call_sid    = data.get("CallSid", "")
    call_status = data.get("CallStatus", "")
    duration    = int(data.get("CallDuration") or 0)

    logger.info("Voice status: sid=%s status=%s dur=%s", call_sid, call_status, duration)

    if call_sid:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            log.status   = call_status
            log.duration = duration
            db.session.commit()

    return "", 204


# ---------------------------------------------------------------------------
# Protected routes
# ---------------------------------------------------------------------------

@twilio_bp.route("/inbox")
@login_required
def inbox():
    from flask_login import current_user
    from models import TwilioConversation
    company = _get_company()
    if not company:
        flash("No company found.", "error")
        return redirect(url_for("main.dashboard"))

    ta = _get_twilio_account(company.id)
    status_filter = request.args.get("status", "all")   # all | unread | opted_out
    search = request.args.get("q", "").strip()

    q = TwilioConversation.query.filter_by(company_id=company.id)
    if status_filter == "unread":
        q = q.filter_by(is_read=False)
    elif status_filter == "opted_out":
        q = q.filter_by(is_opted_out=True)
    if search:
        q = q.filter(
            db.or_(
                TwilioConversation.from_number.ilike(f"%{search}%"),
                TwilioConversation.contact_name.ilike(f"%{search}%"),
                TwilioConversation.last_message_preview.ilike(f"%{search}%"),
            )
        )
    conversations = q.order_by(TwilioConversation.last_message_at.desc()).limit(100).all()
    unread_count  = TwilioConversation.query.filter_by(company_id=company.id, is_read=False).count()

    # Google Contacts status — server-side so bar always renders
    gc_connected = False
    gc_last_sync = None
    gc_contacts  = 0
    try:
        from services.google_contacts import get_token
        tok = get_token(current_user.id)
        if tok and tok.access_token:
            gc_connected = True
            gc_contacts  = tok.contacts_synced or 0
            gc_last_sync = tok.last_sync_at.strftime("%-d %b %H:%M") if tok.last_sync_at else None
    except Exception:
        pass

    return render_template(
        "twilio/inbox.html",
        conversations=conversations,
        unread_count=unread_count,
        ta=ta,
        status_filter=status_filter,
        search=search,
        gc_connected=gc_connected,
        gc_last_sync=gc_last_sync,
        gc_contacts=gc_contacts,
    )


@twilio_bp.route("/comms")
@login_required
def comms_hub():
    """Communications Hub — tabbed wrapper for SMS, Calls, Voicemail, Contacts, etc."""
    from flask_login import current_user
    from models import TwilioConversation, TwilioCallLog

    company = _get_company()
    if not company:
        flash("No company found.", "error")
        return redirect(url_for("main.dashboard"))

    ta           = _get_twilio_account(company.id)
    tab          = request.args.get("tab", "inbox")
    status_filter = request.args.get("status", "all")
    search       = request.args.get("q", "").strip()

    # ── Inbox data ───────────────────────────────────────────────────────
    q = TwilioConversation.query.filter_by(company_id=company.id)
    if status_filter == "unread":
        q = q.filter_by(is_read=False)
    elif status_filter == "opted_out":
        q = q.filter_by(is_opted_out=True)
    if search and tab == "inbox":
        q = q.filter(
            db.or_(
                TwilioConversation.from_number.ilike(f"%{search}%"),
                TwilioConversation.contact_name.ilike(f"%{search}%"),
                TwilioConversation.last_message_preview.ilike(f"%{search}%"),
            )
        )
    conversations = q.order_by(TwilioConversation.last_message_at.desc()).limit(100).all()
    unread_count  = TwilioConversation.query.filter_by(company_id=company.id, is_read=False).count()

    # ── Call log data ─────────────────────────────────────────────────────
    calls              = []
    missed_calls_count = 0
    try:
        calls = (
            TwilioCallLog.query
            .filter_by(company_id=company.id)
            .order_by(TwilioCallLog.created_at.desc())
            .limit(100).all()
        )
        missed_calls_count = TwilioCallLog.query.filter_by(
            company_id=company.id, status="no-answer"
        ).count()
    except Exception:
        pass

    # ── Google Contacts bar ───────────────────────────────────────────────
    gc_connected = False
    gc_last_sync = None
    gc_contacts  = 0
    try:
        from services.google_contacts import get_token
        tok = get_token(current_user.id)
        if tok and tok.access_token:
            gc_connected = True
            gc_contacts  = tok.contacts_synced or 0
            gc_last_sync = tok.last_sync_at.strftime("%-d %b %H:%M") if tok.last_sync_at else None
    except Exception:
        pass

    is_admin = getattr(current_user, "is_admin", False) or getattr(current_user, "is_platform_admin", False)

    return render_template(
        "twilio/comms_hub.html",
        tab=tab,
        conversations=conversations,
        unread_count=unread_count,
        ta=ta,
        status_filter=status_filter,
        search=search,
        calls=calls,
        missed_calls_count=missed_calls_count,
        gc_connected=gc_connected,
        gc_last_sync=gc_last_sync,
        gc_contacts=gc_contacts,
        is_admin=is_admin,
    )


@twilio_bp.route("/comms/settings")
@login_required
def comms_settings():
    """Communications Settings — admin-only configuration hub."""
    from flask_login import current_user
    from models import AutoReplyRule, UserCompanyAccess, User

    is_admin = getattr(current_user, "is_admin", False) or getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        abort(403)

    company = _get_company()
    if not company:
        return redirect(url_for("main.dashboard"))

    ta = _get_twilio_account(company.id)

    # Users + their communications access
    try:
        access_rows = (
            UserCompanyAccess.query
            .filter_by(company_id=company.id)
            .all()
        )
        users_with_access = []
        for acc in access_rows:
            u = User.query.get(acc.user_id)
            if u:
                users_with_access.append({
                    "user":   u,
                    "access": acc,
                })
    except Exception:
        users_with_access = []

    # Auto-reply rules summary
    rules_count = 0
    try:
        rules_count = AutoReplyRule.query.filter_by(company_id=company.id, is_active=True).count()
    except Exception:
        pass

    return render_template(
        "twilio/comms_settings.html",
        ta=ta,
        company=company,
        users_with_access=users_with_access,
        rules_count=rules_count,
    )


@twilio_bp.route("/comms/settings/user/<int:user_id>", methods=["POST"])
@login_required
def comms_settings_user(user_id):
    """
    AJAX endpoint — admin saves per-user Communications Hub feature toggles.
    Accepts JSON body with any subset of the toggle fields.
    Returns JSON {success, message}.
    """
    from flask_login import current_user
    from models import UserCompanyAccess

    is_admin = getattr(current_user, "is_admin", False) or getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        return jsonify({"success": False, "message": "Admin access required."}), 403

    company = _get_company()
    if not company:
        return jsonify({"success": False, "message": "No company found."}), 400

    acc = UserCompanyAccess.query.filter_by(
        user_id=user_id, company_id=company.id
    ).first()
    if not acc:
        return jsonify({"success": False, "message": "User not found in this company."}), 404

    data = request.get_json(silent=True) or {}

    BOOL_FIELDS = [
        "comms_hub_enabled",
        "pwa_access_enabled",
        "calls_enabled",
        "sms_enabled",
        "voicemail_enabled",
        "ai_comms_enabled",
        "forwarding_enabled",
        "communications_license",
        "can_access_mobile_inbox",
    ]
    STR_FIELDS = ["assigned_number", "number_type"]

    changed = []
    for f in BOOL_FIELDS:
        if f in data:
            val = bool(data[f])
            setattr(acc, f, val)
            changed.append(f"{f}={val}")

    for f in STR_FIELDS:
        if f in data:
            val = (data[f] or "").strip() or None
            setattr(acc, f, val)
            changed.append(f"{f}={val!r}")

    if changed:
        try:
            db.session.commit()
            logger.info(
                "comms_settings_user: company=%s user=%s changed=%s by admin=%s",
                company.id, user_id, ", ".join(changed), current_user.id,
            )
        except Exception as exc:
            db.session.rollback()
            logger.error("comms_settings_user save error: %s", exc)
            return jsonify({"success": False, "message": str(exc)}), 500

    return jsonify({"success": True, "message": "Saved.", "changed": changed})


@twilio_bp.route("/inbox/<int:conv_id>")
@login_required
def conversation(conv_id):
    from models import TwilioConversation
    company = _get_company()
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
    ta   = _get_twilio_account(company.id)

    # Mark as read
    if not conv.is_read:
        conv.is_read = True
        db.session.commit()

    messages = conv.messages.order_by(db.text("twilio_message.created_at")).all()
    return render_template(
        "twilio/conversation.html",
        conv=conv,
        messages=messages,
        ta=ta,
    )


@twilio_bp.route("/send", methods=["POST"])
@login_required
def send_message():
    company = _get_company()
    ta = _get_twilio_account(company.id)
    if not ta or not ta.is_configured:
        return jsonify({"success": False, "error": "Twilio not configured for this company."})

    payload   = request.get_json() or {}
    to_number = (payload.get("to") or "").strip()
    body      = (payload.get("body") or "").strip()
    conv_id   = payload.get("conversation_id")

    if not to_number or not body:
        return jsonify({"success": False, "error": "to and body are required."})

    conv = None
    if conv_id:
        from models import TwilioConversation
        conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first()

    if not conv:
        conv = _get_or_create_conversation(company.id, to_number, ta.from_phone or "")

    # Update conversation preview
    conv.last_message_at      = datetime.utcnow()
    conv.last_message_preview = f"You: {body[:150]}"
    conv.message_count        = (conv.message_count or 0) + 1
    db.session.commit()

    result = _send_sms(ta, to_number, body, conversation_id=conv.id)
    return jsonify(result)


@twilio_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    import traceback
    from models import TwilioAccount
    company = _get_company()
    if company is None:
        flash("No company found. Please contact your administrator.", "danger")
        logger.error("SMS settings: no company for user %s\n%s",
                     current_user.id, traceback.format_stack())
        return redirect(url_for("main.dashboard") if "main" in current_app.blueprints else "/")
    ta = _get_twilio_account(company.id)

    if request.method == "POST":
        f = request.form
        account_sid           = f.get("account_sid", "").strip()
        auth_token            = f.get("auth_token", "").strip()
        messaging_service_sid = f.get("messaging_service_sid", "").strip()
        from_phone            = f.get("from_phone", "").strip()
        webhook_base_url      = f.get("webhook_base_url", "").strip()
        sms_fallback_url      = f.get("sms_fallback_url", "").strip()
        voice_fallback_url    = f.get("voice_fallback_url", "").strip()
        automation_enabled    = f.get("automation_enabled") == "on"
        ai_mode               = f.get("ai_mode", "off")
        ai_system_prompt      = f.get("ai_system_prompt", "").strip()
        missed_call_text      = f.get("missed_call_text", "").strip()
        after_hours_text      = f.get("after_hours_text", "").strip()
        sms_forward_to        = f.get("sms_forward_to", "").strip()
        call_forward_to       = f.get("call_forward_to", "").strip()
        # Routing feature toggles
        sms_forwarding_enabled        = f.get("sms_forwarding_enabled") == "on"
        voice_forwarding_enabled      = f.get("voice_forwarding_enabled") == "on"
        after_hours_sms_enabled       = f.get("after_hours_sms_enabled") == "on"
        after_hours_voicemail_enabled = f.get("after_hours_voicemail_enabled") == "on"
        voicemail_greeting_text       = f.get("voicemail_greeting_text", "").strip()
        voicemail_greeting_audio_url  = f.get("voicemail_greeting_audio_url", "").strip()

        if not ta:
            ta = TwilioAccount(company_id=company.id)
            db.session.add(ta)

        if account_sid:
            ta.set_account_sid(account_sid)
        if auth_token:
            ta.set_auth_token(auth_token)
        ta.messaging_service_sid       = messaging_service_sid or ta.messaging_service_sid
        ta.from_phone                  = from_phone or ta.from_phone
        ta.webhook_base_url            = webhook_base_url
        ta.sms_fallback_url            = sms_fallback_url or None
        ta.voice_fallback_url          = voice_fallback_url or None
        ta.automation_enabled          = automation_enabled
        ta.ai_mode                     = ai_mode
        ta.ai_system_prompt            = ai_system_prompt
        ta.missed_call_text            = missed_call_text
        ta.after_hours_text            = after_hours_text
        ta.sms_forward_to              = sms_forward_to or None
        ta.call_forward_to             = call_forward_to or None
        ta.sms_forwarding_enabled      = sms_forwarding_enabled
        ta.voice_forwarding_enabled    = voice_forwarding_enabled
        ta.after_hours_sms_enabled     = after_hours_sms_enabled
        ta.after_hours_voicemail_enabled = after_hours_voicemail_enabled
        ta.voicemail_greeting_text     = voicemail_greeting_text or None
        ta.voicemail_greeting_audio_url = voicemail_greeting_audio_url or None
        ta.is_active                   = True
        db.session.commit()

        # Seed default rules and business hours on first save
        _seed_default_rules(company.id)
        _seed_default_hours(company.id)

        # Auto-configure Twilio Messaging Service webhook
        _auto_configure_twilio_webhook(ta)

        flash("Twilio settings saved successfully!", "success")
        return redirect(url_for("twilio.settings"))

    return render_template("twilio/settings.html", ta=ta, company=company)


@twilio_bp.route("/settings/upload-voicemail", methods=["POST"])
@login_required
def upload_voicemail():
    """Upload an MP3 for the voicemail greeting and return its public URL."""
    import uuid, os
    from flask import current_app
    company = _get_company()
    if company is None:
        return jsonify({"error": "No company"}), 400

    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400

    allowed = {".mp3", ".wav", ".ogg", ".m4a"}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed:
        return jsonify({"error": f"File type {ext} not allowed"}), 400

    audio_dir = os.path.join(current_app.root_path, "static", "audio")
    os.makedirs(audio_dir, exist_ok=True)
    filename = f"voicemail-{company.id}-{uuid.uuid4().hex[:8]}{ext}"
    save_path = os.path.join(audio_dir, filename)
    f.save(save_path)

    base = request.host_url.rstrip("/")
    public_url = f"{base}/static/audio/{filename}"
    return jsonify({"url": public_url})


@twilio_bp.route("/fallback", methods=["GET", "POST"])
def twilio_fallback():
    """
    Twilio calls this URL when the primary SMS or Voice webhook fails to respond.
    Returns a valid TwiML response so the call/message is handled gracefully,
    logs the failure so it appears in the error dashboard, and never returns
    a non-2xx status (which would cause Twilio to retry and double-log).
    """
    try:
        from flask import request as _req
        error_code = _req.values.get("ErrorCode", "unknown")
        error_url  = _req.values.get("ErrorUrl", "")
        call_sid   = _req.values.get("CallSid")
        msg_sid    = _req.values.get("MessageSid")
        logger.error(
            "Twilio fallback triggered: ErrorCode=%s ErrorUrl=%s CallSid=%s MessageSid=%s",
            error_code, error_url, call_sid, msg_sid,
        )
        # Try to log to the error dashboard
        try:
            from models import AppError
            from extensions import db as _db
            _db.session.add(AppError(
                error_type="TwilioFallback",
                error_message=f"Primary webhook failed (ErrorCode {error_code}). URL: {error_url}",
                severity="high",
                source="twilio_fallback",
                context=dict(_req.values),
            ))
            _db.session.commit()
        except Exception:
            pass
    except Exception as log_exc:
        logger.warning("Fallback logging error: %s", log_exc)

    # Return valid TwiML — voice gets a brief message; SMS gets an empty response
    from flask import Response
    if request.values.get("CallSid"):
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Say>We're sorry, we're experiencing technical difficulties. "
            "Please try your call again shortly. Goodbye.</Say>"
            "<Hangup/>"
            "</Response>"
        )
    else:
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

    return Response(twiml, mimetype="text/xml", status=200)


def _auto_configure_twilio_webhook(ta):
    """Push the inbound SMS webhook URL to Twilio Messaging Service via REST API."""
    try:
        if not ta or not ta.messaging_service_sid:
            return
        sid = ta.get_account_sid()
        token = ta.get_auth_token()
        if not sid or not token:
            return

        base = (ta.webhook_base_url or "https://luxit.app").rstrip("/")
        inbound_url  = f"{base}/twilio/sms/inbound"
        status_url   = f"{base}/twilio/sms/status"
        sms_fallback = ta.sms_fallback_url or f"{base}/twilio/fallback"

        from twilio.rest import Client
        client = Client(sid, token)
        client.messaging.v1.services(ta.messaging_service_sid).update(
            inbound_request_url=inbound_url,
            inbound_method="POST",
            fallback_url=sms_fallback,
            fallback_method="POST",
            status_callback=status_url,
            use_inbound_webhook_on_number=False,
        )
        logger.info("Auto-configured Twilio webhook → %s", inbound_url)
    except Exception as exc:
        logger.warning("Could not auto-configure Twilio webhook: %s", exc)


@twilio_bp.route("/rules")
@login_required
def rules():
    from models import AutoReplyRule
    company = _get_company()
    ta      = _get_twilio_account(company.id)
    rule_list = (
        AutoReplyRule.query
        .filter_by(company_id=company.id)
        .order_by(AutoReplyRule.priority.desc(), AutoReplyRule.name)
        .all()
    )
    return render_template("twilio/rules.html", rules=rule_list, ta=ta, days=DAYS)


@twilio_bp.route("/rules/create", methods=["POST"])
@login_required
def create_rule():
    from models import AutoReplyRule
    company = _get_company()
    f = request.form

    keywords_raw = f.get("keywords", "").strip()
    keywords     = [k.strip() for k in keywords_raw.split(",") if k.strip()]

    active_days_raw = request.form.getlist("active_days")
    active_days     = [int(d) for d in active_days_raw] if active_days_raw else None

    rule = AutoReplyRule(
        company_id=company.id,
        name=f.get("name", "Unnamed Rule"),
        trigger_type=f.get("trigger_type", "keyword_contains"),
        keywords=keywords,
        response=f.get("response", "").strip(),
        action=f.get("action", "reply"),
        forward_to=f.get("forward_to", "").strip(),
        tag_value=f.get("tag_value", "").strip(),
        priority=int(f.get("priority") or 0),
        active_days=active_days,
        active_hours_start=f.get("active_hours_start") or None,
        active_hours_end=f.get("active_hours_end") or None,
        is_active=True,
    )
    db.session.add(rule)
    db.session.commit()
    flash(f'Rule "{rule.name}" created.', "success")
    return redirect(url_for("twilio.rules"))


@twilio_bp.route("/rules/<int:rule_id>", methods=["GET"])
@login_required
def get_rule(rule_id):
    """Return rule fields as JSON for the edit modal."""
    from models import AutoReplyRule
    company = _get_company()
    rule = AutoReplyRule.query.filter_by(id=rule_id, company_id=company.id).first_or_404()
    return jsonify({
        "id":                rule.id,
        "name":              rule.name,
        "trigger_type":      rule.trigger_type,
        "keywords":          ", ".join(rule.keywords) if rule.keywords else "",
        "response":          rule.response or "",
        "action":            rule.action,
        "forward_to":        rule.forward_to or "",
        "tag_value":         rule.tag_value or "",
        "priority":          rule.priority,
        "active_days":       rule.active_days or [],
        "active_hours_start": rule.active_hours_start or "",
        "active_hours_end":   rule.active_hours_end or "",
        "is_active":         rule.is_active,
    })


@twilio_bp.route("/rules/<int:rule_id>/edit", methods=["POST"])
@login_required
def edit_rule(rule_id):
    """Update an existing auto-reply rule."""
    from models import AutoReplyRule
    company = _get_company()
    rule = AutoReplyRule.query.filter_by(id=rule_id, company_id=company.id).first_or_404()
    f = request.form

    response_text = f.get("response", "").strip()
    action = f.get("action", "reply")
    if action == "reply" and not response_text:
        logger.warning("Edit rule %d failed: response message is blank", rule_id)
        return jsonify({"success": False, "error": "Response message cannot be blank for Reply action."}), 400

    keywords_raw = f.get("keywords", "").strip()
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

    # Duplicate keyword check (same trigger type, different rule, active)
    trigger_type = f.get("trigger_type", rule.trigger_type)
    if keywords and trigger_type in ("keyword_contains", "keyword_exact"):
        for kw in keywords:
            conflict = (
                AutoReplyRule.query
                .filter_by(company_id=company.id, trigger_type=trigger_type, is_active=True)
                .filter(AutoReplyRule.id != rule_id)
                .all()
            )
            for other in conflict:
                if other.keywords and kw.lower() in [k.lower() for k in other.keywords]:
                    logger.warning(
                        "Edit rule %d: duplicate keyword '%s' conflicts with rule %d", rule_id, kw, other.id
                    )
                    return jsonify({
                        "success": False,
                        "error": f"Keyword '{kw}' already used in active rule \"{other.name}\".",
                        "warning": True,
                    }), 409

    active_days_raw = request.form.getlist("active_days")
    active_days = [int(d) for d in active_days_raw] if active_days_raw else None

    rule.name               = f.get("name", rule.name).strip() or rule.name
    rule.trigger_type       = trigger_type
    rule.keywords           = keywords
    rule.response           = response_text
    rule.action             = action
    rule.forward_to         = f.get("forward_to", "").strip()
    rule.tag_value          = f.get("tag_value", "").strip()
    rule.priority           = int(f.get("priority") or rule.priority)
    rule.active_days        = active_days
    rule.active_hours_start = f.get("active_hours_start") or None
    rule.active_hours_end   = f.get("active_hours_end") or None

    db.session.commit()
    logger.info("Auto-reply rule %d (%s) updated by user", rule_id, rule.name)
    return jsonify({"success": True, "name": rule.name})


@twilio_bp.route("/rules/<int:rule_id>/toggle", methods=["POST"])
@login_required
def toggle_rule(rule_id):
    from models import AutoReplyRule
    company = _get_company()
    rule = AutoReplyRule.query.filter_by(id=rule_id, company_id=company.id).first_or_404()
    rule.is_active = not rule.is_active
    db.session.commit()
    return jsonify({"success": True, "is_active": rule.is_active})


@twilio_bp.route("/rules/<int:rule_id>/delete", methods=["POST"])
@login_required
def delete_rule(rule_id):
    from models import AutoReplyRule
    company = _get_company()
    rule = AutoReplyRule.query.filter_by(id=rule_id, company_id=company.id).first_or_404()
    name = rule.name
    db.session.delete(rule)
    db.session.commit()
    flash(f'Rule "{name}" deleted.', "success")
    return redirect(url_for("twilio.rules"))


@twilio_bp.route("/hours", methods=["GET", "POST"])
@login_required
def business_hours():
    from models import BusinessHours
    company = _get_company()
    ta = _get_twilio_account(company.id)

    if request.method == "POST":
        timezone_val = request.form.get("timezone", "America/Chicago")
        for day in range(7):
            bh = BusinessHours.query.filter_by(
                company_id=company.id, day_of_week=day
            ).first()
            if not bh:
                bh = BusinessHours(company_id=company.id, day_of_week=day)
                db.session.add(bh)
            bh.is_open    = request.form.get(f"open_{day}") == "on"
            bh.open_time  = request.form.get(f"open_time_{day}", "09:00")
            bh.close_time = request.form.get(f"close_time_{day}", "17:00")
            bh.timezone   = timezone_val
        db.session.commit()
        flash("Business hours saved.", "success")
        return redirect(url_for("twilio.business_hours"))

    hours_rows = (
        BusinessHours.query
        .filter_by(company_id=company.id)
        .order_by(BusinessHours.day_of_week)
        .all()
    )
    # Ensure 7 rows exist
    if len(hours_rows) < 7:
        _seed_default_hours(company.id)
        hours_rows = (
            BusinessHours.query
            .filter_by(company_id=company.id)
            .order_by(BusinessHours.day_of_week)
            .all()
        )

    return render_template(
        "twilio/hours.html",
        hours=hours_rows,
        ta=ta,
        days=DAYS,
    )


@twilio_bp.route("/calls")
@login_required
def calls():
    from models import TwilioCallLog
    company = _get_company()
    ta      = _get_twilio_account(company.id)
    call_list = (
        TwilioCallLog.query
        .filter_by(company_id=company.id)
        .order_by(TwilioCallLog.created_at.desc())
        .limit(200)
        .all()
    )
    return render_template("twilio/calls.html", calls=call_list, ta=ta)


@twilio_bp.route("/analytics")
@login_required
def analytics():
    from models import TwilioMessage, TwilioConversation, TwilioCallLog, AutoReplyRule
    company = _get_company()
    ta      = _get_twilio_account(company.id)

    total_inbound   = TwilioMessage.query.filter_by(company_id=company.id, direction="inbound").count()
    total_outbound  = TwilioMessage.query.filter_by(company_id=company.id, direction="outbound").count()
    total_delivered = TwilioMessage.query.filter_by(company_id=company.id, status="delivered").count()
    total_failed    = TwilioMessage.query.filter_by(company_id=company.id, status="failed").count()
    auto_replies    = TwilioMessage.query.filter_by(company_id=company.id, is_auto_reply=True).count()
    total_convs     = TwilioConversation.query.filter_by(company_id=company.id).count()
    opted_out       = TwilioConversation.query.filter_by(company_id=company.id, is_opted_out=True).count()
    leads_captured  = TwilioConversation.query.filter_by(company_id=company.id, lead_captured=True).count()
    total_calls     = TwilioCallLog.query.filter_by(company_id=company.id).count()
    missed_calls    = TwilioCallLog.query.filter_by(company_id=company.id, status="no-answer").count()

    top_rules = (
        AutoReplyRule.query
        .filter_by(company_id=company.id)
        .order_by(AutoReplyRule.match_count.desc())
        .limit(5)
        .all()
    )

    delivery_rate = 0
    if total_outbound > 0:
        delivery_rate = round(total_delivered / total_outbound * 100, 1)

    stats = dict(
        total_inbound=total_inbound,
        total_outbound=total_outbound,
        total_delivered=total_delivered,
        total_failed=total_failed,
        delivery_rate=delivery_rate,
        auto_replies=auto_replies,
        total_convs=total_convs,
        opted_out=opted_out,
        leads_captured=leads_captured,
        total_calls=total_calls,
        missed_calls=missed_calls,
    )
    return render_template(
        "twilio/analytics.html",
        stats=stats,
        top_rules=top_rules,
        ta=ta,
    )


@twilio_bp.route("/conversation/<int:conv_id>/tag", methods=["POST"])
@login_required
def tag_conversation(conv_id):
    from models import TwilioConversation
    company = _get_company()
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
    tag = (request.get_json() or {}).get("tag", "").strip()
    if tag:
        tags = list(conv.tags or [])
        if tag not in tags:
            tags.append(tag)
            conv.tags = tags
            db.session.commit()
    return jsonify({"success": True, "tags": conv.tags})


@twilio_bp.route("/conversation/<int:conv_id>/note", methods=["POST"])
@login_required
def save_note(conv_id):
    from models import TwilioConversation
    company = _get_company()
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
    conv.notes = (request.get_json() or {}).get("notes", "")
    db.session.commit()
    return jsonify({"success": True})


# ── Google Contacts sync ──────────────────────────────────────────────────

@twilio_bp.route("/google-contacts/connect")
@login_required
def google_contacts_connect():
    """Redirect user to Google OAuth consent screen."""
    from flask_login import current_user
    from services.google_contacts import get_auth_url
    import os
    if not os.environ.get("GOOGLE_CLIENT_ID"):
        flash("GOOGLE_CLIENT_ID is not configured. Add it in Settings → Secrets.", "danger")
        return redirect(url_for("twilio.inbox"))
    return redirect(get_auth_url(state=str(current_user.id)))


@twilio_bp.route("/google-contacts/callback")
@login_required
def google_contacts_callback():
    """Handle Google OAuth callback, exchange code, run initial sync."""
    from flask_login import current_user
    from services.google_contacts import exchange_code, sync_contacts
    code  = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        flash(f"Google sign-in was cancelled or failed: {error or 'no code'}", "warning")
        return redirect(url_for("twilio.inbox"))
    try:
        exchange_code(current_user.id, code)
    except Exception as exc:
        flash(f"Google Contacts connection failed: {exc}", "danger")
        return redirect(url_for("twilio.inbox"))
    # Run first sync immediately
    company = _get_company()
    result  = sync_contacts(current_user.id, company.id)
    if result.get("error"):
        flash(f"Connected but sync failed: {result['error']}", "warning")
    else:
        flash(
            f"Google Contacts connected. {result['synced']} contacts fetched, "
            f"{result['matched']} inbox names updated.",
            "success",
        )
    return redirect(url_for("twilio.inbox"))


@twilio_bp.route("/google-contacts/sync", methods=["POST"])
@login_required
def google_contacts_sync():
    """Manually trigger a contact sync. Returns JSON."""
    from flask_login import current_user
    from services.google_contacts import sync_contacts
    company = _get_company()
    result  = sync_contacts(current_user.id, company.id)
    return jsonify(result)


@twilio_bp.route("/google-contacts/status")
@login_required
def google_contacts_status():
    """Return connection status as JSON."""
    from flask_login import current_user
    from services.google_contacts import get_token
    tok = get_token(current_user.id)
    if not tok:
        return jsonify({"connected": False})
    return jsonify({
        "connected":       True,
        "last_sync_at":    tok.last_sync_at.strftime("%b %-d %H:%M") if tok.last_sync_at else None,
        "contacts_synced": tok.contacts_synced or 0,
    })


@twilio_bp.route("/google-contacts/disconnect", methods=["POST"])
@login_required
def google_contacts_disconnect():
    """Revoke and delete Google token."""
    from flask_login import current_user
    from services.google_contacts import disconnect
    disconnect(current_user.id)
    flash("Google Contacts disconnected.", "info")
    return redirect(url_for("twilio.inbox"))


# ===========================================================================
# Phase A — Number Management Admin UI
# ===========================================================================

@twilio_bp.route("/numbers")
@login_required
def number_management():
    """
    Admin UI: list and manage all Twilio phone numbers for the company.
    GET /twilio/numbers
    """
    from models import TwilioPhoneNumber, TwilioAccount

    is_admin = getattr(current_user, "is_admin", False) or \
               getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        abort(403)

    company = _get_company()
    if not company:
        flash("No company found.", "error")
        return redirect(url_for("main.dashboard"))

    ta = _get_twilio_account(company.id)

    # Seed TwilioPhoneNumber rows from legacy TwilioAccount.from_phone (idempotent)
    _seed_phone_numbers_from_accounts()

    numbers = (
        TwilioPhoneNumber.query
        .filter_by(company_id=company.id)
        .order_by(TwilioPhoneNumber.is_primary.desc(), TwilioPhoneNumber.created_at)
        .all()
    )

    return render_template(
        "twilio/numbers.html",
        numbers=numbers,
        ta=ta,
        company=company,
    )


@twilio_bp.route("/numbers/add", methods=["POST"])
@login_required
def number_add():
    """Register a new phone number in the DB."""
    from models import TwilioPhoneNumber

    is_admin = getattr(current_user, "is_admin", False) or \
               getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        abort(403)

    company = _get_company()
    if not company:
        abort(400)

    phone_number   = request.form.get("phone_number", "").strip()
    friendly_name  = request.form.get("friendly_name", "").strip()
    app_assignment = request.form.get("app_assignment", "luxit").strip()
    number_type    = request.form.get("number_type", "local").strip()
    twilio_sid     = request.form.get("twilio_sid", "").strip()

    if not phone_number:
        flash("Phone number is required.", "error")
        return redirect(url_for("twilio.number_management"))

    # Normalise: ensure E.164 with +
    if not phone_number.startswith("+"):
        phone_number = "+" + phone_number.lstrip("+")

    existing = TwilioPhoneNumber.query.filter_by(phone_number=phone_number).first()
    if existing:
        flash(f"{phone_number} is already registered.", "warning")
        return redirect(url_for("twilio.number_management"))

    ta = _get_twilio_account(company.id)

    pn = TwilioPhoneNumber(
        company_id        = company.id,
        twilio_account_id = ta.id if ta else None,
        phone_number      = phone_number,
        friendly_name     = friendly_name or phone_number,
        app_assignment    = app_assignment,
        number_type       = number_type,
        twilio_sid        = twilio_sid or None,
        sms_enabled       = True,
        voice_enabled     = True,
        is_active         = True,
        is_primary        = False,
    )
    db.session.add(pn)
    db.session.commit()
    flash(f"Number {phone_number} added successfully.", "success")
    return redirect(url_for("twilio.number_management"))


@twilio_bp.route("/numbers/<int:number_id>/edit", methods=["POST"])
@login_required
def number_edit(number_id):
    """Update routing settings for a phone number."""
    from models import TwilioPhoneNumber

    is_admin = getattr(current_user, "is_admin", False) or \
               getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        abort(403)

    company = _get_company()
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id).first_or_404()

    pn.friendly_name          = request.form.get("friendly_name", pn.friendly_name).strip()
    pn.app_assignment         = request.form.get("app_assignment", pn.app_assignment).strip()
    pn.number_type            = request.form.get("number_type", pn.number_type).strip()
    pn.sms_enabled            = request.form.get("sms_enabled") == "1"
    pn.voice_enabled          = request.form.get("voice_enabled") == "1"
    pn.sms_forward_to         = request.form.get("sms_forward_to", "").strip() or None
    pn.sms_forwarding_enabled = request.form.get("sms_forwarding_enabled") == "1"
    pn.auto_reply_enabled     = request.form.get("auto_reply_enabled") == "1"
    pn.call_forward_to        = request.form.get("call_forward_to", "").strip() or None
    pn.voice_forwarding_enabled = request.form.get("voice_forwarding_enabled") == "1"
    pn.missed_call_text       = request.form.get("missed_call_text", "").strip() or None
    pn.voicemail_greeting_text = request.form.get("voicemail_greeting_text", "").strip() or None
    pn.after_hours_sms_enabled = request.form.get("after_hours_sms_enabled") == "1"
    pn.after_hours_voicemail_enabled = request.form.get("after_hours_voicemail_enabled") == "1"
    pn.notes                  = request.form.get("notes", "").strip() or None

    # Primary number toggle (only one per company)
    if request.form.get("is_primary") == "1":
        TwilioPhoneNumber.query.filter_by(company_id=company.id).update({"is_primary": False})
        pn.is_primary = True

    db.session.commit()
    flash(f"Number {pn.phone_number} updated.", "success")
    return redirect(url_for("twilio.number_management"))


@twilio_bp.route("/numbers/<int:number_id>/toggle", methods=["POST"])
@login_required
def number_toggle(number_id):
    """Enable / disable a phone number."""
    from models import TwilioPhoneNumber

    is_admin = getattr(current_user, "is_admin", False) or \
               getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        abort(403)

    company = _get_company()
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id).first_or_404()
    pn.is_active = not pn.is_active
    db.session.commit()
    state = "enabled" if pn.is_active else "disabled"
    flash(f"{pn.phone_number} {state}.", "success")
    return redirect(url_for("twilio.number_management"))


@twilio_bp.route("/numbers/<int:number_id>/delete", methods=["POST"])
@login_required
def number_delete(number_id):
    """Remove a phone number record from the DB (does NOT release from Twilio)."""
    from models import TwilioPhoneNumber

    is_admin = getattr(current_user, "is_admin", False) or \
               getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        abort(403)

    company = _get_company()
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id).first_or_404()
    num = pn.phone_number
    db.session.delete(pn)
    db.session.commit()
    flash(f"Number {num} removed from the database.", "info")
    return redirect(url_for("twilio.number_management"))


@twilio_bp.route("/numbers/seed", methods=["POST"])
@login_required
def number_seed():
    """Manually trigger migration of TwilioAccount.from_phone -> TwilioPhoneNumber."""
    is_admin = getattr(current_user, "is_admin", False) or \
               getattr(current_user, "is_platform_admin", False)
    if not is_admin:
        abort(403)
    _seed_phone_numbers_from_accounts()
    flash("Phone numbers synced from account settings.", "success")
    return redirect(url_for("twilio.number_management"))
