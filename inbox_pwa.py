"""
LUXit PWA Inbox — mobile-first messaging interface for Twilio SMS conversations.

Routes:
  GET  /app/inbox                                 — PWA shell page
  GET  /api/inbox/conversations                   — list conversations (JSON)
  GET  /api/inbox/conversations/<id>              — conversation + messages (JSON)
  POST /api/inbox/conversations/<id>/messages     — send reply
  PATCH /api/inbox/conversations/<id>/read        — mark read/unread
  PATCH /api/inbox/conversations/<id>/archive     — archive / unarchive
  PATCH /api/inbox/conversations/<id>/assign      — assign to user
  POST /api/inbox/conversations/<id>/notes        — update internal notes
  PATCH /api/inbox/conversations/<id>/rename      — rename unknown contact
  POST /api/inbox/conversations                   — start new conversation
  GET  /api/inbox/unread-count                    — unread badge count
  POST /api/inbox/push/subscribe                  — save Web Push subscription
  POST /api/inbox/push/test                       — send test push notification
  GET  /api/inbox/google-contacts/status          — Google Contacts OAuth + sync status
  POST /api/inbox/google-contacts/sync            — trigger manual Google Contacts sync
"""

import logging
import os
import queue as _queue_module
import re
import threading
from datetime import datetime, timezone

from flask import (Blueprint, Response, abort, current_app, g, jsonify,
                   render_template, request, session)

from extensions import db

logger = logging.getLogger(__name__)

inbox_pwa_bp = Blueprint("inbox_pwa", __name__)


# Access is controlled via UserCompanyAccess.has_mobile_inbox_access() — no PostHog required.


# ── SSE Event Bus ──────────────────────────────────────────────────────────────
# Keyed by company_id → list of Queue objects (one per connected SSE client).
# Works with gunicorn gthread workers (--worker-class gthread --threads N).
_sse_lock:      threading.Lock              = threading.Lock()
_sse_listeners: dict[int, list]             = {}

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _current_user():
    # Flask-Login is the canonical auth provider; use it first.
    # The app calls login_user() which stores the session key as "_user_id",
    # NOT "user_id" — so session.get("user_id") always returns None for users
    # who logged in via auth.py.  This caused the PWA to redirect every user
    # to /auth/login even when they had a valid session, which then bounced
    # them to /dashboard via _hub_redirect().
    from flask_login import current_user as _cu
    if _cu and _cu.is_authenticated:
        return _cu
    # Fallback for any code-path that sets a manual session key
    from models import User
    uid = session.get("user_id") or session.get("_user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def _require_auth():
    user = _current_user()
    if not user:
        abort(401, "Authentication required")
    return user


def _get_company(user):
    """Return the user's active company, or None — never falls back to an arbitrary company."""
    from models import Company, UserCompanyAccess
    # 1. Prefer the is_default=True access row
    acc = UserCompanyAccess.query.filter_by(user_id=user.id, is_default=True).first()
    if acc:
        c = db.session.get(Company, acc.company_id)
        if c:
            return c
    # 2. Any access row (first one found)
    acc = UserCompanyAccess.query.filter_by(user_id=user.id).first()
    if acc:
        c = db.session.get(Company, acc.company_id)
        if c:
            return c
    # 3. Explicit default_company_id pointer
    if user.default_company_id:
        c = db.session.get(Company, user.default_company_id)
        if c:
            return c
    # 4. Platform admins get the first active company as a fallback context
    if user.is_admin:
        return Company.query.filter_by(is_active=True).first()
    return None


def _check_mobile_inbox_access(user, company) -> bool:
    """Return True if the user may access the Mobile Inbox PWA.

    Access requires explicit admin approval per-user via
    UserCompanyAccess.can_access_mobile_inbox. Platform admins always pass.
    """
    if user.is_admin:
        return True
    if not company:
        return False
    from models import UserCompanyAccess
    acc = UserCompanyAccess.query.filter_by(
        user_id=user.id, company_id=company.id
    ).first()
    if acc:
        return acc.has_mobile_inbox_access()
    # No row for this specific company — try any company row
    any_acc = UserCompanyAccess.query.filter_by(user_id=user.id).first()
    if any_acc:
        return any_acc.has_mobile_inbox_access()
    # No access row means not approved yet.
    return False


def _is_mobile_request() -> bool:
    """Return True when the PWA shell is requested from a mobile device.

    The PWA is intentionally a mobile-only surface. Desktop users should use
    the standard SMS Inbox at /twilio/inbox instead of a duplicate desktop PWA
    shell.
    """
    sec_ch_mobile = (request.headers.get("Sec-CH-UA-Mobile") or "").strip().lower()
    if sec_ch_mobile == "?1":
        return True

    ua = (request.headers.get("User-Agent") or "").lower()
    mobile_tokens = (
        "android", "iphone", "ipad", "ipod", "mobile", "windows phone",
        "blackberry", "opera mini",
    )
    return any(token in ua for token in mobile_tokens)


def _require_company(user):
    """Resolve company context for APIs and avoid None.id crashes."""
    company = _get_company(user)
    if not company:
        abort(409, "No company configured for this user.")
    return company


def _get_twilio_account(company_id):
    from models import TwilioAccount
    return TwilioAccount.query.filter_by(company_id=company_id, is_active=True).first()


def _accessible_numbers_for(user, company_id: int) -> list[str]:
    from services.comms_permissions import accessible_phone_numbers
    return accessible_phone_numbers(user, company_id)


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

def _twilio_send_error_message(exc) -> str:
    """Return a PWA-safe, action-oriented explanation for Twilio send/call failures."""
    from services.twilio_error_handler import twilio_friendly_error
    return twilio_friendly_error(exc)


def _twilio_send_error_message_LEGACY(exc) -> str:
    """DEPRECATED — kept only as reference; use _twilio_send_error_message above."""
    raw = str(exc) or type(exc).__name__
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)

    guidance_by_code = {
        # ── SMS error codes ────────────────────────────────────────────
        21211: "The recipient phone number is invalid. Check the number and try again.",
        21408: "Twilio is not allowed to send SMS to that destination. Enable the region in Twilio Console → Voice & Messaging → Geo Permissions.",
        21606: "The configured Twilio From number cannot send SMS. Check SMS Settings or use an SMS-capable Twilio number.",
        21610: "This contact has opted out. They must reply START before texts can be sent again.",
        21612: "Twilio cannot route SMS to that phone number. Check the recipient number and carrier support.",
        21614: "The recipient does not appear to be a mobile/SMS-capable number.",
        21617: "The message is too long for Twilio to send. Shorten it and try again.",
        30003: "The carrier could not deliver the text. Verify the customer's number or try calling.",
        30004: "The destination handset could not receive the text. Try again later or call the customer.",
        30005: "The destination number is unknown or inactive. Check the customer's phone number.",
        30006: "The destination number is a landline or unreachable by SMS. Try calling instead.",
        30007: "The carrier filtered the message. Reword it to avoid links or promotional wording, then try again.",
        # ── Voice / call error codes ───────────────────────────────────
        20003: "Twilio authentication failed. Check the Account SID and Auth Token in SMS Settings.",
        13224: "Invalid phone number format. Make sure the number is in E.164 format (+1XXXXXXXXXX).",
        13227: "Twilio geographic permissions block calls to that destination. Enable the region in Twilio Console → Voice & Messaging → Geo Permissions.",
        21201: "International calling is not enabled for your Twilio number. Enable it in Twilio Console → Voice & Messaging → Geo Permissions.",
        21210: "The From number is not a valid Twilio phone number. Check SMS Settings.",
        21215: "Twilio does not have permission to dial that number. Enable geographic permissions or check your Twilio account status.",
        21216: "Your Twilio account is not authorized to call this number. If you are on a trial account, verify the number at twilio.com/console → Phone Numbers → Verified Caller IDs.",
        21217: "The dialled number is not reachable via Twilio. Confirm the number is correct and try again.",
        21218: "The From number does not have voice capability. Use a voice-capable Twilio number in SMS Settings.",
        21219: "The To number is not a valid, dialable phone number. Check the number and try again.",
        21401: "Twilio could not parse the phone number. Ensure it starts with + and the country code.",
        32016: "Twilio cannot locate your calling app. Check that your Twilio voice webhook is configured.",
    }
    if code in guidance_by_code:
        return guidance_by_code[code]

    lower_raw = raw.lower()

    # Trial-account restriction (number not verified)
    if ("unverified" in lower_raw or "verified caller" in lower_raw) and (
        "trial" in lower_raw or "upgrade" in lower_raw
    ):
        return (
            "Twilio trial accounts can only call/text numbers you have verified. "
            "Go to twilio.com/console → Phone Numbers → Verified Caller IDs and add this number, "
            "or upgrade your Twilio account to a paid plan."
        )

    if status == 403 or (
        ("unable to create record" in lower_raw and "forbidden" in lower_raw)
        or "http 403" in lower_raw
    ):
        return (
            "Twilio 403: On trial accounts the destination must be a Verified Caller ID "
            "(twilio.com/console → Verified Caller IDs). "
            "Otherwise check Geo Permissions or Voice capability on your Twilio number."
        )

    if "authenticate" in lower_raw or "authentication" in lower_raw or "account sid" in lower_raw:
        return "Twilio authentication failed. Check the Account SID and Auth Token in SMS Settings."
    if "not a valid phone number" in lower_raw or "is not a valid" in lower_raw:
        return "The phone number is invalid. Make sure it includes the country code (e.g. +15551234567)."
    if "geo permission" in lower_raw or "geographic" in lower_raw:
        return "Twilio geographic permissions block that destination. Enable the region in Twilio Console → Voice & Messaging → Geo Permissions."

    return raw


