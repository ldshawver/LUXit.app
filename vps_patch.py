#!/usr/bin/env python3
"""
LUXit VPS patch script.
Run from inside the app directory:
    cd /var/www/luxit-marketing
    python3 vps_patch.py
"""
import shutil
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


# ── 1. Sidebar visible on every /twilio/* page ─────────────────────────────
patch(
    "templates/base.html",
    "or _p.startswith('/sms') or _p.startswith('/social')",
    "or _p.startswith('/sms') or _p.startswith('/twilio') or _p.startswith('/social')",
    "sidebar _sec_core includes /twilio",
)

# ── 2. Nav bar on Settings page ─────────────────────────────────────────────
patch(
    "templates/twilio/settings.html",
    """  <div class="d-flex align-items-center gap-3 mb-4">
    <div>
      <h4 class="mb-0 fw-bold">
        <i data-feather="phone" style="width:18px;height:18px;color:#7c3aed;"></i>
        Twilio Settings
      </h4>
      <small class="text-muted">Per-company Twilio credentials and messaging config</small>
    </div>
    <div class="ms-auto d-flex gap-2">
      <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary">
        <i data-feather="inbox" style="width:13px;height:13px;"></i> Inbox
      </a>
    </div>
  </div>""",
    """  <div class="mb-4">
    <h4 class="mb-1 fw-bold">
      <i data-feather="phone" style="width:18px;height:18px;color:#7c3aed;"></i>
      Twilio Settings
    </h4>
    <small class="text-muted">Per-company Twilio credentials and messaging config</small>
  </div>
  <div class="d-flex flex-wrap gap-2 mb-4">
    <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="inbox" style="width:13px;height:13px;"></i> Inbox
    </a>
    <a href="{{ url_for('twilio.rules') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="zap" style="width:13px;height:13px;"></i> Auto-Reply Rules
    </a>
    <a href="{{ url_for('twilio.business_hours') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="clock" style="width:13px;height:13px;"></i> Business Hours
    </a>
    <a href="{{ url_for('twilio.calls') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="phone-call" style="width:13px;height:13px;"></i> Call Log
    </a>
    <a href="{{ url_for('twilio.analytics') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="bar-chart-2" style="width:13px;height:13px;"></i> Analytics
    </a>
  </div>""",
    "settings page nav bar",
)

# ── 3. Nav bar on Auto-Reply Rules page ──────────────────────────────────────
patch(
    "templates/twilio/rules.html",
    """    <div class="ms-auto d-flex gap-2">
      <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary">
        <i data-feather="inbox" style="width:13px;height:13px;"></i> Inbox
      </a>
      <button class="btn btn-sm btn-primary" data-bs-toggle="modal" data-bs-target="#newRuleModal">
        <i data-feather="plus" style="width:13px;height:13px;"></i> New Rule
      </button>
    </div>
  </div>""",
    """    <div class="ms-auto">
      <button class="btn btn-sm btn-primary" data-bs-toggle="modal" data-bs-target="#newRuleModal">
        <i data-feather="plus" style="width:13px;height:13px;"></i> New Rule
      </button>
    </div>
  </div>
  <div class="d-flex flex-wrap gap-2 mb-4">
    <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="inbox" style="width:13px;height:13px;"></i> Inbox
    </a>
    <a href="{{ url_for('twilio.settings') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="settings" style="width:13px;height:13px;"></i> Settings
    </a>
    <a href="{{ url_for('twilio.business_hours') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="clock" style="width:13px;height:13px;"></i> Business Hours
    </a>
    <a href="{{ url_for('twilio.calls') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="phone-call" style="width:13px;height:13px;"></i> Call Log
    </a>
    <a href="{{ url_for('twilio.analytics') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="bar-chart-2" style="width:13px;height:13px;"></i> Analytics
    </a>
  </div>""",
    "rules page nav bar",
)

# ── 4. Sidebar links: Auto-Reply Rules + SMS Settings ────────────────────────
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

# ── 5. Fix set_secret in ai_action_executor.py ──────────────────────────────
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

# ── 6. Fix set_secret in populate_secrets.py ────────────────────────────────
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

# ── 7. twilio_sms.py — add zoneinfo import ──────────────────────────────────
patch(
    "twilio_sms.py",
    "from datetime import datetime, timezone, timedelta",
    "from datetime import datetime, timezone, timedelta\nfrom zoneinfo import ZoneInfo",
    "twilio_sms zoneinfo import",
)

