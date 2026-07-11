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

import base64
import logging
import os
import queue as _queue_module
import re
import threading
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from flask import (Blueprint, Response, abort, current_app, g, jsonify,
                   render_template, request, session, stream_with_context)

from extensions import db

logger = logging.getLogger(__name__)

inbox_pwa_bp = Blueprint("inbox_pwa", __name__)


@inbox_pwa_bp.before_request
def _phone_pwa_license_gate():
    """Server-side feature gate for the licensed Phone/PWA Communications module."""
    path = request.path or ""
    gated = (
        path in {"/app/inbox", "/app/calls", "/app/calls/settings", "/app/new-text", "/app/dial-pad", "/app/recents", "/app/settings", "/app/favorites", "/app/contacts", "/app/voicemail", "/app/greetings"}
        or path.startswith("/api/inbox/")
        or path.startswith("/api/calls/")
        or path.startswith("/api/phone/")
        or path.startswith("/api/pwa/")
        or path.startswith("/api/push/")
    )
    if not gated:
        return None
    # Keep auth redirects/403s owned by existing route logic.
    user = _current_user()
    company = _get_company(user) if user else None
    if not user or not company:
        return None
    from services.license_service import PHONE_PWA_FEATURE, license_status_details
    details = license_status_details(company.id, PHONE_PWA_FEATURE)
    g.license_warning = details.get("warning")
    if details["allowed"]:
        return None
    if path.startswith("/api/"):
        return jsonify({
            "success": False,
            "error": "Phone/PWA Communications license is not active.",
            "feature_key": PHONE_PWA_FEATURE,
            "status": details["status"],
            "billing_url": "/settings/billing",
        }), 402
    return render_template(
        "licenses/feature_blocked.html",
        status=details["status"],
        message="Phone/PWA Communications is suspended or not licensed. Update billing or contact support.",
    ), 402


def _name_is_phone_number(value, phone_number=None):
    if not value:
        return True
    digits = re.sub(r"\D", "", value or "")
    if phone_number and digits and digits == re.sub(r"\D", "", phone_number or ""):
        return True
    return bool(digits and len(digits) >= 7 and not re.search(r"[A-Za-z]", value or ""))


def _lookup_contact_display(company_id, phone_number, current_name=None):
    """Resolve display metadata for a phone number from CRM/Google cache."""
    current = (current_name or "").strip()
    if not phone_number:
        return {"name": current, "source": None, "contact_id": None}
    if current and not _name_is_phone_number(current, phone_number):
        return {"name": current, "source": None, "contact_id": None}
    try:
        from services.google_contacts import lookup_contact_for_phone
        info = lookup_contact_for_phone(company_id, phone_number)
        name = (info.get("name") or "").strip()
        if name and not _name_is_phone_number(name, phone_number):
            return info
    except Exception:
        logger.exception("Contact display-name lookup failed", extra={"company_id": company_id})
    return {"name": current or phone_number, "source": None, "contact_id": None}


def _refresh_conversation_contact_name(conv):
    info = _lookup_contact_display(conv.company_id, conv.from_number, conv.contact_name)
    resolved = info.get("name") or conv.from_number
    changed = False
    if info.get("contact_id") and conv.contact_id != info["contact_id"]:
        conv.contact_id = info["contact_id"]
        changed = True
    if resolved and resolved != conv.contact_name and not _name_is_phone_number(resolved, conv.from_number):
        conv.contact_name = resolved
        if hasattr(conv, "contact_source") and (info.get("source") or not getattr(conv, "contact_source", None)):
            conv.contact_source = info.get("source") or "contacts_cache"
        changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Unable to refresh conversation contact_name", extra={"conversation_id": conv.id, "company_id": conv.company_id})
    return resolved or conv.from_number


def _refresh_call_contact_name(call):
    lookup_number = call.from_number if call.direction == "inbound" else call.to_number
    info = _lookup_contact_display(call.company_id, lookup_number, call.caller_name)
    resolved = info.get("name") or lookup_number
    changed = False
    if info.get("contact_id") and getattr(call, "contact_id", None) != info["contact_id"]:
        call.contact_id = info["contact_id"]
        changed = True
    if resolved and resolved != call.caller_name and not _name_is_phone_number(resolved, lookup_number):
        call.caller_name = resolved
        changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Unable to refresh call caller_name", extra={"call_id": call.id, "company_id": call.company_id})
    return resolved or lookup_number


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


def _require_mobile_inbox_api_access(user, company):
    """Gate PWA JSON APIs with the same permission as the PWA shell."""
    if not _check_mobile_inbox_access(user, company):
        return jsonify({"success": False, "error": "Mobile inbox access is not enabled for this account."}), 403
    return None


def _get_twilio_account(company_id):
    from models import TwilioAccount
    return TwilioAccount.query.filter_by(company_id=company_id, is_active=True).first()


def _accessible_numbers_for(user, company_id: int) -> list[str]:
    from services.comms_permissions import accessible_phone_numbers
    return accessible_phone_numbers(user, company_id)


def _can_send_sms_from_number(user, company_id: int, from_number: str) -> bool:
    """Return whether the user may send SMS from a tenant-owned line."""
    if not user or not company_id or not from_number:
        return False
    from services.comms_permissions import normalize_role, user_access_for_company
    acc = user_access_for_company(user, company_id)
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    if getattr(user, "is_admin", False) or role in {"owner", "admin"}:
        return True

    from models import PhoneNumberUserPermission, TwilioPhoneNumber
    explicit_count = PhoneNumberUserPermission.query.filter_by(company_id=company_id, user_id=user.id).count()
    pn = TwilioPhoneNumber.query.filter_by(company_id=company_id, phone_number=from_number, is_active=True).first()
    if pn:
        perm = PhoneNumberUserPermission.query.filter_by(
            company_id=company_id, user_id=user.id, phone_number_id=pn.id
        ).first()
        if perm:
            return bool(perm.can_access_pwa and perm.can_view_sms and perm.can_send_sms)
        if explicit_count:
            return False

    assigned = getattr(acc, "assigned_number", None) if acc else None
    if assigned:
        return assigned == from_number
    return bool(acc and acc.has_comms_hub_access())


def _json_error(message: str, status: int, code: str | None = None):
    payload = {"success": False, "error": message}
    if code:
        payload["code"] = code
    return jsonify(payload), status


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


def retry_queued_outbound_messages(company_id: int, limit: int = 100, dry_run: bool = False) -> dict:
    """Admin-safe retry for legacy PWA outbound rows stuck as queued without a Twilio SID."""
    from models import TwilioConversation, TwilioMessage

    ta = _get_twilio_account(company_id)
    if not ta or not ta.is_configured:
        return {"success": False, "error": "Twilio is not configured for this company.", "retried": 0, "failed": 0}

    rows = (TwilioMessage.query
        .join(TwilioConversation, TwilioConversation.id == TwilioMessage.conversation_id)
        .filter(
            TwilioMessage.company_id == company_id,
            TwilioMessage.direction == "outbound",
            TwilioMessage.status == "queued",
            TwilioMessage.twilio_sid.is_(None),
            TwilioConversation.company_id == company_id,
        )
        .order_by(TwilioMessage.created_at.asc(), TwilioMessage.id.asc())
        .limit(max(1, min(int(limit or 100), 500)))
        .all())
    if dry_run:
        return {"success": True, "dry_run": True, "matched": len(rows), "retried": 0, "failed": 0}

    retried = failed = 0
    errors = []
    for row in rows:
        conv = row.conversation
        to_number = row.to_number or getattr(conv, "from_number", None)
        if not to_number:
            row.status = "failed"
            row.error_message = "Queued outbound message has no recipient phone number."
            failed += 1
            errors.append({"id": row.id, "error": row.error_message})
            db.session.commit()
            continue
        from twilio_sms import sendConversationSms
        result = sendConversationSms(conv.id, row.body or "", twilio_account=ta, to_number=to_number, persist_record=False)
        if not result.get("success"):
            row.status = "failed"
            row.error_message = result.get("error") or "SMS retry failed."
            db.session.commit()
            failed += 1
            errors.append({"id": row.id, "error": row.error_message})
        else:
            row.status = "sent"
            row.twilio_sid = result.get("sid")
            row.error_message = None
            row.raw_payload = {
                "retried_from_queue": True,
                "resent_via": "sendConversationSms",
                "provider_status": result.get("provider_status") or result.get("status"),
            }
            db.session.commit()
            retried += 1
    return {"success": failed == 0, "matched": len(rows), "retried": retried, "failed": failed, "errors": errors[:20]}