def _send_sms_internal(ta, to_number: str, body: str, conversation_id=None):
    """Send SMS via Twilio — mirrors twilio_sms._send_sms.

    Keep customer SMS content isolated from notification/log text. Only
    ``sms_body`` is sent to Twilio and persisted as the message body.
    """
    from models import TwilioMessage
    sms_body = _sanitize_body(body)
    try:
        from twilio.rest import Client
        sid = ta.get_account_sid() if hasattr(ta, 'get_account_sid') else ta._account_sid
        tok = ta.get_auth_token()  if hasattr(ta, 'get_auth_token')  else ta._auth_token
        client = Client(sid, tok)
        kwargs = {"body": sms_body, "to": to_number}
        if ta.messaging_service_sid:
            kwargs["messaging_service_sid"] = ta.messaging_service_sid
        elif ta.from_phone:
            kwargs["from_"] = ta.from_phone
        else:
            return None, "No From number or Messaging Service SID configured."
        msg = client.messages.create(**kwargs)
        record = TwilioMessage(
            conversation_id=conversation_id,
            company_id=ta.company_id,
            twilio_sid=msg.sid,
            direction="outbound",
            from_number=ta.from_phone or ta.messaging_service_sid,
            to_number=to_number,
            body=sms_body,
            status=msg.status,
        )
        db.session.add(record)
        db.session.commit()
        logger.info("PWA Inbox outbound SMS: sid=%s to=%s", msg.sid, to_number)
        return record, None
    except Exception as exc:
        logger.error("PWA Inbox SMS send error: %s", exc)
        return None, _twilio_send_error_message(exc)


def _conv_to_dict(conv, brief=True):
    tags = _safe_conversation_tags(conv.tags)
    contact_source = getattr(conv, "contact_source", None)
    d = {
        "id":                  conv.id,
        "from_number":         conv.from_number,
        "contact_name":        conv.contact_name or conv.from_number,
        "display_name":        conv.contact_name or conv.from_number,
        "contact_id":          conv.contact_id,
        "contact_source":      contact_source,
        "is_read":             conv.is_read,
        "is_opted_out":        conv.is_opted_out,
        "is_archived":         "archived" in tags,
        "tags":                tags,
        "assigned_user_id":    conv.assigned_user_id,
        "last_message_at":     conv.last_message_at.isoformat() if conv.last_message_at else None,
        "last_message_preview": conv.last_message_preview or "",
        "message_count":       conv.message_count or 0,
    }
    if not brief:
        d["notes"] = conv.notes or ""
        if conv.assigned_user_id:
            from models import User
            u = db.session.get(User, conv.assigned_user_id)
            d["assigned_user_name"] = u.username if u else None
        else:
            d["assigned_user_name"] = None
    return d


