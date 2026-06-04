"""
VPS deploy — writes all 6 Twilio HTML templates to templates/twilio/.

Run from /root/lux-email-bot:
    python3 vps_deploy_twilio_templates.py

Safe to run multiple times — overwrites are idempotent.
"""
import subprocess
import sys
from pathlib import Path

APP_DIR    = Path(__file__).parent
TMPL_DIR   = APP_DIR / "templates" / "twilio"
TMPL_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATES = {}

# ── settings.html ─────────────────────────────────────────────────────────────
TEMPLATES["settings.html"] = r"""{% extends "base.html" %}
{% block title %}Twilio Settings — LUXit{% endblock %}

{% block content %}
<div class="container-fluid py-4" style="max-width:820px;">

  <div class="mb-4">
    <h4 class="mb-1 fw-bold">
      <i data-feather="phone" style="width:18px;height:18px;color:#7c3aed;"></i>
      Twilio Settings
    </h4>
    <small class="text-muted">Per-company Twilio credentials and routing config</small>
  </div>

  <!-- Sub-navigation -->
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
  </div>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}{% for c,m in messages %}
    <div class="alert alert-{{ 'danger' if c=='error' else c }} alert-dismissible fade show">
      {{ m }}<button class="btn-close" data-bs-dismiss="alert"></button>
    </div>
    {% endfor %}{% endif %}
  {% endwith %}

  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

    <!-- Credentials -->
    <div class="card mb-4" style="background:#0f1117;border:1px solid #1e2535;">
      <div class="card-header d-flex align-items-center gap-2" style="background:#0b0d14;border-bottom:1px solid #1e2535;">
        <i data-feather="key" style="width:14px;height:14px;color:#7c3aed;"></i>
        <span class="fw-semibold" style="font-size:.88rem;">Credentials</span>
        {% if ta and ta.is_configured %}
        <span class="badge bg-success ms-auto" style="font-size:.72rem;">Configured</span>
        {% else %}
        <span class="badge bg-secondary ms-auto" style="font-size:.72rem;">Not Set</span>
        {% endif %}
      </div>
      <div class="card-body">
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">Account SID</label>
          <input type="text" name="account_sid" class="form-control"
                 placeholder="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                 value="{{ ta.get_account_sid()[:8] + '...' if ta and ta._account_sid else '' }}"
                 style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
          <small class="text-muted">Starts with AC — leave blank to keep existing</small>
        </div>
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">Auth Token</label>
          <input type="password" name="auth_token" class="form-control"
                 placeholder="Leave blank to keep existing"
                 style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
        </div>
        <div class="row g-3">
          <div class="col-md-6">
            <label class="form-label" style="font-size:.85rem;">Messaging Service SID</label>
            <input type="text" name="messaging_service_sid" class="form-control"
                   placeholder="MGxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                   value="{{ ta.messaging_service_sid or '' }}"
                   style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
            <small class="text-muted">Recommended — takes priority over From number</small>
          </div>
          <div class="col-md-6">
            <label class="form-label" style="font-size:.85rem;">From Phone Number</label>
            <input type="text" name="from_phone" class="form-control"
                   placeholder="+15551234567"
                   value="{{ ta.from_phone or '' }}"
                   style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
            <small class="text-muted">Fallback if no Messaging Service SID</small>
          </div>
        </div>
      </div>
    </div>

    <!-- Webhook URLs -->
    <div class="card mb-4" style="background:#0f1117;border:1px solid #1e2535;">
      <div class="card-header d-flex align-items-center gap-2" style="background:#0b0d14;border-bottom:1px solid #1e2535;">
        <i data-feather="link" style="width:14px;height:14px;color:#06b6d4;"></i>
        <span class="fw-semibold" style="font-size:.88rem;">Webhook URLs</span>
      </div>
      <div class="card-body">
        <p class="text-muted mb-3" style="font-size:.85rem;">
          Configure these URLs in your Twilio console (Messaging Service → Webhooks, Phone Number → Voice):
        </p>
        {% set base = request.host_url.rstrip('/') %}
        {% for label, path in [
            ('SMS Inbound Webhook',           '/twilio/sms/inbound'),
            ('SMS Delivery Status Callback',  '/twilio/sms/status'),
            ('Voice Inbound Webhook',         '/twilio/voice/inbound'),
            ('Voice Status Callback',         '/twilio/voice/status'),
            ('Voicemail Recording Callback',  '/twilio/voice/recording'),
        ] %}
        <div class="mb-3">
          <label class="form-label" style="font-size:.8rem;color:#9aa4b2;">{{ label }}</label>
          <div class="input-group">
            <input type="text" class="form-control copy-url" readonly
                   value="{{ base }}{{ path }}"
                   style="background:#0b0d14;border-color:#1e2535;color:#a78bfa;">
            <button class="btn btn-outline-secondary btn-sm" type="button"
                    onclick="navigator.clipboard.writeText(this.previousElementSibling.value)">
              <i data-feather="copy" style="width:13px;height:13px;"></i>
            </button>
          </div>
        </div>
        {% endfor %}
        <div class="mb-0">
          <label class="form-label" style="font-size:.8rem;color:#9aa4b2;">Public Webhook Base URL (override)</label>
          <input type="text" name="webhook_base_url" class="form-control"
                 placeholder="https://luxit.app"
                 value="{{ ta.webhook_base_url or '' }}"
                 style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
          <small class="text-muted">Set to https://luxit.app on VPS. Leave blank to use request URL.</small>
        </div>
      </div>
    </div>

    <!-- SMS Forwarding -->
    <div class="card mb-4" style="background:#0f1117;border:1px solid #1e2535;">
      <div class="card-header d-flex align-items-center gap-2" style="background:#0b0d14;border-bottom:1px solid #1e2535;">
        <i data-feather="message-square" style="width:14px;height:14px;color:#06b6d4;"></i>
        <span class="fw-semibold" style="font-size:.88rem;">SMS Forwarding</span>
      </div>
      <div class="card-body">
        <div class="form-check form-switch mb-3">
          <input type="checkbox" class="form-check-input" id="sms_forwarding_enabled"
                 name="sms_forwarding_enabled"
                 {% if not ta or ta.sms_forwarding_enabled %}checked{% endif %}>
          <label class="form-check-label" for="sms_forwarding_enabled" style="font-size:.9rem;">
            Enable SMS forwarding
          </label>
        </div>
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">Forward Texts To</label>
          <input type="text" name="sms_forward_to" class="form-control"
                 placeholder="+15551234567"
                 value="{{ ta.sms_forward_to or '' if ta else '' }}"
                 style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
          <small class="text-muted">Inbound SMS copied to this number</small>
        </div>
        <div class="form-check form-switch mb-2">
          <input type="checkbox" class="form-check-input" id="after_hours_sms_enabled"
                 name="after_hours_sms_enabled"
                 {% if not ta or ta.after_hours_sms_enabled %}checked{% endif %}>
          <label class="form-check-label" for="after_hours_sms_enabled" style="font-size:.9rem;">
            Send after-hours auto-reply
          </label>
        </div>
        <div class="mb-0">
          <label class="form-label" style="font-size:.85rem;">After-Hours Reply Text</label>
          <textarea name="after_hours_text" class="form-control" rows="3"
                    style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">{{ ta.after_hours_text or '' if ta else "Thanks for reaching out! Our team is currently away. We'll reply during business hours." }}</textarea>
        </div>
      </div>
    </div>

    <!-- Voice Forwarding & Voicemail -->
    <div class="card mb-4" style="background:#0f1117;border:1px solid #1e2535;">
      <div class="card-header d-flex align-items-center gap-2" style="background:#0b0d14;border-bottom:1px solid #1e2535;">
        <i data-feather="phone-forwarded" style="width:14px;height:14px;color:#a78bfa;"></i>
        <span class="fw-semibold" style="font-size:.88rem;">Voice Forwarding &amp; Voicemail</span>
      </div>
      <div class="card-body">
        <div class="form-check form-switch mb-3">
          <input type="checkbox" class="form-check-input" id="voice_forwarding_enabled"
                 name="voice_forwarding_enabled"
                 {% if not ta or ta.voice_forwarding_enabled %}checked{% endif %}>
          <label class="form-check-label" for="voice_forwarding_enabled" style="font-size:.9rem;">
            Forward calls during business hours
          </label>
        </div>
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">Forward Calls To</label>
          <input type="text" name="call_forward_to" class="form-control"
                 placeholder="+15551234567"
                 value="{{ ta.call_forward_to or '' if ta else '' }}"
                 style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
          <small class="text-muted">During business hours, calls are forwarded here (25 s). Unanswered → voicemail.</small>
        </div>
        <div class="form-check form-switch mb-3">
          <input type="checkbox" class="form-check-input" id="after_hours_voicemail_enabled"
                 name="after_hours_voicemail_enabled"
                 {% if not ta or ta.after_hours_voicemail_enabled %}checked{% endif %}>
          <label class="form-check-label" for="after_hours_voicemail_enabled" style="font-size:.9rem;">
            Enable after-hours voicemail
          </label>
        </div>
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">Voicemail Greeting Text</label>
          <textarea name="voicemail_greeting_text" class="form-control" rows="3"
                    style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;"
                    placeholder="Thanks for calling. Please leave your name, number, and a message after the tone.">{{ ta.voicemail_greeting_text or '' if ta else '' }}</textarea>
        </div>
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">Voicemail Greeting Audio URL <span class="text-muted">(optional)</span></label>
          <input type="url" name="voicemail_greeting_audio_url" class="form-control"
                 placeholder="https://example.com/greeting.mp3"
                 value="{{ ta.voicemail_greeting_audio_url or '' if ta else '' }}"
                 style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
        </div>
        <div class="mb-0">
          <label class="form-label" style="font-size:.85rem;">Missed Call Text</label>
          <textarea name="missed_call_text" class="form-control" rows="2"
                    style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">{{ ta.missed_call_text or '' if ta else 'Sorry we missed your call! Reply to schedule a callback.' }}</textarea>
        </div>
      </div>
    </div>

    <!-- Automation & AI -->
    <div class="card mb-4" style="background:#0f1117;border:1px solid #1e2535;">
      <div class="card-header d-flex align-items-center gap-2" style="background:#0b0d14;border-bottom:1px solid #1e2535;">
        <i data-feather="zap" style="width:14px;height:14px;color:#ec4899;"></i>
        <span class="fw-semibold" style="font-size:.88rem;">Automation &amp; AI</span>
      </div>
      <div class="card-body">
        <div class="form-check form-switch mb-3">
          <input type="checkbox" class="form-check-input" id="automation_enabled"
                 name="automation_enabled" {% if not ta or ta.automation_enabled %}checked{% endif %}>
          <label class="form-check-label" for="automation_enabled" style="font-size:.9rem;">
            Enable auto-reply rules engine
          </label>
        </div>
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">AI Mode</label>
          <select name="ai_mode" class="form-select" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
            <option value="off" {% if not ta or ta.ai_mode == 'off' %}selected{% endif %}>Off — manual replies only</option>
            <option value="assist" {% if ta and ta.ai_mode == 'assist' %}selected{% endif %}>Assist — suggest replies</option>
            <option value="auto" {% if ta and ta.ai_mode == 'auto' %}selected{% endif %}>Auto — AI replies automatically</option>
          </select>
        </div>
        <div class="mb-0">
          <label class="form-label" style="font-size:.85rem;">AI System Prompt</label>
          <textarea name="ai_system_prompt" class="form-control" rows="3"
                    style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;"
                    placeholder="You are a helpful assistant for [Company Name]. Be concise and friendly.">{{ ta.ai_system_prompt or '' if ta else '' }}</textarea>
        </div>
      </div>
    </div>

    <div class="d-flex gap-2">
      <button type="submit" class="btn btn-primary">
        <i data-feather="save" style="width:14px;height:14px;"></i> Save Settings
      </button>
      <a href="{{ url_for('twilio.inbox') }}" class="btn btn-outline-secondary">Cancel</a>
    </div>
  </form>
</div>
<script>feather.replace();</script>
{% endblock %}
"""

