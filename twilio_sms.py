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

from flask import (
    Blueprint, abort, flash, jsonify, redirect,
    render_template, request, url_for
)
from flask_login import current_user, login_required

from extensions import db, csrf

logger = logging.getLogger(__name__)

twilio_bp = Blueprint("twilio", __name__, url_prefix="/twilio")

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
    """Find TwilioAccount by looking at from_phone — used in webhooks."""
    from models import TwilioAccount
    return TwilioAccount.query.filter(
        (TwilioAccount.from_phone == to_number) |
        (TwilioAccount.from_phone == None)
    ).first()


def _build_client(ta):
    """Create a Twilio REST client from a TwilioAccount record."""
    try:
        from twilio.rest import Client
        return Client(ta.get_account_sid(), ta.get_auth_token())
    except Exception as exc:
        logger.error("Twilio client error: %s", exc)
        return None


def _is_business_hours(company_id: int) -> bool:
    """Return True if current UTC time falls within business hours for the company."""
    from models import BusinessHours
    now_utc = datetime.now(timezone.utc)
    day = now_utc.weekday()   # 0=Mon … 6=Sun
    bh = BusinessHours.query.filter_by(company_id=company_id, day_of_week=day).first()
    if not bh or not bh.is_open:
        return False
    try:
        open_h,  open_m  = [int(x) for x in bh.open_time.split(":")]
        close_h, close_m = [int(x) for x in bh.close_time.split(":")]
        # Simple UTC comparison (full timezone support can be added with pytz later)
        current_minutes = now_utc.hour * 60 + now_utc.minute
        open_minutes    = open_h  * 60 + open_m
        close_minutes   = close_h * 60 + close_m
        return open_minutes <= current_minutes < close_minutes
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
        return {"success": True, "sid": msg.sid, "status": msg.status}
    except Exception as exc:
        logger.error("SMS send error: %s", exc)
        return {"success": False, "error": str(exc)}


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
    stop_words = {"stop", "unsubscribe", "cancel", "quit", "end", "stopall"}
    return body.lower().strip() in stop_words


def _apply_auto_reply_rules(conv, body: str, ta) -> bool:
    """
    Evaluate auto-reply rules in priority order.
    Returns True if any rule fired and a reply was sent.
    """
    from models import AutoReplyRule, Contact
    if not ta.automation_enabled:
        return False

    rules = (
        AutoReplyRule.query
        .filter_by(company_id=ta.company_id, is_active=True)
        .order_by(AutoReplyRule.priority.desc())
        .all()
    )

    now_utc = datetime.now(timezone.utc)

    for rule in rules:
        matched = False

        if rule.trigger_type == "always":
            matched = True
        elif rule.trigger_type == "stop_keyword":
            matched = _is_stop_message(body)
        elif rule.trigger_type == "first_contact":
            matched = conv.is_first_contact
        elif rule.trigger_type == "after_hours":
            matched = not _is_business_hours(ta.company_id)
        elif rule.trigger_type in ("keyword_contains", "keyword_exact", "regex"):
            matched = _match_keywords(body, rule.keywords or [], rule.trigger_type)

        if not matched:
            continue

        # Schedule filter
        if rule.active_days:
            if now_utc.weekday() not in rule.active_days:
                continue
        if rule.active_hours_start and rule.active_hours_end:
            try:
                sh, sm = [int(x) for x in rule.active_hours_start.split(":")]
                eh, em = [int(x) for x in rule.active_hours_end.split(":")]
                cur = now_utc.hour * 60 + now_utc.minute
                if not (sh * 60 + sm <= cur < eh * 60 + em):
                    continue
            except Exception:
                pass

        # Execute action
        if rule.action == "opt_out" or rule.trigger_type == "stop_keyword":
            conv.is_opted_out = True
            if rule.response:
                _send_sms(ta, conv.from_number, rule.response,
                          conversation_id=conv.id, is_auto_reply=True, rule_id=rule.id)
            rule.match_count = (rule.match_count or 0) + 1
            db.session.commit()
            return True

        elif rule.action == "tag" and rule.tag_value:
            tags = list(conv.tags or [])
            if rule.tag_value not in tags:
                tags.append(rule.tag_value)
                conv.tags = tags
            rule.match_count = (rule.match_count or 0) + 1
            db.session.commit()
            # Tag action may also reply
            if rule.response:
                _send_sms(ta, conv.from_number, rule.response,
                          conversation_id=conv.id, is_auto_reply=True, rule_id=rule.id)
            return True

        elif rule.action == "reply" and rule.response:
            result = _send_sms(ta, conv.from_number, rule.response,
                               conversation_id=conv.id, is_auto_reply=True, rule_id=rule.id)
            rule.match_count = (rule.match_count or 0) + 1
            db.session.commit()
            return result.get("success", False)

    return False


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
             response="Thanks for your message! Our team is currently away. We'll reply during business hours (Mon-Fri 9am-5pm).",
             priority=1, action="reply"),
    ]
    for d in defaults:
        rule = AutoReplyRule(company_id=company_id, **d)
        db.session.add(rule)
    db.session.commit()
    logger.info("Seeded %d default auto-reply rules for company %s", len(defaults), company_id)


