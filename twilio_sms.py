"""
Multi-tenant Twilio SMS & Call Platform — LUXit
Blueprint: twilio_bp  (url_prefix=/twilio)

Public webhook endpoints (no login):
  POST /twilio/sms/inbound   — Twilio inbound SMS webhook
  POST /twilio/sms           — legacy/current Twilio inbound SMS webhook alias
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

import hashlib
import json
import logging
import os
import re
import html
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

REQUIRED_AFTER_HOURS_SMS_COPY = "Thanks for reaching out. Our business hours are daily from 2 PM to 2 AM. We’ll respond as soon as we’re back online."
DEFAULT_PHONE_BUSINESS_HOURS = {str(i): {"is_open": True, "open": "14:00", "close": "02:00"} for i in range(7)}

from flask import (
    Blueprint, abort, flash, jsonify, redirect,
    render_template, request, url_for, g, has_request_context
)
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException
from sqlalchemy import text

from extensions import db, csrf

logger = logging.getLogger(__name__)

twilio_bp = Blueprint("twilio", __name__, url_prefix="/twilio")
api_twilio_bp = Blueprint("api_twilio", __name__, url_prefix="/api/twilio")


@twilio_bp.before_request
def _guard_sms_feature():
    """Allow authenticated users with full-app access through Twilio routes.
    Twilio webhook callbacks are always permitted (no auth required).
    Inbox-only users are redirected to the Mobile Inbox PWA instead."""
    from flask import request, redirect, render_template

    # Twilio server-to-server webhooks — never require auth or feature flags
    _WEBHOOK_PATHS = {
        "/twilio/sms",
        "/twilio/sms/inbound",
        "/twilio/sms/status",
        "/twilio/voice/inbound",
        "/twilio/voice/no-answer",
        "/twilio/voice/recording",
        "/twilio/voice/status",
    }
    if request.path in _WEBHOOK_PATHS or request.path.startswith("/twilio/google-contacts/"):
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


def _phone_digits(number: str) -> str:
    """Return only digits for format-insensitive Twilio number matching."""
    return re.sub(r"\D", "", number or "")


def _safe_phone_ref(number: str) -> str:
    """Non-reversible phone correlation value for operational logs."""
    normalized = _normalize_e164(number)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12] if normalized else "missing"


def _normalize_e164(number: str) -> str:
    """Normalize common US phone formats to E.164 for inbound Twilio lookups."""
    raw = (number or "").strip()
    if not raw:
        return ""
    digits = _phone_digits(raw)
    if raw.startswith("+") and raw[1:].isdigit():
        return raw
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return raw


class _NumberScopedTwilioConfig:
    """TwilioAccount view with TwilioPhoneNumber routing overrides applied."""

    _PN_OVERRIDES = (
        "sms_forward_to",
        "sms_forwarding_enabled",
        "auto_reply_enabled",
        "number_auto_reply_text",
        "after_hours_text",
        "business_hours_auto_reply_enabled",
        "business_hours_auto_reply_text",
        "call_forward_to",
        "call_forwarding_enabled",
        "call_forwarding_number",
        "voice_forwarding_enabled",
        "voicemail_greeting_text",
        "voicemail_greeting_audio_url",
        "missed_call_text",
        "after_hours_cooldown_minutes",
        "after_hours_sms_enabled",
        "after_hours_voicemail_enabled",
        "business_hours",
        "timezone",
        "during_hours_route",
        "after_hours_route",
        "browser_calling_enabled",
        "cell_callback_enabled",
        "wifi_only",
        "mobile_data_allowed",
        "fallback_behavior",
        "caller_id_display_name",
    )

    def __init__(self, account, phone_number=None):
        self._account = account
        self.phone_number = phone_number
        for key in self._PN_OVERRIDES:
            value = getattr(phone_number, key, None) if phone_number else None
            setattr(self, key, value if value not in (None, "") else getattr(account, key, None))
        self.company_id = phone_number.company_id if phone_number else account.company_id
        self.from_phone = (
            phone_number.phone_number
            if phone_number and getattr(phone_number, "phone_number", None)
            else account.from_phone
        )
        self.messaging_service_sid = account.messaging_service_sid

    def __getattr__(self, name):
        return getattr(self._account, name)


def _effective_twilio_config(ta, phone_number=None):
    if not ta:
        return None
    return _NumberScopedTwilioConfig(ta, phone_number) if phone_number else ta


def _resolve_number(to_number: str, msg_service_sid: str = ""):
    """
    Phase A: Resolve inbound Twilio webhook to (TwilioPhoneNumber, TwilioAccount).

    Lookup priority:
      1. TwilioPhoneNumber.phone_number == normalized to_number
      2. TwilioAccount.messaging_service_sid == msg_service_sid (messaging service)
      3. TwilioAccount.from_phone == normalized to_number (legacy single-number accounts)

    Returns (pn_or_None, ta_or_None).
    """
    from models import TwilioPhoneNumber, TwilioAccount

    normalized_to = _normalize_e164(to_number)

    # ── 1. Look up by phone number in new multi-number table ─────────────────
    # Twilio and operators can represent the same US number as +19165989519,
    # 19165989519, (916) 598-9519, or 9165989519.  Route by the actual To
    # number first, using digit-equivalent matching before considering any
    # Messaging Service SID fallback.
    pn = None
    if normalized_to:
        candidate_digits = _phone_digits(normalized_to)
        for candidate in TwilioPhoneNumber.query.filter_by(is_active=True).all():
            stored_digits = _phone_digits(candidate.phone_number)
            if stored_digits and (
                stored_digits == candidate_digits
                or stored_digits.endswith(candidate_digits)
                or candidate_digits.endswith(stored_digits)
            ):
                pn = candidate
                break

    if pn:
        # Prefer the account linked directly; fall back to any account for company
        ta = (db.session.get(TwilioAccount, pn.twilio_account_id)
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
    if normalized_to:
        ta = TwilioAccount.query.filter_by(from_phone=normalized_to).first()
        if ta:
            logger.debug("_resolve_number: matched legacy from_phone ta=%s", ta.id)
            return None, ta

    logger.warning("_resolve_number: no Twilio number owner found to_ref=%s", _safe_phone_ref(to_number))
    return None, None


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
                    business_hours          = dict(DEFAULT_PHONE_BUSINESS_HOURS),
                    timezone                = "America/Los_Angeles",
                    during_hours_route      = "ring_pwa",
                    after_hours_route       = "voicemail",
                    browser_calling_enabled = True,
                    cell_callback_enabled   = True,
                    wifi_only               = False,
                    mobile_data_allowed     = True,
                    fallback_behavior       = "cell_callback",
                    voicemail_greeting_text = ta.voicemail_greeting_text,
                    voicemail_greeting_audio_url = ta.voicemail_greeting_audio_url,
                    missed_call_text        = ta.missed_call_text,
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
_START_KEYWORDS = {"start", "unstop", "yes", "subscribe", "join"}

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
             .replace("\u00a0", " ")
    )

def _sanitize_body(text: str) -> str:
    return _safe_sms_text(text).replace("\x00", "")

def _validate_twilio_signature(ta, endpoint_path: str = "/twilio/sms/inbound") -> bool:
    """
    Validate the X-Twilio-Signature header for any Twilio webhook endpoint.

    Production validation is fail-closed. Tests may explicitly disable strict
    validation when exercising business logic without a Twilio signature.
    """
    configured = os.environ.get("TWILIO_STRICT_SIGNATURE")
    strict = (
        configured.lower() not in ("0", "false", "no")
        if configured is not None
        else os.environ.get("FLASK_ENV") != "testing"
    )

    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        logger.error("Twilio signature validation failed category=validator_missing")
        return not strict

    try:
        from services.provider_config import get_provider_config
        _platform_token = get_provider_config("twilio", "platform", "auth_token")
    except Exception:
        _platform_token = None
    token = (ta.get_auth_token() if ta else None) or _platform_token
    if not token:
        logger.error("Twilio signature validation failed category=auth_token_missing")
        return not strict

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


def _is_business_hours(company_id: int, at_time=None, phone_config=None) -> bool:
    """Return True when *at_time* falls within configured hours.

    Prefer per-phone-number business hours when present. Tenant PhoneSettings and
    legacy BusinessHours rows remain as defaults/backward compatibility.
    """
    from models import BusinessHours, PhoneSettings

    settings = PhoneSettings.query.filter_by(company_id=company_id).first()
    phone_hours = getattr(phone_config, "business_hours", None) if phone_config else None
    phone_tz = getattr(phone_config, "timezone", None) if phone_config else None
    tz_name = phone_tz or (
        settings.timezone
        if settings and getattr(settings, "timezone", None)
        else os.environ.get("DEFAULT_PHONE_TIMEZONE")
    ) or "America/Los_Angeles"

    try:
        local_tz = ZoneInfo(tz_name)
    except Exception:
        local_tz = _LA

    now_local = (at_time or datetime.now(timezone.utc)).astimezone(local_tz)
    day = now_local.weekday()   # 0=Mon … 6=Sun

    hours_cfg = phone_hours if phone_hours else (settings.business_hours if settings and getattr(settings, "business_hours", None) else None)
    if hours_cfg:
        day_key = str(day)
        day_cfg = (
            hours_cfg.get(day_key)
            or hours_cfg.get(DAYS[day].lower())
        )
        if day_cfg:
            if day_cfg.get("closed") or day_cfg.get("is_open") is False:
                return False

            open_time = day_cfg.get("open") or day_cfg.get("open_time") or "09:00"
            close_time = day_cfg.get("close") or day_cfg.get("close_time") or "17:00"

            try:
                oh, om = [int(x) for x in open_time.split(":")[:2]]
                ch, cm = [int(x) for x in close_time.split(":")[:2]]
                current = now_local.hour * 60 + now_local.minute
                opens = oh * 60 + om
                closes = ch * 60 + cm

                if closes <= opens:
                    return current >= opens or current < closes
                return opens <= current < closes
            except Exception:
                return True
    bh = BusinessHours.query.filter_by(company_id=company_id, day_of_week=day).first()
    if not bh or not bh.is_open:
        return False
    try:
        open_h,  open_m  = [int(x) for x in bh.open_time.split(":")]
        close_h, close_m = [int(x) for x in bh.close_time.split(":")]
        current  = now_local.hour * 60 + now_local.minute
        opens    = open_h  * 60 + open_m
        closes   = close_h * 60 + close_m
        if closes <= opens:
            # Midnight-crossing (e.g. 23:00 – 01:00 or 11:00 – 01:00 next day)
            return current >= opens or current < closes
        return opens <= current < closes
    except Exception:
        return True


def _pwa_voice_identity(company_id: int) -> str:
    """Shared non-guessable tenant identity for registered PWA CSR browsers."""
    from services.phone_identity import pwa_voice_identity
    return pwa_voice_identity(company_id)



def _business_hours_from_form(form):
    hours = {}
    for i in range(7):
        hours[str(i)] = {
            "is_open": form.get(f"bh_{i}_open") == "1",
            "open": form.get(f"bh_{i}_start") or "09:00",
            "close": form.get(f"bh_{i}_end") or "17:00",
        }
    return hours

def _can_send_call_auto_sms(company_id: int, to_number: str, *, cooldown_hours: int = 24) -> tuple[bool, str]:
    """Guard call-triggered automated SMS for opt-out compliance and cooldown."""
    from models import TwilioConversation, TwilioCallLog, TwilioPhoneNumber, UserCompanyAccess, User, AutoReplyRule, VoiceVoicemailMessage, MarketingAuditLog, PWADevice
    if not to_number:
        return False, "missing_number"
    conv = TwilioConversation.query.filter_by(
        company_id=company_id,
        from_number=to_number,
    ).first()
    if conv and conv.is_opted_out:
        return False, "opted_out"
    cutoff = datetime.utcnow() - timedelta(hours=cooldown_hours)
    recent = TwilioCallLog.query.filter(
        TwilioCallLog.company_id == company_id,
        TwilioCallLog.from_number == to_number,
        TwilioCallLog.missed_text_sent.is_(True),
        TwilioCallLog.created_at >= cutoff,
    ).first()
    if recent:
        return False, "cooldown"
    return True, "ok"


def _get_or_create_conversation(
    company_id: int, from_number: str, to_number: str, phone_number_id: int = None,
    *, create_contact: bool = True,
):
    from models import TwilioConversation
    from services.contact_audience import upsert_contact_from_source
    from services.google_contacts import normalize_phone, _all_forms
    from services.contact_resolver import resolve_contact_identity

    forms = _all_forms(from_number)
    to_forms = [value for value in {_normalize_e164(to_number), to_number} if value]
    conv_query = TwilioConversation.query.filter(
        TwilioConversation.company_id == company_id,
        TwilioConversation.from_number.in_(forms or [from_number]),
    )
    if phone_number_id is not None and hasattr(TwilioConversation, "phone_number_id"):
        conv = conv_query.filter(TwilioConversation.phone_number_id == phone_number_id).first()
    else:
        conv = None
    if not conv and to_forms:
        conv = conv_query.filter(TwilioConversation.to_number.in_(to_forms)).first()
    if not conv:
        conv = conv_query.first()

    contact = None
    contact_name = contact_source = None
    if create_contact:
        contact = upsert_contact_from_source(
            company_id,
            phone=from_number,
            source_channel="inbound_sms",
            source_phone_number=to_number,
            source_provider="twilio",
            source_context="inbound_message",
        )
        resolved = resolve_contact_identity(
            company_id,
            phone=from_number,
            contact_id=contact.id,
            allow_enrichment=True,
        )
        contact_name = (
            None if resolved.safe_display_name == "Name needed"
            else resolved.safe_display_name
        )
        contact_source = resolved.name_source

    if not conv:
        norm = normalize_phone(from_number)
        logger.debug(
            "_get_or_create_conversation: from_ref=%s company=%s contact_match=%s source=%s",
            _safe_phone_ref(norm or from_number), company_id, contact.id if contact else None, contact_source,
        )
        conv = TwilioConversation(
            company_id=company_id,
            from_number=normalize_phone(from_number) or from_number,
            to_number=_normalize_e164(to_number) or to_number,
            phone_number_id=phone_number_id,
            contact_id=contact.id if contact else None,
            contact_name=contact_name,
            contact_source=contact_source,
            is_first_contact=True,
        )
        db.session.add(conv)
        db.session.flush()
    else:
        if contact and not conv.contact_id:
            conv.contact_id = contact.id
        if contact_name and conv.contact_name != contact_name:
            conv.contact_name = contact_name
            conv.contact_source = contact_source
        elif not contact_name and conv.contact_source not in {
            "customer_confirmed", "pwa_verified", "manual", "google_contacts",
        }:
            conv.contact_name = None
            conv.contact_source = None
        normalized_to = _normalize_e164(to_number) or to_number
        if normalized_to and conv.to_number != normalized_to:
            conv.to_number = normalized_to
        if phone_number_id is not None and getattr(conv, "phone_number_id", None) != phone_number_id:
            conv.phone_number_id = phone_number_id
    return conv


def resolve_sms_sender(inbound_message) -> str:
    """Canonical conversational SMS sender resolver.

    For every reply/forward/auto-reply associated with an inbound message, the
    sender must be the business line that received that inbound message:
    ``sender = inbound_message.to_number``.
    """
    if isinstance(inbound_message, dict):
        return (inbound_message.get("to_number") or inbound_message.get("To") or "").strip()
    return (getattr(inbound_message, "to_number", None) or getattr(inbound_message, "To", None) or "").strip()


def sendConversationSms(conversation_id: int, message: str, *,
                        to_number: str = None, twilio_account=None,
                        is_auto_reply: bool = False, rule_id: int = None,
                        persist_record: bool = True,
                        effect_type: str | None = None,
                        idempotency_key: str | None = None,
                        bypass_outbox: bool = False) -> dict:
    """Single entry point for all conversational SMS sends.

    Every outbound reply/forward/auto-reply resolves its sender from the
    inbound conversation via ``resolve_sms_sender(conversation)``. No caller is
    allowed to provide a parallel sender override, MessagingServiceSid, or
    fallback sender pool.
    """
    from models import TwilioMessage, TwilioConversation, TwilioPhoneNumber
    conv = db.session.get(TwilioConversation, conversation_id) if conversation_id else None
    if not conv:
        return {"success": False, "error": "Conversation is required for SMS send."}
    if (
        not bypass_outbox
        and has_request_context()
        and getattr(g, "sms_outbox_inbound_message_id", None)
    ):
        from services.sms_outbox import enqueue_sms_intent
        intent = enqueue_sms_intent(
            inbound_message_id=g.sms_outbox_inbound_message_id,
            company_id=conv.company_id,
            conversation_id=conv.id,
            effect_type=effect_type or (
                f"rule_reply:{rule_id}" if rule_id else
                ("auto_reply" if is_auto_reply else "conversation_reply")
            ),
            to_number=to_number,
            body=_sanitize_body(message),
            is_auto_reply=is_auto_reply,
            rule_id=rule_id,
            idempotency_key=idempotency_key,
        )
        return {"success": True, "queued": True, "intent_id": intent.id, "status": intent.status}
    ta = twilio_account or _get_twilio_account(conv.company_id)
    if not ta:
        return {"success": False, "error": "Twilio account is not configured for this conversation."}
    to_number = to_number or conv.from_number
    try:
        from services.license_service import PHONE_PWA_FEATURE, has_feature
        if getattr(ta, "company_id", None) and not has_feature(ta.company_id, PHONE_PWA_FEATURE):
            logger.warning("Outbound SMS blocked by inactive license: company_id=%s to=%s auto_reply=%s", ta.company_id, to_number, is_auto_reply)
            return {"success": False, "error": "Phone/PWA Communications license is not active.", "license_blocked": True}
    except Exception:
        logger.exception("License check failed before outbound SMS; blocking send for safety")
        return {"success": False, "error": "License check failed.", "license_blocked": True}
    client = _build_client(ta)
    if not client:
        return {"success": False, "error": "Twilio client could not be created."}
    body = _sanitize_body(message)
    try:
        canonical_sender = resolve_sms_sender(conv)
        if not canonical_sender:
            return {"success": False, "error": "No inbound phone line is available for this SMS reply."}
        pn = TwilioPhoneNumber.query.filter_by(company_id=ta.company_id, phone_number=canonical_sender, is_active=True).first()
        if not pn:
            return {"success": False, "error": "Sender phone line is not active for this company."}
        if conv and pn.id != conv.phone_number_id and conv.phone_number_id is not None:
            return {"success": False, "error": "Sender phone line does not match the conversation."}
        kwargs = {
            "body": body,
            "to":   to_number,
            "from_": canonical_sender,
        }

        msg = client.messages.create(**kwargs)
        provider_status = getattr(msg, "status", None) or "sent"

        if persist_record:
            record = TwilioMessage(
                conversation_id=conversation_id,
                company_id=ta.company_id,
                twilio_sid=msg.sid,
                direction="outbound",
                from_number=canonical_sender,
                to_number=to_number,
                body=body,
                status="sent",
                is_auto_reply=is_auto_reply,
                rule_id=rule_id,
                raw_payload={"provider_status": provider_status},
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
        return {
            "success": True, "sid": msg.sid, "status": "sent",
            "provider_status": provider_status, "from_number": canonical_sender,
        }
    except Exception as exc:
        logger.error(
            "SMS send error: code=%s status=%s — %s",
            getattr(exc, "code", None), getattr(exc, "status", None), exc,
        )
        if persist_record:
            try:
                failed_record = TwilioMessage(
                    conversation_id=conversation_id,
                    company_id=ta.company_id,
                    twilio_sid=None,
                    direction="outbound",
                    from_number=canonical_sender if "canonical_sender" in locals() else None,
                    to_number=to_number,
                    body=body,
                    status="failed",
                    error_message=str(exc),
                    is_auto_reply=is_auto_reply,
                    rule_id=rule_id,
                )
                db.session.add(failed_record)
                db.session.commit()
            except Exception:
                db.session.rollback()
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
        http_status = getattr(exc, "status", None)
        delivery_class = (
            "retryable" if http_status == 429 or (http_status and http_status >= 500)
            else ("terminal" if http_status and 400 <= http_status < 500 else "ambiguous")
        )
        return {
            "success": False,
            "error": twilio_friendly_error(exc),
            "error_code": str(getattr(exc, "code", None) or type(exc).__name__),
            "delivery_class": delivery_class,
        }


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


def _normalized_keyword(body: str) -> str:
    return (body or "").strip().lower()

def _is_stop_message(body: str) -> bool:
    return _normalized_keyword(body) in _STOP_KEYWORDS

def _write_sms_compliance_audit(company_id, action, phone_number, keyword, contact_id=None, conversation_id=None):
    try:
        from models import IntegrationAuditLog
        log = IntegrationAuditLog(
            company_id=company_id,
            service_slug="sms_compliance",
            action=action,
            changes={
                "phone_last4": (phone_number or "")[-4:],
                "keyword": keyword,
                "contact_id": contact_id,
                "conversation_id": conversation_id,
            },
        )
        db.session.add(log)
    except Exception:
        logger.exception("Failed to write SMS compliance audit log")

def _update_contact_sms_consent(company_id, phone_number, opted_in, source):
    from models import Contact
    from services.google_contacts import _all_forms
    contact = None
    for form in _all_forms(phone_number):
        contact = Contact.query.filter_by(company_id=company_id, phone=form, is_active=True).first()
        if contact:
            break
    if contact:
        now = datetime.utcnow()
        contact.sms_marketing_opt_in = bool(opted_in)
        contact.sms_consent_status = "opted_in" if opted_in else "opted_out"
        if opted_in:
            contact.sms_marketing_opt_in_at = now
            contact.sms_marketing_opt_in_source = source
            contact.sms_opt_out_at = None
        else:
            contact.sms_opt_out_at = now
    return contact



def _clean_after_hours_text(value) -> str | None:
    text = (value or "").strip()
    return text or None


def _after_hours_legacy_candidates(company_id: int, phone_number_id: int | None = None):
    from models import PhoneSettings, TwilioAccount, TwilioPhoneNumber
    if phone_number_id is not None:
        pn = db.session.get(TwilioPhoneNumber, phone_number_id)
        if pn and pn.company_id == company_id:
            text = _clean_after_hours_text(getattr(pn, "after_hours_text", None))
            if text:
                yield "migrated number setting", text, pn
    ps = PhoneSettings.query.filter_by(company_id=company_id).first()
    text = _clean_after_hours_text(getattr(ps, "after_hours_sms_body", None)) if ps else None
    if text:
        yield "migrated phone settings", text, ps
    ta = TwilioAccount.query.filter_by(company_id=company_id).first()
    text = _clean_after_hours_text(getattr(ta, "after_hours_text", None)) if ta else None
    if text:
        yield "migrated account setting", text, ta
    yield "fallback", REQUIRED_AFTER_HOURS_SMS_COPY, None


def _after_hours_auto_reply_query(company_id: int, phone_number_id: int | None = None):
    from models import AutoReplyRule
    q = AutoReplyRule.query.filter_by(
        company_id=company_id,
        trigger_type="after_hours",
        action="reply",
        is_active=True,
    )
    if phone_number_id is not None:
        q = q.filter(db.or_(
            AutoReplyRule.phone_number_id == phone_number_id,
            AutoReplyRule.phone_number_id.is_(None),
        ))
    else:
        q = q.filter(AutoReplyRule.phone_number_id.is_(None))
    return q.order_by(AutoReplyRule.phone_number_id.desc().nullslast(), AutoReplyRule.priority.desc(), AutoReplyRule.id.asc())


def _current_after_hours_effective_message(company_id: int, phone_number_id: int | None = None) -> dict:
    rule = _after_hours_auto_reply_query(company_id, phone_number_id).first()
    if rule and _clean_after_hours_text(rule.response):
        return {
            "message": rule.response.strip(),
            "source": "number-specific AutoReplyRule" if rule.phone_number_id else "company-wide AutoReplyRule",
            "row": f"auto_reply_rule.id={rule.id}",
            "field": "response",
            "rule": rule,
        }
    source, text, obj = next(_after_hours_legacy_candidates(company_id, phone_number_id))
    table = getattr(obj, "__tablename__", None) if obj is not None else None
    row = f"{table}.id={getattr(obj, 'id', None)}" if table and getattr(obj, "id", None) is not None else "constant.REQUIRED_AFTER_HOURS_SMS_COPY"
    field = "after_hours_text" if table in {"twilio_phone_number", "twilio_account"} else ("after_hours_sms_body" if table == "phone_settings" else "REQUIRED_AFTER_HOURS_SMS_COPY")
    return {"message": text, "source": source if source == "fallback" else source, "row": row, "field": field, "rule": None}


def _ensure_visible_after_hours_rule(company_id: int, phone_number_id: int | None = None):
    """Surface legacy after-hours copy as an editable AutoReplyRule for the UI/webhook."""
    from models import AutoReplyRule
    existing = _after_hours_auto_reply_query(company_id, phone_number_id).first()
    if existing:
        return existing
    source, text, obj = next(_after_hours_legacy_candidates(company_id, phone_number_id))
    if not text:
        return None
    rule = AutoReplyRule(
        company_id=company_id,
        phone_number_id=phone_number_id,
        name="After Hours" if phone_number_id else "After Hours (Company-wide)",
        trigger_type="after_hours",
        keywords=[],
        response=text,
        action="reply",
        priority=50,
        is_active=True,
    )
    db.session.add(rule)
    db.session.flush()
    logger.info("Migrated visible after-hours AutoReplyRule id=%s from %s", rule.id, source)
    return rule

def _after_hours_rule_response(company_id: int, phone_number_id: int | None = None) -> str | None:
    return _current_after_hours_effective_message(company_id, phone_number_id).get("message")


def _send_number_configured_auto_reply(conv, ta, *, in_business=None) -> bool:
    """Fallback per-number auto replies when no AutoReplyRule sends a reply."""
    if not getattr(ta, "auto_reply_enabled", True) or conv.is_opted_out:
        return False
    if in_business is None:
        try:
            in_business = _is_business_hours(ta.company_id, phone_config=ta)
        except TypeError:
            in_business = _is_business_hours(ta.company_id)
    response_body = None
    if in_business:
        response_body = getattr(ta, "business_hours_auto_reply_text", None) if getattr(ta, "business_hours_auto_reply_enabled", False) else None
        response_body = response_body or getattr(ta, "number_auto_reply_text", None)
    else:
        response_body = getattr(ta, "after_hours_text", None) if getattr(ta, "after_hours_sms_enabled", False) else None
    if not response_body:
        return False
    cooldown_minutes = getattr(ta, "after_hours_cooldown_minutes", None)
    if cooldown_minutes is None:
        cooldown_minutes = 720
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(cooldown_minutes))
    from models import TwilioMessage
    recent = TwilioMessage.query.filter(
        TwilioMessage.conversation_id == conv.id,
        TwilioMessage.is_auto_reply == True,
        TwilioMessage.created_at >= cutoff,
    ).first()
    if recent:
        logger.info("[auto-reply company=%s conv=%s] configured number auto reply skipped due to cooldown", ta.company_id, conv.id)
        return False
    result = sendConversationSms(conv.id, response_body, twilio_account=ta, is_auto_reply=True)
    logger.info("[auto-reply company=%s conv=%s] configured number auto reply sent success=%s", ta.company_id, conv.id, result.get("success"))
    return bool(result.get("success"))


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
        .filter(db.or_(
            AutoReplyRule.phone_number_id.is_(None),
            AutoReplyRule.phone_number_id == getattr(getattr(ta, "phone_number", None), "id", None),
        ))
        .order_by(AutoReplyRule.priority.desc())
        .all()
    )

    if not rules_raw:
        logger.info(
            "%s no active auto-reply rules found for company_id=%s; checking per-number configured replies",
            _tag, ta.company_id,
        )
        try:
            in_business = _is_business_hours(ta.company_id, phone_config=ta)
        except TypeError:
            in_business = _is_business_hours(ta.company_id)
        return _send_number_configured_auto_reply(conv, ta, in_business=in_business)

    logger.info("%s evaluating %d active rules for body=%.60r", _tag, len(rules_raw), body)

    # Always evaluate after_hours rules first, then the rest by priority.
    rules = sorted(
        rules_raw,
        key=lambda r: (0 if r.trigger_type == "after_hours" else 1, -r.priority)
    )

    now_utc     = datetime.now(timezone.utc)
    reply_sent  = False
    
    try:
        in_business = _is_business_hours(ta.company_id, phone_config=ta)
    except TypeError:
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
                cooldown_minutes = getattr(ta, "after_hours_cooldown_minutes", None)
                if cooldown_minutes is None:
                    cooldown_minutes = 720
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(cooldown_minutes))
                from models import TwilioMessage
                recent = TwilioMessage.query.filter(
                    TwilioMessage.conversation_id == conv.id,
                    TwilioMessage.is_auto_reply == True,
                    TwilioMessage.created_at >= cutoff,
                ).first()
                if recent:
                    matched = False
                    skip_reason = f"after-hours cooldown active ({cooldown_minutes}m)"
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
                result = sendConversationSms(conv.id, rule.response, twilio_account=ta, is_auto_reply=True, rule_id=rule.id)
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
                result = sendConversationSms(conv.id, rule.response, twilio_account=ta, is_auto_reply=True, rule_id=rule.id)
                reply_sent = result.get("success", False)
                logger.info("%s tag rule reply: success=%s err=%s", _tag, result.get("success"), result.get("error"))
            # Continue — after_hours must still have a chance to fire

        elif rule.action == "reply" and (rule.response or rule.trigger_type == "after_hours"):
            response_body = rule.response
            if rule.trigger_type == "after_hours":
                response_body = _after_hours_rule_response(ta.company_id, getattr(getattr(ta, "phone_number", None), "id", None)) or rule.response or REQUIRED_AFTER_HOURS_SMS_COPY
            if reply_sent:
                logger.debug("%s rule id=%s skipped — reply already sent", _tag, rule.id)
                continue
            result = sendConversationSms(conv.id, response_body, twilio_account=ta, is_auto_reply=True, rule_id=rule.id)
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
        logger.info("%s no rule produced a reply for this message; checking canonical per-number/company fallback", _tag)
        reply_sent = _send_number_configured_auto_reply(conv, ta, in_business=in_business)
    return reply_sent


def _capture_lead(conv, body: str, company_id: int):
    """Create/update the canonical CRM Contact from the conversation."""
    if conv.lead_captured and conv.contact_id:
        return
    inbound_to = _normalize_e164(getattr(conv, "to_number", None) or "")
    from services.contact_audience import upsert_contact_from_source, add_contact_tag

    contact = upsert_contact_from_source(
        company_id,
        phone=conv.from_number,
        tags=["new-lead"],
        source_channel="sms",
        source_phone_number=inbound_to,
        source_provider="twilio",
        source_context="auto_reply" if conv.is_first_contact else "inbound_message",
    )
    # Keep legacy source fields populated for existing CRM views/tests while
    # the normalized source_* fields above power segment and campaign logic.
    if not contact.source or contact.source == "sms":
        contact.source = "sms_inbound"
    legacy_detail = f"Inbound SMS to {inbound_to or 'unknown number'} from {conv.from_number}"
    if not contact.source_detail or contact.source_detail in {"auto_reply", "inbound_message", "sms"}:
        contact.source_detail = legacy_detail
    contact.source_added_at = contact.source_added_at or datetime.utcnow()
    add_contact_tag(contact, "new-lead")
    db.session.flush()
    conv.contact_id   = contact.id
    conv.lead_captured = True
    db.session.commit()
    logger.info("Lead captured from SMS: phone_ref=%s company_id=%s", _safe_phone_ref(conv.from_number), company_id)


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
             response="Thanks for reaching out. We’re currently closed, but your message has been received. A team member will reply as soon as we’re back during business hours. Reply STOP to opt out.",
             priority=50, action="reply"),
    ]
    for d in defaults:
        rule = AutoReplyRule(company_id=company_id, **d)
        db.session.add(rule)
    db.session.commit()
    logger.info("Seeded %d default auto-reply rules for company %s", len(defaults), company_id)


def _seed_default_hours(company_id: int):
    """
    Seed business hours for a new company: 2:00 PM – 2:00 AM (next day),
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
            open_time="14:00",
            close_time="02:00",   # 2 AM next day — midnight-crossing
        )
        db.session.add(bh)
    db.session.commit()
    logger.info("Seeded default business hours (2 PM–2 AM LA) for company %s", company_id)