def _safe_conversation_tags(raw_tags):
    """Return conversation tags as a normalized list for JSON/null/string values."""
    if raw_tags is None:
        return []
    if isinstance(raw_tags, list):
        return raw_tags
    if isinstance(raw_tags, tuple):
        return list(raw_tags)
    if isinstance(raw_tags, str):
        return [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
    return []


def _is_archived_conversation(conv):
    return "archived" in _safe_conversation_tags(getattr(conv, "tags", None))


def _msg_to_dict(m):
    return {
        "id":           m.id,
        "direction":    m.direction,
        "body":         m.body or "",
        "status":       m.status or "received",
        "is_auto_reply": m.is_auto_reply,
        "media_urls":   m.media_urls or [],
        "created_at":   m.created_at.isoformat() if m.created_at else None,
        "twilio_sid":   m.twilio_sid,
    }


# ── PWA shell page ────────────────────────────────────────────────────────────

@inbox_pwa_bp.route("/app/inbox")
def pwa_index():
    from flask import redirect, url_for
    user = _current_user()
    if not user:
        return redirect("/auth/login?next=/app/inbox")
    if not _is_mobile_request():
        return redirect(url_for("twilio.inbox"))
    company = _get_company(user)
    # No company assigned → show setup page
    if not company:
        return render_template("no_company.html", user=user)
    # Access gate — role/flag based, not PostHog
    if not _check_mobile_inbox_access(user, company):
        return render_template(
            "inbox_access_denied.html", user=user, company=company
        ), 403
    vapid_public = os.environ.get("VAPID_PUBLIC_KEY", "")
    return render_template(
        "inbox_pwa/index.html",
        user=user,
        company=company,
        vapid_public=vapid_public,
    )


@inbox_pwa_bp.route("/app/calls")
@inbox_pwa_bp.route("/app/calls/settings")
def pwa_calls():
    from flask import redirect
    user = _current_user()
    if not user:
        return redirect("/auth/login?next=/app/calls")
    company = _get_company(user)
    if not company:
        return render_template("no_company.html", user=user)
    if not _check_mobile_inbox_access(user, company):
        return render_template("inbox_access_denied.html", user=user, company=company), 403
    return render_template("inbox_pwa/calls.html", user=user, company=company)


def _call_to_dict(call):
    voicemail_text = getattr(call, "transcription_text", None)
    return {
        "id": call.id,
        "twilio_call_sid": call.twilio_sid,
        "parent_call_sid": getattr(call, "parent_call_sid", None),
        "direction": call.direction,
        "status": call.status,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "assigned_business_number": call.to_number if call.direction == "inbound" else call.from_number,
        "forwarded_to_number": getattr(call, "forwarded_to_number", None),
        "caller_name": call.caller_name or call.from_number,
        "contact_name": call.caller_name or call.from_number,
        "label": "Missed" if call.status in ("missed", "no-answer", "busy", "failed") else ("Outgoing" if call.direction == "outbound" else "Incoming"),
        "duration_seconds": call.duration or 0,
        "recording_url": getattr(call, "recording_url", None),
        "recording_sid": getattr(call, "recording_sid", None),
        "voicemail_url": getattr(call, "voicemail_url", None),
        "voicemail_exists": bool(getattr(call, "voicemail_url", None) or call.status == "voicemail"),
        "voicemail_sid": getattr(call, "voicemail_sid", None),
        "transcription_text": voicemail_text,
        "transcription_preview": (voicemail_text or "")[:160],
        "transcription_status": getattr(call, "transcription_status", None),
        "transcription_provider": getattr(call, "transcription_provider", None),
        "transcription_error": getattr(call, "transcription_error", None),
        "transcribed_at": call.transcribed_at.isoformat() if getattr(call, "transcribed_at", None) else None,
        "answered_by_user_id": getattr(call, "answered_by_user_id", None),
        "answered_at": call.answered_at.isoformat() if getattr(call, "answered_at", None) else None,
        "started_at": call.created_at.isoformat() if call.created_at else None,
        "ended_at": call.ended_at.isoformat() if getattr(call, "ended_at", None) else None,
        "is_read": getattr(call, "is_read", False),
        "read_at": call.read_at.isoformat() if getattr(call, "read_at", None) else None,
        "callback_target": getattr(call, "callback_target", None) or (call.from_number if call.direction == "inbound" else call.to_number),
        "is_archived": getattr(call, "is_archived", False),
        "created_at": call.created_at.isoformat() if call.created_at else None,
        "updated_at": call.updated_at.isoformat() if getattr(call, "updated_at", None) else None,
    }


def _phone_number_for_payload(company_id, user, payload):
    """Resolve and authorize a TwilioPhoneNumber from an API payload."""
    from models import TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers
    allowed = set(accessible_phone_numbers(user, company_id))
    number_id = payload.get("phone_number_id")
    phone_number = payload.get("phone_number") or payload.get("selected_number")
    q = TwilioPhoneNumber.query.filter_by(company_id=company_id, is_active=True)
    if number_id:
        pn = q.filter_by(id=number_id).first()
    elif phone_number:
        pn = q.filter_by(phone_number=phone_number).first()
    else:
        pn = None
    if pn and pn.phone_number not in allowed:
        abort(403, "No access to that phone number")
    return pn


def _device_to_dict(device):
    pn = getattr(device, "phone_number", None)
    user = getattr(device, "user", None)
    return {
        "id": device.id,
        "device_name": device.device_name or "PWA Device",
        "assigned_user": (user.email or user.username) if user else None,
        "assigned_user_id": device.user_id,
        "phone_number_id": device.phone_number_id,
        "assigned_phone_number": pn.phone_number if pn else None,
        "default_line": pn.friendly_name or pn.phone_number if pn else None,
        "browser": device.browser,
        "device_type": device.device_type,
        "online_status": device.online_status,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "push_enabled": bool(device.push_enabled),
        "microphone_permission": device.microphone_permission or "unknown",
        "pwa_installed": bool(device.pwa_installed),
        "wifi_only": bool(device.wifi_only),
        "cellular_callback_enabled": bool(device.cellular_callback_enabled),
        "mobile_data_calling_allowed": bool(device.mobile_data_calling_allowed),
        "default_calling_method": device.default_calling_method or "browser",
    }


def _upsert_pwa_device(user, company, payload):
    from models import PWADevice
    pn = _phone_number_for_payload(company.id, user, payload)
    device_key = (payload.get("device_key") or payload.get("installation_id") or "").strip()
    if not device_key:
        abort(400, "device_key is required")
    device = PWADevice.query.filter_by(company_id=company.id, user_id=user.id, device_key=device_key).first()
    if not device:
        device = PWADevice(company_id=company.id, user_id=user.id, device_key=device_key)
        db.session.add(device)
    device.phone_number_id = pn.id if pn else device.phone_number_id
    for field in ("device_name", "browser", "device_type", "microphone_permission", "default_calling_method"):
        if field in payload:
            setattr(device, field, (payload.get(field) or "")[:120])
    device.user_agent = request.headers.get("User-Agent", "")[:1000]
    device.online_status = "online"
    device.last_seen_at = datetime.utcnow()
    device.push_enabled = bool(payload.get("push_enabled", device.push_enabled))
    device.pwa_installed = bool(payload.get("pwa_installed", device.pwa_installed))
    device.wifi_only = bool(payload.get("wifi_only", device.wifi_only))
    device.cellular_callback_enabled = bool(payload.get("cellular_callback_enabled", device.cellular_callback_enabled))
    device.mobile_data_calling_allowed = bool(payload.get("mobile_data_calling_allowed", device.mobile_data_calling_allowed))
    return device


# ── API: accessible phone numbers ───────────────────────────────────────────

@inbox_pwa_bp.route("/api/phone/numbers")
def api_phone_numbers():
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioPhoneNumber, TwilioAccount
    numbers = _accessible_numbers_for(user, company.id)
    rows = []
    for pn in TwilioPhoneNumber.query.filter(TwilioPhoneNumber.company_id == company.id, TwilioPhoneNumber.phone_number.in_(numbers or ["__none__"])).all():
        rows.append({
            "id": pn.id,
            "phone_number": pn.phone_number,
            "friendly_name": pn.friendly_name or pn.phone_number,
            "sms_enabled": bool(pn.sms_enabled),
            "voice_enabled": bool(pn.voice_enabled),
            "caller_id": pn.friendly_name or pn.phone_number,
        })
    known = {r["phone_number"] for r in rows}
    for ta in TwilioAccount.query.filter_by(company_id=company.id).all():
        if ta.from_phone in numbers and ta.from_phone not in known:
            rows.append({"id": None, "phone_number": ta.from_phone, "friendly_name": ta.from_phone, "sms_enabled": True, "voice_enabled": True, "caller_id": ta.from_phone})
    return jsonify({"success": True, "numbers": rows})


@inbox_pwa_bp.route("/api/pwa/preferences", methods=["GET", "PATCH"])
def api_pwa_preferences():
    user = _require_auth()
    _require_company(user)
    if request.method == "PATCH":
        data = request.get_json() or {}
        palette = (data.get("palette_id") or data.get("palette") or "lux").strip()
        if palette not in {"lux", "ocean", "forest", "sunset"}:
            return jsonify({"success": False, "error": "Unsupported palette."}), 400
        user.pwa_palette_id = palette
        if data.get("theme_mode") in {"dark", "light", "system"}:
            user.pwa_theme_mode = data.get("theme_mode")
        user.pwa_preferences_updated_at = datetime.utcnow()
        db.session.commit()
    return jsonify({
        "success": True,
        "preferences": {
            "palette_id": user.pwa_palette_id or "lux",
            "theme_mode": user.pwa_theme_mode or "dark",
            "updated_at": user.pwa_preferences_updated_at.isoformat() if user.pwa_preferences_updated_at else None,
        }
    })


@inbox_pwa_bp.route("/api/pwa/devices")
def api_pwa_devices():
    user = _require_auth()
    company = _require_company(user)
    from models import PWADevice
    from services.comms_permissions import accessible_phone_numbers, can_manage_users
    allowed = set(accessible_phone_numbers(user, company.id))
    q = PWADevice.query.filter_by(company_id=company.id)
    if not can_manage_users(user, company.id):
        q = q.filter(PWADevice.user_id == user.id)
    devices = []
    for device in q.order_by(PWADevice.last_seen_at.desc()).all():
        pn = getattr(device, "phone_number", None)
        if pn and pn.phone_number not in allowed:
            continue
        devices.append(_device_to_dict(device))
    return jsonify({"success": True, "devices": devices})


@inbox_pwa_bp.route("/api/pwa/devices/register", methods=["POST"])
def api_pwa_device_register():
    user = _require_auth()
    company = _require_company(user)
    device = _upsert_pwa_device(user, company, request.get_json() or {})
    db.session.commit()
    return jsonify({
        "success": True,
        "device": _device_to_dict(device),
        "preferences": {
            "palette_id": user.pwa_palette_id or "lux",
            "theme_mode": user.pwa_theme_mode or "dark",
        },
    })


@inbox_pwa_bp.route("/api/pwa/devices/heartbeat", methods=["POST"])
def api_pwa_device_heartbeat():
    user = _require_auth()
    company = _require_company(user)
    device = _upsert_pwa_device(user, company, request.get_json() or {})
    db.session.commit()
    return jsonify({"success": True, "device": _device_to_dict(device)})


@inbox_pwa_bp.route("/api/pwa/devices/<int:device_id>/settings", methods=["PATCH"])
def api_pwa_device_settings(device_id):
    user = _require_auth()
    company = _require_company(user)
    from models import PWADevice
    from services.comms_permissions import can_manage_users
    device = PWADevice.query.filter_by(id=device_id, company_id=company.id).first_or_404()
    if device.user_id != user.id and not can_manage_users(user, company.id):
        abort(403)
    data = request.get_json() or {}
    if "phone_number_id" in data or "phone_number" in data or "selected_number" in data:
        pn = _phone_number_for_payload(company.id, user, data)
        device.phone_number_id = pn.id if pn else None
    for field in ("device_name", "microphone_permission", "default_calling_method"):
        if field in data:
            setattr(device, field, (data.get(field) or "")[:120])
    for field in ("push_enabled", "pwa_installed", "wifi_only", "cellular_callback_enabled", "mobile_data_calling_allowed"):
        if field in data:
            setattr(device, field, bool(data.get(field)))
    device.last_seen_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "device": _device_to_dict(device)})


# ── API: conversation list ────────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/calls/recent")
def api_recent_calls():
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioCallLog
    tab = request.args.get("tab", "all")
    from services.comms_permissions import filter_calls_for_user
    q = filter_calls_for_user(TwilioCallLog.query.filter_by(company_id=company.id, is_archived=False), user, company.id)
    if tab == "missed":
        q = q.filter(TwilioCallLog.status.in_(["missed", "no-answer", "busy", "failed"]))
    elif tab == "voicemail":
        q = q.filter(TwilioCallLog.status == "voicemail")
    elif tab == "recordings":
        q = q.filter(TwilioCallLog.recording_url.isnot(None))
    elif tab == "forwarded":
        q = q.filter(db.or_(TwilioCallLog.status == "forwarded", TwilioCallLog.forwarded_to_number.isnot(None)))
    calls = q.order_by(TwilioCallLog.created_at.desc()).limit(100).all()
    return jsonify({"calls": [_call_to_dict(c) for c in calls]})


