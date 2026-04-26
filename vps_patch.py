#!/usr/bin/env python3
"""
LUXit VPS patch script — applies all Twilio routing changes.
Run from inside the app directory:
    cd /var/www/luxit-marketing
    python3 vps_patch.py
"""
import shutil, sys
from pathlib import Path

APP = Path("/var/www/luxit-marketing")
ok = []
skip = []
fail = []


def patch(rel, old, new, label):
    p = APP / rel
    if not p.exists():
        fail.append(f"FILE NOT FOUND — {rel}")
        return
    src = p.read_text()
    if new.strip() in src:
        skip.append(f"already applied — {label}")
        return
    if old not in src:
        fail.append(f"anchor not found — {label} ({rel})")
        return
    shutil.copy(p, str(p) + ".bak")
    p.write_text(src.replace(old, new, 1))
    ok.append(f"patched — {label}")


# ── 1. Fix sidebar blank on /twilio/* pages ────────────────────────────────
patch(
    "templates/base.html",
    "or _p.startswith('/sms') or _p.startswith('/social')",
    "or _p.startswith('/sms') or _p.startswith('/twilio') or _p.startswith('/social')",
    "sidebar visible on /twilio/* pages",
)

# ── 2. Sidebar links ────────────────────────────────────────────────────────
patch(
    "templates/base.html",
    """                    <a href="{{ url_for('twilio.inbox') }}" class="sidebar-nav-item{% if _p.startswith('/twilio') %} active{% endif %}">
                        <i data-feather="message-square"></i> SMS Inbox
                    </a>""",
    """                    <a href="{{ url_for('twilio.inbox') }}" class="sidebar-nav-item{% if _p.startswith('/twilio/inbox') or _p.startswith('/twilio/conversation') %} active{% endif %}">
                        <i data-feather="message-square"></i> SMS Inbox
                    </a>
                    <a href="{{ url_for('twilio.rules') }}" class="sidebar-nav-item{% if _p.startswith('/twilio/rules') %} active{% endif %}">
                        <i data-feather="zap"></i> Auto-Reply Rules
                    </a>
                    <a href="{{ url_for('twilio.settings') }}" class="sidebar-nav-item{% if _p.startswith('/twilio/settings') or _p.startswith('/twilio/hours') or _p.startswith('/twilio/analytics') or _p.startswith('/twilio/calls') %} active{% endif %}">
                        <i data-feather="settings"></i> SMS Settings
                    </a>""",
    "sidebar Auto-Reply Rules + SMS Settings links",
)

# ── 3. Fix set_secret in ai_action_executor.py ──────────────────────────────
patch(
    "ai_action_executor.py",
    """            for secret_key in secrets_list:
                value = os.getenv(secret_key)
                if value:
                    company.set_secret(secret_key, value)
                    added.append(secret_key)
                else:
                    skipped.append(secret_key)""",
    """            for secret_key in secrets_list:
                value = os.getenv(secret_key)
                if value:
                    secret = CompanySecret.query.filter_by(
                        company_id=company.id, key=secret_key
                    ).first()
                    if secret:
                        secret.value = value
                    else:
                        secret = CompanySecret(
                            company_id=company.id,
                            key=secret_key,
                            value=value,
                        )
                        db.session.add(secret)
                    added.append(secret_key)
                else:
                    skipped.append(secret_key)
            db.session.commit()""",
    "ai_action_executor set_secret → CompanySecret ORM",
)

# ── 4. Fix set_secret in populate_secrets.py ────────────────────────────────
patch(
    "populate_secrets.py",
    """        for key, value in SECRETS.items():
            if value:  # Only add if value exists
                company.set_secret(key, value)
                print(f"✓ Added {key}")
                count += 1
            else:
                print(f"⊘ Skipped {key} (not in environment)")""",
    """        for key, value in SECRETS.items():
            if value:  # Only add if value exists
                secret = CompanySecret.query.filter_by(
                    company_id=company.id, key=key
                ).first()
                if secret:
                    secret.value = value
                else:
                    secret = CompanySecret(
                        company_id=company.id,
                        key=key,
                        value=value,
                    )
                    db.session.add(secret)
                print(f"✓ Added {key}")
                count += 1
            else:
                print(f"⊘ Skipped {key} (not in environment)")
        db.session.commit()""",
    "populate_secrets set_secret → CompanySecret ORM",
)