def _conv_to_dict(conv, brief=True):
    tags = _safe_conversation_tags(conv.tags)
    contact_source = getattr(conv, "contact_source", None)
    display_name = _refresh_conversation_contact_name(conv)
    d = {
        "id":                  conv.id,
        "from_number":         conv.from_number,
        "contact_name":        display_name,
        "display_name":        display_name,
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
    tags = set(_safe_conversation_tags(getattr(conv, "tags", None)))
    return bool(tags.intersection({"archived", "resolved", "closed"}))


def _is_twilio_protected_url(url: str) -> bool:
    """Return True for Twilio-hosted media/recording URLs that require provider auth."""
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    return host == "api.twilio.com" or host.endswith(".twilio.com")


def _proxied_message_media_url(message_id: int, index: int) -> str:
    return f"/api/inbox/messages/{message_id}/media/{index}"


def _safe_message_media_urls(m):
    safe = []
    for idx, url in enumerate(m.media_urls or []):
        if _is_twilio_protected_url(url):
            safe.append(_proxied_message_media_url(m.id, idx))
        else:
            safe.append(url)
    return safe


def _msg_to_dict(m):
    return {
        "id":           m.id,
        "direction":    m.direction,
        "body":         m.body or "",
        "status":       m.status or "received",
        "is_auto_reply": m.is_auto_reply,
        "media_urls":   _safe_message_media_urls(m),
        "created_at":   m.created_at.isoformat() if m.created_at else None,
        "twilio_sid":   m.twilio_sid,
    }


# ── PWA shell page ────────────────────────────────────────────────────────────

@inbox_pwa_bp.route("/app/inbox")
@inbox_pwa_bp.route("/app/new-text")
@inbox_pwa_bp.route("/app/dial-pad")
@inbox_pwa_bp.route("/app/settings")
@inbox_pwa_bp.route("/app/favorites")
@inbox_pwa_bp.route("/app/contacts")
@inbox_pwa_bp.route("/app/greetings")
def pwa_index():
    from flask import redirect, url_for
    user = _current_user()
    if not user:
        return redirect("/auth/login?next=/app/inbox")
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
    vapid_missing = _web_push_missing_settings()
    pwa_version = (
        os.environ.get("LUXIT_ASSET_VERSION")
        or os.environ.get("GIT_SHA")
        or os.environ.get("RENDER_GIT_COMMIT")
        or "20260710-push-receipt-ack"
    )
    return render_template(
        "inbox_pwa/index.html",
        user=user,
        company=company,
        vapid_public=vapid_public,
        vapid_missing=vapid_missing,
        pwa_version=pwa_version,
    )


@inbox_pwa_bp.route("/app/calls")
@inbox_pwa_bp.route("/app/calls/settings")
@inbox_pwa_bp.route("/app/recents")
@inbox_pwa_bp.route("/app/voicemail")
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
    pwa_version = (
        os.environ.get("LUXIT_ASSET_VERSION")
        or os.environ.get("GIT_SHA")
        or os.environ.get("RENDER_GIT_COMMIT")
        or "20260710-push-receipt-ack"
    )
    return render_template("inbox_pwa/calls.html", user=user, company=company, pwa_version=pwa_version)


def _call_to_dict(call):
    voicemail_text = getattr(call, "transcription_text", None)
    display_name = _refresh_call_contact_name(call)
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
        "caller_name": display_name,
        "contact_name": display_name,
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
        "last_login_at": device.last_login_at.isoformat() if getattr(device, "last_login_at", None) else None,
        "last_ip": getattr(device, "last_ip", None),
        "approved_status": getattr(device, "approved_status", "pending") or "pending",
        "is_approved": getattr(device, "approved_status", "pending") == "approved",
        "is_revoked": getattr(device, "approved_status", "pending") == "revoked",
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
    is_new = device is None
    if not device:
        device = PWADevice(company_id=company.id, user_id=user.id, device_key=device_key)
        db.session.add(device)
    if is_new and not getattr(company, "require_approved_pwa_devices", False):
        device.approved_status = "approved"
        device.approved_at = datetime.utcnow()
    device.phone_number_id = pn.id if pn else device.phone_number_id
    for field in ("device_name", "browser", "device_type", "microphone_permission", "default_calling_method"):
        if field in payload:
            setattr(device, field, (payload.get(field) or "")[:120])
    device.user_agent = request.headers.get("User-Agent", "")[:1000]
    device.online_status = "online"
    now = datetime.utcnow()
    device.last_seen_at = now
    device.last_login_at = now if payload.get("login", True) else getattr(device, "last_login_at", None)
    device.last_ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip())[:64]
    device.push_enabled = bool(payload.get("push_enabled", device.push_enabled))
    device.pwa_installed = bool(payload.get("pwa_installed", device.pwa_installed))
    device.wifi_only = bool(payload.get("wifi_only", device.wifi_only))
    device.cellular_callback_enabled = bool(payload.get("cellular_callback_enabled", device.cellular_callback_enabled))
    device.mobile_data_calling_allowed = bool(payload.get("mobile_data_calling_allowed", device.mobile_data_calling_allowed))
    return device




def _device_approval_denied(user, company):
    if not getattr(company, "require_approved_pwa_devices", False):
        return None
    key = (request.headers.get("X-PWA-Device-Key") or request.args.get("device_key") or "").strip()
    if not key and request.is_json:
        key = ((request.get_json(silent=True) or {}).get("device_key") or "").strip()
    if not key:
        return jsonify({"success": False, "code": "PWA_DEVICE_PENDING_APPROVAL", "error": "This device is waiting for admin approval."}), 403
    from models import PWADevice
    device = PWADevice.query.filter_by(company_id=company.id, user_id=user.id, device_key=key).first()
    if not device or getattr(device, "approved_status", "pending") == "pending":
        return jsonify({"success": False, "code": "PWA_DEVICE_PENDING_APPROVAL", "error": "This device is waiting for admin approval."}), 403
    if getattr(device, "approved_status", "pending") == "revoked":
        return jsonify({"success": False, "code": "PWA_DEVICE_REVOKED", "error": "This device has been revoked by an admin."}), 403
    device.last_seen_at = datetime.utcnow()
    device.last_ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip())[:64]
    return None

def _require_pwa_communications_access(user, company):
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    return _device_approval_denied(user, company)


def _favorite_to_dict(fav):
    return {
        "id": fav.id,
        "display_name": fav.display_name,
        "phone_number": fav.phone_number,
        "avatar_url": fav.avatar_url,
        "sort_order": fav.sort_order or 0,
        "contact_id": fav.contact_id,
    }


@inbox_pwa_bp.route("/api/pwa/favorites", methods=["GET", "POST", "PUT"])
def api_pwa_favorites():
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    from models import PinnedPhoneFavorite
    if request.method == "POST":
        data = request.get_json() or {}
        fav = PinnedPhoneFavorite(
            user_id=user.id, company_id=company.id,
            display_name=(data.get("display_name") or data.get("name") or "Favorite")[:100],
            phone_number=(data.get("phone_number") or data.get("number") or "")[:20],
            avatar_url=data.get("avatar_url"), contact_id=data.get("contact_id"),
            sort_order=int(data.get("sort_order") or 0),
        )
        if not fav.phone_number:
            return jsonify({"success": False, "error": "phone_number is required"}), 400
        db.session.add(fav); db.session.commit()
        return jsonify({"success": True, "favorite": _favorite_to_dict(fav)}), 201
    if request.method == "PUT":
        for idx, row in enumerate((request.get_json() or {}).get("favorites") or []):
            fav = PinnedPhoneFavorite.query.filter_by(id=row.get("id"), user_id=user.id, company_id=company.id).first()
            if fav:
                fav.sort_order = int(row.get("sort_order", idx))
        db.session.commit()
    favorites = PinnedPhoneFavorite.query.filter_by(user_id=user.id, company_id=company.id).order_by(PinnedPhoneFavorite.sort_order.asc(), PinnedPhoneFavorite.created_at.asc()).all()
    return jsonify({"success": True, "favorites": [_favorite_to_dict(f) for f in favorites]})


@inbox_pwa_bp.route("/api/pwa/favorites/<int:favorite_id>", methods=["PATCH", "DELETE"])
def api_pwa_favorite_detail(favorite_id):
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    from models import PinnedPhoneFavorite
    fav = PinnedPhoneFavorite.query.filter_by(id=favorite_id, user_id=user.id, company_id=company.id).first_or_404()
    if request.method == "DELETE":
        db.session.delete(fav); db.session.commit()
        return jsonify({"success": True})
    data = request.get_json() or {}
    for key in ("display_name", "phone_number", "avatar_url", "contact_id", "sort_order"):
        if key in data:
            setattr(fav, key, data[key])
    db.session.commit()
    return jsonify({"success": True, "favorite": _favorite_to_dict(fav)})

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
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    if request.method == "PATCH":
        data = request.get_json() or {}
        palette = (data.get("palette_id") or data.get("palette") or "lux").strip()
        if palette not in {"lux", "ocean", "forest", "sunset", "slate", "rose"}:
            return jsonify({"success": False, "error": "Unsupported palette."}), 400
        user.pwa_palette_id = palette
        if data.get("theme_mode") in {"dark", "light", "system"}:
            user.pwa_theme_mode = data.get("theme_mode")
        pref_map = {
            "notificationSoundsEnabled": "notification_sounds_enabled",
            "textAlertsEnabled": "pwa_text_alerts_enabled",
            "callAlertsEnabled": "pwa_call_alerts_enabled",
            "voicemailAlertsEnabled": "pwa_voicemail_alerts_enabled",
            "unreadReminderAlertsEnabled": "pwa_unread_reminder_alerts_enabled",
            "vibrationEnabled": "pwa_vibration_enabled",
            "businessHoursOnly": "pwa_alerts_business_hours_only",
            "afterHoursPushEnabled": "pwa_after_hours_push_enabled",
        }
        for json_key, attr in pref_map.items():
            snake_key = re.sub(r"(?<!^)(?=[A-Z])", "_", json_key).lower()
            if json_key in data or snake_key in data:
                setattr(user, attr, bool(data.get(json_key, data.get(snake_key))))
        if "quietHoursStart" in data or "quiet_hours_start" in data:
            user.pwa_quiet_hours_start = (data.get("quietHoursStart") or data.get("quiet_hours_start") or "")[:5] or None
        if "quietHoursEnd" in data or "quiet_hours_end" in data:
            user.pwa_quiet_hours_end = (data.get("quietHoursEnd") or data.get("quiet_hours_end") or "")[:5] or None
        if "unreadRepeatMinutes" in data or "unread_repeat_minutes" in data:
            user.pwa_unread_repeat_minutes = max(1, min(60, int(data.get("unreadRepeatMinutes") or data.get("unread_repeat_minutes") or 1)))
        user.pwa_preferences_updated_at = datetime.utcnow()
        db.session.commit()
    return jsonify({
        "success": True,
        "preferences": {
            "palette_id": user.pwa_palette_id or "lux",
            "theme_mode": user.pwa_theme_mode or "dark",
            "notificationSoundsEnabled": user.notification_sounds_enabled is not False,
            "textAlertsEnabled": getattr(user, "pwa_text_alerts_enabled", True) is not False,
            "callAlertsEnabled": getattr(user, "pwa_call_alerts_enabled", True) is not False,
            "voicemailAlertsEnabled": getattr(user, "pwa_voicemail_alerts_enabled", True) is not False,
            "unreadReminderAlertsEnabled": getattr(user, "pwa_unread_reminder_alerts_enabled", True) is not False,
            "vibrationEnabled": getattr(user, "pwa_vibration_enabled", True) is not False,
            "businessHoursOnly": getattr(user, "pwa_alerts_business_hours_only", True) is not False,
            "afterHoursPushEnabled": getattr(user, "pwa_after_hours_push_enabled", False) is True,
            "quietHoursStart": getattr(user, "pwa_quiet_hours_start", None),
            "quietHoursEnd": getattr(user, "pwa_quiet_hours_end", None),
            "unreadRepeatMinutes": getattr(user, "pwa_unread_repeat_minutes", None) or 1,
            "updated_at": user.pwa_preferences_updated_at.isoformat() if user.pwa_preferences_updated_at else None,
        }
    })