@inbox_pwa_bp.route("/api/calls/<int:call_id>")
def api_call_detail(call_id):
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioCallLog
    from services.comms_permissions import filter_calls_for_user
    call = filter_calls_for_user(TwilioCallLog.query.filter_by(id=call_id, company_id=company.id), user, company.id).first_or_404()
    return jsonify({"call": _call_to_dict(call)})


@inbox_pwa_bp.route("/api/calls/<int:call_id>/mark-read", methods=["POST"])
def api_call_mark_read(call_id):
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioCallLog, VoiceVoicemailMessage
    from services.comms_permissions import filter_calls_for_user
    call = filter_calls_for_user(TwilioCallLog.query.filter_by(id=call_id, company_id=company.id), user, company.id).first_or_404()
    call.is_read = True
    call.read_at = datetime.utcnow()
    call.read_by_user_id = user.id
    vm = VoiceVoicemailMessage.query.filter_by(call_log_id=call.id, company_id=company.id).first()
    if vm:
        vm.is_read = True
        vm.read_at = call.read_at
        vm.read_by_user_id = user.id
    db.session.commit()
    return jsonify({"success": True, "call": _call_to_dict(call)})


@inbox_pwa_bp.route("/api/calls/<int:call_id>/archive", methods=["POST"])
def api_call_archive(call_id):
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioCallLog, VoiceVoicemailMessage
    from services.comms_permissions import filter_calls_for_user
    call = filter_calls_for_user(TwilioCallLog.query.filter_by(id=call_id, company_id=company.id), user, company.id).first_or_404()
    call.is_archived = True
    vm = VoiceVoicemailMessage.query.filter_by(call_log_id=call.id, company_id=company.id).first()
    if vm:
        vm.is_deleted = True
    db.session.commit()
    return jsonify({"success": True})


@inbox_pwa_bp.route("/api/calls/voicemails")
def api_voicemails():
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioCallLog
    from services.comms_permissions import filter_calls_for_user
    calls = filter_calls_for_user(
        TwilioCallLog.query.filter_by(company_id=company.id, status="voicemail", is_archived=False),
        user, company.id,
    ).order_by(TwilioCallLog.created_at.desc()).limit(100).all()
    return jsonify({"voicemails": [_call_to_dict(c) for c in calls]})


def _default_business_hours():
    return {str(i): {"is_open": i < 5, "open": "09:00", "close": "17:00"} for i in range(7)}


def _settings_to_dict(settings):
    return {
        "business_hours": settings.business_hours or _default_business_hours(),
        "timezone": settings.timezone,
        "during_hours_route": settings.during_hours_route,
        "after_hours_route": settings.after_hours_route,
        "forward_number": settings.forward_number,
        "fallback_forward_number": settings.fallback_forward_number,
        "after_hours_forward_number": settings.after_hours_forward_number,
        "after_hours_fallback_forward_number": settings.after_hours_fallback_forward_number,
        "ring_duration_seconds": settings.ring_duration_seconds,
        "voicemail_greeting": settings.voicemail_greeting,
        "after_hours_voicemail_greeting": settings.after_hours_voicemail_greeting,
        "missed_call_sms_enabled": settings.missed_call_sms_enabled,
        "missed_call_sms_body": settings.missed_call_sms_body,
        "after_hours_sms_enabled": settings.after_hours_sms_enabled,
        "after_hours_sms_body": settings.after_hours_sms_body,
        "recording_enabled": settings.recording_enabled,
        "transcription_enabled": settings.transcription_enabled,
    }



@inbox_pwa_bp.route("/api/phone/numbers/<int:number_id>/settings", methods=["GET", "PUT"])
def api_phone_number_settings(number_id):
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers, can_manage_users
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id, is_active=True).first_or_404()
    if pn.phone_number not in accessible_phone_numbers(user, company.id):
        abort(404)
    editable = can_manage_users(user, company.id)
    if request.method == "PUT":
        if not editable:
            return jsonify({"success": False, "error": "Permission denied."}), 403
        data = request.get_json() or {}
        allowed = {
            "business_hours", "timezone", "during_hours_route", "after_hours_route",
            "sms_forward_to", "sms_forwarding_enabled", "auto_reply_enabled",
            "call_forward_to", "voice_forwarding_enabled", "ring_timeout",
            "voicemail_greeting_text", "voicemail_greeting_audio_url",
            "after_hours_text", "after_hours_sms_enabled", "after_hours_voicemail_enabled",
            "browser_calling_enabled", "cell_callback_enabled", "wifi_only",
            "mobile_data_allowed", "fallback_behavior", "caller_id_display_name",
        }
        for key in allowed:
            if key in data:
                setattr(pn, key, data[key])
        db.session.commit()
    return jsonify({
        "success": True,
        "editable": editable,
        "settings": {
            "id": pn.id,
            "phone_number": pn.phone_number,
            "friendly_name": pn.friendly_name or pn.phone_number,
            "business_hours": pn.business_hours or _default_business_hours(),
            "timezone": pn.timezone,
            "during_hours_route": pn.during_hours_route,
            "after_hours_route": pn.after_hours_route,
            "sms_forward_to": pn.sms_forward_to,
            "sms_forwarding_enabled": pn.sms_forwarding_enabled,
            "auto_reply_enabled": pn.auto_reply_enabled,
            "call_forward_to": pn.call_forward_to,
            "voice_forwarding_enabled": pn.voice_forwarding_enabled,
            "ring_timeout": pn.ring_timeout,
            "voicemail_greeting_text": pn.voicemail_greeting_text,
            "voicemail_greeting_audio_url": pn.voicemail_greeting_audio_url,
            "after_hours_text": pn.after_hours_text,
            "after_hours_sms_enabled": pn.after_hours_sms_enabled,
            "after_hours_voicemail_enabled": pn.after_hours_voicemail_enabled,
            "browser_calling_enabled": pn.browser_calling_enabled,
            "cell_callback_enabled": pn.cell_callback_enabled,
            "wifi_only": pn.wifi_only,
            "mobile_data_allowed": pn.mobile_data_allowed,
            "fallback_behavior": pn.fallback_behavior,
            "caller_id_display_name": pn.caller_id_display_name,
        }
    })

@inbox_pwa_bp.route("/api/phone/settings", methods=["GET", "PUT"])
def api_phone_settings():
    user = _require_auth()
    company = _require_company(user)
    from models import PhoneSettings
    settings = PhoneSettings.query.filter_by(company_id=company.id).first()
    if not settings:
        settings = PhoneSettings(company_id=company.id, business_hours=_default_business_hours(), timezone=os.environ.get("DEFAULT_PHONE_TIMEZONE", "America/Los_Angeles"))
        db.session.add(settings)
        db.session.commit()
    if request.method == "PUT":
        data = request.get_json() or {}
        allowed = set(_settings_to_dict(settings).keys())
        for key in allowed:
            if key in data:
                setattr(settings, key, data[key])
        db.session.commit()
    return jsonify({"settings": _settings_to_dict(settings)})


@inbox_pwa_bp.route("/api/phone/test-forwarding", methods=["POST"])
def api_test_forwarding():
    user = _require_auth()
    company = _require_company(user)
    data = request.get_json() or {}
    number = data.get("number")
    if not number:
        return jsonify({"success": False, "error": "number is required"}), 400
    return jsonify({"success": True, "message": f"Forwarding target {number} is syntactically valid. Place a live Twilio test call to verify carrier reachability."})