# ── 5. models.py — add routing toggles + voicemail columns ──────────────────
patch(
    "models.py",
    "    sms_forward_to       = db.Column(db.String(20))   # Forward all inbound SMS to this number\n    call_forward_to      = db.Column(db.String(20))   # Forward all inbound calls to this number\n\n    created_at",
    """    sms_forward_to       = db.Column(db.String(20))   # Forward all inbound SMS to this number
    call_forward_to      = db.Column(db.String(20))   # Forward all inbound calls to this number

    # Routing feature toggles
    sms_forwarding_enabled       = db.Column(db.Boolean, default=True,  server_default="true")
    voice_forwarding_enabled     = db.Column(db.Boolean, default=True,  server_default="true")
    after_hours_sms_enabled      = db.Column(db.Boolean, default=True,  server_default="true")
    after_hours_voicemail_enabled = db.Column(db.Boolean, default=True, server_default="true")

    # Voicemail
    voicemail_greeting_text      = db.Column(db.Text)
    voicemail_greeting_audio_url = db.Column(db.String(500), nullable=True)

    created_at""",
    "models.py routing toggles + voicemail columns",
)

# ── 6. twilio_sms.py — add zoneinfo import ──────────────────────────────────
patch(
    "twilio_sms.py",
    "from datetime import datetime, timezone, timedelta",
    "from datetime import datetime, timezone, timedelta\nfrom zoneinfo import ZoneInfo",
    "twilio_sms zoneinfo import",
)

# ── 7. twilio_sms.py — add _LA constant + helpers after _build_client ────────
NEW_HELPERS = '''

_LA = ZoneInfo("America/Los_Angeles")

_STOP_REPLY = "You have been unsubscribed. Reply START to opt back in."
_START_REPLY = "You have been subscribed. Thanks for joining LUXit SMS updates."
_HELP_REPLY  = "LUXit SMS Support: Reply STOP to opt out. For help, contact support@luxit.app."

TWILIO_WEBHOOK_PUBLIC_URL = os.environ.get(
    "TWILIO_WEBHOOK_PUBLIC_URL", "https://luxit.app/twilio/sms/inbound"
)


def _twiml_message(text: str):
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f\'\'\'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>\'\'\'
    return xml, 200, {"Content-Type": "text/xml"}


def _validate_twilio_signature(ta, endpoint_path: str = "/twilio/sms/inbound") -> bool:
    is_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"))
    if is_replit:
        return True
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        return True
    token = (ta.get_auth_token() if ta else None) or os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False
    base = (ta.webhook_base_url.rstrip("/") if ta and ta.webhook_base_url
            else TWILIO_WEBHOOK_PUBLIC_URL.rsplit("/twilio/", 1)[0])
    url = base + endpoint_path
    validator = RequestValidator(token)
    valid = validator.validate(url, request.form, signature)
    if not valid:
        logger.warning("Request rejected — invalid signature (url=%s)", url)
    return valid

'''

patch(
    "twilio_sms.py",
    "\ndef _is_business_hours",
    NEW_HELPERS + "\ndef _is_business_hours",
    "twilio_sms constants + helpers",
)

# ── 8. twilio_sms.py — replace _is_business_hours with LA timezone version ───
OLD_BIZ = '''def _is_business_hours(company_id: int) -> bool:
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
        return True'''