@inbox_pwa_bp.route("/api/pwa/devices")
def api_pwa_devices():
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
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
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
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
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    device = _upsert_pwa_device(user, company, request.get_json() or {})
    db.session.commit()
    return jsonify({"success": True, "device": _device_to_dict(device)})


@inbox_pwa_bp.route("/api/pwa/devices/<int:device_id>/settings", methods=["PATCH"])
def api_pwa_device_settings(device_id):
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
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


@inbox_pwa_bp.route("/api/calls/<int:call_id>/mark-unread", methods=["POST"])
def api_call_mark_unread(call_id):
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioCallLog, VoiceVoicemailMessage
    from services.comms_permissions import filter_calls_for_user
    call = filter_calls_for_user(TwilioCallLog.query.filter_by(id=call_id, company_id=company.id), user, company.id).first_or_404()
    call.is_read = False
    call.read_at = None
    call.read_by_user_id = None
    vm = VoiceVoicemailMessage.query.filter_by(call_log_id=call.id, company_id=company.id).first()
    if vm:
        vm.is_read = False
        vm.read_at = None
        vm.read_by_user_id = None
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


def _twilio_recording_auth(company_id):
    from models import TwilioAccount
    ta = TwilioAccount.query.filter_by(company_id=company_id).first()
    sid = os.environ.get("TWILIO_ACCOUNT_SID") or (ta.get_account_sid() if ta and hasattr(ta, "get_account_sid") else None)
    token = os.environ.get("TWILIO_AUTH_TOKEN") or (ta.get_auth_token() if ta and hasattr(ta, "get_auth_token") else None)
    return sid, token


@inbox_pwa_bp.route("/api/calls/<int:call_id>/voicemail/audio")
def api_call_voicemail_audio(call_id):
    """Proxy voicemail media through the server so users never need Twilio credentials."""
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    from models import TwilioCallLog, VoiceVoicemailMessage
    from services.comms_permissions import filter_calls_for_user
    call = filter_calls_for_user(TwilioCallLog.query.filter_by(id=call_id, company_id=company.id), user, company.id).first_or_404()
    vm = VoiceVoicemailMessage.query.filter_by(call_log_id=call.id, company_id=company.id).order_by(VoiceVoicemailMessage.created_at.desc()).first()
    media_url = (getattr(vm, "recording_url", None) if vm else None) or getattr(call, "voicemail_url", None) or getattr(call, "recording_url", None)
    if not media_url:
        return jsonify({"success": False, "error": "No voicemail audio is available for this call."}), 404
    if media_url.startswith("data:"):
        header, encoded = media_url.split(",", 1)
        mime = header.split(";")[0].replace("data:", "") or "audio/mpeg"
        data = base64.b64decode(encoded)
        return Response(data, mimetype=mime, headers={"Content-Disposition": f"inline; filename=voicemail-{call.id}.mp3"})
    parsed = urlparse(media_url)
    auth = None
    if "twilio.com" in parsed.netloc.lower():
        sid, token = _twilio_recording_auth(company.id)
        if not sid or not token:
            return jsonify({"success": False, "error": "Voicemail playback is not configured. Ask an admin to verify Twilio credentials."}), 503
        auth = (sid, token)
    try:
        import requests
        upstream = requests.get(media_url, auth=auth, timeout=15)
        upstream.raise_for_status()
    except Exception:
        logger.exception("Voicemail media proxy failed", extra={"company_id": company.id, "call_id": call.id})
        return jsonify({"success": False, "error": "Voicemail audio could not be loaded. Please try again or contact support."}), 502
    return Response(
        upstream.content,
        mimetype=upstream.headers.get("Content-Type", "audio/mpeg"),
        headers={"Content-Disposition": f"inline; filename=voicemail-{call.id}.mp3"},
    )


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
    return {str(i): {"is_open": True, "open": "14:00", "close": "02:00"} for i in range(7)}


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




def _normalize_e164(value):
    raw = (value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return None

@inbox_pwa_bp.route("/api/phone/numbers/<int:number_id>/settings", methods=["GET", "PUT"])
def api_phone_number_settings(number_id):
    user = _require_auth()
    company = _require_company(user)
    from models import TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers, can_manage_users, normalize_role, user_access_for_company
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id, is_active=True).first_or_404()
    if pn.phone_number not in accessible_phone_numbers(user, company.id):
        abort(404)
    acc = user_access_for_company(user, company.id)
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    editable = can_manage_users(user, company.id) or role in {"owner", "admin"} or getattr(user, "is_admin", False)
    if request.method == "PUT":
        if not editable:
            return jsonify({"success": False, "error": "Permission denied."}), 403
        data = request.get_json() or {}
        allowed = {
            "business_hours", "timezone", "during_hours_route", "after_hours_route",
            "sms_forward_to", "sms_forwarding_enabled", "auto_reply_enabled", "number_auto_reply_text",
            "call_forward_to", "voice_forwarding_enabled", "call_forwarding_enabled", "call_forwarding_number", "business_hours_auto_reply_enabled", "business_hours_auto_reply_text", "ring_timeout",
            "voicemail_greeting_text", "voicemail_greeting_audio_url", "missed_call_text",
            "after_hours_text", "after_hours_sms_body", "after_hours_cooldown_minutes", "after_hours_sms_enabled", "after_hours_voicemail_enabled",
            "browser_calling_enabled", "cell_callback_enabled", "wifi_only",
            "mobile_data_allowed", "fallback_behavior", "caller_id_display_name",
        }
        if "call_forwarding_number" in data or "call_forward_to" in data:
            normalized = _normalize_e164(data.get("call_forwarding_number") or data.get("call_forward_to"))
            if normalized is None:
                return jsonify({"success": False, "error": "Forwarding number must be E.164 (for example +15551234567)."}), 400
            data["call_forwarding_number"] = normalized
            data["call_forward_to"] = normalized
        for key in allowed:
            if key in data:
                if key == "after_hours_sms_body":
                    pn.after_hours_text = data[key]
                else:
                    setattr(pn, key, data[key])
        if "call_forwarding_enabled" in data:
            pn.voice_forwarding_enabled = bool(data["call_forwarding_enabled"])
        if "voice_forwarding_enabled" in data:
            pn.call_forwarding_enabled = bool(data["voice_forwarding_enabled"])
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
            "number_auto_reply_text": pn.number_auto_reply_text,
            "call_forward_to": pn.call_forward_to,
            "voice_forwarding_enabled": pn.voice_forwarding_enabled,
            "call_forwarding_number": pn.call_forwarding_number or pn.call_forward_to,
            "call_forwarding_enabled": bool(pn.call_forwarding_enabled or pn.voice_forwarding_enabled),
            "business_hours_auto_reply_enabled": bool(pn.business_hours_auto_reply_enabled),
            "business_hours_auto_reply_text": pn.business_hours_auto_reply_text or "",
            "ring_timeout": pn.ring_timeout,
            "voicemail_greeting_text": pn.voicemail_greeting_text,
            "voicemail_greeting_audio_url": pn.voicemail_greeting_audio_url,
            "missed_call_text": pn.missed_call_text,
            "after_hours_text": pn.after_hours_text,
            "after_hours_sms_body": pn.after_hours_text,
            "after_hours_cooldown_minutes": pn.after_hours_cooldown_minutes,
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



