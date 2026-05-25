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


@inbox_pwa_bp.before_request
def _guard_sms_feature():
    """Block PWA inbox unless SMS-features flag is on."""
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return None   # let login redirect handle it
        from services.feature_flags import sms_blueprint_guard
        result = sms_blueprint_guard()
        if result is not None:
            return result
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("SMS feature flag guard error: %s", exc)


# ── SSE Event Bus ──────────────────────────────────────────────────────────────
# Keyed by company_id → list of Queue objects (one per connected SSE client).
# Works with gunicorn gthread workers (--worker-class gthread --threads N).
_sse_lock:      threading.Lock              = threading.Lock()
_sse_listeners: dict[int, list]             = {}

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _current_user():
    from models import User
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def _require_auth():
    user = _current_user()
    if not user:
        abort(401, "Authentication required")
    return user


def _get_company(user):
    from models import Company, UserCompanyAccess
    acc = UserCompanyAccess.query.filter_by(user_id=user.id).first()
    if acc:
        return Company.query.get(acc.company_id)
    if user.default_company_id:
        return Company.query.get(user.default_company_id)
    return Company.query.first()


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

def _sanitize_body(text: str) -> str:
    """Replace common non-latin-1 Unicode chars before sending via Twilio."""
    if not text:
        return text
    text = text.translate(_UNICODE_REPLACEMENTS)
    return text.encode("latin-1", errors="replace").decode("latin-1")


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
        return None, str(exc)


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
    from flask import redirect, url_for as _url_for
    user = _current_user()
    if not user:
        return redirect("/auth/login?next=/app/inbox")
    company = _get_company(user)
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
    company = _get_company(user)
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
    company = _get_company(user)
    from models import TwilioConversation
    conv = TwilioConversation.query.filter_by(id=conv_id, company_id=company.id).first_or_404()

    payload = request.get_json() or {}
    body    = (payload.get("body") or "").strip()
    if not body:
        return jsonify({"success": False, "error": "Message body is required."}), 400

    ta = _get_twilio_account(company.id)
    if not ta or not ta.is_configured:
        return jsonify({"success": False, "error": "Twilio not configured for this company."}), 400

    record, err = _send_sms_internal(ta, conv.from_number, body, conversation_id=conv.id)
    if err:
        logger.error("PWA inbox send failed conv=%d: %s", conv_id, err)
        return jsonify({"success": False, "error": err}), 500

    # Update conversation preview
    conv.last_message_at      = datetime.utcnow()
    conv.last_message_preview = f"You: {body[:150]}"
    conv.message_count        = (conv.message_count or 0) + 1
    conv.is_read              = True
    db.session.commit()

    return jsonify({"success": True, "message": _msg_to_dict(record)})


# ── API: start new conversation ───────────────────────────────────────────────

@inbox_pwa_bp.route("/api/inbox/conversations", methods=["POST"])
def new_conversation():
    user    = _require_auth()
    company = _get_company(user)
    payload = request.get_json() or {}
    to_num  = (payload.get("to") or "").strip()
    body    = (payload.get("body") or "").strip()
    if not to_num or not body:
        return jsonify({"success": False, "error": "to and body are required."}), 400

    ta = _get_twilio_account(company.id)
    if not ta or not ta.is_configured:
        return jsonify({"success": False, "error": "Twilio not configured."}), 400

    from models import TwilioConversation
    conv = TwilioConversation.query.filter_by(
        company_id=company.id, from_number=to_num
    ).first()
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
        return jsonify({"success": False, "error": err}), 500

    conv.last_message_at      = datetime.utcnow()
    conv.last_message_preview = f"You: {body[:150]}"
    conv.message_count        = (conv.message_count or 0) + 1
    db.session.commit()

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
    user, err = _require_auth()
    if err:
        return jsonify({"missed_calls": 0, "voicemails": 0})
    company = _get_company(user)
    if not company:
        return jsonify({"missed_calls": 0, "voicemails": 0})

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
    user, err = _require_auth()
    if err:
        return jsonify({"contacts": []})
    company = _get_company(user)
    if not company:
        return jsonify({"contacts": []})

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