@inbox_pwa_bp.route("/api/phone/voice-token")
def api_phone_voice_token():
    """Issue a Twilio Voice SDK access token for the current tenant PWA."""
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioAccount
    ta = TwilioAccount.query.filter_by(company_id=company.id).first()
    account_sid = (
        os.environ.get("TWILIO_ACCOUNT_SID")
        or (ta.get_account_sid() if ta and hasattr(ta, "get_account_sid") else None)
    )
    api_key = os.environ.get("TWILIO_API_KEY")
    api_secret = os.environ.get("TWILIO_API_SECRET")
    if not account_sid or not api_key or not api_secret:
        return jsonify({
            "success": False,
            "error": "Twilio Voice SDK credentials are not configured. Set TWILIO_ACCOUNT_SID, TWILIO_API_KEY, and TWILIO_API_SECRET.",
        }), 503
    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant
        from services.phone_identity import pwa_voice_identity
        identity = pwa_voice_identity(company.id)
        token = AccessToken(account_sid, api_key, api_secret, identity=identity)
        token.add_grant(VoiceGrant(
            incoming_allow=True,
            outgoing_application_sid=os.environ.get("TWILIO_TWIML_APP_SID"),
        ))
        return jsonify({
            "success": True,
            "token": token.to_jwt(),
            "identity": identity,
            "user_id": user.id,
            "company_id": company.id,
        })
    except Exception as exc:
        logger.exception("Unable to issue Twilio Voice token")
        return jsonify({"success": False, "error": str(exc)}), 500


def _update_call_action(call_id: int, *, status: str, user_field: str | None = None):
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioCallLog, CallEvent
    from services.comms_permissions import filter_calls_for_user
    call = filter_calls_for_user(TwilioCallLog.query.filter_by(id=call_id, company_id=company.id), user, company.id).first_or_404()
    call.status = status
    if user_field:
        setattr(call, user_field, user.id)
    if status == "answered":
        call.answered_at = call.answered_at or datetime.utcnow()
    if status in ("completed", "declined", "voicemail"):
        call.ended_at = call.ended_at or datetime.utcnow()
    event_id = f"pwa:{status}:{user.id}"
    if not CallEvent.query.filter_by(call_log_id=call.id, event_type="pwa_action", provider_event_id=event_id).first():
        db.session.add(CallEvent(
            call_log_id=call.id,
            event_type="pwa_action",
            provider_event_id=event_id,
            payload={"status": status, "user_id": user.id},
        ))
    db.session.commit()
    return jsonify({"success": True, "call": _call_to_dict(call)})


@inbox_pwa_bp.route("/api/calls/<int:call_id>/accept", methods=["POST"])
def api_call_accept(call_id):
    return _update_call_action(call_id, status="answered", user_field="answered_by_user_id")


@inbox_pwa_bp.route("/api/calls/<int:call_id>/decline", methods=["POST"])
def api_call_decline(call_id):
    return _update_call_action(call_id, status="declined")


@inbox_pwa_bp.route("/api/calls/<int:call_id>/voicemail", methods=["POST"])
def api_call_send_voicemail(call_id):
    return _update_call_action(call_id, status="voicemail")


@inbox_pwa_bp.route("/api/calls/<int:call_id>/end", methods=["POST"])
def api_call_end(call_id):
    return _update_call_action(call_id, status="completed")

@inbox_pwa_bp.route("/api/inbox/conversations")
def list_conversations():
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"conversations": [], "unread_count": 0, "total": 0})

    from models import TwilioConversation
    filter_by = request.args.get("filter", "all")
    search    = request.args.get("q", "").strip()
    page      = int(request.args.get("page", 1))
    number_filter = (request.args.get("number") or "").strip()

    from services.comms_permissions import filter_conversations_for_user, accessible_phone_numbers
    q = filter_conversations_for_user(TwilioConversation.query.filter_by(company_id=company.id), user, company.id)

    if filter_by == "unread":
        q = q.filter_by(is_read=False)
    elif filter_by == "mine":
        q = q.filter_by(assigned_user_id=user.id)

    if filter_by == "opted_out":
        q = q.filter_by(is_opted_out=True)

    if number_filter:
        digits = lambda value: re.sub(r"\D", "", value or "")
        requested_digits = digits(number_filter)
        allowed_numbers = accessible_phone_numbers(user, company.id)
        matched_number = next((n for n in allowed_numbers if digits(n) == requested_digits), None)
        if not matched_number:
            q = q.filter(db.text("1=0"))
        else:
            q = q.filter(TwilioConversation.to_number == matched_number)

    if search:
        q = q.filter(db.or_(
            TwilioConversation.from_number.ilike(f"%{search}%"),
            TwilioConversation.contact_name.ilike(f"%{search}%"),
            TwilioConversation.last_message_preview.ilike(f"%{search}%"),
        ))

    unread_count = filter_conversations_for_user(
        TwilioConversation.query.filter_by(company_id=company.id, is_read=False),
        user, company.id,
    ).count()

    convs = q.order_by(TwilioConversation.last_message_at.desc()).all()
    if filter_by == "archived":
        convs = [c for c in convs if _is_archived_conversation(c)]
    else:
        convs = [c for c in convs if not _is_archived_conversation(c)]

    total = len(convs)
    per_page = 50
    convs = convs[(page - 1) * per_page:page * per_page]

    return jsonify({
        "success":       True,
        "conversations": [_conv_to_dict(c) for c in convs],
        "unread_count":  unread_count,
        "total":         total,
        "page":          page,
    })


# ── API: single conversation + messages ───────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>")
def get_conversation(conv_id):
    user    = _require_auth()
    company = _require_company(user)
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()

    # Mark as read when opened
    if not conv.is_read:
        conv.is_read = True
        db.session.commit()

    msgs = conv.messages.order_by(db.text("twilio_message.created_at")).all()

    # Contact info enrichment
    contact_data = None
    if conv.contact_id:
        from models import Contact
        c = db.session.get(Contact, conv.contact_id)
        if c:
            contact_data = {
                "id":    c.id,
                "name":  f"{c.first_name or ''} {c.last_name or ''}".strip(),
                "email": c.email,
                "phone": c.phone,
                "tags":  c.tags,
            }

    return jsonify({
        "conversation": _conv_to_dict(conv, brief=False),
        "messages":     [_msg_to_dict(m) for m in msgs],
        "contact":      contact_data,
    })


# ── API: send message ─────────────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/messages", methods=["POST"])
def send_message(conv_id):
    user    = _require_auth()
    company = _require_company(user)
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()

    payload = request.get_json() or {}
    body    = (payload.get("body") or "").strip()
    if not body:
        return jsonify({"success": False, "error": "Message body is required."}), 400

    ta = _get_twilio_account(company.id)
    if not ta:
        logger.warning("send_message: no Twilio account for company=%d user=%d conv=%d",
                       company.id, user.id, conv_id)
        return jsonify({
            "success": False,
            "error": "Twilio is not configured for this account. Add your Twilio credentials in SMS Settings."
        }), 400
    if not ta.is_configured:
        logger.warning("send_message: Twilio account incomplete for company=%d user=%d conv=%d",
                       company.id, user.id, conv_id)
        return jsonify({
            "success": False,
            "error": "Twilio credentials are incomplete. Check your Account SID, Auth Token, and phone number in SMS Settings."
        }), 400

    if not conv.from_number:
        return jsonify({"success": False, "error": "Conversation has no destination phone number."}), 400

    record, err = _send_sms_internal(ta, conv.from_number, body, conversation_id=conv.id)
    if err:
        logger.error("send_message failed: user=%d company=%d conv=%d to=%s error=%s",
                     user.id, company.id, conv_id, conv.from_number, err)
        return jsonify({"success": False, "error": err}), 500

    # Update conversation preview
    conv.last_message_at      = datetime.utcnow()
    conv.last_message_preview = f"You: {body[:150]}"
    conv.message_count        = (conv.message_count or 0) + 1
    conv.is_read              = True
    db.session.commit()

    logger.info("send_message: user=%d company=%d conv=%d to=%s sid=%s",
                user.id, company.id, conv_id, conv.from_number, record.twilio_sid)
    return jsonify({"success": True, "message": _msg_to_dict(record)})


def _normalize_phone(raw: str) -> str:
    """Normalise a phone number to E.164 (+1XXXXXXXXXX for US numbers)."""
    import re
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits[0] == "1":
        return f"+{digits}"
    if raw.startswith("+"):
        return raw.strip()
    return f"+{digits}" if digits else raw.strip()