# ── 8. twilio_sms.py — inject constants + helpers after _build_client ────────
NEW_HELPERS = '''

_LA = ZoneInfo("America/Los_Angeles")

# System-level keyword responses (always fire regardless of auto-reply rules)
_STOP_REPLY = "You have been unsubscribed. Reply START to opt back in."
_START_REPLY = "You have been subscribed. Thanks for joining LUXit SMS updates."
_HELP_REPLY  = "LUXit SMS Support: Reply STOP to opt out. For help, contact support@luxit.app."

TWILIO_WEBHOOK_PUBLIC_URL = os.environ.get(
    "TWILIO_WEBHOOK_PUBLIC_URL", "https://luxit.app/twilio/sms/inbound"
)


def _twiml_message(text: str):
    """Return a TwiML <Response><Message> reply."""
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f\'\'\'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>\'\'\'
    return xml, 200, {"Content-Type": "text/xml"}


def _validate_twilio_signature(ta) -> bool:
    """Validate X-Twilio-Signature. Always passes on Replit dev."""
    is_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"))
    if is_replit:
        return True
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        return True
    token = (ta.get_auth_token() if ta else None) or os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:
        logger.warning("Twilio signature validation skipped: no auth token")
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("Inbound SMS rejected: missing X-Twilio-Signature")
        return False
    url = (ta.webhook_base_url.rstrip("/") + "/twilio/sms/inbound"
           if ta and ta.webhook_base_url else TWILIO_WEBHOOK_PUBLIC_URL)
    validator = RequestValidator(token)
    valid = validator.validate(url, request.form, signature)
    if not valid:
        logger.warning("Inbound SMS rejected: invalid signature (url=%s)", url)
    return valid

'''

patch(
    "twilio_sms.py",
    "\ndef _is_business_hours",
    NEW_HELPERS + "\ndef _is_business_hours",
    "twilio_sms constants + _twiml_message + _validate_twilio_signature",
)

# ── 9. twilio_sms.py — replace _is_business_hours with LA timezone version ───
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
    """
    Return True if current America/Los_Angeles time is within business hours.
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
            # Midnight-crossing (e.g. 11:00 AM – 1:00 AM next day)
            return current >= opens or current < closes
        return opens <= current < closes
    except Exception:
        return True'''

patch("twilio_sms.py", OLD_BIZ, NEW_BIZ, "twilio_sms _is_business_hours LA timezone + midnight-crossing")

# ── 10. twilio_sms.py — add STOP/START/HELP + signature validation to inbound_sms ─
OLD_INBOUND_CORE = '''        # Auto-capture lead on first contact
        if conv.is_first_contact:
            _capture_lead(conv, body, ta.company_id)

        # Run auto-reply rules if not opted out
        if not conv.is_opted_out:
            _apply_auto_reply_rules(conv, body, ta)'''

NEW_INBOUND_CORE = '''        # ── System-level keyword handling (always, before rule engine) ──────
        kw = body.upper().strip()
        if kw == "STOP":
            conv.is_opted_out = True
            db.session.commit()
            logger.info("STOP received: opted out %s", from_number)
            return _twiml_message(_STOP_REPLY)
        if kw == "START":
            conv.is_opted_out = False
            db.session.commit()
            logger.info("START received: opted in %s", from_number)
            return _twiml_message(_START_REPLY)
        if kw == "HELP":
            logger.info("HELP received from %s", from_number)
            return _twiml_message(_HELP_REPLY)

        # Auto-capture lead on first contact
        if conv.is_first_contact:
            _capture_lead(conv, body, ta.company_id)

        # Run auto-reply rules if not opted out
        if not conv.is_opted_out:
            _apply_auto_reply_rules(conv, body, ta)'''

patch("twilio_sms.py", OLD_INBOUND_CORE, NEW_INBOUND_CORE,
      "twilio_sms inbound_sms STOP/START/HELP system keywords")

# ── 11. twilio_sms.py — add signature validation call in inbound_sms ─────────
patch(
    "twilio_sms.py",
    """    if not ta:
        logger.warning("Inbound SMS: no TwilioAccount found for to=%s", to_number)
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    try:""",
    """    if not ta:
        logger.warning("Inbound SMS: no TwilioAccount found for to=%s", to_number)
        return '<Response></Response>', 200, {"Content-Type": "text/xml"}

    # Validate Twilio signature (skipped on Replit dev, required on VPS)
    if not _validate_twilio_signature(ta):
        abort(403)

    try:""",
    "twilio_sms inbound_sms signature validation",
)

# ── Summary ──────────────────────────────────────────────────────────────────
print("\n=== LUXit VPS Patch Results ===")
for m in ok:
    print("  ✓", m)
for m in skip:
    print("  –", m)
for m in fail:
    print("  ✗", m)

if fail:
    print("\nSome patches could not be applied. Check the ✗ items above.")
    print("They may already be applied or the file content differs.")
else:
    print("\nAll patches applied successfully.")

print("\nRestart the service:")
print("  sudo systemctl restart luxit")