def _backfill_legacy_greetings(company, pn, user_id=None):
    from models import PhoneSettings, VoiceGreeting
    created = False
    existing = VoiceGreeting.query.filter_by(company_id=company.id, phone_number_id=pn.id).first()
    if existing:
        return False
    settings = PhoneSettings.query.filter_by(company_id=company.id).first()
    legacy_text = (getattr(pn, "voicemail_greeting_text", None) or (settings.voicemail_greeting if settings else None) or "").strip()
    legacy_audio = (getattr(pn, "voicemail_greeting_audio_url", None) or "").strip()
    if legacy_audio or legacy_text:
        db.session.add(VoiceGreeting(
            company_id=company.id, phone_number_id=pn.id,
            name="Migrated voicemail greeting",
            greeting_type="upload" if legacy_audio else "standard",
            text_body=legacy_text or None, audio_url=legacy_audio or None,
            applies_to="voicemail_default", is_active=True, created_by_user_id=user_id,
        ))
        created = True
    after_text = (settings.after_hours_voicemail_greeting if settings else "") or ""
    if after_text.strip():
        db.session.add(VoiceGreeting(
            company_id=company.id, phone_number_id=pn.id,
            name="Migrated after-hours greeting", greeting_type="standard",
            text_body=after_text.strip(), applies_to="after_hours", is_active=True, created_by_user_id=user_id,
        ))
        created = True
    if created:
        db.session.commit()
    return created

def _voice_greeting_to_dict(greeting):
    return {
        "id": greeting.id,
        "company_id": greeting.company_id,
        "phone_number_id": greeting.phone_number_id,
        "name": greeting.name,
        "greeting_type": greeting.greeting_type,
        "text_body": greeting.text_body,
        "audio_url": greeting.audio_url,
        "storage_path": greeting.storage_path,
        "voice_name": greeting.voice_name,
        "is_active": bool(greeting.is_active),
        "applies_to": greeting.applies_to,
        "created_at": greeting.created_at.isoformat() if greeting.created_at else None,
        "updated_at": greeting.updated_at.isoformat() if greeting.updated_at else None,
    }


@inbox_pwa_bp.route("/api/phone/numbers/<int:number_id>/greetings", methods=["GET", "POST"])
def api_phone_number_greetings(number_id):
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    from models import TwilioPhoneNumber, VoiceGreeting
    from services.comms_permissions import accessible_phone_numbers
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company.id, is_active=True).first_or_404()
    if pn.phone_number not in accessible_phone_numbers(user, company.id):
        abort(403, "No access to that phone number")
    _backfill_legacy_greetings(company, pn, user.id)
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form or {}
        greeting_type = (data.get("greeting_type") or "standard").strip()
        if greeting_type in {"ai", "ai_voice"}: greeting_type = "text_to_speech"
        if greeting_type not in {"upload", "recorded", "text_to_speech", "standard"}:
            return jsonify({"success": False, "error": "Unsupported greeting type."}), 400
        applies_to = (data.get("applies_to") or "voicemail_default").strip()
        if applies_to not in {"business_hours", "after_hours", "voicemail_default"}:
            return jsonify({"success": False, "error": "Unsupported greeting scope."}), 400
        greeting = VoiceGreeting(
            company_id=company.id, phone_number_id=pn.id,
            name=(data.get("name") or "Voicemail greeting")[:160],
            greeting_type=greeting_type, text_body=data.get("text_body") or data.get("greeting_text"),
            audio_url=data.get("audio_url"), storage_path=data.get("storage_path"), voice_name=data.get("voice_name"),
            applies_to=applies_to, is_active=bool(data.get("is_active")), created_by_user_id=user.id,
        )
        if greeting.is_active:
            VoiceGreeting.query.filter_by(company_id=company.id, phone_number_id=pn.id, applies_to=applies_to, is_active=True).update({"is_active": False})
        db.session.add(greeting); db.session.commit()
        return jsonify({"success": True, "greeting": _voice_greeting_to_dict(greeting)}), 201
    greetings = VoiceGreeting.query.filter_by(company_id=company.id, phone_number_id=pn.id).order_by(VoiceGreeting.created_at.desc()).all()
    return jsonify({"success": True, "greetings": [_voice_greeting_to_dict(g) for g in greetings]})


@inbox_pwa_bp.route("/api/phone/greetings/<int:greeting_id>", methods=["DELETE"])
def api_delete_voice_greeting(greeting_id):
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    from models import VoiceGreeting
    greeting = VoiceGreeting.query.filter_by(id=greeting_id, company_id=company.id).first_or_404()
    if greeting.is_active:
        return jsonify({"success": False, "error": "Active greetings cannot be deleted until another greeting is activated."}), 400
    db.session.delete(greeting); db.session.commit()
    return jsonify({"success": True})


@inbox_pwa_bp.route("/api/phone/greetings/<int:greeting_id>/activate", methods=["POST"])
def api_activate_voice_greeting(greeting_id):
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    from models import TwilioPhoneNumber, VoiceGreeting
    from services.comms_permissions import accessible_phone_numbers
    greeting = VoiceGreeting.query.filter_by(id=greeting_id, company_id=company.id).first_or_404()
    pn = TwilioPhoneNumber.query.filter_by(id=greeting.phone_number_id, company_id=company.id, is_active=True).first_or_404()
    if pn.phone_number not in accessible_phone_numbers(user, company.id):
        abort(403, "No access to that phone number")
    VoiceGreeting.query.filter_by(company_id=company.id, phone_number_id=pn.id, applies_to=greeting.applies_to, is_active=True).update({"is_active": False})
    greeting.is_active = True
    db.session.commit()
    return jsonify({"success": True, "greeting": _voice_greeting_to_dict(greeting)})


@inbox_pwa_bp.route("/api/phone/test-forwarding", methods=["POST"])
def api_test_forwarding():
    user = _require_auth()
    company = _require_company(user)
    data = request.get_json() or {}
    number = data.get("number")
    if not number:
        return jsonify({"success": False, "error": "number is required"}), 400
    return jsonify({"success": True, "message": f"Forwarding target {number} is syntactically valid. Place a live Twilio test call to verify carrier reachability."})


@inbox_pwa_bp.route("/api/phone/voice-client-error", methods=["POST"])
def api_phone_voice_client_error():
    """Log precise browser Voice SDK setup failures for support diagnostics."""
    user = _require_auth()
    company = _require_company(user)
    data = request.get_json(silent=True) or {}
    logger.warning(
        "Browser voice client error",
        extra={
            "user_id": user.id,
            "company_id": company.id,
            "voice_error_code": data.get("code"),
            "voice_error_message": data.get("message"),
            "voice_error_detail": data.get("detail"),
        },
    )
    return jsonify({"success": True})


@inbox_pwa_bp.route("/api/phone/voice-token")
def api_phone_voice_token():
    """Issue a Twilio Voice SDK access token for an authorized PWA user."""
    user = _require_auth()
    company = _require_company(user)
    from models import PhoneNumberUserPermission, TwilioAccount, TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers

    allowed_numbers = accessible_phone_numbers(user, company.id)
    if not allowed_numbers:
        logger.info("Voice token denied: no assigned calling number", extra={"user_id": user.id, "company_id": company.id})
        return jsonify({"success": False, "code": "NO_ASSIGNED_NUMBER", "error": "No calling number assigned"}), 403

    callable_numbers = [
        p.phone_number.phone_number for p in PhoneNumberUserPermission.query
        .join(TwilioPhoneNumber, PhoneNumberUserPermission.phone_number_id == TwilioPhoneNumber.id)
        .filter(
            PhoneNumberUserPermission.company_id == company.id,
            PhoneNumberUserPermission.user_id == user.id,
            PhoneNumberUserPermission.can_access_pwa.is_(True),
            PhoneNumberUserPermission.can_call.is_(True),
            TwilioPhoneNumber.is_active.is_(True),
        ).all()
        if p.phone_number and p.phone_number.phone_number
    ]
    explicit_call_permissions = PhoneNumberUserPermission.query.filter_by(company_id=company.id, user_id=user.id).count() > 0
    if explicit_call_permissions:
        allowed_numbers = [n for n in allowed_numbers if n in set(callable_numbers)]
    if not allowed_numbers:
        logger.info("Voice token denied: user lacks call permission", extra={"user_id": user.id, "company_id": company.id})
        return jsonify({"success": False, "code": "NO_ASSIGNED_NUMBER", "error": "No calling number assigned"}), 403

    ta = TwilioAccount.query.filter_by(company_id=company.id).first()
    account_sid = (
        os.environ.get("TWILIO_ACCOUNT_SID")
        or (ta.get_account_sid() if ta and hasattr(ta, "get_account_sid") else None)
    )
    api_key = os.environ.get("TWILIO_API_KEY")
    api_secret = os.environ.get("TWILIO_API_SECRET")
    if not account_sid or not api_key or not api_secret:
        logger.error("Voice token failed: Twilio Voice SDK credentials are not configured", extra={"user_id": user.id, "company_id": company.id})
        return jsonify({
            "success": False,
            "code": "TOKEN_ENDPOINT_FAILED",
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
            "calling_number": allowed_numbers[0],
            "permitted_numbers": allowed_numbers,
        })
    except Exception as exc:
        logger.exception("Unable to issue Twilio Voice token", extra={"user_id": user.id, "company_id": company.id})
        return jsonify({"success": False, "code": "TOKEN_ENDPOINT_FAILED", "error": str(exc)}), 500


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
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied

    from models import TwilioConversation
    filter_by = request.args.get("filter", "all")
    search    = request.args.get("q", "").strip()
    page      = int(request.args.get("page", 1))
    number_filter = (request.args.get("number") or "").strip()

    from services.comms_permissions import filter_conversations_for_user, accessible_phone_numbers
    allowed_numbers = accessible_phone_numbers(user, company.id)
    if not allowed_numbers:
        return jsonify({
            "success": True,
            "conversations": [],
            "unread_count": 0,
            "total": 0,
            "page": page,
            "code": "NO_ASSIGNED_NUMBER",
            "empty_title": "No phone number assigned",
            "empty_message": "Ask an admin to assign a phone number before using the mobile inbox.",
        })
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