# ── API: start new conversation ───────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations", methods=["POST"])
def new_conversation():
    user    = _require_auth()
    company = _require_company(user)
    payload = request.get_json() or {}
    to_raw  = (payload.get("to") or "").strip()
    body    = (payload.get("body") or "").strip()

    if not to_raw:
        return jsonify({"success": False, "error": "Recipient phone number is required."}), 400
    if not body:
        return jsonify({"success": False, "error": "Message body is required."}), 400

    to_num = _normalize_phone(to_raw)
    if len(to_num) < 7:
        return jsonify({"success": False, "error": f"Invalid phone number: {to_raw}"}), 400

    ta = _get_twilio_account(company.id)
    if not ta or not ta.is_configured:
        return jsonify({
            "success": False,
            "error": "Twilio is not configured for this account. Add your Twilio credentials in SMS Settings."
        }), 400

    from models import TwilioConversation
    # Look up by both the normalised form and the raw form so existing convs are found
    conv = (
        TwilioConversation.query.filter_by(company_id=company.id, from_number=to_num).first()
        or TwilioConversation.query.filter_by(company_id=company.id, from_number=to_raw).first()
    )
    if not conv:
        conv = TwilioConversation(
            company_id=company.id,
            from_number=to_num,
            to_number=ta.from_phone or "",
            is_read=True,
        )
        db.session.add(conv)
        db.session.flush()

    record, err = _send_sms_internal(ta, to_num, body, conversation_id=conv.id)
    if err:
        logger.error("new_conversation send failed user=%d company=%d to=%s: %s",
                     user.id, company.id, to_num, err)
        return jsonify({"success": False, "error": err}), 500

    conv.last_message_at      = datetime.utcnow()
    conv.last_message_preview = f"You: {body[:150]}"
    conv.message_count        = (conv.message_count or 0) + 1
    db.session.commit()

    logger.info("new_conversation: user=%d company=%d to=%s conv=%d sid=%s",
                user.id, company.id, to_num, conv.id, record.twilio_sid)
    return jsonify({"success": True, "conversation_id": conv.id, "message": _msg_to_dict(record)})


# ── API: mark read / unread ───────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/read", methods=["PATCH"])
def mark_read(conv_id):
    user    = _require_auth()
    company = _get_company(user)
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()
    payload  = request.get_json() or {}
    conv.is_read = payload.get("is_read", True)
    db.session.commit()
    return jsonify({"success": True, "is_read": conv.is_read})


# ── API: archive / unarchive ──────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/archive", methods=["PATCH"])
def archive_conversation(conv_id):
    user    = _require_auth()
    company = _get_company(user)
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()
    payload  = request.get_json() or {}
    archive  = payload.get("archived", True)
    tags     = _safe_conversation_tags(conv.tags)
    if archive and "archived" not in tags:
        tags.append("archived")
    elif not archive and "archived" in tags:
        tags.remove("archived")
    conv.tags = tags
    try:
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(conv, "tags")
    except Exception:
        pass
    db.session.commit()
    return jsonify({"success": True, "is_archived": archive})


# ── API: assign ───────────────────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/assign", methods=["PATCH"])
def assign_conversation(conv_id):
    user    = _require_auth()
    company = _get_company(user)
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()
    payload = request.get_json() or {}
    assign_to = payload.get("user_id")  # null to unassign
    conv.assigned_user_id = assign_to
    db.session.commit()
    return jsonify({"success": True, "assigned_user_id": conv.assigned_user_id})


# ── API: internal notes ───────────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/notes", methods=["POST"])
def update_notes(conv_id):
    user    = _require_auth()
    company = _get_company(user)
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()
    payload  = request.get_json() or {}
    conv.notes = payload.get("notes", conv.notes or "")
    db.session.commit()
    return jsonify({"success": True})


# ── API: rename contact ───────────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/rename", methods=["PATCH"])
def rename_contact(conv_id):
    user    = _require_auth()
    company = _get_company(user)
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()
    payload  = request.get_json() or {}
    name     = (payload.get("name") or "").strip()
    if name:
        conv.contact_name = name
        db.session.commit()
    return jsonify({"success": True, "contact_name": conv.contact_name})


# ── API: unread count only ────────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/unread-count")
def unread_count():
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"count": 0})
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    count = filter_conversations_for_user(
        TwilioConversation.query.filter_by(company_id=company.id, is_read=False),
        user, company.id,
    ).count()
    return jsonify({"count": count})



# ── API: PWA notification center ─────────────────────────────────────────────

def _notification_to_dict(row):
    return {
        "id": row.id,
        "title": row.title,
        "message": row.message or "",
        "category": row.category or "system",
        "event_type": getattr(row, "event_type", None) or row.category or "system",
        "icon": row.icon or "bell",
        "link": row.link or "",
        "is_read": bool(row.is_read),
        "phone_number_id": getattr(row, "phone_number_id", None),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _authorized_notification_users(company_id: int, phone_number_id=None):
    from models import PhoneNumberUserPermission, User, UserCompanyAccess
    from services.comms_permissions import normalize_role

    rows = UserCompanyAccess.query.filter_by(company_id=company_id).all()
    users = []
    for acc in rows:
        role = normalize_role(getattr(acc, "role", None))
        allowed = role in {"owner", "admin"} or bool(getattr(acc, "pwa_access_enabled", False) or getattr(acc, "can_access_mobile_inbox", False))
        if phone_number_id and role not in {"owner", "admin"}:
            perm = PhoneNumberUserPermission.query.filter_by(
                company_id=company_id,
                phone_number_id=phone_number_id,
                user_id=acc.user_id,
                can_access_pwa=True,
            ).first()
            allowed = bool(perm and (perm.can_view_sms or perm.can_view_calls or perm.can_view_voicemail))
        if allowed:
            user = db.session.get(User, acc.user_id)
            if user:
                users.append(user)
    return users


def _create_notification_records(company_id: int, *, event_type: str, title: str, notification_body: str,
                                 link: str = "/app/inbox", phone_number_id=None, category: str = "communications",
                                 icon: str = "bell"):
    from models import Notification
    created = []
    for user in _authorized_notification_users(company_id, phone_number_id):
        row = Notification(
            user_id=user.id,
            company_id=company_id,
            phone_number_id=phone_number_id,
            event_type=event_type,
            title=title[:200],
            message=notification_body,
            category=category,
            icon=icon,
            link=link,
        )
        db.session.add(row)
        created.append(row)
    if created:
        db.session.commit()
    return created


@inbox_pwa_bp.route("/api/pwa/notifications")
def pwa_notifications():
    user = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": True, "notifications": [], "unread_count": 0})
    from models import Notification
    unread_only = request.args.get("filter") == "unread" or request.args.get("unread") == "1"
    q = Notification.query.filter_by(user_id=user.id, company_id=company.id)
    if unread_only:
        q = q.filter_by(is_read=False)
    rows = q.order_by(Notification.created_at.desc()).limit(100).all()
    unread_count = Notification.query.filter_by(user_id=user.id, company_id=company.id, is_read=False).count()
    return jsonify({"success": True, "notifications": [_notification_to_dict(r) for r in rows], "unread_count": unread_count})


@inbox_pwa_bp.route("/api/pwa/notifications/read", methods=["POST"])
def pwa_notifications_read():
    user = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400
    from models import Notification
    payload = request.get_json(silent=True) or request.form or {}
    notification_id = payload.get("notification_id") or payload.get("id")
    q = Notification.query.filter_by(user_id=user.id, company_id=company.id)
    if notification_id and str(notification_id) != "all":
        q = q.filter_by(id=int(notification_id))
    updated = 0
    for row in q.all():
        row.is_read = True
        updated += 1
    db.session.commit()
    return jsonify({"success": True, "updated": updated})

# ── API: Push notification subscribe ─────────────────────────────────────────

@inbox_pwa_bp.route("/api/pwa/push/subscribe", methods=["POST"])
@inbox_pwa_bp.route("/api/inbox/push/subscribe", methods=["POST"])
def push_subscribe():
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400

    payload  = request.get_json() or {}
    endpoint = payload.get("endpoint", "")
    device_key = payload.get("device_key") or payload.get("deviceKey")
    p256dh   = payload.get("keys", {}).get("p256dh", "")
    auth_key = payload.get("keys", {}).get("auth", "")

    if not endpoint:
        return jsonify({"success": False, "error": "endpoint required"}), 400

    from models import PushSubscription
    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if not sub:
        sub = PushSubscription(
            user_id=user.id,
            company_id=company.id,
            device_key=device_key,
            endpoint=endpoint,
            p256dh=p256dh,
            auth_key=auth_key,
        )
        db.session.add(sub)
    else:
        sub.user_id = user.id
        sub.company_id = company.id
        sub.device_key = device_key or getattr(sub, "device_key", None)
        sub.p256dh   = p256dh
        sub.auth_key = auth_key
    if device_key:
        from models import PWADevice
        device = PWADevice.query.filter_by(company_id=company.id, user_id=user.id, device_key=device_key).first()
        if device:
            device.push_enabled = True
            device.last_seen_at = datetime.utcnow()
    db.session.commit()
    logger.info("Push subscription saved for user %d", user.id)
    return jsonify({"success": True, "device_key": device_key})


@inbox_pwa_bp.route("/api/pwa/push/test", methods=["POST"])
@inbox_pwa_bp.route("/api/inbox/push/test", methods=["POST"])
def push_test():
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400

    from models import PushSubscription
    subs = PushSubscription.query.filter_by(user_id=user.id).all()
    if not subs:
        return jsonify({"success": False, "error": "No push subscription found. Enable notifications first."})

    vapid_private = os.environ.get("VAPID_PRIVATE_KEY", "")
    vapid_public  = os.environ.get("VAPID_PUBLIC_KEY", "")
    vapid_claims  = {"sub": "mailto:admin@luxit.app"}
    if not vapid_private or not vapid_public:
        return jsonify({"success": False, "configured": False, "error": "Web push is not configured. Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY."})

    sent = 0
    errors = []
    for sub in subs:
        try:
            from pywebpush import webpush, WebPushException
            import json
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth_key},
                },
                data=json.dumps({
                    "title": "LUXit Inbox",
                    "body":  "Push notifications are working!",
                    "url":   "/app/inbox",
                }),
                vapid_private_key=vapid_private,
                vapid_claims=vapid_claims,
            )
            sent += 1
        except ImportError:
            errors.append("pywebpush not installed — push not available")
            break
        except Exception as exc:
            errors.append(str(exc))
            # Remove expired subscription
            if "410" in str(exc) or "404" in str(exc):
                db.session.delete(sub)
                db.session.commit()

    if sent:
        return jsonify({"success": True, "sent": sent})
    return jsonify({"success": False, "error": errors[0] if errors else "Unknown error"})


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _push_sse_event(company_id: int, event_type: str, data: dict):
    """Broadcast a JSON event to every SSE listener for a company."""
    import json
    payload = json.dumps({"type": event_type, **data})
    with _sse_lock:
        listeners = _sse_listeners.get(company_id, [])
        dead = []
        for q in listeners:
            try:
                q.put_nowait(payload)
            except _queue_module.Full:
                dead.append(q)
        for q in dead:
            try:
                listeners.remove(q)
            except ValueError:
                pass