NEW_BIZ = '''def _is_business_hours(company_id: int) -> bool:
    """Return True if current America/Los_Angeles time is within business hours.
    Handles midnight-crossing (e.g. open=11:00, close=01:00)."""
    from models import BusinessHours
    now_la = datetime.now(timezone.utc).astimezone(_LA)
    day = now_la.weekday()
    bh = BusinessHours.query.filter_by(company_id=company_id, day_of_week=day).first()
    if not bh or not bh.is_open:
        return False
    try:
        open_h,  open_m  = [int(x) for x in bh.open_time.split(":")]
        close_h, close_m = [int(x) for x in bh.close_time.split(":")]
        current = now_la.hour * 60 + now_la.minute
        opens   = open_h  * 60 + open_m
        closes  = close_h * 60 + close_m
        if closes <= opens:
            return current >= opens or current < closes
        return opens <= current < closes
    except Exception:
        return True'''

patch("twilio_sms.py", OLD_BIZ, NEW_BIZ, "twilio_sms _is_business_hours LA timezone")

# ── 9. twilio_sms.py — seed hours to 11AM-1AM ───────────────────────────────
patch(
    "twilio_sms.py",
    """        bh = BusinessHours(
            company_id=company_id,
            day_of_week=day,
            is_open=True,
            open_time="09:00",
            close_time="17:00",
        )""",
    """        bh = BusinessHours(
            company_id=company_id,
            day_of_week=day,
            is_open=True,
            open_time="11:00",
            close_time="01:00",
        )""",
    "twilio_sms seed hours 11AM-1AM",
)

# ── 10. twilio_sms.py — add STOP/START/HELP + signature validation ───────────
patch(
    "twilio_sms.py",
    """    if not ta:
        logger.warning("Inbound SMS: no TwilioAccount found for to=%s", to_number)
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    try:""",
    """    if not ta:
        logger.warning("Inbound SMS: no TwilioAccount found for to=%s", to_number)
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    if not _validate_twilio_signature(ta, "/twilio/sms/inbound"):
        abort(403)

    # Owner reply relay: if FROM is the forwarding number and body is "reply +1XXX message"
    if ta.sms_forward_to and from_number == ta.sms_forward_to:
        relay_match = re.match(
            r\'^reply\\s+(\\+?1?\\d{10,15})\\s+(.+)$\', body, re.IGNORECASE | re.DOTALL
        )
        if relay_match:
            target_number = relay_match.group(1).strip()
            if not target_number.startswith("+"):
                target_number = "+" + target_number
            relay_body = relay_match.group(2).strip()
            target_conv = _get_or_create_conversation(ta.company_id, target_number, to_number)
            result = _send_sms(ta, target_number, relay_body, conversation_id=target_conv.id)
            logger.info("Owner relay: %s → %s success=%s", from_number, target_number, result.get("success"))
            return \'<Response></Response>\', 200, {"Content-Type": "text/xml"}

    try:""",
    "twilio_sms inbound_sms signature + relay",
)

# ── 11. twilio_sms.py — STOP/START/HELP inside try block ─────────────────────
patch(
    "twilio_sms.py",
    """        # Auto-capture lead on first contact
        if conv.is_first_contact:
            _capture_lead(conv, body, ta.company_id)

        # Run auto-reply rules if not opted out
        if not conv.is_opted_out:
            _apply_auto_reply_rules(conv, body, ta)""",
    """        kw = body.upper().strip()
        if kw == "STOP":
            conv.is_opted_out = True
            db.session.commit()
            return _twiml_message(_STOP_REPLY)
        if kw == "START":
            conv.is_opted_out = False
            db.session.commit()
            return _twiml_message(_START_REPLY)
        if kw == "HELP":
            return _twiml_message(_HELP_REPLY)

        if conv.is_first_contact:
            _capture_lead(conv, body, ta.company_id)

        if not conv.is_opted_out:
            _apply_auto_reply_rules(conv, body, ta)""",
    "twilio_sms inbound_sms STOP/START/HELP",
)