@inbox_pwa_bp.route("/api/inbox/messages")
def list_messages():
    """List recent messages scoped to conversations the PWA user may access."""
    user = _require_auth()
    company = _require_company(user)
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied

    from models import TwilioConversation, TwilioMessage
    from services.comms_permissions import accessible_phone_numbers
    allowed_numbers = accessible_phone_numbers(user, company.id)
    if not allowed_numbers:
        return jsonify({
            "success": True,
            "messages": [],
            "total": 0,
            "code": "NO_ASSIGNED_NUMBER",
            "empty_title": "No phone number assigned",
            "empty_message": "Ask an admin to assign a phone number before using the mobile inbox.",
        })

    limit = min(int(request.args.get("limit", 100)), 250)
    rows = (TwilioMessage.query
        .join(TwilioConversation, TwilioMessage.conversation_id == TwilioConversation.id)
        .filter(
            TwilioMessage.company_id == company.id,
            TwilioConversation.company_id == company.id,
            TwilioConversation.to_number.in_(allowed_numbers),
        )
        .order_by(TwilioMessage.created_at.desc())
        .limit(limit)
        .all())
    return jsonify({"success": True, "messages": [_msg_to_dict(m) for m in rows], "total": len(rows)})


@inbox_pwa_bp.route("/api/inbox/messages/<int:message_id>/media/<int:media_index>")
def api_inbox_message_media(message_id, media_index):
    """Authenticated SMS/MMS media proxy so Twilio credentials stay server-side."""
    user = _require_auth()
    company = _require_company(user)
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied
    from models import TwilioConversation, TwilioMessage
    from services.comms_permissions import filter_conversations_for_user

    msg = (TwilioMessage.query
        .join(TwilioConversation, TwilioConversation.id == TwilioMessage.conversation_id)
        .filter(TwilioMessage.id == message_id, TwilioMessage.company_id == company.id)
        .first_or_404())
    filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=msg.conversation_id, company_id=company.id), user, company.id
    ).first_or_404()

    media_urls = msg.media_urls or []
    if media_index < 0 or media_index >= len(media_urls):
        return jsonify({"success": False, "error": "Media attachment not found."}), 404
    media_url = media_urls[media_index]
    if not _is_twilio_protected_url(media_url):
        return jsonify({"success": False, "error": "Only protected provider media is available through this proxy."}), 400

    sid, token = _twilio_recording_auth(company.id)
    if not sid or not token:
        return jsonify({"success": False, "error": "Media playback is not configured. Ask an admin to verify Twilio credentials."}), 503
    try:
        import requests
        upstream = requests.get(media_url, auth=(sid, token), timeout=20, stream=True)
        upstream.raise_for_status()
    except Exception:
        logger.exception("Twilio message media proxy failed", extra={"company_id": company.id, "message_id": message_id})
        return jsonify({"success": False, "error": "Media attachment could not be loaded. Please try again or contact support."}), 502

    filename = quote((urlparse(media_url).path.rstrip('/').split('/')[-1] or f"message-{message_id}-media-{media_index}"))
    headers = {"Content-Disposition": f"inline; filename*=UTF-8''{filename}"}
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length
    return Response(
        stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
        mimetype=upstream.headers.get("Content-Type", "application/octet-stream"),
        headers=headers,
    )


# ── API: single conversation + messages ───────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>")
def get_conversation(conv_id):
    user    = _require_auth()
    company = _require_company(user)
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied
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
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied
    from models import TwilioConversation
    from services.comms_permissions import filter_conversations_for_user
    conv = filter_conversations_for_user(
        TwilioConversation.query.filter_by(id=conv_id, company_id=company.id), user, company.id
    ).first()
    if not conv:
        return _json_error("Conversation not found or is not assigned to one of your phone lines.", 404, "conversation_not_found")

    payload = request.get_json(silent=True) or {}
    body    = (payload.get("body") or "").strip()
    if not body:
        return _json_error("Message body is required.", 422, "invalid_message")
    if not conv.from_number:
        return _json_error("Conversation has no recipient phone number.", 422, "invalid_recipient")
    if not conv.to_number:
        return _json_error("Conversation has no sending phone line.", 404, "phone_line_not_found")
    if not _can_send_sms_from_number(user, company.id, conv.to_number):
        return _json_error("You do not have permission to send SMS from this phone line.", 403, "phone_line_forbidden")

    ta = _get_twilio_account(company.id)
    if not ta:
        logger.warning("send_message: no Twilio account for company=%d user=%d conv=%d",
                       company.id, user.id, conv_id)
        return _json_error(
            "Twilio is not configured for this account. Add your Twilio credentials in SMS Settings.",
            404,
            "phone_line_not_found",
        )
    if not ta.is_configured:
        logger.warning("send_message: Twilio account incomplete for company=%d user=%d conv=%d",
                       company.id, user.id, conv_id)
        return _json_error(
            "Twilio credentials are incomplete. Check your Account SID, Auth Token, and phone number in SMS Settings.",
            422,
            "twilio_not_configured",
        )

    from twilio_sms import sendConversationSms
    result = sendConversationSms(conv.id, body, twilio_account=ta)
    if not result.get("success"):
        logger.error("send_message failed: user=%d company=%d conv=%d to=%s error=%s",
                     user.id, company.id, conv_id, conv.from_number, result.get("error"))
        return _json_error(result.get("error") or "SMS send failed", 502, "provider_failure")
    from models import TwilioMessage
    record = TwilioMessage.query.filter_by(conversation_id=conv.id, direction="outbound").order_by(TwilioMessage.id.desc()).first()

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
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    to_raw  = (payload.get("to") or "").strip()
    body    = (payload.get("body") or "").strip()

    if not to_raw:
        return _json_error("Recipient phone number is required.", 422, "invalid_recipient")
    if not body:
        return _json_error("Message body is required.", 422, "invalid_message")

    to_num = _normalize_phone(to_raw)
    if len(to_num) < 7:
        return _json_error(f"Invalid phone number: {to_raw}", 422, "invalid_recipient")

    ta = _get_twilio_account(company.id)
    if not ta:
        return _json_error(
            "No active sending phone line was found for this company.",
            404,
            "phone_line_not_found",
        )
    if not ta.is_configured:
        return _json_error(
            "Twilio is not configured for this account. Add your Twilio credentials in SMS Settings.",
            422,
            "twilio_not_configured",
        )
    if not ta.from_phone:
        return _json_error("No sending phone line is configured for this company.", 404, "phone_line_not_found")
    if not _can_send_sms_from_number(user, company.id, ta.from_phone):
        return _json_error("You do not have permission to send SMS from this phone line.", 403, "phone_line_forbidden")

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

    from twilio_sms import sendConversationSms
    result = sendConversationSms(conv.id, body, twilio_account=ta, to_number=to_num)
    if not result.get("success"):
        logger.error("new_conversation send failed user=%d company=%d to=%s: %s",
                     user.id, company.id, to_num, result.get("error"))
        return _json_error(result.get("error") or "SMS send failed", 502, "provider_failure")
    from models import TwilioMessage
    record = TwilioMessage.query.filter_by(conversation_id=conv.id, direction="outbound").order_by(TwilioMessage.id.desc()).first()

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




def _pwa_badge_counts_for(user, company):
    """Tenant- and user-scoped communication badge counts for installed PWAs."""
    from models import Notification, TwilioCallLog, TwilioConversation, VoiceVoicemailMessage
    from services.comms_permissions import filter_calls_for_user, filter_conversations_for_user

    sms_unread = filter_conversations_for_user(
        TwilioConversation.query.filter_by(company_id=company.id, is_read=False),
        user, company.id,
    ).all()
    sms_unread = [c for c in sms_unread if not _is_archived_conversation(c)]

    missed_calls = filter_calls_for_user(
        TwilioCallLog.query.filter_by(company_id=company.id, direction="inbound", is_read=False, is_archived=False),
        user, company.id,
    ).filter(TwilioCallLog.status.in_(["missed", "no-answer", "busy", "failed"])).count()

    voicemails = VoiceVoicemailMessage.query.filter_by(
        company_id=company.id,
        is_read=False,
        is_deleted=False,
    )
    try:
        from models import TwilioPhoneNumber
        accessible_numbers = _accessible_numbers_for(user, company.id)
        accessible_ids = [pn.id for pn in TwilioPhoneNumber.query.filter(TwilioPhoneNumber.company_id == company.id, TwilioPhoneNumber.phone_number.in_(accessible_numbers or ["__none__"])).all()]
        if accessible_ids:
            voicemails = voicemails.filter(db.or_(VoiceVoicemailMessage.phone_number_id.is_(None), VoiceVoicemailMessage.phone_number_id.in_(accessible_ids)))
    except Exception:
        pass
    voicemails = voicemails.count()

    notifications = Notification.query.filter_by(
        user_id=user.id,
        company_id=company.id,
        is_read=False,
    ).count()

    total = len(sms_unread) + missed_calls + voicemails + notifications
    return {
        "count": total,
        "smsUnread": len(sms_unread),
        "missedCalls": missed_calls,
        "voicemails": voicemails,
        "notifications": notifications,
    }


