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
"""

import logging
import os
import queue as _queue_module
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
    return User.query.get(uid)


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
        c = Company.query.get(acc.company_id)
        if c:
            return c
    # 2. Any access row (first one found)
    acc = UserCompanyAccess.query.filter_by(user_id=user.id).first()
    if acc:
        c = Company.query.get(acc.company_id)
        if c:
            return c
    # 3. Explicit default_company_id pointer
    if user.default_company_id:
        c = Company.query.get(user.default_company_id)
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
    """Return a PWA-safe, action-oriented explanation for Twilio send failures."""
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
    """Send SMS via Twilio — mirrors twilio_sms._send_sms."""
    from models import TwilioMessage
    body = _sanitize_body(body)
    try:
        from twilio.rest import Client
        sid = ta.get_account_sid() if hasattr(ta, 'get_account_sid') else ta._account_sid
        tok = ta.get_auth_token()  if hasattr(ta, 'get_auth_token')  else ta._auth_token
        client = Client(sid, tok)
        kwargs = {"body": body, "to": to_number}
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
            body=body,
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
    tags = conv.tags or []
    d = {
        "id":                  conv.id,
        "from_number":         conv.from_number,
        "contact_name":        conv.contact_name or conv.from_number,
        "display_name":        conv.contact_name or conv.from_number,
        "contact_id":          conv.contact_id,
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
            u = User.query.get(conv.assigned_user_id)
            d["assigned_user_name"] = u.username if u else None
        else:
            d["assigned_user_name"] = None
    return d


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


# ── API: conversation list ────────────────────────────────────────────────────

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

    q = TwilioConversation.query.filter_by(company_id=company.id)

    if filter_by == "unread":
        q = q.filter_by(is_read=False)
    elif filter_by == "mine":
        q = q.filter_by(assigned_user_id=user.id)
    elif filter_by == "archived":
        q = q.filter(TwilioConversation.tags.contains(["archived"]))
    else:
        # Default "all" excludes archived
        q = q.filter(~TwilioConversation.tags.contains(["archived"]))

    if filter_by == "opted_out":
        q = q.filter_by(is_opted_out=True)

    if search:
        q = q.filter(db.or_(
            TwilioConversation.from_number.ilike(f"%{search}%"),
            TwilioConversation.contact_name.ilike(f"%{search}%"),
            TwilioConversation.last_message_preview.ilike(f"%{search}%"),
        ))

    total        = q.count()
    unread_count = TwilioConversation.query.filter_by(
        company_id=company.id, is_read=False
    ).count()
    convs = q.order_by(TwilioConversation.last_message_at.desc()).offset((page-1)*50).limit(50).all()

    return jsonify({
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
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()

    # Mark as read when opened
    if not conv.is_read:
        conv.is_read = True
        db.session.commit()

    msgs = conv.messages.order_by(db.text("twilio_message.created_at")).all()

    # Contact info enrichment
    contact_data = None
    if conv.contact_id:
        from models import Contact
        c = Contact.query.get(conv.contact_id)
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
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()

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
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
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
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
    payload  = request.get_json() or {}
    archive  = payload.get("archived", True)
    tags     = list(conv.tags or [])
    if archive and "archived" not in tags:
        tags.append("archived")
    elif not archive and "archived" in tags:
        tags.remove("archived")
    conv.tags = tags
    db.session.commit()
    return jsonify({"success": True, "is_archived": archive})


# ── API: assign ───────────────────────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations/<int:conv_id>/assign", methods=["PATCH"])
def assign_conversation(conv_id):
    user    = _require_auth()
    company = _get_company(user)
    from models import TwilioConversation
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
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
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
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
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()
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
    count = TwilioConversation.query.filter_by(
        company_id=company.id, is_read=False
    ).count()
    return jsonify({"count": count})


# ── API: Push notification subscribe ─────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/push/subscribe", methods=["POST"])
def push_subscribe():
    user    = _require_auth()
    company = _get_company(user)
    if not company:
        return jsonify({"success": False, "error": "No company"}), 400

    payload  = request.get_json() or {}
    endpoint = payload.get("endpoint", "")
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
            endpoint=endpoint,
            p256dh=p256dh,
            auth_key=auth_key,
        )
        db.session.add(sub)
    else:
        sub.p256dh   = p256dh
        sub.auth_key = auth_key
    db.session.commit()
    logger.info("Push subscription saved for user %d", user.id)
    return jsonify({"success": True})


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
        like = f"%{q}%"
        contacts = (
            Contact.query
            .filter(
                Contact.company_id == company.id,
                db.or_(
                    Contact.name.ilike(like),
                    Contact.phone.ilike(like),
                ),
            )
            .limit(8)
            .all()
        )
        for c in contacts:
            if c.phone:
                results.append({"name": c.name or c.phone, "phone": c.phone})
    except Exception:
        pass

    if not results:
        try:
            from models import TwilioConversation
            like = f"%{q}%"
            convs = (
                TwilioConversation.query
                .filter(
                    TwilioConversation.company_id == company.id,
                    db.or_(
                        TwilioConversation.contact_name.ilike(like),
                        TwilioConversation.from_number.ilike(like),
                    ),
                )
                .limit(8)
                .all()
            )
            seen = set()
            for cv in convs:
                phone = cv.from_number
                if phone and phone not in seen:
                    seen.add(phone)
                    results.append({"name": cv.contact_name or phone, "phone": phone})
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
    conv = TwilioConversation.query.filter_by(
        id=conv_id, company_id=company.id
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
        forward_to  = payload.get("forward_to") or ta.call_forward_to
        customer_no = conv.from_number

        # TwiML URL that bridges the call to the customer once the agent picks up
        twiml_url = url_for(
            "twilio.outbound_call_twiml",
            to=customer_no,
            caller=ta.from_phone,
            _external=True,
        )

        if forward_to:
            # Call the agent first; TwiML dials the customer when agent answers
            call = client.calls.create(
                to=forward_to,
                from_=ta.from_phone,
                url=twiml_url,
            )
            msg = f"Calling your phone ({forward_to}). Answer to be connected to {customer_no}."
        else:
            # Call the customer directly (e.g. to leave a voicemail / test)
            call = client.calls.create(
                to=customer_no,
                from_=ta.from_phone,
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

        forward_to = payload.get("forward_to") or ta.call_forward_to

        twiml_url = _url_for(
            "twilio.outbound_call_twiml",
            to=to_number,
            caller=ta.from_phone,
            _external=True,
        )

        if forward_to:
            call = client.calls.create(
                to=forward_to,
                from_=ta.from_phone,
                url=twiml_url,
            )
            msg = f"Calling your phone ({forward_to}). Answer to be connected to {to_number}."
        else:
            call = client.calls.create(
                to=to_number,
                from_=ta.from_phone,
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
    """Called from the inbound SMS webhook — fires push to all subscribed users."""
    vapid_private = os.environ.get("VAPID_PRIVATE_KEY", "")
    vapid_public  = os.environ.get("VAPID_PUBLIC_KEY", "")
    if not vapid_private or not vapid_public:
        return

    from models import PushSubscription
    subs = PushSubscription.query.filter_by(company_id=company_id).all()
    if not subs:
        return

    import json
    sender = conv.contact_name or conv.from_number
    payload = json.dumps({
        "title": f"New message from {sender}",
        "body":  message_body[:100],
        "url":   f"/app/inbox?conv={conv.id}",
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