# ---------------------------------------------------------------------------
# Public webhook endpoints
# ---------------------------------------------------------------------------

def _lock_inbound_sid(company_id: int, message_sid: str) -> None:
    """Serialize SID reservation before any contact or message side effect."""
    if db.session.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"inbound:{company_id}:{message_sid}".encode()).digest()[:8]
    key = int.from_bytes(digest, byteorder="big", signed=True)
    db.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _set_inbound_processing_state(message_id: int, status: str, error_code: str | None = None):
    from models import TwilioMessage
    row = db.session.get(TwilioMessage, message_id)
    if not row:
        return None
    row.processing_status = status
    row.processing_error_code = error_code
    if status == "completed":
        row.processed_at = datetime.utcnow()
    return row


def _deliver_and_finalize_inbound(message_id: int, twilio_account) -> tuple[bool, str]:
    """Commit intents, deliver them without locks, then finalize the inbound SID."""
    from models import TwilioMessage
    from services.sms_outbox import deliver_inbound_intents

    row = _set_inbound_processing_state(message_id, "outbound_pending")
    db.session.commit()
    delivery = deliver_inbound_intents(message_id, twilio_account=twilio_account)
    row = db.session.get(TwilioMessage, message_id)
    if delivery["all_delivered"]:
        row.processing_status = "completed"
        row.processing_error_code = None
        row.processed_at = datetime.utcnow()
        success, status = True, "completed"
    elif delivery["has_retryable"]:
        row.processing_status = "failed_retryable"
        row.processing_error_code = "outbound_delivery_retryable"
        success, status = False, "failed_retryable"
    else:
        row.processing_status = "failed_terminal"
        row.processing_error_code = "outbound_delivery_unresolved"
        success, status = False, "failed_terminal"
    db.session.commit()
    return success, status