def _seed_default_hours(company_id: int):
    """Seed Mon-Sun business hours for a new company."""
    from models import BusinessHours
    existing = BusinessHours.query.filter_by(company_id=company_id).count()
    if existing > 0:
        return
    for day in range(7):
        bh = BusinessHours(
            company_id=company_id,
            day_of_week=day,
            is_open=(day < 5),
            open_time="09:00",
            close_time="17:00",
        )
        db.session.add(bh)
    db.session.commit()


# ---------------------------------------------------------------------------
# Public webhook endpoints
# ---------------------------------------------------------------------------

@twilio_bp.route("/sms/inbound", methods=["POST"])
@csrf.exempt
def inbound_sms():
    """
    Twilio inbound SMS webhook.
    Set this URL in your Twilio Messaging Service or phone number config.
    Returns TwiML (200 OK with empty <Response> — replies are sent via API).
    """
    from models import TwilioAccount, TwilioConversation, TwilioMessage

    # Validate Twilio signature in production
    # Skipped here for initial testing; add RequestValidator when webhook URL is stable.

    data        = request.form
    from_number = data.get("From", "").strip()
    to_number   = data.get("To",   "").strip()
    body        = (data.get("Body") or "").strip()
    twilio_sid  = data.get("MessageSid", "")
    num_media   = int(data.get("NumMedia", 0))

    logger.info("Inbound SMS: from=%s to=%s sid=%s body=%.60r", from_number, to_number, twilio_sid, body)

    # Find which company owns this destination number / messaging service
    ta = TwilioAccount.query.filter(
        (TwilioAccount.from_phone == to_number) |
        (TwilioAccount.messaging_service_sid == data.get("MessagingServiceSid"))
    ).first()

    if not ta:
        # Fallback: use the first active account
        ta = TwilioAccount.query.filter_by(is_active=True).first()

    if not ta:
        logger.warning("Inbound SMS: no TwilioAccount found for to=%s", to_number)
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    try:
        # Get or create conversation thread
        conv = _get_or_create_conversation(ta.company_id, from_number, to_number)

        # Idempotency: skip if already processed
        if twilio_sid and TwilioMessage.query.filter_by(twilio_sid=twilio_sid).first():
            return '<Response></Response>', 200, {"Content-Type": "text/xml"}

        # Collect media URLs
        media_urls = []
        for i in range(num_media):
            url = data.get(f"MediaUrl{i}")
            if url:
                media_urls.append(url)

        # Save the inbound message
        msg = TwilioMessage(
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
        db.session.add(msg)

        # Update conversation
        conv.is_read = False
        conv.last_message_at      = datetime.utcnow()
        conv.last_message_preview = body[:200] if body else "(media)"
        conv.message_count        = (conv.message_count or 0) + 1
        db.session.commit()

        # Auto-capture lead on first contact
        if conv.is_first_contact:
            _capture_lead(conv, body, ta.company_id)

        # Run auto-reply rules if not opted out
        if not conv.is_opted_out:
            _apply_auto_reply_rules(conv, body, ta)

        # SMS forwarding — send a copy to the forwarding number
        if ta.sms_forward_to:
            try:
                fwd_body = f"FWD from {from_number}: {body}" if body else f"FWD from {from_number}: (media)"
                _send_sms(ta, ta.sms_forward_to, fwd_body)
                logger.info("SMS forwarded from %s to %s", from_number, ta.sms_forward_to)
            except Exception as fwd_exc:
                logger.warning("SMS forward failed: %s", fwd_exc)

        # Mark that we've now received at least one message
        if conv.is_first_contact:
            conv.is_first_contact = False
            db.session.commit()

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

    return "", 204


@twilio_bp.route("/voice/inbound", methods=["POST"])
@csrf.exempt
def inbound_call():
    """Twilio voice webhook — logs the call and sends a missed-call text if unanswered."""
    from models import TwilioAccount, TwilioCallLog

    data        = request.form
    from_number = data.get("From", "")
    to_number   = data.get("To",   "")
    call_sid    = data.get("CallSid", "")
    call_status = data.get("CallStatus", "")
    duration    = int(data.get("CallDuration") or 0)
    caller_name = data.get("CallerName", "")

    ta = TwilioAccount.query.filter(
        TwilioAccount.from_phone == to_number
    ).first() or TwilioAccount.query.filter_by(is_active=True).first()

    if ta:
        # Log the call
        existing = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if not existing:
            log = TwilioCallLog(
                company_id=ta.company_id,
                twilio_sid=call_sid,
                direction="inbound",
                from_number=from_number,
                to_number=to_number,
                status=call_status,
                duration=duration,
                caller_name=caller_name,
                raw_payload=dict(data),
            )
            db.session.add(log)
            db.session.commit()

            # Missed call → send auto-text
            if call_status in ("no-answer", "busy") and ta.missed_call_text and not log.missed_text_sent:
                result = _send_sms(ta, from_number, ta.missed_call_text)
                if result.get("success"):
                    log.missed_text_sent = True
                    db.session.commit()

    # Build TwiML: forward the call if a forward number is set, otherwise voicemail
    if ta and ta.call_forward_to:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial timeout="30" action="/twilio/voice/inbound">{ta.call_forward_to}</Dial>
  <Say>Sorry, we could not reach anyone. Please try again later.</Say>
</Response>"""
    else:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Thank you for calling. Please leave a message after the tone.</Say>
  <Record maxLength="120" />
</Response>"""
    return twiml, 200, {"Content-Type": "text/xml"}


# ---------------------------------------------------------------------------
# Protected routes
# ---------------------------------------------------------------------------

@twilio_bp.route("/inbox")
@login_required
def inbox():
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

    return render_template(
        "twilio/inbox.html",
        conversations=conversations,
        unread_count=unread_count,
        ta=ta,
        status_filter=status_filter,
        search=search,
    )


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
    from models import TwilioAccount
    company = _get_company()
    ta = _get_twilio_account(company.id)

    if request.method == "POST":
        account_sid          = request.form.get("account_sid", "").strip()
        auth_token           = request.form.get("auth_token", "").strip()
        messaging_service_sid = request.form.get("messaging_service_sid", "").strip()
        from_phone           = request.form.get("from_phone", "").strip()
        webhook_base_url     = request.form.get("webhook_base_url", "").strip()
        automation_enabled   = request.form.get("automation_enabled") == "on"
        ai_mode              = request.form.get("ai_mode", "off")
        ai_system_prompt     = request.form.get("ai_system_prompt", "").strip()
        missed_call_text     = request.form.get("missed_call_text", "").strip()
        after_hours_text     = request.form.get("after_hours_text", "").strip()
        sms_forward_to       = request.form.get("sms_forward_to", "").strip()
        call_forward_to      = request.form.get("call_forward_to", "").strip()

        if not ta:
            ta = TwilioAccount(company_id=company.id)
            db.session.add(ta)

        if account_sid:
            ta.set_account_sid(account_sid)
        if auth_token:
            ta.set_auth_token(auth_token)
        ta.messaging_service_sid = messaging_service_sid or ta.messaging_service_sid
        ta.from_phone            = from_phone or ta.from_phone
        ta.webhook_base_url      = webhook_base_url
        ta.automation_enabled    = automation_enabled
        ta.ai_mode               = ai_mode
        ta.ai_system_prompt      = ai_system_prompt
        ta.missed_call_text      = missed_call_text
        ta.after_hours_text      = after_hours_text
        ta.sms_forward_to        = sms_forward_to or None
        ta.call_forward_to       = call_forward_to or None
        ta.is_active             = True
        db.session.commit()

        # Seed default rules and business hours on first save
        _seed_default_rules(company.id)
        _seed_default_hours(company.id)

        flash("Twilio settings saved successfully!", "success")
        return redirect(url_for("twilio.settings"))

    return render_template("twilio/settings.html", ta=ta, company=company)


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