@inbox_pwa_bp.route("/api/pwa/badge-count")
def pwa_badge_count():
    user = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"count": 0, "smsUnread": 0, "missedCalls": 0, "voicemails": 0, "notifications": 0})
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    return jsonify(_pwa_badge_counts_for(user, company))

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


def _event_allowed_for_user(user, event_type: str) -> bool:
    event_type = (event_type or "").lower()
    if event_type in {"inbound_sms", "incoming_sms", "new_message", "unread_message_reminder", "unread_reminder"}:
        attr = "pwa_unread_reminder_alerts_enabled" if event_type in {"unread_message_reminder", "unread_reminder"} else "pwa_text_alerts_enabled"
    elif event_type in {"incoming_call", "missed_call", "call"}:
        attr = "pwa_call_alerts_enabled"
    elif event_type in {"voicemail", "new_voicemail"}:
        attr = "pwa_voicemail_alerts_enabled"
    else:
        attr = None
    return True if not attr else getattr(user, attr, True) is not False



def _canonical_push_event_type(event_type: str) -> str:
    aliases = {
        "inbound_sms": "incoming_sms",
        "new_message": "incoming_sms",
        "unread_message_reminder": "unread_reminder",
        "new_voicemail": "voicemail",
        "call": "missed_call",
    }
    return aliases.get((event_type or "notification").lower(), (event_type or "notification").lower())


def _hm_to_minutes(value):
    try:
        hour, minute = [int(part) for part in (value or "").split(":", 1)]
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except Exception:
        return None
    return None


def _now_in_user_quiet_hours(user, now=None) -> bool:
    start = _hm_to_minutes(getattr(user, "pwa_quiet_hours_start", None))
    end = _hm_to_minutes(getattr(user, "pwa_quiet_hours_end", None))
    if start is None or end is None or start == end:
        return False
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    return start <= current < end if start < end else (current >= start or current < end)


def _push_debug_decision(user, event_type: str, *, requested_silent: bool, in_business_hours=True):
    canonical = _canonical_push_event_type(event_type)
    if not _event_allowed_for_user(user, event_type) and not _event_allowed_for_user(user, canonical):
        return {"send": False, "reason": "user_alert_preference_disabled", "event_type": canonical}
    if getattr(user, "pwa_alerts_business_hours_only", True) is not False and in_business_hours is False and getattr(user, "pwa_after_hours_push_enabled", False) is not True:
        return {"send": False, "reason": "outside_business_hours_and_after_hours_push_disabled", "event_type": canonical}
    quiet = _now_in_user_quiet_hours(user)
    sound_enabled = getattr(user, "notification_sounds_enabled", True) is not False
    allow_silent_override = os.environ.get("LUXIT_ALLOW_SILENT_PUSH", "").lower() in {"1", "true", "yes"}
    would_silence = bool(requested_silent or quiet or not sound_enabled)
    silent = bool(would_silence and allow_silent_override)
    vibration_enabled = getattr(user, "pwa_vibration_enabled", True) is not False
    return {
        "send": True,
        "reason": "quiet_hours" if quiet else ("sound_preference_disabled" if not sound_enabled else "alert_enabled"),
        "would_silence_without_lock": would_silence,
        "silent_override_locked": would_silence and not allow_silent_override,
        "event_type": canonical,
        "silent": silent,
        "vibrate": [200, 100, 200] if vibration_enabled else [],
        "sound": "default",
        "renotify": True,
    }

def _notification_payload_preferences(user, *, silent: bool = False):
    return {
        "soundEnabled": (getattr(user, "notification_sounds_enabled", True) is not False) and not silent,
        "vibrationEnabled": (getattr(user, "pwa_vibration_enabled", True) is not False) and not silent,
        "silent": bool(silent),
        "quietHoursStart": getattr(user, "pwa_quiet_hours_start", None),
        "quietHoursEnd": getattr(user, "pwa_quiet_hours_end", None),
    }


def _create_notification_records(company_id: int, *, event_type: str, title: str, notification_body: str,
                                 link: str = "/app/inbox", phone_number_id=None, category: str = "communications",
                                 icon: str = "bell"):
    from models import Notification
    created = []
    for user in _authorized_notification_users(company_id, phone_number_id):
        if not _event_allowed_for_user(user, event_type):
            continue
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
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
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
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
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


def _redact_push_endpoint(endpoint: str) -> dict:
    endpoint = endpoint or ""
    if not endpoint:
        return {"endpoint_host": "", "endpoint_tail": "", "endpoint_redacted": None}
    try:
        parsed = urlparse(endpoint)
        return {
            "endpoint_host": parsed.netloc,
            "endpoint_tail": endpoint[-18:],
            "endpoint_redacted": f"{parsed.scheme}://{parsed.netloc}/…{endpoint[-18:]}" if parsed.scheme and parsed.netloc else f"…{endpoint[-18:]}",
        }
    except Exception:
        return {"endpoint_host": "", "endpoint_tail": endpoint[-18:], "endpoint_redacted": f"…{endpoint[-18:]}"}


def _recent_push_receipts(company_id: int, user_id: int, device_key: str = "", limit: int = 10):
    from models import MarketingAuditLog
    rows = MarketingAuditLog.query.filter_by(
        company_id=company_id,
        created_by_user_id=user_id,
        entity_type="pwa_push_receipt",
    ).order_by(MarketingAuditLog.created_at.desc(), MarketingAuditLog.id.desc()).limit(50).all()
    receipts = []
    for row in rows:
        details = row.details or {}
        if device_key and details.get("device_key") and details.get("device_key") != device_key:
            continue
        receipts.append({
            "stage": details.get("stage"),
            "received_at": details.get("received_at"),
            "server_received_at": row.created_at.isoformat() if row.created_at else None,
            "sw_version": details.get("sw_version"),
            "event_type": details.get("event_type"),
            "tag": details.get("tag"),
            "silent": details.get("silent"),
            "renotify": details.get("renotify"),
            "vibrate": details.get("vibrate"),
            "requireInteraction": details.get("requireInteraction"),
            "endpoint_host": details.get("endpoint_host"),
            "endpoint_tail": details.get("endpoint_tail"),
            "endpoint_redacted": details.get("endpoint_redacted"),
            "error": details.get("error"),
        })
        if len(receipts) >= limit:
            break
    return receipts

def _web_push_missing_settings():
    return [key for key in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT") if not os.environ.get(key)]


@inbox_pwa_bp.route("/api/push/public-key")
@inbox_pwa_bp.route("/api/pwa/push/public-key")
def push_public_key():
    missing = _web_push_missing_settings()
    return jsonify({"success": True, "publicKey": os.environ.get("VAPID_PUBLIC_KEY", ""), "configured": not missing, "missing": missing})


@inbox_pwa_bp.route("/api/pwa/push/status")
def push_setup_status():
    missing = _web_push_missing_settings()
    return jsonify({"success": True, "configured": not missing, "missing": missing, "message": "Web Push is configured." if not missing else "Web Push server setup is incomplete: " + ", ".join(missing)})


def _web_push_configured():
    return not _web_push_missing_settings()

def _send_web_push_to_subscriptions(subscriptions, payload: dict):
    """Server-side Web Push sender; disables expired subscriptions and never broadens caller-provided scope."""
    if not _web_push_configured():
        return {"sent": 0, "errors": ["Web push is not configured. Set VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, and VAPID_SUBJECT."]}
    import json
    sent, errors = 0, []
    for sub in subscriptions:
        if getattr(sub, "is_active", True) is False:
            continue
        try:
            from pywebpush import webpush
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth_key}},
                data=json.dumps(payload),
                vapid_private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
                vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:admin@luxit.app")},
            )
            sub.last_used_at = datetime.utcnow()
            sent += 1
        except Exception as exc:
            msg = str(exc)
            errors.append(msg)
            if "410" in msg or "404" in msg:
                sub.is_active = False
                sub.updated_at = datetime.utcnow()
    db.session.commit()
    return {"sent": sent, "errors": errors}