# ── inbox.html ─────────────────────────────────────────────────────────────────
TEMPLATES["inbox.html"] = r"""{% extends "base.html" %}
{% block title %}SMS Inbox — LUXit{% endblock %}

{% block content %}
<div class="container-fluid py-3" style="max-width:1100px;">

  <div class="d-flex align-items-center gap-3 mb-3">
    <div>
      <h4 class="mb-0 fw-bold">
        <i data-feather="message-square" style="width:20px;height:20px;color:#7c3aed;"></i>
        SMS Inbox
        {% if unread_count > 0 %}<span class="badge bg-danger ms-2" style="font-size:.7rem;">{{ unread_count }}</span>{% endif %}
      </h4>
      <small class="text-muted">Multi-tenant Twilio messaging platform</small>
    </div>
    <div class="ms-auto d-flex gap-2 flex-wrap">
      <a href="{{ url_for('twilio.settings') }}" class="btn btn-sm btn-outline-secondary">
        <i data-feather="settings" style="width:13px;height:13px;"></i> Settings
      </a>
      <a href="{{ url_for('twilio.rules') }}" class="btn btn-sm btn-outline-secondary">
        <i data-feather="zap" style="width:13px;height:13px;"></i> Rules
      </a>
      <a href="{{ url_for('twilio.analytics') }}" class="btn btn-sm btn-outline-secondary">
        <i data-feather="bar-chart-2" style="width:13px;height:13px;"></i> Analytics
      </a>
      <button class="btn btn-sm btn-primary" data-bs-toggle="modal" data-bs-target="#newMsgModal">
        <i data-feather="plus" style="width:13px;height:13px;"></i> New Message
      </button>
    </div>
  </div>

  {% if not ta or not ta.is_configured %}
  <div class="alert alert-warning d-flex align-items-center gap-3">
    <i data-feather="alert-triangle" style="width:20px;height:20px;"></i>
    <div>Twilio is not configured.
      <a href="{{ url_for('twilio.settings') }}" class="alert-link">Add your Twilio credentials →</a>
    </div>
  </div>
  {% endif %}

  <div class="d-flex align-items-center gap-2 mb-3 flex-wrap">
    <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm {% if status_filter == 'all' %}btn-primary{% else %}btn-outline-secondary{% endif %}">All</a>
    <a href="{{ url_for('twilio.inbox', status='unread') }}" class="btn btn-sm {% if status_filter == 'unread' %}btn-primary{% else %}btn-outline-secondary{% endif %}">Unread</a>
    <a href="{{ url_for('twilio.inbox', status='opted_out') }}" class="btn btn-sm {% if status_filter == 'opted_out' %}btn-primary{% else %}btn-outline-secondary{% endif %}">Opted Out</a>
    <form class="ms-auto d-flex gap-2" method="get" action="">
      <input type="hidden" name="status" value="{{ status_filter }}">
      <input type="search" name="q" class="form-control form-control-sm"
             placeholder="Search number or name..." value="{{ search }}"
             style="width:220px;background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
      <button class="btn btn-sm btn-outline-secondary" type="submit">
        <i data-feather="search" style="width:13px;height:13px;"></i>
      </button>
    </form>
  </div>

  {% if conversations %}
  <div class="card" style="background:#0f1117;border:1px solid #1e2535;">
    {% for conv in conversations %}
    <a href="{{ url_for('twilio.conversation', conv_id=conv.id) }}"
       class="d-flex align-items-center gap-3 px-3 py-3 text-decoration-none {% if not conv.is_read %}fw-semibold{% endif %}"
       style="border-bottom:1px solid #1e2535;color:#f7f7fb;background:{% if not conv.is_read %}rgba(124,58,237,.07){% else %}transparent{% endif %};">
      <div style="width:42px;height:42px;border-radius:50%;background:#1e2535;display:flex;align-items:center;justify-content:center;flex-shrink:0;border:2px solid {% if conv.is_opted_out %}#6b7280{% elif not conv.is_read %}#7c3aed{% else %}#374151{% endif %};">
        <i data-feather="user" style="width:18px;height:18px;color:#6b7280;"></i>
      </div>
      <div class="flex-grow-1 min-w-0">
        <div class="d-flex justify-content-between align-items-baseline">
          <span style="font-size:.9rem;">
            {{ conv.contact_name or conv.from_number }}
            {% if conv.contact_name %}<small class="text-muted ms-1">{{ conv.from_number }}</small>{% endif %}
          </span>
          <small class="text-muted flex-shrink-0">{{ conv.last_message_at.strftime('%b %-d') if conv.last_message_at else '' }}</small>
        </div>
        <div class="d-flex align-items-center gap-2">
          <span class="text-muted text-truncate" style="font-size:.82rem;max-width:400px;">{{ conv.last_message_preview or 'No messages yet' }}</span>
          <div class="ms-auto d-flex gap-1 flex-shrink-0">
            {% if conv.is_opted_out %}<span class="badge bg-secondary" style="font-size:.68rem;">Opted Out</span>{% endif %}
            {% if conv.lead_captured %}<span class="badge" style="background:#7c3aed;font-size:.68rem;">Lead</span>{% endif %}
            {% for tag in (conv.tags or [])[:2] %}<span class="badge bg-dark border" style="font-size:.68rem;border-color:#374151!important;">{{ tag }}</span>{% endfor %}
            {% if not conv.is_read %}<span class="badge bg-primary" style="font-size:.68rem;width:8px;height:8px;border-radius:50%;padding:0;"></span>{% endif %}
          </div>
        </div>
      </div>
    </a>
    {% endfor %}
  </div>
  {% else %}
  <div class="card text-center py-5" style="background:#0f1117;border:1px solid #1e2535;">
    <div class="card-body">
      <i data-feather="inbox" style="width:48px;height:48px;opacity:.3;"></i>
      <p class="text-muted mt-3">{% if search %}No conversations matching "{{ search }}"{% else %}No conversations yet.{% endif %}</p>
    </div>
  </div>
  {% endif %}

  <div class="d-flex gap-3 mt-3" style="font-size:.83rem;">
    <a href="{{ url_for('twilio.calls') }}" class="text-muted text-decoration-none"><i data-feather="phone" style="width:13px;height:13px;"></i> Call Log</a>
    <a href="{{ url_for('twilio.business_hours') }}" class="text-muted text-decoration-none"><i data-feather="clock" style="width:13px;height:13px;"></i> Business Hours</a>
  </div>
</div>

<div class="modal fade" id="newMsgModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content" style="background:#0f1117;border:1px solid #1e2535;">
      <div class="modal-header" style="border-bottom:1px solid #1e2535;">
        <h5 class="modal-title">New SMS</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">To (phone number)</label>
          <input type="tel" id="new-to" class="form-control" placeholder="+15551234567" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;">
        </div>
        <div class="mb-3">
          <label class="form-label" style="font-size:.85rem;">Message</label>
          <textarea id="new-body" class="form-control" rows="4" maxlength="1600" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;resize:none;"></textarea>
        </div>
        <div id="new-msg-result" style="display:none;"></div>
      </div>
      <div class="modal-footer" style="border-top:1px solid #1e2535;">
        <button class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-sm btn-primary" onclick="sendNewMessage()"><i data-feather="send" style="width:13px;height:13px;"></i> Send</button>
      </div>
    </div>
  </div>
</div>

<script>
feather.replace();
const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '{{ csrf_token() }}';
async function sendNewMessage() {
  const to = document.getElementById('new-to').value.trim();
  const body = document.getElementById('new-body').value.trim();
  const res = document.getElementById('new-msg-result');
  if (!to || !body) { res.style.display='block'; res.innerHTML='<div class="alert alert-warning py-2">To and message are required.</div>'; return; }
  const r = await fetch('/twilio/send', { method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':CSRF}, body:JSON.stringify({to,body}) });
  const data = await r.json();
  res.style.display = 'block';
  if (data.success) { res.innerHTML='<div class="alert alert-success py-2">Sent!</div>'; setTimeout(()=>location.reload(),1200); }
  else { res.innerHTML=`<div class="alert alert-danger py-2">${data.error||'Send failed.'}</div>`; }
}
</script>
{% endblock %}
"""