# ── 12. twilio_sms.py — improved SMS forwarding format ───────────────────────
patch(
    "twilio_sms.py",
    """        if ta.sms_forward_to:
            try:
                fwd_body = (f"FWD from {from_number}: {body}"
                            if body else f"FWD from {from_number}: (media)")
                _send_sms(ta, ta.sms_forward_to, fwd_body)
                logger.info("SMS forwarded from %s to %s", from_number, ta.sms_forward_to)
            except Exception as fwd_exc:
                logger.warning("SMS forward failed: %s", fwd_exc)""",
    """        sms_fwd_enabled = getattr(ta, 'sms_forwarding_enabled', True)
        if ta.sms_forward_to and sms_fwd_enabled:
            try:
                company_name = (ta.company.name if ta.company else "LUXit")
                fwd_body = (
                    f"New {company_name} SMS from {from_number}: {body}\\n\\n"
                    f"Reply format: reply {from_number} your message"
                    if body else
                    f"New {company_name} SMS from {from_number}: (media)\\n\\n"
                    f"Reply format: reply {from_number} your message"
                )
                _send_sms(ta, ta.sms_forward_to, fwd_body)
                logger.info("SMS forwarded from %s to %s", from_number, ta.sms_forward_to)
            except Exception as fwd_exc:
                logger.warning("SMS forward failed: %s", fwd_exc)""",
    "twilio_sms improved SMS forwarding format",
)

# ── 13. twilio_sms.py — rewrite inbound_call for biz-hours routing ───────────
OLD_CALL = '''@twilio_bp.route("/voice/inbound", methods=["POST"])
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
    return twiml, 200, {"Content-Type": "text/xml"}'''