@inbox_pwa_bp.route("/api/inbox/stream")
def sse_stream():
    """
    Server-Sent Events stream — real-time message delivery.
    Requires gthread gunicorn workers (--worker-class gthread --threads N).
    Each connected client holds one thread; heartbeats every 25 s keep it alive.
    """
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"error": "No company"}), 400

    def generate():
        q = _queue_module.Queue(maxsize=100)
        with _sse_lock:
            _sse_listeners.setdefault(company.id, []).append(q)
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    payload = q.get(timeout=25)
                    yield f"data: {payload}\n\n"
                except _queue_module.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _sse_lock:
                lst = _sse_listeners.get(company.id, [])
                try:
                    lst.remove(q)
                except ValueError:
                    pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ── Internal helper: fire push for new inbound message ───────────────────────

@inbox_pwa_bp.route("/api/inbox/badge-counts")
def badge_counts():
    """Return missed-call and unread-voicemail counts for PWA badges."""
    user = _require_auth()
    company = _require_company(user)

    missed = 0
    vmails = 0
    try:
        from models import TwilioCallLog
        missed = TwilioCallLog.query.filter(
            TwilioCallLog.company_id == company.id,
            TwilioCallLog.direction == "inbound",
            TwilioCallLog.call_status.in_(["no-answer", "busy", "failed"]),
        ).count()
    except Exception:
        pass
    try:
        from models import TwilioVoicemail
        vmails = TwilioVoicemail.query.filter_by(
            company_id=company.id, is_read=False
        ).count()
    except Exception:
        pass
    return jsonify({"missed_calls": missed, "voicemails": vmails})


@inbox_pwa_bp.route("/api/inbox/contacts/search")
def search_contacts():
    """Search contacts by name or phone for the compose modal autocomplete."""
    user = _require_auth()
    company = _require_company(user)

    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"contacts": []})

    results = []
    try:
        from models import Contact
        digits = "".join(ch for ch in q if ch.isdigit())
        like = f"%{q}%"
        digit_like = f"%{digits}%" if digits else like
        contacts = (
            Contact.query
            .filter(
                Contact.company_id == company.id,
                db.or_(
                    Contact.first_name.ilike(like),
                    Contact.last_name.ilike(like),
                    Contact.company.ilike(like),
                    Contact.email.ilike(like),
                    Contact.phone.ilike(like),
                    Contact.phone.ilike(digit_like),
                ),
            )
            .limit(8)
            .all()
        )
        for c in contacts:
            if c.phone:
                name = " ".join([part for part in [c.first_name, c.last_name] if part]).strip()
                results.append({"name": name or c.company or c.email or c.phone, "company": c.company, "email": c.email, "phone": c.phone, "source": "contact"})
    except Exception:
        pass

    try:
        from models import TwilioConversation
        from services.comms_permissions import filter_conversations_for_user
        like = f"%{q}%"
        convs = (
            filter_conversations_for_user(TwilioConversation.query.filter_by(company_id=company.id), user, company.id)
            .filter(db.or_(TwilioConversation.contact_name.ilike(like), TwilioConversation.from_number.ilike(like)))
            .limit(8)
            .all()
        )
        seen = {r["phone"] for r in results}
        for cv in convs:
            phone = cv.from_number
            if phone and phone not in seen:
                seen.add(phone)
                results.append({"name": cv.contact_name or phone, "phone": phone, "source": "conversation"})
    except Exception:
        pass

    try:
        from models import TwilioCallLog
        from services.comms_permissions import filter_calls_for_user
        like = f"%{q}%"
        calls = (
            filter_calls_for_user(TwilioCallLog.query.filter_by(company_id=company.id), user, company.id)
            .filter(db.or_(TwilioCallLog.caller_name.ilike(like), TwilioCallLog.from_number.ilike(like), TwilioCallLog.to_number.ilike(like)))
            .order_by(TwilioCallLog.created_at.desc())
            .limit(8)
            .all()
        )
        seen = {r["phone"] for r in results}
        for call in calls:
            phone = call.from_number if call.direction == "inbound" else call.to_number
            if phone and phone not in seen:
                seen.add(phone)
                results.append({"name": call.caller_name or phone, "phone": phone, "source": "call_log"})
    except Exception:
        pass

    return jsonify({"contacts": results})


@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/call", methods=["POST"])
def place_outbound_call(conv_id):
    """
    Initiate a Twilio outbound call to the contact in this conversation.

    If the TwilioAccount has `call_forward_to` set, Twilio first calls that
    number (the agent's phone); when answered, the call bridges to the customer.
    Otherwise the call goes directly to the customer (useful for leaving a
    message or testing).  Requires Twilio to be configured for the company.
    """
    user = _require_auth()
    company = _require_company(user)

    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first_or_404()

    ta = _get_twilio_account(company.id)
    if not ta:
        return jsonify({"success": False, "error": "Twilio is not configured for this account."}), 400
    if not ta.from_phone:
        return jsonify({"success": False, "error": "No outbound phone number configured in Twilio settings."}), 400

    try:
        from twilio.rest import Client
        from flask import url_for
        sid = ta.get_account_sid() if hasattr(ta, "get_account_sid") else ta._account_sid
        tok = ta.get_auth_token()  if hasattr(ta, "get_auth_token")  else ta._auth_token
        client = Client(sid, tok)

        payload     = request.get_json() or {}
        from models import TwilioPhoneNumber
        from services.comms_permissions import accessible_phone_numbers
        allowed_numbers = set(accessible_phone_numbers(user, company.id))
        business_number = conv.to_number if conv.to_number in allowed_numbers else ta.from_phone
        pn = TwilioPhoneNumber.query.filter_by(company_id=company.id, phone_number=business_number, is_active=True).first()
        method = payload.get("calling_method") or "cell_callback"
        if method == "browser" and pn and not pn.browser_calling_enabled:
            return jsonify({"success": False, "error": "Browser/WiFi calling is disabled for this number."}), 403
        if method == "cell_callback" and pn and not pn.cell_callback_enabled:
            return jsonify({"success": False, "error": "Cell callback calling is disabled for this number."}), 403
        if method == "browser" and pn and pn.wifi_only and payload.get("network_type") == "cellular":
            return jsonify({"success": False, "error": "This line is WiFi-only for browser calling."}), 403
        forward_to  = payload.get("forward_to") or ta.call_forward_to
        customer_no = conv.from_number

        # TwiML URL that bridges the call to the customer once the agent picks up
        twiml_url = url_for(
            "twilio.outbound_call_twiml",
            to=customer_no,
            caller=business_number,
            _external=True,
        )

        if forward_to:
            # Call the agent first; TwiML dials the customer when agent answers
            call = client.calls.create(
                to=forward_to,
                from_=business_number,
                url=twiml_url,
            )
            msg = f"Calling your phone ({forward_to}). Answer to be connected to {customer_no}."
        else:
            # Call the customer directly (e.g. to leave a voicemail / test)
            call = client.calls.create(
                to=customer_no,
                from_=business_number,
                url=twiml_url,
            )
            msg = f"Outbound call initiated to {customer_no}."

        logger.info("Outbound call initiated: sid=%s to=%s", call.sid, call.to)
        return jsonify({"success": True, "call_sid": call.sid, "status": call.status, "message": msg})

    except Exception as exc:
        logger.error("Outbound call error: %s", exc)
        return jsonify({"success": False, "error": _twilio_send_error_message(exc)}), 500