# ── conversation.html ──────────────────────────────────────────────────────────
TEMPLATES["conversation.html"] = r"""{% extends "base.html" %}
{% block title %}{{ conv.contact_name or conv.from_number }} — SMS Inbox{% endblock %}

{% block content %}
<div class="container-fluid py-3" style="max-width:860px;">
  <div class="d-flex align-items-center gap-3 mb-3">
    <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary">
      <i data-feather="arrow-left" style="width:13px;height:13px;"></i>
    </a>
    <div>
      <h5 class="mb-0 fw-bold">{{ conv.contact_name or conv.from_number }}</h5>
      <small class="text-muted">
        {{ conv.from_number }}
        {% if conv.is_opted_out %} · <span class="text-danger">Opted Out</span>{% endif %}
        {% if conv.lead_captured %} · <span style="color:#7c3aed;">Lead Captured</span>{% endif %}
      </small>
    </div>
    <div class="ms-auto d-flex gap-2">
      {% if not conv.is_opted_out %}
      <button class="btn btn-sm btn-outline-secondary" onclick="addTag()">
        <i data-feather="tag" style="width:13px;height:13px;"></i> Tag
      </button>
      {% endif %}
    </div>
  </div>

  {% if conv.tags %}
  <div class="d-flex gap-1 mb-3 flex-wrap">
    {% for tag in conv.tags %}<span class="badge bg-dark border" style="border-color:#374151!important;font-size:.78rem;">{{ tag }}</span>{% endfor %}
  </div>
  {% endif %}

  <div class="card mb-3" style="background:#0f1117;border:1px solid #1e2535;">
    <div class="card-body py-2 px-3">
      <div class="d-flex align-items-center gap-2 mb-1">
        <i data-feather="file-text" style="width:13px;height:13px;color:#9aa4b2;"></i>
        <small class="text-muted">Notes</small>
        <button class="btn btn-xs ms-auto" style="padding:0 6px;font-size:.75rem;color:#7c3aed;" onclick="saveNote()">Save</button>
      </div>
      <textarea id="notes-field" class="form-control form-control-sm"
                style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;resize:none;" rows="2"
                placeholder="Internal notes about this contact...">{{ conv.notes or '' }}</textarea>
    </div>
  </div>

  <div id="thread" class="mb-3" style="max-height:420px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding-right:4px;">
    {% for msg in messages %}
    <div class="d-flex {% if msg.direction == 'outbound' %}justify-content-end{% endif %}">
      <div style="max-width:72%;padding:10px 14px;border-radius:{% if msg.direction == 'outbound' %}16px 4px 16px 16px{% else %}4px 16px 16px 16px{% endif %};background:{% if msg.direction == 'outbound' %}{% if msg.is_auto_reply %}rgba(6,182,212,.15);border:1px solid rgba(6,182,212,.3){% else %}#7c3aed{% endif %}{% else %}#1e2535{% endif %};">
        <p class="mb-1" style="font-size:.88rem;color:#f7f7fb;word-break:break-word;">{{ msg.body or '(media)' }}</p>
        <div class="d-flex align-items-center gap-2">
          <small style="font-size:.72rem;color:#9aa4b2;">{{ msg.created_at.strftime('%b %-d %I:%M %p') if msg.created_at else '' }}{% if msg.is_auto_reply %} · <span style="color:#06b6d4;">auto-reply</span>{% endif %}</small>
          {% if msg.direction == 'outbound' %}
          <small style="font-size:.72rem;color:{% if msg.status == 'delivered' %}#22c55e{% elif msg.status == 'failed' %}#ef4444{% elif msg.status == 'undelivered' %}#f59e0b{% else %}#9aa4b2{% endif %};">{{ msg.status }}</small>
          {% endif %}
        </div>
        {% if msg.media_urls %}{% for url in msg.media_urls %}<img src="{{ url }}" alt="MMS" style="max-width:200px;border-radius:8px;margin-top:6px;">{% endfor %}{% endif %}
      </div>
    </div>
    {% endfor %}
    {% if not messages %}<p class="text-muted text-center py-4">No messages yet.</p>{% endif %}
  </div>

  {% if not conv.is_opted_out %}
  {% if ta and ta.is_configured %}
  <div class="card" style="background:#0f1117;border:1px solid #1e2535;">
    <div class="card-body">
      <textarea id="reply-body" class="form-control mb-2" placeholder="Type a message..." rows="3" maxlength="1600"
                style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;resize:none;"></textarea>
      <div class="d-flex justify-content-between align-items-center">
        <small id="reply-count" class="text-muted">0 / 1600</small>
        <button class="btn btn-sm btn-primary" onclick="sendReply()" id="send-btn">
          <i data-feather="send" style="width:13px;height:13px;"></i> Send
        </button>
      </div>
      <div id="reply-result" class="mt-2" style="display:none;"></div>
    </div>
  </div>
  {% else %}
  <div class="alert alert-warning"><a href="{{ url_for('twilio.settings') }}" class="alert-link">Configure Twilio</a> to send replies.</div>
  {% endif %}
  {% else %}
  <div class="alert alert-secondary">This contact has opted out of messages.</div>
  {% endif %}
</div>

<script>
feather.replace();
const CSRF = '{{ csrf_token() }}';
const CONV_ID = {{ conv.id }};
const thread = document.getElementById('thread');
if (thread) thread.scrollTop = thread.scrollHeight;
const replyBody = document.getElementById('reply-body');
if (replyBody) replyBody.addEventListener('input', () => { document.getElementById('reply-count').textContent = replyBody.value.length + ' / 1600'; });
async function sendReply() {
  const body = replyBody?.value.trim();
  if (!body) return;
  const btn = document.getElementById('send-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>';
  const r = await fetch('/twilio/send', { method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':CSRF}, body:JSON.stringify({to:'{{ conv.from_number }}',body,conversation_id:CONV_ID}) });
  const data = await r.json();
  if (data.success) { replyBody.value=''; setTimeout(()=>location.reload(),600); }
  else { const el=document.getElementById('reply-result'); el.style.display='block'; el.innerHTML=`<div class="alert alert-danger py-2">${data.error||'Send failed.'}</div>`; btn.disabled=false; btn.innerHTML='<i data-feather="send" style="width:13px;height:13px;"></i> Send'; feather.replace(); }
}
async function addTag() {
  const tag = prompt('Enter tag (e.g. hot-lead, vip):');
  if (!tag) return;
  const r = await fetch(`/twilio/conversation/{{ conv.id }}/tag`, { method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':CSRF}, body:JSON.stringify({tag}) });
  const data = await r.json();
  if (data.success) location.reload();
}
async function saveNote() {
  const notes = document.getElementById('notes-field').value;
  await fetch(`/twilio/conversation/{{ conv.id }}/note`, { method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':CSRF}, body:JSON.stringify({notes}) });
}
</script>
{% endblock %}
"""