@twilio_bp.route("/sms", methods=["POST"])
@twilio_bp.route("/sms/inbound", methods=["POST"])
@csrf.exempt
def inbound_sms():
    """
    Twilio inbound SMS webhook.
    Set this URL in your Twilio Messaging Service or phone number config.

    Flow:
      1. Identify TwilioAccount by To number / MessagingServiceSid
      2. Validate the Twilio signature against the externally visible URL
      3. Log inbound message
      4. Handle STOP / START / HELP system keywords (always, regardless of rules)
      5. Run auto-reply rule engine for all other messages
    """
    from models import Contact, TwilioConversation, TwilioMessage

    correlation_id = getattr(g, "request_id", request.headers.get("X-Request-ID", "-"))
    if not (request.mimetype or "").startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        logger.warning(
            "Inbound SMS rejected correlation_id=%s status=invalid_content_type category=request_format",
            correlation_id,
        )
        return '<Response></Response>', 415, {"Content-Type": "text/xml"}

    data            = request.form
    from_number     = data.get("From", "").strip()
    to_number       = data.get("To",   "").strip()
    body            = (data.get("Body") or "").strip()
    twilio_sid      = data.get("MessageSid", "")
    msg_service_sid = data.get("MessagingServiceSid", "")
    num_media       = int(data.get("NumMedia", 0))

    if not twilio_sid or not from_number or not to_number:
        logger.warning(
            "Inbound SMS rejected correlation_id=%s sid=%s from_ref=%s to_ref=%s "
            "status=invalid_payload category=required_field_missing",
            correlation_id, twilio_sid or "missing", _safe_phone_ref(from_number),
            _safe_phone_ref(to_number),
        )
        return '<Response></Response>', 400, {"Content-Type": "text/xml"}

    logger.info(
        "Inbound SMS received correlation_id=%s sid=%s from_ref=%s to_ref=%s "
        "status=received category=webhook",
        correlation_id, twilio_sid, _safe_phone_ref(from_number), _safe_phone_ref(to_number),
    )

    # ── 1. Find TwilioPhoneNumber + TwilioAccount (Phase A multi-number) ────
    pn, ta = _resolve_number(to_number, msg_service_sid)
    if not ta:
        logger.error(
            "Inbound SMS unresolved correlation_id=%s sid=%s from_ref=%s to_ref=%s "
            "status=unresolved category=tenant_resolution",
            correlation_id, twilio_sid, _safe_phone_ref(from_number), _safe_phone_ref(to_number),
        )
        return '<Response></Response>', 500, {"Content-Type": "text/xml"}
    ta = _effective_twilio_config(ta, pn)
    if not hasattr(g, "voice_inbound_debug"):
        g.voice_inbound_debug = {}
    g.voice_inbound_debug.update({
        "company_id": getattr(ta, "company_id", None),
        "phone_ref": _safe_phone_ref(getattr(ta, "from_phone", None) or to_number),
    })

    # ── 2. Validate Twilio signature ───────────────────────────────────────
    if not _validate_twilio_signature(ta, request.path):
        logger.warning(
            "Inbound SMS rejected correlation_id=%s sid=%s from_ref=%s to_ref=%s "
            "status=forbidden category=signature_validation",
            correlation_id, twilio_sid, _safe_phone_ref(from_number), _safe_phone_ref(to_number),
        )
        abort(403)

    # Serialize and classify the SID before contact creation, enrichment,
    # tagging, or response generation. Failed reservations are retryable.
    _lock_inbound_sid(ta.company_id, twilio_sid)
    existing_message = (
        TwilioMessage.query.filter_by(twilio_sid=twilio_sid)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if existing_message and existing_message.processing_status == "completed":
        response_body = existing_message.response_body
        db.session.rollback()
        return response_body or '<Response></Response>', 200, {"Content-Type": "text/xml"}
    if existing_message and existing_message.processing_status in {
        "outbound_pending", "failed_retryable",
    }:
        existing_id = existing_message.id
        db.session.commit()
        delivered, status = _deliver_and_finalize_inbound(existing_id, ta)
        return '<Response></Response>', (200 if delivered or status == "failed_terminal" else 500), {"Content-Type": "text/xml"}
    if existing_message and existing_message.processing_status == "failed_terminal":
        db.session.rollback()
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}
    if existing_message and existing_message.processing_status == "processing":
        started = existing_message.processing_started_at or existing_message.created_at
        if started and started > datetime.utcnow() - timedelta(minutes=5):
            db.session.rollback()
            return '<Response></Response>', 500, {"Content-Type": "text/xml"}
        existing_message.processing_status = "failed"
        existing_message.processing_error_code = "stale_processing_reservation"
        db.session.commit()
        _lock_inbound_sid(ta.company_id, twilio_sid)

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
        admin_conv = _get_or_create_conversation(
            ta.company_id, ta.sms_forward_to, to_number,
            getattr(pn, "id", None), create_contact=False,
        )
        relay_receipt = existing_message or TwilioMessage(
            conversation_id=admin_conv.id,
            company_id=ta.company_id,
            twilio_sid=twilio_sid,
            direction="inbound",
            from_number=from_number,
            to_number=to_number,
            body=body,
            status="received",
            raw_payload=dict(data),
            processing_status="processing",
            processing_attempts=1,
            processing_started_at=datetime.utcnow(),
        )
        if not existing_message:
            db.session.add(relay_receipt)
        else:
            relay_receipt.processing_status = "processing"
            relay_receipt.processing_attempts = (relay_receipt.processing_attempts or 0) + 1
        db.session.commit()
        g.sms_outbox_inbound_message_id = relay_receipt.id

        def _owner_outbox_response():
            delivered, status = _deliver_and_finalize_inbound(relay_receipt.id, ta)
            return '<Response></Response>', (
                200 if delivered or status == "failed_terminal" else 500
            ), {"Content-Type": "text/xml"}

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
                sendConversationSms(admin_conv.id, f"[{company_name}] Reply not sent — message was empty.", twilio_account=ta, effect_type="owner_relay_validation")
                return _owner_outbox_response()
            target_conv = _get_or_create_conversation(ta.company_id, target_number, to_number)
            result = sendConversationSms(target_conv.id, relay_body, twilio_account=ta, to_number=target_number, effect_type="owner_relay")
            logger.info(
                "Owner relay: %s → %s (success=%s err=%s)",
                from_number, target_number, result.get("success"), result.get("error")
            )
            if not result.get("success"):
                sendConversationSms(admin_conv.id, f"[{company_name}] Failed to send to {target_number}: {result.get('error')}", twilio_account=ta, effect_type="owner_relay_failure")
            return _owner_outbox_response()

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
                result = sendConversationSms(last_conv.id, relay_body, twilio_account=ta, effect_type="owner_recent_relay")
                logger.info(
                    "Owner 'r' relay → %s (success=%s err=%s)",
                    last_conv.from_number, result.get("success"), result.get("error"),
                )
                if not result.get("success"):
                    sendConversationSms(admin_conv.id, f"[{company_name}] Failed to reply to {last_conv.from_number}: {result.get('error')}", twilio_account=ta, effect_type="owner_recent_relay_failure")
            else:
                logger.warning("Owner 'r' relay: no recent conversation found")
                sendConversationSms(admin_conv.id, f"[{company_name}] No recent customer conversation found to reply to.", twilio_account=ta, effect_type="owner_recent_relay_missing")
            return _owner_outbox_response()

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
        sendConversationSms(admin_conv.id, help_msg, twilio_account=ta, effect_type="owner_relay_help")
        return _owner_outbox_response()

    message_persisted = False
    msg_record_id = None
    try:
        # Collect media URLs
        media_urls = [data.get(f"MediaUrl{i}") for i in range(num_media)
                      if data.get(f"MediaUrl{i}")]

        if existing_message:
            if existing_message.company_id != ta.company_id:
                raise ValueError("MessageSid belongs to another tenant")
            conv = db.session.get(TwilioConversation, existing_message.conversation_id)
            msg_record = existing_message
            msg_record.processing_status = "processing"
            msg_record.processing_error_code = None
            msg_record.processing_started_at = datetime.utcnow()
            msg_record.processing_attempts = (msg_record.processing_attempts or 0) + 1
        else:
            # Reserve the SID with a conversation shell; contact creation and
            # all identity/tag mutations happen after this durable reservation.
            conv = _get_or_create_conversation(
                ta.company_id, from_number, to_number, getattr(pn, "id", None),
                create_contact=False,
            )
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
                processing_status="processing",
                processing_attempts=1,
                processing_started_at=datetime.utcnow(),
            )
            db.session.add(msg_record)
            conv.is_read = False
            conv.last_message_at = datetime.utcnow()
            conv.last_message_preview = body[:200] if body else "(media)"
            conv.message_count = (conv.message_count or 0) + 1
        db.session.commit()
        message_persisted = True
        msg_record_id = msg_record.id
        g.sms_outbox_inbound_message_id = msg_record_id

        # Contact/source/tag work starts only after a unique SID reservation.
        conv = _get_or_create_conversation(
            ta.company_id, from_number, to_number, getattr(pn, "id", None),
            create_contact=True,
        )
        logger.info(
            "Inbound SMS persisted correlation_id=%s sid=%s company_id=%s conversation_id=%s "
            "status=persisted category=message",
            correlation_id, twilio_sid, ta.company_id, conv.id,
        )

        # CRM classification is isolated from auto-reply and compliance
        # processing.  The persisted SID is the event idempotency key, and the
        # count spans every conversation attached to the canonical contact.
        try:
            from models import TwilioConversation
            from services.crm_automation import (
                FIRST_INBOUND_TRIGGER, dispatch_event, ensure_my_order_automation,
            )
            first_inbound_message_id = (
                db.session.query(db.func.min(TwilioMessage.id))
                .join(TwilioConversation, TwilioConversation.id == TwilioMessage.conversation_id)
                .filter(
                    TwilioMessage.company_id == ta.company_id,
                    TwilioMessage.direction == "inbound",
                    TwilioConversation.contact_id == conv.contact_id,
                )
                .scalar()
            )
            is_first_inbound = bool(conv.contact_id and first_inbound_message_id == msg_record.id)
            ensure_my_order_automation(ta.company_id, create_missing=True)
            dispatch_event(
                ta.company_id, conv.contact_id, FIRST_INBOUND_TRIGGER,
                f"twilio:{twilio_sid}", direction="inbound", channel="sms",
                first_inbound_sms=is_first_inbound, message_sid=twilio_sid,
                conversation_id=conv.id,
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "CRM first-inbound automation failed sid=%s company_id=%s; auto-reply processing continues",
                twilio_sid, ta.company_id,
            )

        # NOTE: Tuya inbound acceptance is intentionally NOT invoked here.
        # It is invoked once, later in this function, only after
        # _deliver_and_finalize_inbound() completes (see the "Optional
        # external integration" block below) so that Tuya notifications
        # aren't triggered ahead of the durable delivery/finalize step.
        # accept_inbound() is idempotent per Twilio MessageSid, so a second
        # call would have been harmless, but only one call site should exist.

        # ── 4. System-level keyword handling ──────────────────────────────
        # These always fire and return a TwiML reply immediately.
        kw = _normalized_keyword(body)

        if kw in _STOP_KEYWORDS:
            conv.is_opted_out   = True
            conv.sms_opt_out_at = datetime.utcnow()
            contact = _update_contact_sms_consent(ta.company_id, from_number, False, f"keyword:{kw}")
            _write_sms_compliance_audit(ta.company_id, "opt_out", from_number, kw, getattr(contact, "id", None), conv.id)
            try:
                from services.sms_keyword_engine import mark_opt_out
                mark_opt_out(ta.company_id, from_number, conv)
            except Exception as opt_exc:
                logger.warning("Campaign opt-out sync failed: %s", opt_exc)
            reply_tuple = _twiml_message(_STOP_REPLY)
            completed = _set_inbound_processing_state(msg_record_id, "completed")
            completed.response_body = reply_tuple[0]
            db.session.commit()
            logger.info("Opt-out keyword received: company_id=%s phone_last4=%s", ta.company_id, from_number[-4:])
            return reply_tuple

        active_identity_confirmation = bool(
            conv.contact_id and getattr(db.session.get(Contact, conv.contact_id), "identity_status", None) == "awaiting_confirmation"
        )
        if kw in _START_KEYWORDS and not (kw == "yes" and active_identity_confirmation):
            conv.is_opted_out  = False
            conv.sms_opt_in_at = datetime.utcnow()
            conv.sms_opt_out_at = None
            contact = _update_contact_sms_consent(ta.company_id, from_number, True, f"keyword:{kw}")
            _write_sms_compliance_audit(ta.company_id, "opt_in", from_number, kw, getattr(contact, "id", None), conv.id)
            try:
                from services.sms_keyword_engine import mark_opt_in
                mark_opt_in(ta.company_id, from_number, conv)
            except Exception as opt_exc:
                logger.warning("Campaign opt-in sync failed: %s", opt_exc)
            reply_tuple = _twiml_message(_START_REPLY)
            completed = _set_inbound_processing_state(msg_record_id, "completed")
            completed.response_body = reply_tuple[0]
            db.session.commit()
            logger.info("Opt-in keyword received: company_id=%s phone_last4=%s", ta.company_id, from_number[-4:])
            return reply_tuple

        if kw in {"help", "info"}:
            _write_sms_compliance_audit(ta.company_id, "help", from_number, kw, conv.contact_id, conv.id)
            reply_tuple = _twiml_message(_HELP_REPLY)
            completed = _set_inbound_processing_state(msg_record_id, "completed")
            completed.response_body = reply_tuple[0]
            db.session.commit()
            logger.info("HELP/INFO received: company_id=%s phone_last4=%s", ta.company_id, from_number[-4:])
            return reply_tuple

        # Identity collection happens only after the inbound message has been
        # durably committed and compliance keywords have taken precedence.
        identity_reply_sent = False
        try:
            from models import Contact
            from services.contact_identity import (
                apply_cached_google_match, begin_identity_collection,
                process_identity_message, should_request_identity,
            )
            contact = db.session.get(Contact, conv.contact_id) if conv.contact_id else None
            if contact:
                match_status = apply_cached_google_match(contact)
                outcome = process_identity_message(contact, conv, body, twilio_sid)
                reply = outcome.get("reply")
                if not reply and match_status != "matched" and should_request_identity(contact):
                    reply = begin_identity_collection(contact, twilio_sid)
                db.session.commit()
                if reply:
                    result = sendConversationSms(conv.id, reply, twilio_account=ta, is_auto_reply=True, effect_type="identity_reply")
                    identity_reply_sent = bool(result.get("success"))
                    if identity_reply_sent:
                        msg_record.auto_responded = True
                        db.session.commit()
        except Exception:
            db.session.rollback()
            _set_inbound_processing_state(
                msg_record_id, "failed", "identity_processing_failed"
            )
            db.session.commit()
            logger.exception(
                "Inbound SMS downstream failure correlation_id=%s sid=%s company_id=%s "
                "status=retryable category=identity_enrichment",
                correlation_id, twilio_sid, ta.company_id,
            )
            return '<Response></Response>', 500, {"Content-Type": "text/xml"}

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
                sendConversationSms(conv.id, fwd_body, twilio_account=ta, to_number=ta.sms_forward_to, effect_type="inbound_forward")
                logger.info(
                    "SMS forwarded: customer=%s → admin=%s company=%s",
                    from_number, ta.sms_forward_to, company_name,
                )
            except Exception as fwd_exc:
                logger.warning("SMS forward failed: %s", fwd_exc)

        # ── 5b. Campaign keyword/auto-reply engine ────────────────────────
        campaign_reply_sent = False
        try:
            from services.sms_keyword_engine import (
                attribute_inbound_reply,
                process_keyword_rules,
            )
            attribute_inbound_reply(ta.company_id, from_number, body, conv.id)
            campaign_reply = process_keyword_rules(ta.company_id, body, conv, ta)
            if campaign_reply and not identity_reply_sent:
                result = sendConversationSms(conv.id, campaign_reply, twilio_account=ta, is_auto_reply=True, effect_type="campaign_reply")
                campaign_reply_sent = bool(result.get("success"))
        except Exception as campaign_rule_exc:
            logger.exception("Error in campaign SMS keyword engine: %s", campaign_rule_exc)

        auto_reply_sent = False
        # ── 6. Auto-reply rule engine ──────────────────────────────────────
        try:
            # Auto-capture/update CRM contact for every non-system inbound SMS.
            _capture_lead(conv, body, ta.company_id)

            # Run rules only if no higher-precedence reply (identity collection or
            # the campaign/keyword engine) has already answered this message, and
            # not opted out. This keeps one inbound SMS from generating two
            # independent auto-replies.
            if not identity_reply_sent and not campaign_reply_sent and not conv.is_opted_out and getattr(ta, "auto_reply_enabled", True):
                auto_reply_sent = _apply_auto_reply_rules(conv, body, ta)
        except Exception as rule_exc:
            logger.exception("Error in auto-reply rule engine: %s", rule_exc)
        if auto_reply_sent:
            msg_record.auto_responded = True
            db.session.commit()

        # Clear first-contact flag after first processed message
        if conv.is_first_contact:
            conv.is_first_contact = False
        db.session.commit()

        # ── 7. Inbound-awareness side effects ────────────────────────────
        # Runs exactly once, immediately after the inbound message above is
        # durably persisted and committed -- independent of whether the
        # optional outbound auto-reply below succeeds, fails, or needs a
        # retry. A failed or delayed auto-reply must never suppress
        # awareness of an inbound message that was already accepted.
        #
        # This only runs on the first-time processing pass for a given
        # MessageSid: the retry short-circuits near the top of this function
        # (existing_message.processing_status in {"completed",
        # "outbound_pending", "failed_retryable", "failed_terminal"}) return
        # before reaching this point, so a Twilio webhook retry of the same
        # SID cannot re-fire it. Its unique event/message SID also makes each
        # sub-call individually idempotent if it ever were re-invoked.
        try:
            from services.tuya_notification import accept_inbound
            accept_inbound(
                ta.company_id, twilio_sid, _normalize_e164(to_number),
                customer_phone=from_number,
            )
        except Exception:
            db.session.rollback()
            logger.exception(
                "Tuya notification acceptance failed sid=%s company_id=%s",
                twilio_sid, ta.company_id,
            )

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
            try:
                in_business_for_alert = _is_business_hours(ta.company_id, phone_config=ta)
            except Exception:
                in_business_for_alert = True
            alert_silent = False
            _push_sse_event(ta.company_id, "new_message", {
                "conversation_id":      conv.id,
                "from_number":          from_number,
                "contact_name":         conv.contact_name or "",
                "body":                 (body or "(media)")[:200],
                "has_media":            num_media > 0,
                "last_message_at":      conv.last_message_at.isoformat() if conv.last_message_at else None,
                "last_message_preview": conv.last_message_preview or "",
                "silent":               alert_silent,
                "auto_responded":       bool(auto_reply_sent),
            })
            _fire_push_notification(ta.company_id, conv, body or "(media)", silent=alert_silent)
        except Exception:
            logger.exception(
                "Inbound SMS downstream failure correlation_id=%s sid=%s company_id=%s "
                "status=persisted category=push_dispatch",
                correlation_id, twilio_sid, ta.company_id,
            )

        # ── 8. Deliver any queued outbound auto-reply/identity/campaign SMS ─
        delivered, delivery_status = _deliver_and_finalize_inbound(msg_record_id, ta)
        if not delivered:
            logger.warning(
                "Inbound outbound effects unresolved sid=%s status=%s",
                twilio_sid, delivery_status,
            )
            if delivery_status == "failed_retryable":
                return '<Response></Response>', 500, {"Content-Type": "text/xml"}
            return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    except Exception as exc:
        if not message_persisted:
            db.session.rollback()
            logger.exception(
                "Inbound SMS failed correlation_id=%s sid=%s status=failed category=persistence",
                correlation_id, twilio_sid,
            )
            return '<Response></Response>', 500, {"Content-Type": "text/xml"}
        logger.exception(
            "Inbound SMS downstream failure correlation_id=%s sid=%s company_id=%s "
            "status=retryable category=post_persistence",
            correlation_id, twilio_sid, ta.company_id,
        )
        db.session.rollback()
        if msg_record_id:
            _set_inbound_processing_state(
                msg_record_id, "failed", "post_persistence_failure"
            )
            db.session.commit()
        return '<Response></Response>', 500, {"Content-Type": "text/xml"}

    return '<Response></Response>', 200, {"Content-Type": "text/xml"}