@inbox_pwa_bp.route("/api/inbox/call/dial", methods=["POST"])
def dial_number():
    """
    Initiate a Twilio outbound call to an arbitrary number from the PWA dial pad.
    Uses the same forwarding model as place_outbound_call — Twilio calls the
    agent's call_forward_to number first, then bridges to the dialled number.
    The recipient sees the company's Twilio number as caller ID.
    """
    user    = _require_auth()
    company = _require_company(user)

    ta = _get_twilio_account(company.id)
    if not ta:
        return jsonify({"success": False, "error": "Twilio is not configured for this account."}), 400
    if not ta.from_phone:
        return jsonify({"success": False, "error": "No outbound phone number configured in Twilio settings."}), 400

    payload   = request.get_json() or {}
    to_number = (payload.get("to") or "").strip()
    if not to_number:
        return jsonify({"success": False, "error": "A phone number to dial is required."}), 400

    import re
    digits = re.sub(r"[^\d]", "", to_number)
    if not digits:
        return jsonify({"success": False, "error": "Invalid phone number."}), 400
    if not to_number.startswith("+"):
        to_number = ("+1" + digits) if len(digits) == 10 else ("+" + digits)

    try:
        from twilio.rest import Client
        from flask import url_for as _url_for
        sid    = ta.get_account_sid() if hasattr(ta, "get_account_sid") else ta._account_sid
        tok    = ta.get_auth_token()  if hasattr(ta, "get_auth_token")  else ta._auth_token
        client = Client(sid, tok)

        from models import TwilioPhoneNumber
        from services.comms_permissions import accessible_phone_numbers
        allowed_numbers = set(accessible_phone_numbers(user, company.id))
        business_number = payload.get("from_phone_number") or payload.get("selected_number") or ta.from_phone
        if business_number not in allowed_numbers:
            return jsonify({"success": False, "error": "No access to the selected business number."}), 403
        pn = TwilioPhoneNumber.query.filter_by(company_id=company.id, phone_number=business_number, is_active=True).first()
        method = payload.get("calling_method") or "cell_callback"
        if method == "browser" and pn and not pn.browser_calling_enabled:
            return jsonify({"success": False, "error": "Browser/WiFi calling is disabled for this number."}), 403
        if method == "cell_callback" and pn and not pn.cell_callback_enabled:
            return jsonify({"success": False, "error": "Cell callback calling is disabled for this number."}), 403
        if method == "browser" and pn and pn.wifi_only and payload.get("network_type") == "cellular":
            return jsonify({"success": False, "error": "This line is WiFi-only for browser calling."}), 403
        if method == "browser" and pn and not pn.mobile_data_allowed and payload.get("network_type") == "cellular":
            return jsonify({"success": False, "error": "Mobile-data browser calling is blocked for this number."}), 403

        forward_to = payload.get("forward_to") or ta.call_forward_to

        twiml_url = _url_for(
            "twilio.outbound_call_twiml",
            to=to_number,
            caller=business_number,
            _external=True,
        )

        if forward_to:
            call = client.calls.create(
                to=forward_to,
                from_=business_number,
                url=twiml_url,
            )
            msg = f"Calling your phone ({forward_to}). Answer to be connected to {to_number}."
        else:
            call = client.calls.create(
                to=to_number,
                from_=business_number,
                url=twiml_url,
            )
            msg = f"Outbound call initiated to {to_number}."

        logger.info("Dial pad outbound call: sid=%s to=%s", call.sid, call.to)
        return jsonify({"success": True, "call_sid": call.sid, "status": call.status, "message": msg})

    except Exception as exc:
        twilio_code   = getattr(exc, "code",   None)
        twilio_status = getattr(exc, "status", None)
        logger.error(
            "Dial pad call error: twilio_code=%s http_status=%s to=%s from=%s — %s",
            twilio_code, twilio_status, to_number, ta.from_phone, exc,
        )
        return jsonify({"success": False, "error": _twilio_send_error_message(exc)}), 400


def _fire_push_notification(company_id: int, conv, message_body: str):
    """Called from inbound SMS webhook; records notification history and sends Web Push."""
    sender = conv.contact_name or conv.from_number
    phone_number_id = getattr(conv, "phone_number_id", None)
    notification_body = (message_body or "(media)")[:160]
    title = f"New message from {sender}"
    link = f"/app/inbox?conv={conv.id}"
    _create_notification_records(
        company_id,
        event_type="inbound_sms",
        title=title,
        notification_body=notification_body,
        link=link,
        phone_number_id=phone_number_id,
        icon="message-square",
    )

    vapid_private = os.environ.get("VAPID_PRIVATE_KEY", "")
    vapid_public  = os.environ.get("VAPID_PUBLIC_KEY", "")
    if not vapid_private or not vapid_public:
        return

    from models import PushSubscription
    allowed_user_ids = [u.id for u in _authorized_notification_users(company_id, phone_number_id)]
    if not allowed_user_ids:
        return
    subs = PushSubscription.query.filter(
        PushSubscription.company_id == company_id,
        PushSubscription.user_id.in_(allowed_user_ids),
    ).all()
    if not subs:
        return

    import json
    payload = json.dumps({
        "title": title,
        "body":  notification_body,
        "url":   link,
        "tag":   f"sms-{conv.id}",
        "vibrate": [80, 40, 80],
    })

    for sub in subs:
        try:
            from pywebpush import webpush
            webpush(
                subscription_info={"endpoint": sub.endpoint,
                                   "keys": {"p256dh": sub.p256dh, "auth": sub.auth_key}},
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": "mailto:admin@luxit.app"},
            )
        except Exception as exc:
            logger.debug("Push send failed for sub %d: %s", sub.id, exc)
            if "410" in str(exc) or "404" in str(exc):
                db.session.delete(sub)
                db.session.commit()


def create_pwa_notification(company_id: int, *, event_type: str, title: str, body: str,
                            link: str = "/app/inbox", phone_number_id=None, icon: str = "bell"):
    """Public helper for call/voicemail webhooks to persist notification history."""
    return _create_notification_records(
        company_id,
        event_type=event_type,
        title=title,
        notification_body=body,
        link=link,
        phone_number_id=phone_number_id,
        icon=icon,
    )


# ── Google Contacts status + sync (PWA API) ───────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/google-contacts/status")
def gc_status():
    """Return Google Contacts OAuth + sync status for the PWA settings sheet."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user
    from services.google_contacts import get_token, is_token_expired
    tok = get_token(user.id)
    if not tok:
        return jsonify({
            "connected":       False,
            "oauth_expired":   False,
            "last_sync_at":    None,
            "contacts_synced": 0,
            "sync_error":      None,
            "connect_url":     "/twilio/google-contacts/connect",
        })
    return jsonify({
        "connected":       True,
        "oauth_expired":   is_token_expired(tok),
        "last_sync_at":    tok.last_sync_at.strftime("%b %-d %H:%M") if tok.last_sync_at else None,
        "contacts_synced": tok.contacts_synced or 0,
        "sync_error":      getattr(tok, "sync_error", None),
        "connect_url":     "/twilio/google-contacts/connect",
    })


@inbox_pwa_bp.route("/api/inbox/google-contacts/sync", methods=["POST"])
def gc_sync():
    """Trigger a manual Google Contacts sync from the PWA."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user
    company = _get_company(user)
    if not company:
        return jsonify({"error": "No company found."}), 400
    from services.google_contacts import sync_contacts
    result = sync_contacts(user.id, company.id)
    return jsonify(result)