NEW_CALL = '''@twilio_bp.route("/voice/inbound", methods=["POST"])
@csrf.exempt
def inbound_call():
    """Business-hours-aware voice routing: forward during hours, voicemail after."""
    from models import TwilioAccount, TwilioCallLog

    data        = request.form
    from_number = data.get("From", "")
    to_number   = data.get("To",   "")
    call_sid    = data.get("CallSid", "")
    call_status = data.get("CallStatus", "")
    duration    = int(data.get("CallDuration") or 0)
    caller_name = data.get("CallerName", "")

    ta = (
        TwilioAccount.query.filter(TwilioAccount.from_phone == to_number).first()
        or TwilioAccount.query.filter_by(is_active=True).first()
    )

    if not ta:
        return (\'<?xml version="1.0" encoding="UTF-8"?><Response><Say>Thank you for calling. Goodbye.</Say></Response>\',
                200, {"Content-Type": "text/xml"})

    if not _validate_twilio_signature(ta, "/twilio/voice/inbound"):
        abort(403)

    existing = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
    if not existing and call_sid:
        log = TwilioCallLog(
            company_id=ta.company_id, twilio_sid=call_sid, direction="inbound",
            from_number=from_number, to_number=to_number,
            status=call_status or "ringing", duration=duration,
            caller_name=caller_name, raw_payload=dict(data),
        )
        db.session.add(log)
        db.session.commit()

    in_hours = _is_business_hours(ta.company_id)
    voice_fwd = getattr(ta, \'voice_forwarding_enabled\', True)
    ah_vm     = getattr(ta, \'after_hours_voicemail_enabled\', True)
    greeting  = (getattr(ta, \'voicemail_greeting_text\', None) or
                 "Thank you for calling. Please leave your name and message after the tone.")
    vm_audio  = getattr(ta, \'voicemail_greeting_audio_url\', None)

    def _safe(s):
        return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    def _voicemail_twiml():
        g_xml = (f"<Play>{vm_audio}</Play>" if vm_audio
                 else f"<Say>{_safe(greeting)}</Say>")
        return (
            \'<?xml version="1.0" encoding="UTF-8"?>\\n\'
            f"<Response>\\n  {g_xml}\\n"
            \'  <Record maxLength="180" playBeep="true"\\n\'
            \'          recordingStatusCallback="/twilio/voice/recording"\\n\'
            \'          recordingStatusCallbackMethod="POST" />\\n\'
            "  <Say>We did not receive a recording. Goodbye.</Say>\\n"
            "</Response>"
        )

    if in_hours and voice_fwd and ta.call_forward_to:
        caller_id = ta.from_phone or to_number
        twiml = (
            \'<?xml version="1.0" encoding="UTF-8"?>\\n\'
            f\'<Response>\\n  <Dial callerId="{caller_id}" timeout="25"\\n\'
            \'        action="/twilio/voice/no-answer" method="POST">\\n\'
            f\'    <Number>{ta.call_forward_to}</Number>\\n  </Dial>\\n</Response>\'
        )
    elif not in_hours and ah_vm:
        twiml = _voicemail_twiml()
    elif ta.call_forward_to and voice_fwd:
        caller_id = ta.from_phone or to_number
        twiml = (
            \'<?xml version="1.0" encoding="UTF-8"?>\\n\'
            f\'<Response>\\n  <Dial callerId="{caller_id}" timeout="25"\\n\'
            \'        action="/twilio/voice/no-answer" method="POST">\\n\'
            f\'    <Number>{ta.call_forward_to}</Number>\\n  </Dial>\\n</Response>\'
        )
    else:
        twiml = _voicemail_twiml()

    logger.info("Voice inbound: from=%s in_hours=%s fwd=%s", from_number, in_hours, ta.call_forward_to)
    return twiml, 200, {"Content-Type": "text/xml"}


@twilio_bp.route("/voice/no-answer", methods=["POST"])
@csrf.exempt
def voice_no_answer():
    """Fired when forwarded call not answered — route caller to voicemail."""
    from models import TwilioAccount, TwilioCallLog
    data      = request.form
    call_sid  = data.get("CallSid", "")
    to_number = data.get("To", "")
    from_number = data.get("From", "")
    ta = (TwilioAccount.query.filter(TwilioAccount.from_phone == to_number).first()
          or TwilioAccount.query.filter_by(is_active=True).first())
    logger.info("Voice no-answer: sid=%s", call_sid)
    if call_sid:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            log.status = "no-answer"
            db.session.commit()
    if ta and ta.missed_call_text and from_number:
        result = _send_sms(ta, from_number, ta.missed_call_text)
        if result.get("success"):
            log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
            if log and not log.missed_text_sent:
                log.missed_text_sent = True
                db.session.commit()
    greeting = (getattr(ta, \'voicemail_greeting_text\', None) if ta else None
                ) or "Please leave a message after the tone."
    vm_audio = getattr(ta, \'voicemail_greeting_audio_url\', None) if ta else None
    def _safe(s):
        return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    g_xml = f"<Play>{vm_audio}</Play>" if vm_audio else f"<Say>{_safe(greeting)}</Say>"
    twiml = (
        \'<?xml version="1.0" encoding="UTF-8"?>\\n\'
        f"<Response>\\n  {g_xml}\\n"
        \'  <Record maxLength="180" playBeep="true"\\n\'
        \'          recordingStatusCallback="/twilio/voice/recording"\\n\'
        \'          recordingStatusCallbackMethod="POST" />\\n\'
        "  <Say>We did not receive a recording. Goodbye.</Say>\\n"
        "</Response>"
    )
    return twiml, 200, {"Content-Type": "text/xml"}


@twilio_bp.route("/voice/recording", methods=["POST"])
@csrf.exempt
def voice_recording():
    """Twilio recording status callback — saves voicemail URL to call log."""
    from models import TwilioCallLog
    data          = request.form
    call_sid      = data.get("CallSid", "")
    recording_url = data.get("RecordingUrl", "")
    recording_dur = data.get("RecordingDuration", "0")
    logger.info("Voicemail recording: sid=%s dur=%ss url=%s", call_sid, recording_dur, recording_url)
    if call_sid and recording_url:
        log = TwilioCallLog.query.filter_by(twilio_sid=call_sid).first()
        if log:
            log.notes  = f"Voicemail: {recording_url} ({recording_dur}s)"
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
    return "", 204'''

patch("twilio_sms.py", OLD_CALL, NEW_CALL, "twilio_sms voice routing rewrite + no-answer + recording + status routes")