# ── rules.html ─────────────────────────────────────────────────────────────────
TEMPLATES["rules.html"] = r"""{% extends "base.html" %}
{% block title %}Auto-Reply Rules — LUXit{% endblock %}

{% block content %}
<div class="container-fluid py-4" style="max-width:960px;">
  <div class="d-flex align-items-center gap-3 mb-3">
    <div>
      <h4 class="mb-0 fw-bold"><i data-feather="zap" style="width:18px;height:18px;color:#7c3aed;"></i> Auto-Reply Rules</h4>
      <small class="text-muted">Rules fire in priority order — highest priority first</small>
    </div>
    <div class="ms-auto">
      <button class="btn btn-sm btn-primary" data-bs-toggle="modal" data-bs-target="#newRuleModal">
        <i data-feather="plus" style="width:13px;height:13px;"></i> New Rule
      </button>
    </div>
  </div>

  <div class="d-flex flex-wrap gap-2 mb-4">
    <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="inbox" style="width:13px;height:13px;"></i> Inbox</a>
    <a href="{{ url_for('twilio.settings') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="settings" style="width:13px;height:13px;"></i> Settings</a>
    <a href="{{ url_for('twilio.business_hours') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="clock" style="width:13px;height:13px;"></i> Business Hours</a>
    <a href="{{ url_for('twilio.calls') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="phone-call" style="width:13px;height:13px;"></i> Call Log</a>
    <a href="{{ url_for('twilio.analytics') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="bar-chart-2" style="width:13px;height:13px;"></i> Analytics</a>
  </div>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}{% for c,m in messages %}
    <div class="alert alert-{{ 'danger' if c=='error' else c }} alert-dismissible fade show">{{ m }}<button class="btn-close" data-bs-dismiss="alert"></button></div>
    {% endfor %}{% endif %}
  {% endwith %}

  {% if not ta or not ta.automation_enabled %}
  <div class="alert alert-warning d-flex gap-3 align-items-center">
    <i data-feather="alert-triangle" style="width:18px;height:18px;"></i>
    <span>Automation is disabled. <a href="{{ url_for('twilio.settings') }}" class="alert-link">Enable it in Settings →</a></span>
  </div>
  {% endif %}

  {% if rules %}
  <div class="card" style="background:#0f1117;border:1px solid #1e2535;">
    <div class="table-responsive">
      <table class="table table-dark mb-0" style="font-size:.87rem;">
        <thead style="border-bottom:1px solid #1e2535;color:#9aa4b2;">
          <tr><th style="width:40px;">Pri</th><th>Name</th><th>Trigger</th><th>Keywords</th><th>Action</th><th style="width:60px;">Fired</th><th style="width:90px;">Status</th><th style="width:80px;"></th></tr>
        </thead>
        <tbody>
          {% for rule in rules %}
          <tr id="rule-row-{{ rule.id }}" style="border-bottom:1px solid #1e2535;">
            <td class="text-muted">{{ rule.priority }}</td>
            <td class="fw-semibold">{{ rule.name }}</td>
            <td><span class="badge" style="font-size:.72rem;background:{% if rule.trigger_type == 'keyword_contains' %}rgba(124,58,237,.3);color:#a78bfa{% elif rule.trigger_type == 'keyword_exact' %}rgba(6,182,212,.3);color:#06b6d4{% elif rule.trigger_type == 'after_hours' %}rgba(249,115,22,.3);color:#fb923c{% elif rule.trigger_type == 'first_contact' %}rgba(34,197,94,.3);color:#4ade80{% else %}rgba(107,114,128,.3);color:#9ca3af{% endif %};">{{ rule.trigger_type | replace('_', ' ') | title }}</span></td>
            <td class="text-muted">{% if rule.keywords %}{{ rule.keywords[:3] | join(', ') }}{% if rule.keywords|length > 3 %}...{% endif %}{% else %}—{% endif %}</td>
            <td><span style="font-size:.8rem;color:{% if rule.action == 'reply' %}#a78bfa{% elif rule.action == 'opt_out' %}#f87171{% elif rule.action == 'tag' %}#4ade80{% else %}#9aa4b2{% endif %};">{{ rule.action }}{% if rule.tag_value %} ({{ rule.tag_value }}){% endif %}{% if rule.forward_to %} → {{ rule.forward_to }}{% endif %}</span></td>
            <td class="text-muted">{{ rule.match_count or 0 }}</td>
            <td><div class="form-check form-switch mb-0"><input type="checkbox" class="form-check-input" role="switch" {% if rule.is_active %}checked{% endif %} onchange="toggleRule({{ rule.id }}, this)"></div></td>
            <td>
              <form method="POST" action="{{ url_for('twilio.delete_rule', rule_id=rule.id) }}" onsubmit="return confirm('Delete rule {{ rule.name }}?');">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button type="submit" class="btn btn-xs btn-outline-danger" style="padding:2px 8px;font-size:.75rem;"><i data-feather="trash-2" style="width:11px;height:11px;"></i></button>
              </form>
            </td>
          </tr>
          {% if rule.response %}
          <tr style="border-bottom:1px solid #1e2535;">
            <td colspan="8" class="text-muted py-1 ps-4" style="font-size:.8rem;font-style:italic;">Reply: "{{ rule.response[:120] }}{% if rule.response|length > 120 %}...{% endif %}"</td>
          </tr>
          {% endif %}
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% else %}
  <div class="card text-center py-5" style="background:#0f1117;border:1px solid #1e2535;">
    <div class="card-body">
      <i data-feather="zap" style="width:40px;height:40px;opacity:.3;"></i>
      <p class="text-muted mt-3">No rules yet. Create your first rule or save Twilio settings to seed defaults.</p>
      <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#newRuleModal">Create Rule</button>
    </div>
  </div>
  {% endif %}
</div>

<div class="modal fade" id="newRuleModal" tabindex="-1">
  <div class="modal-dialog modal-lg modal-dialog-centered">
    <div class="modal-content" style="background:#0f1117;border:1px solid #1e2535;">
      <form method="POST" action="{{ url_for('twilio.create_rule') }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <div class="modal-header" style="border-bottom:1px solid #1e2535;">
          <h5 class="modal-title">New Auto-Reply Rule</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
        </div>
        <div class="modal-body">
          <div class="row g-3">
            <div class="col-md-6"><label class="form-label" style="font-size:.85rem;">Rule Name</label><input type="text" name="name" class="form-control" required style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;" placeholder="e.g. Pricing Inquiry"></div>
            <div class="col-md-3"><label class="form-label" style="font-size:.85rem;">Priority</label><input type="number" name="priority" class="form-control" value="5" min="0" max="100" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;"></div>
            <div class="col-md-3"><label class="form-label" style="font-size:.85rem;">Action</label><select name="action" class="form-select" id="action-sel" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;" onchange="toggleAction(this.value)"><option value="reply">Reply</option><option value="tag">Tag Contact</option><option value="opt_out">Opt Out</option><option value="forward">Forward</option></select></div>
            <div class="col-md-6"><label class="form-label" style="font-size:.85rem;">Trigger Type</label><select name="trigger_type" class="form-select" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;" onchange="toggleKeywords(this.value)"><option value="keyword_contains">Keyword Contains</option><option value="keyword_exact">Keyword Exact Match</option><option value="regex">Regex Pattern</option><option value="first_contact">First Contact</option><option value="after_hours">After Hours</option><option value="stop_keyword">Stop / Unsubscribe</option><option value="always">Always</option></select></div>
            <div class="col-md-6" id="keywords-field"><label class="form-label" style="font-size:.85rem;">Keywords <small class="text-muted">(comma-separated)</small></label><input type="text" name="keywords" class="form-control" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;" placeholder="price, pricing, cost"></div>
            <div class="col-12"><label class="form-label" style="font-size:.85rem;">Response Message</label><textarea name="response" class="form-control" rows="3" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;" placeholder="Our pricing starts at..."></textarea></div>
            <div class="col-md-6" id="tag-field" style="display:none;"><label class="form-label" style="font-size:.85rem;">Tag Value</label><input type="text" name="tag_value" class="form-control" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;" placeholder="hot-lead"></div>
            <div class="col-md-6" id="forward-field" style="display:none;"><label class="form-label" style="font-size:.85rem;">Forward To</label><input type="text" name="forward_to" class="form-control" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;" placeholder="+15551234567"></div>
          </div>
        </div>
        <div class="modal-footer" style="border-top:1px solid #1e2535;">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-sm btn-primary">Create Rule</button>
        </div>
      </form>
    </div>
  </div>
</div>
<script>
feather.replace();
const CSRF = '{{ csrf_token() }}';
function toggleKeywords(type) { document.getElementById('keywords-field').style.display = ['keyword_contains','keyword_exact','regex'].includes(type) ? '' : 'none'; }
function toggleAction(action) { document.getElementById('tag-field').style.display = action==='tag' ? '' : 'none'; document.getElementById('forward-field').style.display = action==='forward' ? '' : 'none'; }
async function toggleRule(ruleId, checkbox) {
  const r = await fetch(`/twilio/rules/${ruleId}/toggle`, { method:'POST', headers:{'X-CSRFToken':CSRF} });
  const data = await r.json();
  if (!data.success) { checkbox.checked = !checkbox.checked; alert('Toggle failed.'); }
}
</script>
{% endblock %}
"""