@twilio_bp.route("/sms/status", methods=["POST"])
@csrf.exempt
def sms_status():
    """Twilio delivery status callback — updates message status."""
    from models import SMSCampaign, SMSRecipient, TwilioAccount, TwilioMessage

    data       = request.form
    sid        = data.get("MessageSid", "")
    status     = data.get("MessageStatus", "")
    error_code = data.get("ErrorCode")
    error_msg  = data.get("ErrorMessage")

    logger.info("SMS status: sid=%s status=%s", sid, status)

    if sid:
        msg = TwilioMessage.query.filter_by(twilio_sid=sid).first()
        ta = None
        if msg:
            ta = TwilioAccount.query.filter_by(company_id=msg.company_id).first()
        if not ta:
            recipient = SMSRecipient.query.filter_by(provider_message_sid=sid).first()
            if recipient:
                campaign = db.session.get(SMSCampaign, recipient.campaign_id) if recipient.campaign_id else None
                company_id = recipient.company_id or (campaign.company_id if campaign else None)
                if company_id:
                    ta = TwilioAccount.query.filter_by(company_id=company_id).first()
        if not _validate_twilio_signature(ta, "/twilio/sms/status"):
            abort(403)
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
        try:
            from services.sms_keyword_engine import update_delivery_status
            update_delivery_status(sid, status, error_code, error_msg)
            db.session.commit()
        except Exception as campaign_status_exc:
            db.session.rollback()
            logger.warning("Campaign delivery status sync failed: %s", campaign_status_exc)

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
    """Rollback-safe Twilio voice webhook wrapper."""
    try:
        return _inbound_call_impl()
    except HTTPException:
        raise
    except Exception:
        ctx = getattr(g, "voice_inbound_debug", {}) if has_request_context() else {}
        logger.exception(
            "FIRST_EXCEPTION voice inbound failed call_sid=%s to=%s from=%s company_id=%s phone_number_id=%s route_decision=%s forward_to=%s",
            ctx.get("call_sid"), ctx.get("to_number"), ctx.get("from_number"),
            ctx.get("company_id"), ctx.get("phone_number_id"), ctx.get("route_decision"), ctx.get("forward_to"),
        )
        try:
            db.session.rollback()
        except Exception:
            logger.exception("Voice inbound rollback failed after first exception")
        return _voice_inbound_safe_fallback_twiml(ctx), 200, {"Content-Type": "text/xml"}