# ── 14. twilio_sms.py — update settings POST to save new fields ──────────────
patch(
    "twilio_sms.py",
    """        account_sid          = request.form.get("account_sid", "").strip()
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
        db.session.commit()""",
    """        f = request.form
        account_sid           = f.get("account_sid", "").strip()
        auth_token            = f.get("auth_token", "").strip()
        messaging_service_sid = f.get("messaging_service_sid", "").strip()
        from_phone            = f.get("from_phone", "").strip()
        webhook_base_url      = f.get("webhook_base_url", "").strip()
        automation_enabled    = f.get("automation_enabled") == "on"
        ai_mode               = f.get("ai_mode", "off")
        ai_system_prompt      = f.get("ai_system_prompt", "").strip()
        missed_call_text      = f.get("missed_call_text", "").strip()
        after_hours_text      = f.get("after_hours_text", "").strip()
        sms_forward_to        = f.get("sms_forward_to", "").strip()
        call_forward_to       = f.get("call_forward_to", "").strip()
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
        ta.messaging_service_sid         = messaging_service_sid or ta.messaging_service_sid
        ta.from_phone                    = from_phone or ta.from_phone
        ta.webhook_base_url              = webhook_base_url
        ta.automation_enabled            = automation_enabled
        ta.ai_mode                       = ai_mode
        ta.ai_system_prompt              = ai_system_prompt
        ta.missed_call_text              = missed_call_text
        ta.after_hours_text              = after_hours_text
        ta.sms_forward_to                = sms_forward_to or None
        ta.call_forward_to               = call_forward_to or None
        ta.sms_forwarding_enabled        = sms_forwarding_enabled
        ta.voice_forwarding_enabled      = voice_forwarding_enabled
        ta.after_hours_sms_enabled       = after_hours_sms_enabled
        ta.after_hours_voicemail_enabled = after_hours_voicemail_enabled
        ta.voicemail_greeting_text       = voicemail_greeting_text or None
        ta.voicemail_greeting_audio_url  = voicemail_greeting_audio_url or None
        ta.is_active                     = True
        db.session.commit()""",
    "twilio_sms settings POST saves new routing fields",
)

# ── 15. Database migration for new columns ──────────────────────────────────
print("\n=== Running DB column migration ===")
try:
    import os, sys
    sys.path.insert(0, str(APP))
    os.chdir(APP)
    os.environ.setdefault("FLASK_ENV", "production")

    from app import create_app
    from extensions import db as _db
    app = create_app()
    with app.app_context():
        new_cols = [
            ("sms_forwarding_enabled",        "BOOLEAN DEFAULT TRUE"),
            ("voice_forwarding_enabled",       "BOOLEAN DEFAULT TRUE"),
            ("after_hours_sms_enabled",        "BOOLEAN DEFAULT TRUE"),
            ("after_hours_voicemail_enabled",  "BOOLEAN DEFAULT TRUE"),
            ("voicemail_greeting_text",        "TEXT"),
            ("voicemail_greeting_audio_url",   "VARCHAR(500)"),
        ]
        conn = _db.engine.connect()
        for col, coltype in new_cols:
            try:
                conn.execute(_db.text(
                    f"ALTER TABLE twilio_account ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
                print(f"  ✓ column twilio_account.{col}")
            except Exception as ce:
                print(f"  – already exists or error: {col} ({ce})")
        conn.commit()
        conn.close()
    print("  DB migration complete.")
except Exception as me:
    print(f"  DB migration error (may need manual ALTER TABLE): {me}")

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n=== Patch Results ===")
for m in ok:
    print("  ✓", m)
for m in skip:
    print("  –", m)
for m in fail:
    print("  ✗", m)

if fail:
    print("\nSome patches could not be applied (see ✗ items above).")
    print("They may be in a different format on your VPS — check manually.")
else:
    print("\nAll patches applied successfully.")

print("\nRestart the service:")
print("  sudo systemctl restart luxit")