def send_pwa_push_notification(company_id: int, *, user_ids, title: str, body: str, link: str = "/app/inbox", tag: str = "luxit-inbox", event_type: str = "notification", phone_number_id=None, silent: bool = False, in_business_hours=True):
    """Internal tenant-scoped push helper for notifications already permission-filtered by caller."""
    from models import PushSubscription, User
    ids = [int(uid) for uid in (user_ids or [])]
    if not ids:
        return {"sent": 0, "errors": [], "debug": []}

    users = User.query.filter(User.id.in_(ids)).all()
    total = 0
    errors = []
    debug = []
    for user in users:
        decision = _push_debug_decision(user, event_type, requested_silent=silent, in_business_hours=in_business_hours)
        decision.update({"user_id": user.id})
        debug.append(decision)
        if not decision.get("send"):
            continue
        subs = PushSubscription.query.filter_by(
            company_id=company_id,
            user_id=user.id,
            is_active=True,
        ).all()
        decision["active_subscriptions"] = len(subs)
        try:
            from models import Company
            company = db.session.get(Company, company_id)
            badge_count = _pwa_badge_counts_for(user, company)["count"] if company else None
        except Exception:
            badge_count = None
        payload = {
            "title": title,
            "body": body,
            "url": link,
            "tag": tag,
            "renotify": True,
            "requireInteraction": event_type in {"incoming_sms", "missed_call", "voicemail"},
            "eventType": decision["event_type"],
            "icon": "/static/icons/icon-192.png",
            "badge": "/static/icons/badge-72.png",
            "sound": decision["sound"],
            "silent": bool(decision["silent"]),
            "vibrate": decision["vibrate"],
            "channel_id": "high_priority_messages",
            "channelId": "high_priority_messages",
            "importance": "high",
            "data": {
                "company_id": company_id,
                "phone_number_id": phone_number_id,
                "event_type": decision["event_type"],
                "silent": bool(decision["silent"]),
                "debug_reason": decision["reason"],
                "badgeCount": badge_count,
                "channel_id": "high_priority_messages",
                "channelId": "high_priority_messages",
                "importance": "high",
            },
            "badgeCount": badge_count,
        }
        result = _send_web_push_to_subscriptions(subs, payload)
        logger.info("PWA push send", extra={
            "user_id": user.id, "company_id": company_id, "phone_number_id": phone_number_id,
            "event_type": decision["event_type"], "silent": payload["silent"], "sound": payload["sound"],
            "vibrate": payload["vibrate"], "renotify": payload["renotify"], "tag": payload["tag"],
            "badgeCount": badge_count, "channel": payload["channelId"], "importance": payload["importance"],
            "sw_version": os.environ.get("LUXIT_ASSET_VERSION") or os.environ.get("GIT_SHA") or os.environ.get("RENDER_GIT_COMMIT") or "20260710-push-receipt-ack",
            "push_provider_result": result,
        })
        total += result.get("sent", 0)
        errors.extend(result.get("errors", []))
    return {"sent": total, "errors": errors, "debug": debug}


@inbox_pwa_bp.route("/api/pwa/push/debug")
def pwa_push_debug():
    """Explain whether the current user's next PWA push would alert, vibrate, or be skipped."""
    user = _current_user()
    if not user:
        return jsonify({
            "success": False,
            "code": "AUTHENTICATION_REQUIRED",
            "error": "Authentication required. Open the PWA and sign in again.",
        }), 401
    company = _get_company(user)
    if not company:
        return jsonify({
            "success": False,
            "code": "NO_COMPANY",
            "error": "No company is configured for this user.",
        }), 409
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    event_type = request.args.get("event_type") or "incoming_sms"
    in_business = str(request.args.get("in_business_hours", "1")).lower() not in {"0", "false", "no"}
    decision = _push_debug_decision(user, event_type, requested_silent=False, in_business_hours=in_business)
    from models import PushSubscription
    device_key = (request.headers.get("X-PWA-Device-Key") or request.args.get("device_key") or "").strip()
    active_query = PushSubscription.query.filter_by(company_id=company.id, user_id=user.id, is_active=True)
    active_subscriptions = active_query.count()
    device_active_subscriptions = active_query.filter_by(device_key=device_key).count() if device_key else 0
    subscriptions = active_query.order_by(PushSubscription.updated_at.desc(), PushSubscription.id.desc()).limit(10).all()
    missing = _web_push_missing_settings()
    return jsonify({
        "success": True,
        "notification_permission": "client-reported",
        "active_subscription": active_subscriptions > 0,
        "active_subscriptions": active_subscriptions,
        "device_key": device_key,
        "device_active_subscription": device_active_subscriptions > 0,
        "device_active_subscriptions": device_active_subscriptions,
        "vapid_configured": not missing,
        "vapid_public_key_present": bool(os.environ.get("VAPID_PUBLIC_KEY")),
        "vapid_missing": missing,
        "service_worker_version": os.environ.get("LUXIT_ASSET_VERSION") or os.environ.get("GIT_SHA") or os.environ.get("RENDER_GIT_COMMIT") or "20260710-push-receipt-ack",
        "decision": decision,
        "device_instructions": [
            "Android: Chrome/LUXit PWA notification channel cannot be Silent or Low Importance.",
            "Android: enable sound and vibration for the Chrome/LUXit PWA notification category.",
            "iPhone: install to Home Screen, then enable Settings > Notifications > LUXit > Sounds.",
            "Disable Focus / Do Not Disturb during notification sound tests.",
        ],
        "push_receipts": _recent_push_receipts(company.id, user.id, device_key),
        "latest_push_receipt": (_recent_push_receipts(company.id, user.id, device_key, limit=1) or [None])[0],
        "subscriptions": [{
            "id": sub.id,
            "device_key": sub.device_key,
            **_redact_push_endpoint(sub.endpoint),
            "is_active": sub.is_active,
            "created_at": sub.created_at.isoformat() if sub.created_at else None,
            "updated_at": sub.updated_at.isoformat() if sub.updated_at else None,
            "last_successful_registration_attempt_at": sub.updated_at.isoformat() if sub.updated_at else None,
        } for sub in subscriptions],
        "user": {"id": user.id, "email": user.email, "username": user.username},
        "company": {"id": company.id, "name": company.name},
        "mobile_inbox_access": True,
    })



@inbox_pwa_bp.route("/api/pwa/push/receipt", methods=["POST"])
def pwa_push_receipt():
    """Service-worker acknowledgement that a push was received/displayed on-device."""
    user = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied

    payload = request.get_json(silent=True) or {}
    stage = (payload.get("stage") or "received").strip()[:40]
    subscription = payload.get("subscription") or {}
    endpoint = subscription.get("endpoint") or payload.get("endpoint") or ""
    redacted = _redact_push_endpoint(endpoint)
    # Prefer the already-redacted SW summary when the full endpoint is intentionally omitted.
    if not endpoint:
        redacted = {
            "endpoint_host": subscription.get("endpoint_host", ""),
            "endpoint_tail": subscription.get("endpoint_tail", ""),
            "endpoint_redacted": subscription.get("endpoint_redacted"),
        }

    from models import MarketingAuditLog, PushSubscription
    device_key = (payload.get("device_key") or request.headers.get("X-PWA-Device-Key") or "").strip()
    if endpoint:
        sub = PushSubscription.query.filter_by(company_id=company.id, user_id=user.id, endpoint=endpoint, is_active=True).first()
        if sub:
            device_key = device_key or (sub.device_key or "")

    details = {
        "stage": stage,
        "received_at": payload.get("received_at"),
        "sw_version": payload.get("sw_version"),
        "event_type": payload.get("event_type"),
        "tag": payload.get("tag"),
        "silent": payload.get("silent"),
        "renotify": payload.get("renotify"),
        "vibrate": payload.get("vibrate"),
        "requireInteraction": payload.get("requireInteraction"),
        "error": payload.get("error"),
        "device_key": device_key,
        **redacted,
    }
    db.session.add(MarketingAuditLog(
        company_id=company.id,
        created_by_user_id=user.id,
        entity_type="pwa_push_receipt",
        action=stage,
        details=details,
    ))
    db.session.commit()
    logger.info("PWA push receipt", extra={"user_id": user.id, "company_id": company.id, "stage": stage, **redacted})
    return jsonify({"success": True, "stage": stage, "device_key": device_key, **redacted})

# ── API: Push notification subscribe ─────────────────────────────────────────

@inbox_pwa_bp.route("/api/push/subscribe", methods=["POST"])
@inbox_pwa_bp.route("/api/pwa/push/subscribe", methods=["POST"])
@inbox_pwa_bp.route("/api/inbox/push/subscribe", methods=["POST"])
def push_subscribe():
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied

    payload  = request.get_json() or {}
    endpoint = payload.get("endpoint", "")
    device_key = (payload.get("device_key") or payload.get("deviceKey") or request.headers.get("X-PWA-Device-Key") or "").strip()
    keys = payload.get("keys") or {}
    p256dh   = keys.get("p256dh", "")
    auth_key = keys.get("auth", "")

    if not endpoint:
        return jsonify({"success": False, "error": "endpoint required", "code": "MISSING_ENDPOINT"}), 400
    if not p256dh or not auth_key:
        return jsonify({"success": False, "error": "subscription keys p256dh and auth are required", "code": "MISSING_SUBSCRIPTION_KEYS"}), 400

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
            user_agent=request.headers.get('User-Agent'),
            device_label=payload.get('device_label') or payload.get('deviceName') or device_key,
            is_active=True,
        )
        db.session.add(sub)
    else:
        sub.user_id = user.id
        sub.company_id = company.id
        sub.device_key = device_key or getattr(sub, "device_key", None)
        sub.p256dh   = p256dh
        sub.auth_key = auth_key
        sub.user_agent = request.headers.get('User-Agent')
        sub.device_label = payload.get('device_label') or payload.get('deviceName') or device_key
        sub.is_active = True
        sub.updated_at = datetime.utcnow()
    if device_key:
        from models import PWADevice
        device = PWADevice.query.filter_by(company_id=company.id, user_id=user.id, device_key=device_key).first()
        if not device:
            device = PWADevice(
                company_id=company.id,
                user_id=user.id,
                device_key=device_key,
                device_name=payload.get('device_label') or payload.get('deviceName') or device_key,
                browser=(request.user_agent.browser or None),
                push_enabled=True,
                last_seen_at=datetime.utcnow(),
            )
            db.session.add(device)
        else:
            device.push_enabled = True
            device.last_seen_at = datetime.utcnow()
    db.session.commit()
    active_query = PushSubscription.query.filter_by(user_id=user.id, company_id=company.id, is_active=True)
    active_subscriptions = active_query.count()
    device_active_subscriptions = active_query.filter_by(device_key=device_key).count() if device_key else 0
    logger.info("Push subscription saved", extra={
        "user_id": user.id,
        "company_id": company.id,
        "device_key": device_key,
        "endpoint_host": endpoint.split("/")[2] if "/" in endpoint else "",
        "active_subscriptions": active_subscriptions,
        "device_active_subscriptions": device_active_subscriptions,
    })
    return jsonify({
        "success": True,
        "device_key": device_key,
        "subscription_id": sub.id,
        "endpoint_saved": True,
        "database_record_created": True,
        "last_successful_registration_attempt_at": sub.updated_at.isoformat() if sub.updated_at else None,
        "active_subscription": active_subscriptions > 0,
        "active_subscriptions": active_subscriptions,
        "device_active_subscription": device_active_subscriptions > 0,
        "device_active_subscriptions": device_active_subscriptions,
    })