# ── hours.html ─────────────────────────────────────────────────────────────────
TEMPLATES["hours.html"] = r"""{% extends "base.html" %}
{% block title %}Business Hours — LUXit{% endblock %}

{% block content %}
<div class="container-fluid py-4" style="max-width:680px;">
  <div class="mb-3">
    <h4 class="mb-1 fw-bold"><i data-feather="clock" style="width:18px;height:18px;color:#7c3aed;"></i> Business Hours</h4>
    <small class="text-muted">Used for after-hours auto-reply rules (America/Los_Angeles)</small>
  </div>
  <div class="d-flex flex-wrap gap-2 mb-4">
    <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="inbox" style="width:13px;height:13px;"></i> Inbox</a>
    <a href="{{ url_for('twilio.settings') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="settings" style="width:13px;height:13px;"></i> Settings</a>
    <a href="{{ url_for('twilio.rules') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="zap" style="width:13px;height:13px;"></i> Auto-Reply Rules</a>
    <a href="{{ url_for('twilio.calls') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="phone-call" style="width:13px;height:13px;"></i> Call Log</a>
    <a href="{{ url_for('twilio.analytics') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="bar-chart-2" style="width:13px;height:13px;"></i> Analytics</a>
  </div>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}{% for c,m in messages %}
    <div class="alert alert-{{ 'danger' if c=='error' else c }} alert-dismissible fade show">{{ m }}<button class="btn-close" data-bs-dismiss="alert"></button></div>
    {% endfor %}{% endif %}
  {% endwith %}
  <form method="POST">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="card mb-4" style="background:#0f1117;border:1px solid #1e2535;">
      <div class="card-header" style="background:#0b0d14;border-bottom:1px solid #1e2535;">
        <div class="row align-items-center" style="font-size:.82rem;color:#9aa4b2;">
          <div class="col-3">Day</div><div class="col-2">Open</div><div class="col-3">Opens</div><div class="col-3">Closes</div>
        </div>
      </div>
      <div class="card-body p-0">
        {% for bh in hours %}
        <div class="row align-items-center px-3 py-2" style="border-bottom:1px solid #1e2535;">
          <div class="col-3 fw-semibold" style="font-size:.88rem;">{{ days[bh.day_of_week] }}</div>
          <div class="col-2"><div class="form-check form-switch"><input class="form-check-input" type="checkbox" name="open_{{ bh.day_of_week }}" {% if bh.is_open %}checked{% endif %} onchange="toggleDay({{ bh.day_of_week }}, this.checked)"></div></div>
          <div class="col-3" id="times_{{ bh.day_of_week }}" {% if not bh.is_open %}style="opacity:.3;"{% endif %}><input type="time" name="open_time_{{ bh.day_of_week }}" class="form-control form-control-sm" value="{{ bh.open_time }}" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;"></div>
          <div class="col-3" id="times_close_{{ bh.day_of_week }}" {% if not bh.is_open %}style="opacity:.3;"{% endif %}><input type="time" name="close_time_{{ bh.day_of_week }}" class="form-control form-control-sm" value="{{ bh.close_time }}" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;"></div>
        </div>
        {% endfor %}
      </div>
    </div>
    <div class="mb-4">
      <label class="form-label" style="font-size:.85rem;">Timezone</label>
      <select name="timezone" class="form-select" style="background:#0b0d14;border-color:#1e2535;color:#f7f7fb;max-width:300px;">
        {% set tz = hours[0].timezone if hours else 'America/Los_Angeles' %}
        {% for zone in ['America/New_York','America/Chicago','America/Denver','America/Los_Angeles','America/Phoenix','America/Anchorage','Pacific/Honolulu','UTC'] %}
        <option value="{{ zone }}" {% if zone == tz %}selected{% endif %}>{{ zone }}</option>
        {% endfor %}
      </select>
    </div>
    <button type="submit" class="btn btn-primary"><i data-feather="save" style="width:14px;height:14px;"></i> Save Hours</button>
  </form>
</div>
<script>
feather.replace();
function toggleDay(day, open) { ['times_'+day,'times_close_'+day].forEach(id => { const el=document.getElementById(id); if(el) el.style.opacity=open?'1':'0.3'; }); }
</script>
{% endblock %}
"""