def _voice_inbound_safe_fallback_twiml(ctx=None):
    ctx = ctx or {}
    forward_to = ctx.get("forward_to")
    to_number = html.escape(ctx.get("to_number") or "", quote=True)
    caller_id = html.escape(ctx.get("caller_id") or ctx.get("to_number") or "", quote=True)
    if forward_to:
        safe_forward = html.escape(forward_to, quote=True)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Response>\n'
            f'  <Dial callerId="{caller_id}" timeout="25" action="/twilio/voice/no-answer?to={to_number}" method="POST">\n'
            f'    <Number>{safe_forward}</Number>\n'
            '  </Dial>\n'
            '</Response>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Response>\n'
        '  <Say>Thank you for calling. Please leave a message after the tone.</Say>\n'
        '  <Record maxLength="180" playBeep="true" recordingStatusCallback="/twilio/voice/recording" recordingStatusCallbackMethod="POST" />\n'
        '  <Say>We did not receive a recording. Goodbye.</Say>\n'
        '</Response>'
    )


def _inbound_call_impl():
    """
    Twilio voice webhook — business-hours-aware call routing.

    Business hours  + voice_forwarding_enabled  → Dial forwarding number (25 s timeout).
      No answer → /twilio/voice/no-answer → voicemail.
    After hours + after_hours_voicemail_enabled → Voicemail greeting + Record.
    Fallback: generic voicemail.
    """
    from models import TwilioCallLog, PhoneSettings, CallEvent

    data        = request.form
    from_number = data.get("From", "")
    to_number   = data.get("To",   "")
    call_sid    = data.get("CallSid", "")
    call_status = data.get("CallStatus", "")
    duration    = int(data.get("CallDuration") or 0)
    caller_name = data.get("CallerName", "")
    g.voice_inbound_debug = {
        "call_sid": call_sid,
        "to_number": to_number,
        "from_number": from_number,
        "company_id": None,
        "phone_number_id": None,
        "route_decision": "received",
        "forward_to": None,
        "caller_id": to_number,
    }

    pn, ta = _resolve_number(to_number)
    g.voice_inbound_debug.update({
        "company_id": getattr(ta, "company_id", None) or getattr(pn, "company_id", None),
        "phone_number_id": getattr(pn, "id", None),
        "route_decision": "resolved" if ta else "unresolved",
    })

    if not ta:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>Thank you for calling. Goodbye.</Say></Response>"""
        return twiml, 200, {"Content-Type": "text/xml"}
    ta = _effective_twilio_config(ta, pn)
    g.voice_inbound_debug.update({"company_id": getattr(ta, "company_id", None), "caller_id": getattr(ta, "from_phone", None) or to_number})

    # Validate Twilio signature
    signature_path = "/twilio/voice/inbound" if request.path.startswith("/twilio/") else "/api/twilio/voice/incoming"
    if not _validate_twilio_signature(ta, signature_path):
        abort(403)

    settings = PhoneSettings.query.filter_by(company_id=ta.company_id).first()

    # Log the call idempotently
    existing = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
    if not existing and call_sid:
        log = TwilioCallLog(
            company_id=ta.company_id,
            phone_number_id=getattr(pn, "id", None),
            twilio_sid=call_sid,
            direction="inbound",
            from_number=from_number,
            to_number=to_number,
            status=call_status or "ringing",
            duration=duration,
            caller_name=caller_name,
            transcription_status="not_requested",
            raw_payload=dict(data),
        )
        db.session.add(log)
        db.session.commit()
    else:
        log = existing
    if log:
        evt = CallEvent.query.filter_by(call_log_id=log.id, event_type="incoming", provider_event_id=call_sid).first()
        if not evt:
            db.session.add(CallEvent(call_log_id=log.id, event_type="incoming", provider_event_id=call_sid, payload=dict(data)))
            db.session.commit()

    # Determine routing
    in_hours = _is_business_hours(ta.company_id, phone_config=ta)
    after_hours_sms_enabled = (
        getattr(ta, "after_hours_sms_enabled", False)
        if pn and getattr(pn, "after_hours_sms_enabled", None) is not None
        else bool(settings and settings.after_hours_sms_enabled)
    )
    after_hours_sms_body = (
        _after_hours_rule_response(ta.company_id, getattr(pn, "id", None) or getattr(ta, "phone_number_id", None))
        or (settings.after_hours_sms_body if settings else None)
    )
    if (not in_hours) and after_hours_sms_enabled and after_hours_sms_body and from_number and log and not log.missed_text_sent:
        try:
            can_send, reason = _can_send_call_auto_sms(ta.company_id, from_number)
            if can_send:
                sms_conv = _get_or_create_conversation(ta.company_id, from_number, to_number, getattr(pn, "id", None))
                result = sendConversationSms(sms_conv.id, after_hours_sms_body, twilio_account=ta, is_auto_reply=True)
                if result.get("success"):
                    log.missed_text_sent = True
                    db.session.commit()
            else:
                logger.info("After-hours SMS auto-reply skipped for call sid=%s reason=%s", call_sid, reason)
        except Exception as exc:
            logger.warning("After-hours SMS auto-reply failed for call sid=%s: %s", call_sid, exc)

    # Business-hours auto reply is separate from canonical after-hours rules and only runs in hours.
    if in_hours and getattr(ta, "business_hours_auto_reply_enabled", False) and getattr(ta, "business_hours_auto_reply_text", None) and from_number and log and not log.missed_text_sent:
        try:
            can_send, reason = _can_send_call_auto_sms(ta.company_id, from_number)
            if can_send:
                sms_conv = _get_or_create_conversation(ta.company_id, from_number, to_number, getattr(pn, "id", None))
                result = sendConversationSms(sms_conv.id, ta.business_hours_auto_reply_text, twilio_account=ta, is_auto_reply=True)
                if result.get("success"):
                    log.missed_text_sent = True
                    db.session.commit()
            else:
                logger.info("Business-hours SMS auto-reply skipped for call sid=%s reason=%s", call_sid, reason)
        except Exception as exc:
            logger.warning("Business-hours SMS auto-reply failed for call sid=%s: %s", call_sid, exc)

    def _voicemail_twiml(after_hours=False):
        greeting = ((ta.voicemail_greeting_text if pn else None) or
                    (settings.after_hours_voicemail_greeting if after_hours and settings else None) or
                    (settings.voicemail_greeting if settings else None) or
                    ta.voicemail_greeting_text or
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
            f'  <Record maxLength="180" playBeep="true" transcribe="{str(bool(settings and settings.transcription_enabled)).lower()}"\n'
            f'          recordingStatusCallback="/twilio/voice/recording"\n'
            f'          transcribeCallback="/twilio/voice/transcription"\n'
            f'          recordingStatusCallbackMethod="POST" />\n'
            f"  <Say>We did not receive a recording. Goodbye.</Say>\n"
            f"</Response>"
        )

    number_route = getattr(ta, "during_hours_route", None) if in_hours else getattr(ta, "after_hours_route", None)
    settings_route = (settings.during_hours_route if settings and in_hours else settings.after_hours_route if settings else None)
    # Treat model defaults as unset so tenant PhoneSettings keep routing calls unless
    # the selected phone number has a non-default explicit route.
    if settings_route and number_route in (None, "", "ring_pwa") and settings_route != number_route:
        route = settings_route
    else:
        route = number_route or settings_route
    configured_call_forward_to = getattr(ta, "call_forwarding_number", None) or getattr(ta, "call_forward_to", None)
    settings_forward_to = ((settings.forward_number if in_hours else settings.after_hours_forward_number) if settings else None)
    forward_to = (
        (configured_call_forward_to if pn else settings_forward_to)
        or settings_forward_to
        or configured_call_forward_to
    )
    fallback_to = ((settings.fallback_forward_number if in_hours else settings.after_hours_fallback_forward_number) if settings else None)
    timeout = (settings.ring_duration_seconds if settings else None) or 25
    record_attr = ' record="record-from-answer" recordingStatusCallback="/twilio/voice/recording"' if settings and settings.recording_enabled else ""
    g.voice_inbound_debug.update({
        "route_decision": route or ("ring_pwa" if in_hours else "voicemail"),
        "forward_to": forward_to,
        "in_hours": in_hours,
    })

    def _dial_twiml(number, fallback=None):
        caller_id = ta.from_phone or to_number
        if log:
            log.status = "forwarded"
            log.forwarded_to_number = number
            log.forwarded = True
            log.forwarded_to = number
            log.final_status = "forwarded"
            db.session.commit()
        fallback_xml = f"<Number>{fallback}</Number>" if fallback else ""
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f"<Response>\n"
            f'  <Dial callerId="{caller_id}" timeout="{timeout}" action="/twilio/voice/no-answer?to={html.escape(to_number or "", quote=True)}" method="POST"{record_attr}>\n'
            f"    <Number>{number}</Number>{fallback_xml}\n"
            f"  </Dial>\n"
            f"</Response>"
        )

    if route == "forward" and forward_to and (settings_forward_to or ta.voice_forwarding_enabled or getattr(ta, "call_forwarding_enabled", False)):
        # Explicit per-line call forwarding wins over browser ringing. This
        # applies only to voice webhooks; SMS routing never reads these fields.
        twiml = _dial_twiml(forward_to, fallback_to)
    elif in_hours and (route in (None, "ring_pwa")):
        if log:
            log.status = "ringing"
            db.session.commit()
        try:
            from inbox_pwa import _push_sse_event, create_pwa_notification
            _push_sse_event(ta.company_id, "incoming_call", {
                "call_id": log.id if log else None,
                "call_sid": call_sid,
                "from_number": from_number,
                "to_number": to_number,
                "caller_name": caller_name or from_number,
            })
            create_pwa_notification(
                ta.company_id,
                event_type="incoming_call",
                title=f"Incoming call from {caller_name or from_number}",
                body=f"Call to {to_number}",
                link="/app/inbox?tab=calls",
                phone_number_id=getattr(pn, "id", None),
                icon="phone",
                emit_sse=False,
            )
        except Exception as exc:
            logger.debug("PWA incoming call event failed: %s", exc)
        caller_id = ta.from_phone or to_number
        client_identities = []
        try:
            from models import PWADevice
            from services.phone_identity import pwa_voice_identity
            for device in PWADevice.query.filter_by(
                company_id=ta.company_id,
                approved_status="approved",
                lifecycle_status="active",
            ).all():
                if device.phone_number_id and pn and device.phone_number_id != pn.id:
                    continue
                client_identities.append(pwa_voice_identity(ta.company_id, device.user_id, device.device_key))
        except Exception:
            logger.exception("Unable to resolve eligible PWA voice devices", extra={"company_id": ta.company_id})
        if not client_identities:
            client_identities = [_pwa_voice_identity(ta.company_id)]
        safe_from = html.escape(from_number or "", quote=True)
        safe_caller = html.escape(caller_name or from_number or "", quote=True)
        safe_caller_id = html.escape(caller_id or "", quote=True)
        clients_twiml = "".join(
            '    <Client>\n'
            f'      <Identity>{identity}</Identity>\n'
            f'      <Parameter name="call_log_id" value="{log.id if log else ""}"/>\n'
            f'      <Parameter name="from_number" value="{safe_from}"/>\n'
            f'      <Parameter name="caller_name" value="{safe_caller}"/>\n'
            '    </Client>\n'
            for identity in client_identities
        )
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Response>\n'
            f'  <Dial callerId="{safe_caller_id}" timeout="{timeout}" action="/twilio/voice/no-answer?to={html.escape(to_number or "", quote=True)}" method="POST"{record_attr}>\n'
            f'{clients_twiml}'
            '  </Dial>\n'
            '</Response>'
        )
    elif route == "forward" and forward_to:
        twiml = _dial_twiml(forward_to, fallback_to)
    elif route == "voicemail":
        twiml = _voicemail_twiml(after_hours=not in_hours)
    elif not in_hours and ta.after_hours_voicemail_enabled:
        twiml = _voicemail_twiml(after_hours=True)
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
    from models import TwilioCallLog, PhoneSettings

    data        = request.form
    call_sid    = data.get("CallSid", "")
    dial_status = data.get("DialCallStatus", "")
    to_number   = request.args.get("to") or data.get("To", "")

    pn, ta = _resolve_number(to_number)
    ta = _effective_twilio_config(ta, pn)
    g.voice_inbound_debug.update({"company_id": getattr(ta, "company_id", None), "caller_id": getattr(ta, "from_phone", None) or to_number}) if ta else None

    logger.info("Voice no-answer: sid=%s dial_status=%s", call_sid, dial_status)

    # Update call log
    if call_sid:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            log.status = "no-answer"
            db.session.commit()

    # Send missed-call text if configured
    from_number = data.get("From", "")
    settings = PhoneSettings.query.filter_by(company_id=ta.company_id).first() if ta else None
    sms_body = None
    if ta and pn and ta.missed_call_text:
        sms_body = ta.missed_call_text
    elif ta and settings and settings.missed_call_sms_enabled:
        sms_body = settings.missed_call_sms_body or ta.missed_call_text
    elif ta and not settings and ta.missed_call_text:
        sms_body = ta.missed_call_text
    if ta and sms_body and from_number:
        can_send, reason = _can_send_call_auto_sms(ta.company_id, from_number)
        if can_send:
            sms_conv = _get_or_create_conversation(ta.company_id, from_number, to_number, getattr(pn, "id", None))
            result = sendConversationSms(sms_conv.id, sms_body, twilio_account=ta, is_auto_reply=True)
            if result.get("success"):
                log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
                if log and not log.missed_text_sent:
                    log.missed_text_sent = True
                    db.session.commit()
        else:
            logger.info("Missed-call SMS auto-reply skipped for call sid=%s reason=%s", call_sid, reason)

    # Fall through to voicemail
    if ta:
        greeting = ((ta.voicemail_greeting_text if pn else None) or
                    (settings.voicemail_greeting if settings else None) or ta.voicemail_greeting_text or
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
    from models import TwilioCallLog, VoiceVoicemailMessage, CallRecording, CallEvent

    data           = request.form
    call_sid       = data.get("CallSid", "")
    recording_url  = data.get("RecordingUrl", "")
    recording_sid  = data.get("RecordingSid", "")
    recording_dur  = data.get("RecordingDuration", "0")
    _pn, ta = _resolve_number(data.get("To", ""))
    if ta:
        endpoint = "/twilio/voice/recording" if request.path.startswith("/twilio/") else "/api/twilio/voice/recording"
        if not _validate_twilio_signature(ta, endpoint):
            abort(403)

    logger.info(
        "Voicemail recording: sid=%s recording=%s dur=%ss url=%s",
        call_sid, recording_sid, recording_dur, recording_url,
    )

    if call_sid and recording_url:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            is_voicemail = (data.get("RecordingSource") == "RecordVerb") or log.status in ("no-answer", "missed", "voicemail")
            log.recording_url = recording_url
            log.recording_sid = recording_sid
            log.duration = int(recording_dur or log.duration or 0)
            if is_voicemail:
                log.voicemail_url = recording_url
                log.voicemail_sid = recording_sid
                log.status = "voicemail"
                if not VoiceVoicemailMessage.query.filter_by(recording_sid=recording_sid).first():
                    db.session.add(VoiceVoicemailMessage(
                        company_id=log.company_id,
                        call_log_id=log.id,
                        from_number=log.from_number,
                        to_number=log.to_number,
                        call_sid=call_sid,
                        recording_sid=recording_sid,
                        recording_url=recording_url,
                        duration_secs=int(recording_dur or 0),
                        transcription_status="not_requested",
                    ))
            elif log.status != "voicemail":
                log.status = log.status or "completed"
            if recording_sid and not CallRecording.query.filter_by(recording_sid=recording_sid).first():
                db.session.add(CallRecording(
                    company_id=log.company_id,
                    call_log_id=log.id,
                    call_sid=call_sid,
                    recording_sid=recording_sid,
                    recording_url=recording_url,
                    duration_secs=int(recording_dur or 0),
                    status=data.get("RecordingStatus") or "completed",
                ))
            if not CallEvent.query.filter_by(call_log_id=log.id, event_type="recording", provider_event_id=recording_sid).first():
                db.session.add(CallEvent(call_log_id=log.id, event_type="recording", provider_event_id=recording_sid, payload=dict(data)))
            db.session.commit()
            if is_voicemail:
                try:
                    from inbox_pwa import create_pwa_notification
                    create_pwa_notification(
                        log.company_id,
                        event_type="voicemail",
                        title=f"New voicemail from {log.from_number or 'Unknown caller'}",
                        body=f"Voicemail recording ({int(recording_dur or 0)}s)",
                        link="/app/inbox?tab=voicemail",
                        phone_number_id=getattr(log, "phone_number_id", None),
                        icon="mic",
                    )
                except Exception as exc:
                    logger.debug("PWA voicemail notification skipped: %s", exc)

    return "", 204


@twilio_bp.route("/voice/status", methods=["POST"])
@csrf.exempt
def voice_status():
    """Twilio voice status callback — updates call record."""
    from models import TwilioCallLog, CallEvent

    data        = request.form
    call_sid    = data.get("CallSid", "")
    call_status = data.get("CallStatus", "")
    duration    = int(data.get("CallDuration") or 0)
    _pn, ta = _resolve_number(data.get("To", ""))
    if ta:
        endpoint = "/twilio/voice/status" if request.path.startswith("/twilio/") else "/api/twilio/voice/status"
        if not _validate_twilio_signature(ta, endpoint):
            abort(403)

    logger.info("Voice status: sid=%s status=%s dur=%s", call_sid, call_status, duration)

    if call_sid:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            log.status   = call_status
            log.duration = duration
            log.final_status = "answered" if call_status in ("completed", "in-progress") else call_status
            if call_status == "completed":
                log.ended_at = datetime.utcnow()
            if call_status == "in-progress":
                log.status = "answered"
                log.answered_at = log.answered_at or datetime.utcnow()
            if call_status in ("no-answer", "busy", "failed", "canceled"):
                log.status = "missed"
                log.final_status = "missed"
                notify_event_id = f"{call_sid}:missed_call_notification"
                if not CallEvent.query.filter_by(call_log_id=log.id, event_type="pwa_notification:missed_call", provider_event_id=notify_event_id).first():
                    try:
                        from inbox_pwa import create_pwa_notification
                        create_pwa_notification(
                            log.company_id,
                            event_type="missed_call",
                            title=f"Missed call from {log.caller_name or log.from_number or 'Unknown caller'}",
                            body=f"Missed call to {log.to_number or data.get('To', '')}",
                            link="/app/inbox?tab=calls",
                            phone_number_id=getattr(log, "phone_number_id", None),
                            icon="phone-missed",
                        )
                        db.session.add(CallEvent(call_log_id=log.id, event_type="pwa_notification:missed_call", provider_event_id=notify_event_id, payload={"status": call_status}))
                    except Exception as exc:
                        logger.debug("PWA missed call notification failed: %s", exc)
            if not CallEvent.query.filter_by(call_log_id=log.id, event_type="status", provider_event_id=f"{call_sid}:{call_status}").first():
                db.session.add(CallEvent(call_log_id=log.id, event_type="status", provider_event_id=f"{call_sid}:{call_status}", payload=dict(data)))
            db.session.commit()

    return "", 204


@twilio_bp.route("/voice/voicemail", methods=["POST"])
@twilio_bp.route("/voice/transcription", methods=["POST"])
@csrf.exempt
def voice_transcription():
    """Twilio transcription callback for voicemail Record verbs."""
    from models import TwilioCallLog, VoiceVoicemailMessage, CallEvent
    data = request.form
    call_sid = data.get("CallSid", "")
    recording_sid = data.get("RecordingSid", "")
    transcription_sid = data.get("TranscriptionSid", "")
    text = data.get("TranscriptionText", "")
    status = (data.get("TranscriptionStatus") or ("complete" if text else "failed")).lower()
    _pn, ta = _resolve_number(data.get("To", ""))
    if ta:
        api_tail = "voicemail" if request.path.endswith("/voicemail") else "transcription"
        endpoint = f"/twilio/voice/{api_tail}" if request.path.startswith("/twilio/") else f"/api/twilio/voice/{api_tail}"
        if not _validate_twilio_signature(ta, endpoint):
            abort(403)
    log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
    if log:
        log.transcription_text = text or log.transcription_text
        log.transcription_status = "complete" if status == "completed" else status
        log.transcription_provider = "twilio"
        log.transcription_error = data.get("TranscriptionError") or data.get("ErrorMessage")
        log.transcribed_at = datetime.utcnow() if text or status in ("completed", "complete", "failed") else log.transcribed_at
        vm = VoiceVoicemailMessage.query.filter_by(call_log_id=log.id).first()
        if vm:
            vm.transcript = text or vm.transcript
            vm.transcription_text = text or vm.transcription_text
            vm.transcription_status = log.transcription_status
            vm.transcription_provider = "twilio"
            vm.transcription_error = log.transcription_error
            vm.transcribed_at = log.transcribed_at
        event_id = transcription_sid or recording_sid or f"{call_sid}:transcription"
        if not CallEvent.query.filter_by(call_log_id=log.id, event_type="transcription", provider_event_id=event_id).first():
            db.session.add(CallEvent(call_log_id=log.id, event_type="transcription", provider_event_id=event_id, payload=dict(data)))
        db.session.commit()
    return "", 204


# PWA/API-compatible Twilio webhook aliases. They call the canonical /twilio
# handlers so signature validation, routing, idempotency, and persistence stay
# in one implementation while satisfying the /api/twilio/voice/* contract.
@api_twilio_bp.route("/voice/incoming", methods=["POST"])
@csrf.exempt
def api_voice_incoming():
    return inbound_call()


@api_twilio_bp.route("/voice/status", methods=["POST"])
@csrf.exempt
def api_voice_status():
    return voice_status()


@api_twilio_bp.route("/voice/recording", methods=["POST"])
@csrf.exempt
def api_voice_recording():
    return voice_recording()


@api_twilio_bp.route("/voice/voicemail", methods=["POST"])
@api_twilio_bp.route("/voice/transcription", methods=["POST"])
@csrf.exempt
def api_voice_transcription():
    return voice_transcription()


# ---------------------------------------------------------------------------
# Protected routes
# ---------------------------------------------------------------------------

@twilio_bp.route("/inbox")
@login_required
def inbox():
    """Legacy SMS inbox route; Communications Hub is canonical."""
    return redirect(url_for("twilio.comms_hub", tab="inbox"))


@twilio_bp.route("/comms/devices/<int:device_id>", methods=["POST"])
@login_required
def comms_device_action(device_id):
    from models import PWADevice, MarketingAuditLog
    from services.comms_permissions import can_manage_users
    company = _get_company()
    if not company:
        abort(404)
    if not can_manage_users(current_user, company.id):
        abort(403)
    device = PWADevice.query.filter_by(id=device_id, company_id=company.id).first_or_404()
    action = (request.form.get("action") or "").strip().lower()
    now = datetime.utcnow()
    if action == "approve":
        device.approved_status = "approved"
        device.approved_at = now
        device.approved_by_user_id = current_user.id
        device.lifecycle_status = "active" if device.push_enabled else "pending"
        flash("Device approved.", "success")
    elif action in {"revoke", "block", "disable"}:
        device.approved_status = "revoked"
        device.revoked_at = now
        device.revoked_by_user_id = current_user.id
        device.push_enabled = False
        device.lifecycle_status = "disabled"
        device.disabled_at = now
        from models import PushSubscription
        PushSubscription.query.filter_by(company_id=company.id, user_id=device.user_id, device_key=device.device_key).update({"is_active": False, "updated_at": now})
        flash("Device disabled.", "success")
    elif action == "rename":
        device.device_name = (request.form.get("device_name") or device.device_name or "PWA Device")[:120]
        flash("Device renamed.", "success")
    elif action == "remove":
        from models import PushSubscription
        PushSubscription.query.filter_by(company_id=company.id, user_id=device.user_id, device_key=device.device_key).delete(synchronize_session=False)
        db.session.delete(device)
        flash("Device removed.", "success")
    elif action == "test":
        if device.lifecycle_status != "active" or not device.push_enabled:
            flash("Only active devices can receive a test notification.", "warning")
            return redirect(url_for("twilio.comms_hub", tab="devices"))
        from inbox_pwa import send_pwa_push_notification
        result = send_pwa_push_notification(
            company.id, user_ids=[device.user_id], device_key=device.device_key,
            title="LUXit Communications", body="Test notification delivered through the production push service.",
            link="/app/inbox", tag="device-test", event_type="push_test",
        )
        flash("Test notification sent." if result.get("sent") else "Test notification was not accepted by the push provider.", "success" if result.get("sent") else "warning")
    else:
        abort(400)
    db.session.add(MarketingAuditLog(company_id=company.id, created_by_user_id=current_user.id, entity_type="pwa_device", entity_id=device.id, action=f"pwa_device_{action}", details={"user_id": device.user_id}))
    db.session.commit()
    return redirect(url_for("twilio.comms_hub", tab="devices"))

@twilio_bp.route("/comms/devices/settings", methods=["POST"])
@login_required
def comms_device_settings():
    from models import MarketingAuditLog
    from services.comms_permissions import can_manage_users
    company = _get_company()
    if not company:
        abort(404)
    if not can_manage_users(current_user, company.id):
        abort(403)
    company.require_approved_pwa_devices = bool(request.form.get("require_approved_pwa_devices"))
    db.session.add(MarketingAuditLog(company_id=company.id, created_by_user_id=current_user.id, entity_type="company", entity_id=company.id, action="pwa_device_approval_setting_updated", details={"required": company.require_approved_pwa_devices}))
    db.session.commit()
    flash("PWA device approval setting updated.", "success")
    return redirect(url_for("twilio.comms_hub", tab="devices"))

@twilio_bp.route("/comms")
@login_required
def comms_hub():
    """Communications Hub — tabbed wrapper for SMS, Calls, Voicemail, Contacts, etc."""
    from flask_login import current_user
    from models import Contact, TwilioConversation, TwilioCallLog, TwilioPhoneNumber, UserCompanyAccess, User, AutoReplyRule, VoiceVoicemailMessage, MarketingAuditLog

    company = _get_company()
    if not company:
        flash("No company found.", "error")
        return redirect(url_for("main.dashboard"))

    ta           = _get_twilio_account(company.id)
    tab          = getattr(g, "comms_forced_tab", None) or request.args.get("tab", "overview")
    if tab == "sms":
        tab = "inbox"
    if tab == "logs":
        tab = "calls"
    phone_numbers = TwilioPhoneNumber.query.filter_by(company_id=company.id, is_active=True).order_by(TwilioPhoneNumber.is_primary.desc(), TwilioPhoneNumber.created_at.asc()).all()
    selected_number_id = request.args.get("number_id", type=int)
    selected_number = None
    if phone_numbers:
        selected_number = next((pn for pn in phone_numbers if pn.id == selected_number_id), None) or phone_numbers[0]
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
    from services.contact_resolver import resolve_contact_identity
    conversation_resolutions = {}
    for conv in conversations:
        resolved = resolve_contact_identity(company.id, phone=conv.from_number, contact_id=conv.contact_id, allow_enrichment=True)
        conversation_resolutions[conv.id] = resolved
        if resolved.canonical_contact_id and not conv.contact_id:
            conv.contact_id = resolved.canonical_contact_id
        if resolved.safe_display_name != "Name needed":
            conv.contact_name = resolved.safe_display_name
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
        for call in calls:
            lookup_phone = call.from_number if call.direction == "inbound" else call.to_number
            resolved = resolve_contact_identity(company.id, phone=lookup_phone, contact_id=getattr(call, "contact_id", None), allow_enrichment=True)
            if resolved.safe_display_name != "Name needed":
                call.caller_name = resolved.safe_display_name
        db.session.commit()
    except Exception:
        pass

    # ── Google Contacts bar ───────────────────────────────────────────────
    gc_connected = False
    gc_last_sync = None
    gc_contacts  = 0
    gc_status = "disconnected"
    gc_last_error = None
    try:
        from services.google_contacts import get_token
        tok = get_token(current_user.id)
        if tok and tok.access_token:
            gc_connected = True
            gc_contacts  = tok.contacts_synced or 0
            gc_last_sync = tok.last_sync_at.strftime("%-d %b %H:%M") if tok.last_sync_at else None
            gc_status = getattr(tok, "last_sync_status", None) or "connected"
            gc_last_error = getattr(tok, "sync_error", None)
    except Exception:
        pass

    is_admin = getattr(current_user, "is_admin", False) or getattr(current_user, "is_platform_admin", False)
    from services.comms_permissions import can_manage_users
    can_admin_tuya = can_manage_users(current_user, company.id)
    tuya = None
    tuya_events = None
    tuya_page = 1
    tuya_pages = 0
    if tab == "tuya_notifications":
        if not can_admin_tuya:
            abort(403)
        from models import TuyaNotificationActivation, TuyaNotificationEvent
        from services.tuya_notification import (
            as_utc, company_owns_integration, masked_configuration, worker_health,
        )
        cfg = masked_configuration()
        owner = company_owns_integration(company.id)
        activation = TuyaNotificationActivation.query.filter_by(company_id=company.id).first()
        event_filter = request.args.get("event_filter", "all")
        events_query = TuyaNotificationEvent.query.filter_by(company_id=company.id)
        if event_filter == "successful":
            events_query = events_query.filter_by(result="succeeded")
        elif event_filter == "failed":
            events_query = events_query.filter_by(result="failed")
        elif event_filter == "active":
            events_query = events_query.filter_by(requested_state=True, result="succeeded")
        elif event_filter == "manual_tests":
            events_query = events_query.filter_by(trigger_source="manual_test")
        tuya_page = max(1, request.args.get("page", 1, type=int))
        per_page = 25
        total = events_query.count()
        tuya_pages = (total + per_page - 1) // per_page
        tuya_events = events_query.order_by(TuyaNotificationEvent.received_at.desc()).offset(
            (tuya_page - 1) * per_page).limit(per_page).all()
        tuya = {
            "config": cfg, "owns_integration": owner, "worker": worker_health(),
            "activation": activation, "event_filter": event_filter,
            "device_state": "unknown",
            "remaining_seconds": max(
                0,
                int((as_utc(activation.off_deadline) - datetime.now(timezone.utc)).total_seconds()),
            ) if activation and activation.off_deadline else None,
        }
    users_with_access = []
    try:
        from models import PhoneNumberUserPermission
        for acc in UserCompanyAccess.query.filter_by(company_id=company.id).all():
            u = db.session.get(User, acc.user_id)
            if u:
                perm = None
                if selected_number:
                    perm = PhoneNumberUserPermission.query.filter_by(
                        phone_number_id=selected_number.id, user_id=u.id
                    ).first()
                users_with_access.append({"user": u, "access": acc, "permission": perm})
    except Exception:
        pass
    after_hours_effective = None
    rules = []
    try:
        selected_rule_number_id = selected_number.id if selected_number else None
        _ensure_visible_after_hours_rule(company.id, selected_rule_number_id)
        db.session.commit()
        after_hours_effective = _current_after_hours_effective_message(company.id, selected_rule_number_id)
        rules_query = AutoReplyRule.query.filter_by(company_id=company.id)
        if selected_rule_number_id is not None:
            rules_query = rules_query.filter(db.or_(
                AutoReplyRule.phone_number_id == selected_rule_number_id,
                AutoReplyRule.phone_number_id.is_(None),
            ))
        else:
            rules_query = rules_query.filter(AutoReplyRule.phone_number_id.is_(None))
        rules = (
            rules_query
            .order_by(AutoReplyRule.priority.desc(), AutoReplyRule.id.asc())
            .all()
        )
    except Exception as exc:
        logger.warning("Could not load Communications Hub auto-reply rules: %s", exc)
    voicemails = []
    try:
        voicemails = VoiceVoicemailMessage.query.filter_by(company_id=company.id, is_deleted=False).order_by(VoiceVoicemailMessage.created_at.desc()).limit(100).all()
    except Exception:
        pass
    activity = []
    try:
        activity = MarketingAuditLog.query.filter_by(company_id=company.id).order_by(MarketingAuditLog.created_at.desc()).limit(25).all()
    except Exception:
        pass
    devices = []
    try:
        devices = PWADevice.query.filter_by(company_id=company.id).order_by(PWADevice.last_seen_at.desc()).limit(100).all()
    except Exception:
        pass
    dedupe_preview = None
    if tab == "reports" and is_admin:
        try:
            from services.contact_dedupe import preview_duplicate_contacts
            dedupe_preview = preview_duplicate_contacts(company.id)
        except Exception as exc:
            logger.warning("Could not preview contact dedupe: %s", exc)

    approval_contacts = []
    if tab == "identity_review":
        approval_contacts = Contact.query.filter(
            Contact.company_id == company.id, Contact.is_active.is_(True),
            db.or_(Contact.identity_conflict_status != "none", Contact.approval_status == "review_required"),
        ).order_by(Contact.created_at.asc()).limit(200).all()
    return render_template(
        "twilio/comms_hub.html",
        tab=tab,
        company=company,
        conversations=conversations,
        conversation_resolutions=conversation_resolutions,
        unread_count=unread_count,
        ta=ta,
        status_filter=status_filter,
        search=search,
        calls=calls,
        missed_calls_count=missed_calls_count,
        gc_connected=gc_connected,
        gc_last_sync=gc_last_sync,
        gc_contacts=gc_contacts,
        gc_status=gc_status,
        gc_last_error=gc_last_error,
        is_admin=is_admin,
        can_admin_tuya=can_admin_tuya,
        tuya=tuya,
        tuya_events=tuya_events,
        tuya_page=tuya_page,
        tuya_pages=tuya_pages,
        phone_numbers=phone_numbers,
        selected_number=selected_number,
        users_with_access=users_with_access,
        rules=rules,
        after_hours_effective=after_hours_effective,
        voicemails=voicemails,
        activity=activity,
        devices=devices,
        dedupe_preview=dedupe_preview,
        approval_contacts=approval_contacts,
    )


@twilio_bp.route("/comms/contacts/<int:contact_id>/identity-review", methods=["POST"])
@login_required
def comms_identity_review(contact_id):
    from models import Contact, MarketingAuditLog
    from services.comms_permissions import can_manage_users
    from services.contact_audience import add_contact_tag
    company = _get_company()
    if not company: abort(404)
    if not can_manage_users(current_user, company.id): abort(403)
    contact = Contact.query.filter_by(id=contact_id, company_id=company.id, is_active=True).first_or_404()
    action = request.form.get("action")
    if action not in {"approve", "reject"}: abort(400)
    now = datetime.utcnow()
    if action == "approve":
        contact.approval_status, contact.approved_at = "approved", contact.approved_at or now
        contact.approved_by_user_id, contact.approval_source = current_user.id, "communications_hub"
        contact.approval_rejected_at, contact.identity_conflict_status = None, "none"
        if contact.identity_status == "awaiting_confirmation" and contact.pending_first_name and contact.pending_last_name and contact.pending_email:
            contact.identity_status = "confirmed"
        add_contact_tag(contact, "Approved Contact")
    else:
        contact.approval_status, contact.approval_rejected_at = "rejected", now
    db.session.add(MarketingAuditLog(company_id=company.id, created_by_user_id=current_user.id,
        entity_type="contact", entity_id=contact.id, action=f"identity_{action}", details={"conflict_status": contact.identity_conflict_status}))
    db.session.commit()
    flash(f"Identity {action}d.", "success")
    return redirect(url_for("twilio.comms_hub", tab="identity_review"))


@twilio_bp.route("/comms/tuya-notifications")
@login_required
def comms_tuya_notifications():
    """Stable URL for the Tuya tab inside the canonical Communications Hub."""
    g.comms_forced_tab = "tuya_notifications"
    return comms_hub()


def _tuya_admin_company():
    from services.comms_permissions import can_manage_users
    from services.tuya_notification import company_owns_integration
    company = _get_company()
    if not company:
        abort(404)
    if not can_manage_users(current_user, company.id):
        abort(403)
    if not company_owns_integration(company.id):
        abort(404)
    return company


@twilio_bp.route("/comms/tuya-notifications/test", methods=["POST"])
@login_required
def comms_tuya_test():
    company = _tuya_admin_company()
    from services.tuya_notification import configured, queue_manual_test, worker_health
    if not configured():
        flash("Tuya configuration is incomplete.", "error")
    elif worker_health()["status"] != "healthy":
        flash("The scheduler worker is not healthy.", "error")
    elif queue_manual_test(company.id, current_user.id):
        flash("The 60-second notification test was queued.", "success")
    else:
        flash("The request could not be queued. No credentials were changed.", "error")
    return redirect(url_for("twilio.comms_tuya_notifications"))


@twilio_bp.route("/comms/tuya-notifications/off", methods=["POST"])
@login_required
def comms_tuya_off():
    company = _tuya_admin_company()
    from services.tuya_notification import queue_manual_off
    if queue_manual_off(company.id, current_user.id):
        flash("An immediate OFF request was queued.", "success")
    else:
        flash("The request could not be queued. No credentials were changed.", "error")
    return redirect(url_for("twilio.comms_tuya_notifications"))



@twilio_bp.route("/comms/numbers/<int:number_id>/permissions", methods=["POST"])
@login_required
def comms_number_permissions(number_id):
    from models import TwilioPhoneNumber, PhoneNumberUserPermission, UserCompanyAccess
    from services.comms_permissions import can_manage_users, normalize_role, user_access_for_company
    company = _get_company()
    acc = user_access_for_company(current_user, company.id) if company else None
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    if not company or not (can_manage_users(current_user, company.id) or role in {"owner", "admin"}):
        abort(403)
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id).first_or_404()
    user_id = request.form.get("user_id", type=int)
    if not user_id or not UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company.id).first():
        flash("Select a valid tenant user.", "error")
        return redirect(url_for("twilio.comms_hub", tab="users", number_id=pn.id))
    perm = PhoneNumberUserPermission.query.filter_by(phone_number_id=pn.id, user_id=user_id).first()
    if not perm:
        perm = PhoneNumberUserPermission(company_id=company.id, phone_number_id=pn.id, user_id=user_id)
        db.session.add(perm)
    for field in ("can_access_pwa", "can_view_sms", "can_send_sms", "can_view_calls", "can_call", "can_view_voicemail", "can_manage_number", "can_send_campaigns"):
        setattr(perm, field, request.form.get(field) == "1")
    target_acc = UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company.id).first()
    if target_acc and perm.can_access_pwa:
        target_acc.assigned_number = pn.phone_number
        target_acc.pwa_access_enabled = True
        target_acc.comms_hub_enabled = True
    db.session.commit()
    flash(f"Permissions updated for {pn.phone_number}.", "success")
    return redirect(url_for("twilio.comms_hub", tab="users", number_id=pn.id))

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
            u = db.session.get(User, acc.user_id)
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
    from services.contact_resolver import resolve_contact_identity
    contact_resolution = resolve_contact_identity(company.id, phone=conv.from_number, contact_id=conv.contact_id, allow_enrichment=True)
    return render_template(
        "twilio/conversation.html",
        conv=conv,
        messages=messages,
        ta=ta,
        contact_resolution=contact_resolution,
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

    if not body:
        return jsonify({"success": False, "error": "Message body is required."})

    conv = None
    if conv_id:
        from models import TwilioConversation
        conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first()

    if conv and not to_number:
        to_number = conv.from_number
    if not to_number:
        return jsonify({"success": False, "error": "Recipient phone number is required."})
    if not conv:
        conv = _get_or_create_conversation(company.id, to_number, ta.from_phone or "")

    result = sendConversationSms(conv.id, body, twilio_account=ta, to_number=to_number)
    if result.get("success"):
        conv.last_message_at      = datetime.utcnow()
        conv.last_message_preview = f"You: {body[:150]}"
        conv.message_count        = (conv.message_count or 0) + 1
        db.session.commit()
    else:
        logger.error("/twilio/send failed company=%s conv=%s to=%s error=%s", company.id, conv.id, to_number, result.get("error"))
    return jsonify(result)


@twilio_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """Legacy Twilio settings route; Communications Hub is canonical."""
    return redirect(url_for("twilio.comms_hub", tab="integrations"), code=302)


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
    """Legacy auto-reply route; Communications Hub is canonical."""
    return redirect(url_for("twilio.comms_hub", tab="auto"))


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
        phone_number_id=f.get("phone_number_id", type=int),
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
        is_active=f.get("is_active", "1") == "1",
    )
    db.session.add(rule)
    db.session.commit()
    flash(f'Rule "{rule.name}" created.', "success")
    return redirect(url_for("twilio.comms_hub", tab="auto", number_id=rule.phone_number_id))


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
        "phone_number_id":   rule.phone_number_id,
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
    rule.phone_number_id    = f.get("phone_number_id", type=int)
    rule.trigger_type       = trigger_type
    rule.keywords           = keywords
    rule.response           = response_text
    rule.action             = action
    rule.forward_to         = f.get("forward_to", "").strip()
    rule.tag_value          = f.get("tag_value", "").strip()
    rule.priority           = int(f.get("priority") or rule.priority)
    rule.active_days        = active_days
    rule.is_active          = f.get("is_active") == "1"
    rule.active_hours_start = f.get("active_hours_start") or None
    rule.active_hours_end   = f.get("active_hours_end") or None

    db.session.commit()
    logger.info("Auto-reply rule %d (%s) updated by user", rule_id, rule.name)
    if request.headers.get("Accept") == "application/json" or request.is_json:
        return jsonify({"success": True, "name": rule.name})
    flash(f'Rule "{rule.name}" updated.', "success")
    return_number_id = f.get("return_number_id", type=int) or rule.phone_number_id
    return redirect(url_for("twilio.comms_hub", tab="auto", number_id=return_number_id))


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
    return_number_id = request.args.get("number_id", type=int) or request.form.get("return_number_id", type=int)
    return redirect(url_for("twilio.comms_hub", tab="auto", number_id=return_number_id))


@twilio_bp.route("/hours", methods=["GET", "POST"])
@login_required
def business_hours():
    """Legacy business-hours route; per-number hours live in Communications Hub."""
    return redirect(url_for("twilio.comms_hub", tab="hours"), code=302)


@twilio_bp.route("/calls")
@login_required
def calls():
    """Legacy call-log route; Communications Hub is canonical."""
    return redirect(url_for("twilio.comms_hub", tab="calls"), code=302)


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




@twilio_bp.route("/contacts/dedupe", methods=["POST"])
@login_required
def contact_dedupe_run():
    """Admin-only safe duplicate contact merge preview/run."""
    from services.comms_permissions import can_manage_users, normalize_role, user_access_for_company

    company = _get_company()
    acc = user_access_for_company(current_user, company.id) if company else None
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    is_admin = getattr(current_user, "is_admin", False) or getattr(current_user, "is_platform_admin", False)
    if not company or not (is_admin or can_manage_users(current_user, company.id) or role in {"owner", "admin"}):
        abort(403)

    dry_run = request.form.get("mode", "dry_run") != "run"
    from services.contact_dedupe import merge_duplicate_contacts
    result = merge_duplicate_contacts(company.id, dry_run=dry_run, actor_user_id=getattr(current_user, "id", None))
    if request.headers.get("Accept") == "application/json" or request.is_json:
        return jsonify({"success": True, "result": result})
    if dry_run:
        flash(
            f"Duplicate contact preview: {result['duplicate_groups']} group(s), "
            f"{result['contacts_merged']} contact(s) would be merged.",
            "info",
        )
    else:
        flash(
            f"Duplicate contact merge complete: {result['contacts_merged']} contact(s) merged; "
            f"{result['references_updated']} related record(s) repointed.",
            "success",
        )
    return redirect(url_for("twilio.comms_hub", tab="reports"))


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

def _mobile_inbox_return_url():
    """Return the safe post-OAuth destination for mobile-inbox-only users."""
    try:
        from models import UserCompanyAccess
        acc = UserCompanyAccess.query.filter_by(user_id=current_user.id).first()
        if acc and not acc.has_full_app_access() and acc.has_mobile_inbox_access():
            return "/app/inbox"
    except Exception as exc:
        logger.warning("Google Contacts return URL access check failed: %s", exc)
    return url_for("twilio.inbox")


def _google_contacts_redirect():
    return redirect(_mobile_inbox_return_url())


@twilio_bp.route("/google-contacts/connect")
@login_required
def google_contacts_connect():
    """Redirect user to Google OAuth consent screen."""
    from flask_login import current_user
    from services.google_contacts import get_auth_url
    import os
    if not os.environ.get("GOOGLE_CLIENT_ID"):
        flash("GOOGLE_CLIENT_ID is not configured. Add it in Settings → Secrets.", "danger")
        return _google_contacts_redirect()
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
        return _google_contacts_redirect()
    try:
        exchange_code(current_user.id, code)
    except Exception as exc:
        flash(f"Google Contacts connection failed: {exc}", "danger")
        return _google_contacts_redirect()
    # Run first sync immediately
    company = _get_company()
    if not company:
        flash("Google Contacts connected, but no company is assigned to this account.", "warning")
        return _google_contacts_redirect()
    try:
        result  = sync_contacts(current_user.id, company.id)
    except Exception as exc:
        logger.exception("Google Contacts initial sync failed for user %s", current_user.id)
        flash(f"Google Contacts connected, but sync failed: {exc}", "warning")
        return _google_contacts_redirect()
    if result.get("error"):
        flash(f"Connected but sync failed: {result['error']}", "warning")
    else:
        flash(
            f"Google Contacts connected. {result['synced']} contacts fetched, "
            f"{result['matched']} inbox names updated.",
            "success",
        )
    return _google_contacts_redirect()


@twilio_bp.route("/google-contacts/sync", methods=["POST"])
@login_required
def google_contacts_sync():
    """Manually trigger a contact sync. Returns JSON."""
    from flask_login import current_user
    from services.google_contacts import sync_contacts
    company = _get_company()
    dry_run = request.args.get("dry_run") in {"1", "true", "yes"} or bool((request.get_json(silent=True) or {}).get("dry_run"))
    result  = sync_contacts(current_user.id, company.id, dry_run=dry_run)
    return jsonify(result)


@twilio_bp.route("/google-contacts/status")
@login_required
def google_contacts_status():
    """Return connection status as JSON — includes sync error and expiry state."""
    from flask_login import current_user
    from services.google_contacts import get_token, token_needs_reconnect
    tok = get_token(current_user.id)
    if not tok:
        return jsonify({
            "connected":       False,
            "oauth_expired":   False,
            "last_sync_at":    None,
            "contacts_synced": 0,
            "sync_error":      None,
        })
    return jsonify({
        "connected":       True,
        "oauth_expired":   token_needs_reconnect(tok),
        "last_sync_at":    tok.last_sync_at.strftime("%b %-d %H:%M") if tok.last_sync_at else None,
        "contacts_synced": tok.contacts_synced or 0,
        "contacts_created": getattr(tok, "contacts_created", 0) or 0,
        "contacts_updated": getattr(tok, "contacts_updated", 0) or 0,
        "contacts_merged": getattr(tok, "contacts_merged", 0) or 0,
        "contacts_skipped": getattr(tok, "contacts_skipped", 0) or 0,
        "last_successful_sync_at": tok.last_successful_sync_at.strftime("%b %-d %H:%M") if getattr(tok, "last_successful_sync_at", None) else None,
        "last_sync_duration_ms": getattr(tok, "last_sync_duration_ms", None),
        "sync_status": getattr(tok, "last_sync_status", None),
        "google_account_email": getattr(tok, "google_account_email", None),
        "sync_error":      getattr(tok, "sync_error", None),
        "reconnect_required": token_needs_reconnect(tok),
    })


@twilio_bp.route("/google-contacts/disconnect", methods=["POST"])
@login_required
def google_contacts_disconnect():
    """Revoke and delete Google token."""
    from flask_login import current_user
    from services.google_contacts import disconnect
    disconnect(current_user.id)
    flash("Google Contacts disconnected.", "info")
    return _google_contacts_redirect()


# ======
# Phase A — Number Management Admin UI
# ======

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
    from services.comms_permissions import can_manage_users, normalize_role, user_access_for_company

    company = _get_company()
    acc = user_access_for_company(current_user, company.id) if company else None
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    is_admin = getattr(current_user, "is_admin", False) or getattr(current_user, "is_platform_admin", False)
    if not company or not (is_admin or can_manage_users(current_user, company.id) or role in {"owner", "admin"}):
        abort(403)

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
    from services.comms_permissions import can_manage_users, normalize_role, user_access_for_company

    company = _get_company()
    acc = user_access_for_company(current_user, company.id) if company else None
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    is_admin = getattr(current_user, "is_admin", False) or getattr(current_user, "is_platform_admin", False)
    if not company or not (is_admin or can_manage_users(current_user, company.id) or role in {"owner", "admin"}):
        abort(403)

    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id).first_or_404()

    pn.friendly_name          = request.form.get("friendly_name", pn.friendly_name).strip()
    pn.app_assignment         = request.form.get("app_assignment", pn.app_assignment).strip()
    pn.number_type            = request.form.get("number_type", pn.number_type).strip()
    pn.sms_enabled = request.form.get("sms_enabled", "1" if pn.sms_enabled else "0") == "1"
    pn.voice_enabled = request.form.get("voice_enabled", "1" if pn.voice_enabled else "0") == "1"
    pn.sms_forward_to         = request.form.get("sms_forward_to", "").strip() or None
    pn.sms_forwarding_enabled = request.form.get("sms_forwarding_enabled") == "1"
    pn.auto_reply_enabled     = request.form.get("auto_reply_enabled") == "1"
    pn.call_forward_to        = request.form.get("call_forward_to", "").strip() or None
    pn.voice_forwarding_enabled = request.form.get("voice_forwarding_enabled") == "1"
    pn.missed_call_text       = request.form.get("missed_call_text", "").strip() or None
    pn.voicemail_greeting_text = request.form.get("voicemail_greeting_text", "").strip() or None
    pn.voicemail_greeting_audio_url = request.form.get("voicemail_greeting_audio_url", "").strip() or None
    pn.business_hours = _business_hours_from_form(request.form) if any(k.startswith("bh_") for k in request.form.keys()) else (pn.business_hours or {})
    pn.timezone = request.form.get("timezone", pn.timezone or "America/Los_Angeles").strip()
    pn.during_hours_route = request.form.get("during_hours_route", pn.during_hours_route or "ring_pwa").strip()
    pn.after_hours_route = request.form.get("after_hours_route", pn.after_hours_route or "voicemail").strip()
    pn.browser_calling_enabled = request.form.get("browser_calling_enabled") == "1"
    pn.cell_callback_enabled = request.form.get("cell_callback_enabled") == "1"
    pn.wifi_only = request.form.get("wifi_only") == "1"
    pn.mobile_data_allowed = request.form.get("mobile_data_allowed") == "1"
    pn.fallback_behavior = request.form.get("fallback_behavior", pn.fallback_behavior or "cell_callback").strip()
    pn.caller_id_display_name = request.form.get("caller_id_display_name", "").strip() or None
    # SMS auto-reply copy lives in Auto Reply Rules. Keep only routing/toggles here.
    pn.after_hours_sms_enabled = request.form.get("after_hours_sms_enabled") == "1"
    pn.after_hours_voicemail_enabled = request.form.get("after_hours_voicemail_enabled") == "1"
    pn.notes                  = request.form.get("notes", "").strip() or None

    # Primary number toggle (only one per company)
    if request.form.get("is_primary") == "1":
        TwilioPhoneNumber.query.filter_by(company_id=company.id).update({"is_primary": False})
        pn.is_primary = True

    db.session.commit()
    flash(f"Number {pn.phone_number} updated.", "success")
    if request.form.get("return_to") == "comms":
        return redirect(url_for("twilio.comms_hub", tab="settings", number_id=pn.id))
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