@inbox_pwa_bp.route("/api/push/unsubscribe", methods=["POST"])
@inbox_pwa_bp.route("/api/pwa/push/unsubscribe", methods=["POST"])
@inbox_pwa_bp.route("/api/inbox/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    user = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied
    payload = request.get_json() or {}
    endpoint = payload.get("endpoint", "")
    from models import PushSubscription
    q = PushSubscription.query.filter_by(user_id=user.id)
    if company:
        q = q.filter_by(company_id=company.id)
    if endpoint:
        q = q.filter_by(endpoint=endpoint)
    count = 0
    for sub in q.all():
        sub.is_active = False
        sub.updated_at = datetime.utcnow()
        count += 1
    db.session.commit()
    return jsonify({"success": True, "disabled": count})


@inbox_pwa_bp.route("/api/push/test", methods=["POST"])
@inbox_pwa_bp.route("/api/pwa/push/test", methods=["POST"])
@inbox_pwa_bp.route("/api/inbox/push/test", methods=["POST"])
def push_test():
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied

    from models import PushSubscription
    subs = PushSubscription.query.filter_by(user_id=user.id, company_id=company.id, is_active=True).all()
    if not subs:
        return jsonify({
            "success": False,
            "backend_persisted": False,
            "provider_accepted": False,
            "service_worker_receipt_confirmed": False,
            "notification_display_confirmed": False,
            "error": "No push subscription found. Enable notifications first.",
        })

    if not _web_push_configured():
        return jsonify({
            "success": False,
            "configured": False,
            "backend_persisted": True,
            "provider_accepted": False,
            "service_worker_receipt_confirmed": False,
            "notification_display_confirmed": False,
            "error": "Web push is not configured. Set VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, and VAPID_SUBJECT.",
        })

    result = _send_web_push_to_subscriptions(subs, {
        "title": "LUXit Inbox",
        "body": "Push notifications are working!",
        "url": "/app/inbox",
        "tag": "push-test",
        "icon": "/static/favicon.png",
        "badge": "/static/favicon.png",
        "sound": "default",
        "silent": False,
        "vibrate": [200, 100, 200],
        "renotify": True,
        "requireInteraction": False,
        "data": {"event_type": "push_test", "silent": False},
    })
    sent = result["sent"]
    errors = result["errors"]

    if sent:
        return jsonify({
            "success": True,
            "sent": sent,
            "backend_persisted": True,
            "provider_accepted": True,
            "service_worker_receipt_confirmed": False,
            "notification_display_confirmed": False,
            "delivery_note": "Push provider accepted the test notification. Confirm service-worker receipt via lastPushReceivedAt in client diagnostics.",
        })
    return jsonify({
        "success": False,
        "sent": sent,
        "backend_persisted": True,
        "provider_accepted": False,
        "service_worker_receipt_confirmed": False,
        "notification_display_confirmed": False,
        "error": errors[0] if errors else "Unknown error",
    })


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


def _conversation_in_business_hours(conv) -> bool:
    phone_config = getattr(conv, "phone_number", None)
    # Legacy rows without explicit per-number hours should not suddenly silence
    # alerts/reminders because no schedule was configured.
    if not (getattr(phone_config, "business_hours", None) if phone_config else None):
        return True
    try:
        import twilio_sms
        return twilio_sms._is_business_hours(conv.company_id, phone_config=phone_config)
    except Exception:
        return True


def _conversation_has_stop_condition(conv, now=None) -> bool:
    if getattr(conv, "is_read", False):
        return True
    if _is_archived_conversation(conv):
        return True
    from models import TwilioMessage
    latest = conv.messages.order_by(TwilioMessage.created_at.desc()).first()
    if latest and latest.direction == "outbound":
        return True
    if latest and getattr(latest, "is_auto_reply", False):
        return True
    return False


def _fire_push_notification(company_id: int, conv, message_body: str, *, silent: bool | None = None):
    """Called from inbound SMS webhook; records notification history and sends Web Push."""
    sender = conv.contact_name or conv.from_number
    phone_number_id = getattr(conv, "phone_number_id", None)
    notification_body = (message_body or "(media)")[:160]
    title = f"New message from {sender}"
    link = f"/app/inbox?conv={conv.id}"
    _create_notification_records(
        company_id,
        event_type="incoming_sms",
        title=title,
        notification_body=notification_body,
        link=link,
        phone_number_id=phone_number_id,
        icon="message-square",
    )

    allowed_user_ids = [u.id for u in _authorized_notification_users(company_id, phone_number_id)]
    in_business = _conversation_in_business_hours(conv)
    silent = False if silent is None else bool(silent)
    send_pwa_push_notification(
        company_id,
        user_ids=allowed_user_ids,
        title=title,
        body=notification_body,
        link=link,
        tag=f"sms-{conv.id}",
        event_type="incoming_sms",
        phone_number_id=phone_number_id,
        silent=silent,
        in_business_hours=in_business,
    )


def create_pwa_notification(company_id: int, *, event_type: str, title: str, body: str,
                            link: str = "/app/inbox", phone_number_id=None, icon: str = "bell", silent: bool = False, emit_sse: bool = True):
    """Public helper for call/voicemail webhooks to persist notification history and push to permitted users."""
    records = _create_notification_records(
        company_id,
        event_type=event_type,
        title=title,
        notification_body=body,
        link=link,
        phone_number_id=phone_number_id,
        icon=icon,
    )
    allowed_user_ids = [u.id for u in _authorized_notification_users(company_id, phone_number_id)]
    send_pwa_push_notification(
        company_id,
        user_ids=allowed_user_ids,
        title=title,
        body=body,
        link=link,
        tag=f"{event_type}-{phone_number_id or 'company'}",
        event_type=event_type,
        phone_number_id=phone_number_id,
        silent=silent,
    )
    if emit_sse:
        _push_sse_event(company_id, event_type, {
            "title": title,
            "body": body,
            "link": link,
            "url": link,
            "phone_number_id": phone_number_id,
            "silent": bool(silent),
        })
    return records


def create_unread_message_reminders(now=None, dry_run: bool = False):
    """Create one-minute unread SMS reminders without duplicate storms."""
    from datetime import timedelta
    from models import Notification, TwilioConversation
    now = now or datetime.utcnow()
    created = []
    would_create = 0
    unread = TwilioConversation.query.filter_by(is_read=False).all()
    for conv in unread:
        if _conversation_has_stop_condition(conv, now=now):
            continue
        if not _conversation_in_business_hours(conv):
            continue
        last = Notification.query.filter_by(
            company_id=conv.company_id,
            phone_number_id=getattr(conv, "phone_number_id", None),
            event_type="unread_message_reminder",
            link=f"/app/inbox?conv={conv.id}",
        ).filter(Notification.created_at >= now - timedelta(minutes=1)).first()
        if last:
            continue
        if dry_run:
            would_create += 1
            continue
        rows = create_pwa_notification(
            conv.company_id,
            event_type="unread_message_reminder",
            title=f"Unread message from {conv.contact_name or conv.from_number}",
            body=conv.last_message_preview or "Unread message needs attention",
            link=f"/app/inbox?conv={conv.id}",
            phone_number_id=getattr(conv, "phone_number_id", None),
            icon="message-circle",
        )
        created.extend(rows)
    return {"created": len(created), "would_create": would_create, "dry_run": dry_run}


@inbox_pwa_bp.route("/api/pwa/reminders/unread/run", methods=["POST"])
def api_run_unread_reminders():
    user = _require_auth()
    company = _require_company(user)
    denied = _require_mobile_inbox_api_access(user, company)
    if denied:
        return denied
    dry_run = str(request.args.get("dry_run", "")).lower() in {"1", "true", "yes"}
    result = create_unread_message_reminders(dry_run=dry_run)
    return jsonify({"success": True, **result})


# ── Google Contacts status + sync (PWA API) ───────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/google-contacts/status")
def gc_status():
    """Return Google Contacts OAuth + sync status for the PWA settings sheet."""
    user = _require_auth()
    if isinstance(user, tuple):
        return user
    from services.google_contacts import get_token, token_needs_reconnect
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
    payload = request.get_json(silent=True) or {}
    dry_run = request.args.get("dry_run") in {"1", "true", "yes"} or bool(payload.get("dry_run"))
    result = sync_contacts(user.id, company.id, dry_run=dry_run)
    return jsonify(result)