# ── calls.html ─────────────────────────────────────────────────────────────────
TEMPLATES["calls.html"] = r"""{% extends "base.html" %}
{% block title %}Call Log — LUXit{% endblock %}

{% block content %}
<div class="container-fluid py-4" style="max-width:960px;">
  <div class="mb-3">
    <h4 class="mb-1 fw-bold"><i data-feather="phone" style="width:18px;height:18px;color:#7c3aed;"></i> Call Log</h4>
    <small class="text-muted">Inbound &amp; outbound call history</small>
  </div>
  <div class="d-flex flex-wrap gap-2 mb-4">
    <a href="{{ url_for('twilio.inbox') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="inbox" style="width:13px;height:13px;"></i> Inbox</a>
    <a href="{{ url_for('twilio.settings') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="settings" style="width:13px;height:13px;"></i> Settings</a>
    <a href="{{ url_for('twilio.rules') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="zap" style="width:13px;height:13px;"></i> Auto-Reply Rules</a>
    <a href="{{ url_for('twilio.business_hours') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="clock" style="width:13px;height:13px;"></i> Business Hours</a>
    <a href="{{ url_for('twilio.analytics') }}" class="btn btn-sm btn-outline-secondary"><i data-feather="bar-chart-2" style="width:13px;height:13px;"></i> Analytics</a>
  </div>
  {% if calls %}
  <div class="card" style="background:#0f1117;border:1px solid #1e2535;">
    <div class="table-responsive">
      <table class="table table-dark mb-0" style="font-size:.87rem;">
        <thead style="border-bottom:1px solid #1e2535;color:#9aa4b2;">
          <tr><th>From</th><th>To</th><th>Caller</th><th>Direction</th><th>Status</th><th>Duration</th><th>Missed Text</th><th>Date</th></tr>
        </thead>
        <tbody>
          {% for call in calls %}
          <tr style="border-bottom:1px solid #1e2535;">
            <td>{{ call.from_number }}</td>
            <td>{{ call.to_number }}</td>
            <td class="text-muted">{{ call.caller_name or '—' }}</td>
            <td><span class="badge" style="font-size:.72rem;background:{% if call.direction=='inbound' %}rgba(124,58,237,.3);color:#a78bfa{% else %}rgba(6,182,212,.3);color:#06b6d4{% endif %};">{{ call.direction }}</span></td>
            <td><span class="badge" style="font-size:.72rem;background:{% if call.status=='completed' %}rgba(34,197,94,.3);color:#4ade80{% elif call.status in ('no-answer','busy') %}rgba(239,68,68,.3);color:#f87171{% else %}rgba(107,114,128,.3);color:#9ca3af{% endif %};">{{ call.status }}</span></td>
            <td class="text-muted">{% if call.duration %}{{ call.duration }}s{% else %}—{% endif %}</td>
            <td>{% if call.missed_text_sent %}<span class="badge bg-success" style="font-size:.7rem;">Sent</span>{% else %}—{% endif %}</td>
            <td class="text-muted">{{ call.created_at.strftime('%b %-d %I:%M %p') if call.created_at else '—' }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% else %}
  <div class="card text-center py-5" style="background:#0f1117;border:1px solid #1e2535;">
    <div class="card-body">
      <i data-feather="phone-off" style="width:40px;height:40px;opacity:.3;"></i>
      <p class="text-muted mt-3">No call records yet. Configure the voice webhook to start logging calls.</p>
    </div>
  </div>
  {% endif %}
</div>
<script>feather.replace();</script>
{% endblock %}
"""

# ── Write all templates ────────────────────────────────────────────────────────
print(f"\nWriting Twilio templates to: {TMPL_DIR}\n")
for filename, content in TEMPLATES.items():
    dest = TMPL_DIR / filename
    dest.write_text(content.lstrip("\n"))
    print(f"  ✓  {filename}  ({len(content):,} chars)")

print(f"\nAll {len(TEMPLATES)} templates written.")

# ── Restart hint ──────────────────────────────────────────────────────────────
print("\n══ Done. Restart: ══")
print("   sudo systemctl restart luxit")
print("   curl -s -o /dev/null -w '%{http_code}' https://luxit.app/twilio/settings")
print("   Expected: 200 (or 302 if not logged in)")
