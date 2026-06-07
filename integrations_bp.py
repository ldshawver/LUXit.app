"""
Integration Blueprint — /api/integrations/*, /platform/integrations, /platform/api-hub

Endpoints:
  GET  /api/integrations/health
  POST /api/integrations/health/<provider>/test

  POST /api/twilio/send-sms
  POST /api/twilio/inbound-sms
  POST /api/twilio/voice/status

  POST /api/outlook/send-email
  GET  /api/outlook/messages/recent
  GET  /api/outlook/calendar/events
  POST /api/outlook/calendar/events

  GET  /api/airtable/health
  GET  /api/airtable/records
  POST /api/airtable/records
  PATCH /api/airtable/records/<record_id>
  DELETE /api/airtable/records/<record_id>
  POST /api/airtable/sync/lead/<int:contact_id>
  POST /api/airtable/sync/onboarding/<int:project_id>
  POST /api/airtable/sync/support/<int:ticket_id>
  GET  /api/airtable/sync/logs
  GET  /api/airtable/sync/stats

  GET  /api/github/repos
  GET  /api/github/<owner>/<repo>/issues
  POST /api/github/<owner>/<repo>/issues
  GET  /api/github/<owner>/<repo>/pulls
  GET  /api/github/<owner>/<repo>/latest-commit

  POST /api/webhooks/revenuecat

  GET  /platform/integrations           (admin UI — all providers)
  GET  /platform/integrations/airtable  (admin UI — Airtable detail + sync)
"""
import json
import logging

from flask import Blueprint, jsonify, request, render_template, abort
from flask_login import current_user, login_required

logger = logging.getLogger(__name__)

integrations_bp = Blueprint("integrations", __name__)


# ============================================================
# Helpers
# ============================================================

def _company_id():
    if current_user.is_authenticated:
        c = current_user.get_default_company()
        return c.id if c else None
    return None


def _require_platform_admin():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


def _require_login():
    if not current_user.is_authenticated:
        abort(401)


# ============================================================
# Health endpoints
# ============================================================

@integrations_bp.route("/api/integrations/health")
@login_required
def integration_health():
    """
    Returns safe health status for all 5 integrations.
    Platform admins: all providers.
    Company admins/users: same statuses (platform-level connections).
    No secrets appear in response.
    """
    from services.integrations.health import check_all
    results = check_all()
    # Strip any 'detail' keys that might include sensitive path info for non-admins
    if not current_user.is_admin:
        results = {
            provider: {"status": info.get("status", "error")}
            for provider, info in results.items()
        }
    return jsonify(results)


@integrations_bp.route("/api/integrations/health/<provider>/test", methods=["POST"])
@login_required
def test_integration(provider):
    """Re-run health check for a single provider."""
    from services.integrations.health import check_one
    allowed = {"twilio", "github", "outlook", "airtable", "revenuecat"}
    if provider not in allowed:
        return jsonify({"error": "unknown provider"}), 400
    result = check_one(provider)
    if not current_user.is_admin:
        result = {"status": result.get("status", "error")}
    return jsonify(result)


# ============================================================
# Twilio endpoints
# ============================================================

@integrations_bp.route("/api/twilio/send-sms", methods=["POST"])
@login_required
def twilio_send_sms():
    data = request.get_json(silent=True) or {}
    to   = data.get("to", "").strip()
    body = data.get("body", "").strip()
    if not to or not body:
        return jsonify({"ok": False, "reason": "to and body are required"}), 400

    from services.integrations.twilio_service import send_sms
    result = send_sms(to, body, company_id=_company_id())
    code   = 200 if result.get("ok") else 422
    return jsonify(result), code


@integrations_bp.route("/api/twilio/inbound-sms", methods=["POST"])
def twilio_inbound_sms():
    """Public Twilio webhook — no auth (Twilio signs requests)."""
    from_number = request.form.get("From") or request.values.get("From", "")
    to_number   = request.form.get("To")   or request.values.get("To", "")
    body        = request.form.get("Body") or request.values.get("Body", "")

    from services.integrations.twilio_service import handle_inbound
    result = handle_inbound(from_number, body, to_number)

    reply = result.get("reply")
    if reply:
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Message>{reply}</Message></Response>'
        )
    else:
        twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

    from flask import Response
    return Response(twiml, mimetype="application/xml")


@integrations_bp.route("/api/twilio/voice/status", methods=["POST"])
def twilio_voice_status():
    """Twilio voice status callback — acknowledge only."""
    logger.info("Twilio voice status: %s", request.values.get("CallStatus"))
    return jsonify({"ok": True})


# ============================================================
# Outlook endpoints
# ============================================================

@integrations_bp.route("/api/outlook/send-email", methods=["POST"])
@login_required
def outlook_send_email():
    data     = request.get_json(silent=True) or {}
    to       = data.get("to", "").strip()
    subject  = data.get("subject", "").strip()
    html     = data.get("html_body", "")
    text     = data.get("text_body")

    if not to or not subject:
        return jsonify({"ok": False, "reason": "to and subject are required"}), 400

    from services.integrations.outlook_service import send_email
    result = send_email(to, subject, html, text, company_id=_company_id())
    return jsonify(result), 200 if result.get("ok") else 422


@integrations_bp.route("/api/outlook/messages/recent")
@login_required
def outlook_recent_messages():
    limit = min(int(request.args.get("limit", 20)), 50)
    from services.integrations.outlook_service import list_recent_emails
    result = list_recent_emails(limit=limit)
    return jsonify(result)


@integrations_bp.route("/api/outlook/calendar/events", methods=["GET"])
@login_required
def outlook_list_events():
    limit = min(int(request.args.get("limit", 20)), 50)
    from services.integrations.outlook_service import list_calendar_events
    result = list_calendar_events(limit=limit)
    return jsonify(result)


@integrations_bp.route("/api/outlook/calendar/events", methods=["POST"])
@login_required
def outlook_create_event():
    data      = request.get_json(silent=True) or {}
    subject   = data.get("subject", "").strip()
    start_dt  = data.get("start_dt", "").strip()
    end_dt    = data.get("end_dt", "").strip()
    body_html = data.get("body_html", "")
    attendees = data.get("attendees", [])
    location  = data.get("location")

    if not (subject and start_dt and end_dt):
        return jsonify({"ok": False, "reason": "subject, start_dt, end_dt required"}), 400

    from services.integrations.outlook_service import create_calendar_event
    result = create_calendar_event(
        subject, start_dt, end_dt,
        body_html=body_html,
        attendees=attendees,
        location=location,
        company_id=_company_id(),
    )
    return jsonify(result), 200 if result.get("ok") else 422


# ============================================================
# Airtable endpoints
# ============================================================

@integrations_bp.route("/api/airtable/records")
@login_required
def airtable_list_records():
    base_id    = request.args.get("base_id", "")
    table_name = request.args.get("table_name", "")
    filter_formula = request.args.get("filter_formula")
    if not base_id or not table_name:
        return jsonify({"ok": False, "reason": "base_id and table_name required"}), 400

    from services.integrations.airtable_service import list_records
    result = list_records(base_id, table_name, filter_formula=filter_formula)
    return jsonify(result)


@integrations_bp.route("/api/airtable/records", methods=["POST"])
@login_required
def airtable_create_record():
    data       = request.get_json(silent=True) or {}
    base_id    = data.get("base_id", "")
    table_name = data.get("table_name", "")
    fields     = data.get("fields", {})
    if not base_id or not table_name:
        return jsonify({"ok": False, "reason": "base_id and table_name required"}), 400

    from services.integrations.airtable_service import create_record
    result = create_record(base_id, table_name, fields, company_id=_company_id())
    return jsonify(result), 200 if result.get("ok") else 422


@integrations_bp.route("/api/airtable/records/<record_id>", methods=["PATCH"])
@login_required
def airtable_update_record(record_id):
    data       = request.get_json(silent=True) or {}
    base_id    = data.get("base_id", "")
    table_name = data.get("table_name", "")
    fields     = data.get("fields", {})
    if not base_id or not table_name:
        return jsonify({"ok": False, "reason": "base_id and table_name required"}), 400

    from services.integrations.airtable_service import update_record
    result = update_record(base_id, table_name, record_id, fields, company_id=_company_id())
    return jsonify(result), 200 if result.get("ok") else 422


@integrations_bp.route("/api/airtable/records/<record_id>", methods=["DELETE"])
@login_required
def airtable_delete_record(record_id):
    data       = request.get_json(silent=True) or {}
    base_id    = data.get("base_id", "")
    table_name = data.get("table_name", "")
    if not base_id or not table_name:
        return jsonify({"ok": False, "reason": "base_id and table_name required"}), 400

    from services.integrations.airtable_service import delete_record
    result = delete_record(base_id, table_name, record_id, company_id=_company_id())
    return jsonify(result), 200 if result.get("ok") else 422


# ============================================================
# Airtable — dedicated health endpoint
# ============================================================

@integrations_bp.route("/api/airtable/health")
@login_required
def airtable_health():
    from services.integrations.airtable_service import health_check
    result = health_check()
    code = 200 if result.get("status") == "connected" else 503
    return jsonify(result), code


# ============================================================
# Airtable — sync endpoints
# ============================================================

@integrations_bp.route("/api/airtable/sync/lead/<int:contact_id>", methods=["POST"])
@login_required
def airtable_sync_lead(contact_id):
    """Sync a single Contact (lead) to the Airtable Leads table."""
    cid = _company_id()
    from services.integrations.airtable_service import sync_lead_to_airtable
    result = sync_lead_to_airtable(contact_id, cid)
    return jsonify(result), 200 if result.get("ok") else 422


@integrations_bp.route("/api/airtable/sync/onboarding/<int:project_id>", methods=["POST"])
@login_required
def airtable_sync_onboarding(project_id):
    """Sync a CustomerOnboardingProject to the Airtable Onboarding Pipeline table."""
    cid = _company_id()
    from services.integrations.airtable_service import sync_onboarding_to_airtable
    result = sync_onboarding_to_airtable(project_id, cid)
    return jsonify(result), 200 if result.get("ok") else 422


@integrations_bp.route("/api/airtable/sync/support/<int:ticket_id>", methods=["POST"])
@login_required
def airtable_sync_support(ticket_id):
    """Sync a FeedbackTicket (support note) to the Airtable Support table."""
    cid = _company_id()
    from services.integrations.airtable_service import sync_support_note_to_airtable
    result = sync_support_note_to_airtable(ticket_id, cid)
    return jsonify(result), 200 if result.get("ok") else 422


@integrations_bp.route("/api/airtable/sync/logs")
@login_required
def airtable_sync_logs():
    """Return recent ExternalSyncRecord rows for this user's company."""
    cid         = _company_id()
    entity_type = request.args.get("entity_type")
    limit       = min(int(request.args.get("limit", 50)), 200)
    from services.integrations.airtable_service import get_sync_logs
    logs = get_sync_logs(company_id=cid, entity_type=entity_type, limit=limit)
    return jsonify({"logs": logs})


@integrations_bp.route("/api/airtable/sync/stats")
@login_required
def airtable_sync_stats():
    """Return counts of synced/failed/pending for this company."""
    cid = _company_id()
    from services.integrations.airtable_service import get_sync_stats
    return jsonify(get_sync_stats(company_id=cid))


# ============================================================
# Airtable — admin UI detail page
# ============================================================

@integrations_bp.route("/platform/integrations/airtable")
@login_required
def airtable_admin_panel():
    import os
    from services.integrations.airtable_service import get_sync_logs, get_sync_stats, health_check
    from models import IntegrationConnection

    # Live health check (quick)
    health = health_check()

    # Sync stats for current company
    cid   = _company_id()
    stats = get_sync_stats(company_id=cid)
    logs  = get_sync_logs(company_id=cid, limit=30)

    # Last connection record
    conn_row = IntegrationConnection.query.filter_by(
        provider="airtable", company_id=None
    ).first()

    tables_configured = {
        "Leads":          bool(os.environ.get("AIRTABLE_LEADS_TABLE")),
        "Onboarding":     bool(os.environ.get("AIRTABLE_ONBOARDING_TABLE")),
        "Implementation": bool(os.environ.get("AIRTABLE_IMPLEMENTATION_TABLE")),
        "Licenses":       bool(os.environ.get("AIRTABLE_LICENSES_TABLE")),
        "Support":        bool(os.environ.get("AIRTABLE_SUPPORT_TABLE")),
        "Sync Metadata":  bool(os.environ.get("AIRTABLE_SYNC_TABLE")),
    }

    return render_template(
        "platform/airtable.html",
        health=health,
        stats=stats,
        logs=logs,
        conn_row=conn_row,
        tables_configured=tables_configured,
        sync_enabled=os.environ.get("AIRTABLE_SYNC_ENABLED", "false").strip().lower() in ("true", "1"),
        is_platform_admin=current_user.is_admin,
    )


# ============================================================
# GitHub endpoints — platform admins only
# ============================================================

@integrations_bp.route("/api/github/repos")
@login_required
def github_list_repos():
    _require_platform_admin()
    from services.integrations.github_service import list_repos
    return jsonify(list_repos())


@integrations_bp.route("/api/github/<owner>/<repo>/issues")
@login_required
def github_list_issues(owner, repo):
    _require_platform_admin()
    state = request.args.get("state", "open")
    from services.integrations.github_service import list_issues
    return jsonify(list_issues(owner, repo, state=state))


@integrations_bp.route("/api/github/<owner>/<repo>/issues", methods=["POST"])
@login_required
def github_create_issue(owner, repo):
    _require_platform_admin()
    data   = request.get_json(silent=True) or {}
    title  = data.get("title", "").strip()
    body   = data.get("body", "").strip()
    labels = data.get("labels", [])
    if not title:
        return jsonify({"ok": False, "reason": "title required"}), 400

    from services.integrations.github_service import create_issue
    result = create_issue(owner, repo, title, body, labels=labels)
    return jsonify(result), 200 if result.get("ok") else 422


@integrations_bp.route("/api/github/<owner>/<repo>/pulls")
@login_required
def github_list_prs(owner, repo):
    _require_platform_admin()
    state = request.args.get("state", "open")
    from services.integrations.github_service import list_pull_requests
    return jsonify(list_pull_requests(owner, repo, state=state))


@integrations_bp.route("/api/github/<owner>/<repo>/latest-commit")
@login_required
def github_latest_commit(owner, repo):
    _require_platform_admin()
    from services.integrations.github_service import get_latest_commit
    return jsonify(get_latest_commit(owner, repo))


# ============================================================
# RevenueCat webhook
# ============================================================

@integrations_bp.route("/api/webhooks/revenuecat", methods=["POST"])
def revenuecat_webhook():
    """RevenueCat server notification webhook — no auth (validate in prod via shared secret)."""
    payload = request.get_json(silent=True) or {}
    from services.integrations.revenuecat_service import handle_webhook
    result = handle_webhook(payload)
    return jsonify(result), 200


# ============================================================
# API Hub — /platform/api-hub
# ============================================================

# Provider display metadata for the API Hub UI
_HUB_PROVIDERS = [
    # slug, display name, category, scope, icon-name, key fields (env var names)
    ("openai",       "OpenAI / GPT-4o",        "AI & Content",   "platform", "cpu",         ["OPENAI_API_KEY"]),
    ("twilio",       "Twilio SMS & Voice",      "SMS & Voice",    "platform", "phone",       ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"]),
    ("stripe",       "Stripe Billing",          "Payments",       "platform", "credit-card", ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET"]),
    ("smtp",         "SMTP Email",              "Email",          "platform", "mail",        ["SMTP_HOST", "SMTP_USER", "SMTP_PASS"]),
    ("mailgun",      "Mailgun",                 "Email",          "platform", "send",        ["MAILGUN_API_KEY", "MAILGUN_DOMAIN"]),
    ("ms365",        "Microsoft 365 / Outlook", "Email",          "platform", "cloud",       ["MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"]),
    ("facebook",     "Facebook",                "Social Media",   "platform", "thumbs-up",   ["FACEBOOK_APP_ID", "FACEBOOK_ACCESS_TOKEN"]),
    ("tiktok",       "TikTok",                  "Social Media",   "platform", "music",       ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"]),
    ("twitter",      "X (Twitter)",             "Social Media",   "platform", "twitter",     ["TWITTER_API_KEY", "TWITTER_BEARER_TOKEN"]),
    ("linkedin",     "LinkedIn",                "Social Media",   "platform", "linkedin",    ["LINKEDIN_CLIENT_ID", "LINKEDIN_ACCESS_TOKEN"]),
    ("youtube",      "YouTube",                 "Social Media",   "platform", "youtube",     ["YOUTUBE_API_KEY"]),
    ("reddit",       "Reddit",                  "Social Media",   "platform", "message-circle", ["REDDIT_CLIENT_ID"]),
    ("google_ads",   "Google Ads",              "Advertising",    "platform", "target",      ["GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID"]),
    ("posthog",      "PostHog Analytics",       "Analytics",      "platform", "bar-chart-2", ["POSTHOG_API_KEY"]),
    ("unsplash",     "Unsplash Images",         "Media",          "platform", "image",       ["UNSPLASH_ACCESS_KEY"]),
    ("pexels",       "Pexels Images",           "Media",          "platform", "image",       ["PEXELS_API_KEY"]),
    ("github",       "GitHub",                  "Developer",      "platform", "github",      ["GITHUB_PERSONAL_ACCESS_TOKEN"]),
    ("airtable",     "Airtable",                "Data Sync",      "platform", "grid",        ["AIRTABLE_API_KEY", "AIRTABLE_BASE_ID"]),
    ("revenuecat",   "RevenueCat",              "Payments",       "platform", "dollar-sign", ["REVENUECAT_WEBHOOK_SECRET"]),
    ("woocommerce",  "WooCommerce",             "E-Commerce",     "platform", "shopping-bag",["WOOCOMMERCE_URL", "WOOCOMMERCE_KEY"]),
]

# Providers that only platform admins can see (credentials visible only to platform_admin)
_PLATFORM_ONLY_PROVIDERS = {"stripe", "openai", "github", "posthog", "revenuecat"}

# Company-scoped providers — visible/manageable by company admins
# Each company can configure its own social tokens, email, and integration keys
_COMPANY_PROVIDERS = [
    # slug,              display name,            category,      scope,     icon,         env_keys (reference only; company stores in ProviderCredential scope=company)
    ("facebook_page",   "Facebook Page Token",   "Social Media", "company", "thumbs-up",  ["FACEBOOK_ACCESS_TOKEN"]),
    ("instagram_biz",   "Instagram Business",    "Social Media", "company", "camera",     ["INSTAGRAM_ACCESS_TOKEN"]),
    ("twitter_company", "X / Twitter",           "Social Media", "company", "twitter",    ["TWITTER_BEARER_TOKEN"]),
    ("linkedin_company","LinkedIn Company Page", "Social Media", "company", "linkedin",   ["LINKEDIN_ACCESS_TOKEN"]),
    ("youtube_company", "YouTube Channel",       "Social Media", "company", "youtube",    ["YOUTUBE_CHANNEL_API_KEY"]),
    ("mailchimp",       "Mailchimp",             "Email",        "company", "mail",       ["MAILCHIMP_API_KEY"]),
    ("zapier",          "Zapier Webhook",         "Automation",  "company", "zap",        ["ZAPIER_WEBHOOK_KEY"]),
    ("google_analytics","Google Analytics",      "Analytics",    "company", "bar-chart",  ["GA4_PROPERTY_ID"]),
]


def _build_provider_card(slug, display_name, category, scope, icon, env_keys, company_id=None):
    """Build a safe provider card dict (no raw secrets).

    company_id — when provided, filters ProviderCredential by company_id so that
    company-scoped cards show credentials stored for that specific company.
    """
    import os
    from models import ProviderCredential, ApiHubAuditLog
    from datetime import datetime, timezone

    q = ProviderCredential.query.filter_by(
        provider_slug=slug, scope=scope, is_active=True
    )
    if company_id is not None:
        q = q.filter_by(company_id=company_id)
    rows = q.all()
    db_keys = {r.key for r in rows}

    # Env metadata is visible ONLY to platform admins (company_id=None means platform view).
    # Company-admin views (company_id provided) must never surface platform env secrets.
    show_env = (company_id is None)

    # Resolve env values through the central resolver (DB-first, env fallback inside resolver)
    if show_env:
        try:
            from services.provider_config import get_provider_config as _gpc
            _env_vals = {k: _gpc(slug, scope, field=k, key=k) for k in env_keys}
        except Exception:
            _env_vals = {k: None for k in env_keys}
    else:
        _env_vals = {k: None for k in env_keys}

    env_present = {k: bool(_env_vals[k]) for k in env_keys}
    db_present  = {k: (k in db_keys) for k in env_keys}

    # Determine overall status
    any_key = env_keys[0] if env_keys else None
    main_row = next((r for r in rows if r.key == any_key), None) if any_key else None
    env_ok   = show_env and any(_env_vals[k] for k in env_keys)
    db_ok    = bool(rows)

    if db_ok:
        source = "db"
        status = "configured"
        last_tested = main_row.last_tested_at if main_row else None
        test_status = main_row.last_test_status if main_row else None
    elif env_ok:
        source = "env"
        status = "env_only"
        last_tested = None
        test_status = None
    else:
        source = "none"
        status = "missing"
        last_tested = None
        test_status = None

    # Masked key tails for display
    masked_fields = []
    for k in env_keys:
        row_for_key = next((r for r in rows if r.key == k), None)
        if row_for_key:
            masked_fields.append({"key": k, "masked": row_for_key.masked_value(), "source": "db"})
        elif show_env and _env_vals.get(k):
            v = _env_vals[k]
            tail = v[-4:] if len(v) > 4 else "****"
            masked_fields.append({"key": k, "masked": f"****{tail}", "source": "env"})
        else:
            masked_fields.append({"key": k, "masked": None, "source": "none"})

    return {
        "slug": slug,
        "display_name": display_name,
        "category": category,
        "scope": scope,
        "icon": icon,
        "status": status,
        "source": source,
        "last_tested": last_tested.isoformat() if last_tested else None,
        "test_status": test_status,
        "masked_fields": masked_fields,
        "env_keys": env_keys,
    }


@integrations_bp.route("/platform/api-hub")
@login_required
def api_hub():
    """
    API Hub — tiered credential management by role:

    platform_admin  → sees ALL platform-scoped providers (20 entries)
                       Full CRUD: test / save / rotate / delete / import-from-env
    company_admin   → sees only company-scoped providers (social tokens, email
                       keys, etc.) stored per company_id in ProviderCredential
                       Full CRUD within their company scope
    regular_user    → 403 (OAuth tokens managed via Settings)
    """
    if not current_user.is_authenticated:
        abort(403)

    is_platform_admin = bool(current_user.is_admin)
    cid = _company_id()

    # Determine role
    is_company_admin = False
    if not is_platform_admin and cid:
        from models import UserCompanyAccess
        uca = UserCompanyAccess.query.filter_by(
            user_id=current_user.id, company_id=cid
        ).first()
        is_company_admin = bool(uca and uca.can_admin())

    if not is_platform_admin and not is_company_admin:
        abort(403)

    # Select provider list based on role
    provider_list = _HUB_PROVIDERS if is_platform_admin else _COMPANY_PROVIDERS

    try:
        cards = []
        for slug, name, cat, scope, icon, env_keys in provider_list:
            # Platform-only providers are hidden from company admins
            if slug in _PLATFORM_ONLY_PROVIDERS and not is_platform_admin:
                continue
            try:
                # For company-scoped providers, pass company_id so _build_provider_card
                # looks up credentials scoped to this company
                card = _build_provider_card(slug, name, cat, scope, icon, env_keys,
                                            company_id=cid if not is_platform_admin else None)
                cards.append(card)
            except Exception as card_err:
                logger.warning("api_hub: card build error for %s: %s", slug, card_err)
                cards.append({
                    "slug": slug, "display_name": name, "category": cat,
                    "scope": scope, "icon": icon, "status": "error",
                    "source": "none", "last_tested": None, "test_status": None,
                    "masked_fields": [], "env_keys": env_keys,
                })
    except Exception as exc:
        logger.error("api_hub: failed to build cards: %s", exc)
        cards = []

    # Group by category
    categories = {}
    for card in cards:
        categories.setdefault(card["category"], []).append(card)

    # Audit log — platform admins see all; company admins see their company's entries
    recent_audit = []
    try:
        from models import ApiHubAuditLog
        q = ApiHubAuditLog.query
        if not is_platform_admin:
            q = q.filter_by(company_id=cid)
        recent_audit = q.order_by(ApiHubAuditLog.timestamp.desc()).limit(20).all()
    except Exception:
        pass

    from services.provider_config import write_audit_log
    _audit_scope = "platform" if is_platform_admin else "company"
    write_audit_log("*", "viewed_hub", _audit_scope, company_id=cid,
                    actor_user_id=current_user.id, result="ok")

    return render_template(
        "platform/api_hub.html",
        cards=cards,
        categories=categories,
        is_platform_admin=is_platform_admin,
        is_company_admin=is_company_admin,
        recent_audit=recent_audit,
    )


@integrations_bp.route("/api/api-hub/<slug>/test", methods=["POST"])
@login_required
def api_hub_test(slug):
    """
    Test connection for a provider.
    platform_admin → can test any platform-scoped provider
    company_admin  → can only test their company-scoped providers
    Returns only: status, latency_ms, safe detail string. Never raw keys.
    """
    import time

    is_platform_admin = bool(current_user.is_admin)
    cid = _company_id()

    is_company_admin = False
    if not is_platform_admin and cid:
        from models import UserCompanyAccess
        uca = UserCompanyAccess.query.filter_by(
            user_id=current_user.id, company_id=cid
        ).first()
        is_company_admin = bool(uca and uca.can_admin())

    if not is_platform_admin and not is_company_admin:
        abort(403)

    # Company admins may only test company-scoped providers
    platform_slugs = {p[0] for p in _HUB_PROVIDERS}
    if not is_platform_admin and slug in platform_slugs:
        abort(403)

    t0 = time.time()
    result = {"slug": slug, "status": "untested", "latency_ms": 0, "detail": ""}

    try:
        if slug == "openai":
            from services.provider_config import get_provider_config
            key = get_provider_config("openai", "platform")
            if not key:
                result.update(status="missing", detail="No OpenAI key configured")
            else:
                from openai import OpenAI
                client = OpenAI(api_key=key)
                client.models.list()
                result.update(status="connected", detail="OpenAI API reachable")

        elif slug == "twilio":
            from services.integrations.twilio_service import health_check
            hc = health_check()
            result.update(status=hc.get("status", "error"),
                          detail=hc.get("detail") or hc.get("account_name", ""))

        elif slug == "stripe":
            from services.provider_config import get_provider_config
            key = get_provider_config("stripe", "platform", "secret_key")
            if not key:
                result.update(status="missing", detail="No Stripe secret key configured")
            else:
                import stripe as stripe_sdk
                stripe_sdk.api_key = key
                stripe_sdk.Balance.retrieve()
                result.update(status="connected", detail="Stripe API reachable")

        elif slug in {"ms365", "outlook"}:
            from services.integrations.health import check_one
            hc = check_one("outlook")
            result.update(status=hc.get("status", "error"),
                          detail=hc.get("detail", ""))

        elif slug == "airtable":
            from services.integrations.health import check_one
            hc = check_one("airtable")
            result.update(status=hc.get("status", "error"),
                          detail=hc.get("detail", ""))

        elif slug == "github":
            from services.integrations.health import check_one
            hc = check_one("github")
            result.update(status=hc.get("status", "error"),
                          detail=hc.get("detail", ""))

        else:
            # Generic: check if ANY active credential exists for this provider+scope
            # (not limited to a single field name like "api_key" — providers like
            # SMTP, ms365, Twilio each use different field names)
            import os as _os
            _test_scope = "platform" if is_platform_admin else "company"
            _test_cid   = None if is_platform_admin else cid

            db_ok = False
            try:
                from models import ProviderCredential
                q = ProviderCredential.query.filter_by(
                    provider_slug=slug, scope=_test_scope, is_active=True
                )
                if _test_cid is not None:
                    q = q.filter_by(company_id=_test_cid)
                db_ok = q.first() is not None
            except Exception:
                db_ok = False

            env_ok = False
            if not db_ok:
                # Fallback: check via resolver (DB-first, env fallback inside resolver)
                all_providers = _HUB_PROVIDERS if is_platform_admin else _COMPANY_PROVIDERS
                for pslug, _n, _c, _sc, _ic, _env_keys in all_providers:
                    if pslug == slug:
                        try:
                            from services.provider_config import get_provider_config as _gpc2
                            env_ok = any(_gpc2(pslug, "platform", field=k, key=k) for k in _env_keys)
                        except Exception:
                            pass
                        break

            ok = db_ok or env_ok
            result.update(
                status="configured" if ok else "missing",
                detail=("DB credential found" if db_ok else ("Env key present" if env_ok else "No key found in DB or env")),
            )

        latency = round((time.time() - t0) * 1000)
        result["latency_ms"] = latency

        # Update last_tested_at on the credential row (scope-aware)
        _credential_scope = "platform" if is_platform_admin else "company"
        _credential_cid   = None if is_platform_admin else cid
        try:
            from models import ProviderCredential
            from extensions import db
            from datetime import datetime, timezone
            q = ProviderCredential.query.filter_by(
                provider_slug=slug, scope=_credential_scope, is_active=True
            )
            if _credential_cid is not None:
                q = q.filter_by(company_id=_credential_cid)
            rows = q.all()
            for row in rows:
                row.last_tested_at   = datetime.now(timezone.utc)
                row.last_test_status = result["status"]
            if rows:
                db.session.commit()
        except Exception:
            pass

        # Audit log — success path
        from services.provider_config import write_audit_log
        write_audit_log(slug, "tested", _credential_scope, company_id=cid,
                        actor_user_id=current_user.id,
                        result=result["status"],
                        notes=f"latency={latency}ms")

    except Exception as exc:
        latency = round((time.time() - t0) * 1000)
        result.update(status="error", latency_ms=latency,
                      detail=str(exc)[:200])
        logger.warning("api_hub_test %s error: %s", slug, exc)
        # Audit log — failure path (guaranteed regardless of where exception arose)
        try:
            from services.provider_config import write_audit_log
            _exc_scope = "platform" if is_platform_admin else "company"
            write_audit_log(slug, "tested", _exc_scope, company_id=cid,
                            actor_user_id=current_user.id,
                            result="error", notes=f"exception: {type(exc).__name__}")
        except Exception:
            pass

    # Sanitize detail before returning — never expose internal exception text
    _safe_detail = result.get("detail", "")
    # Strip stack-trace-like content; keep short human-readable messages only
    if len(_safe_detail) > 80 or "\n" in _safe_detail or "Traceback" in _safe_detail:
        _safe_detail = "Connection check failed — see server logs for details"

    return jsonify({
        "slug": result["slug"],
        "status": result["status"],
        "latency_ms": result["latency_ms"],
        "detail": _safe_detail,
    })


@integrations_bp.route("/api/api-hub/<slug>/save", methods=["POST"])
@login_required
def api_hub_save(slug):
    """
    Create or rotate a credential for *slug*.

    platform_admin → may save any platform-scoped credential (company_id=None)
    company_admin  → may save company-scoped credentials for their own company

    Expects JSON body:
        { "key": "ENV_VAR_NAME", "value": "plaintext_secret_here" }

    The value is encrypted before storage.  The raw value is NEVER logged or
    returned.  Only the masked tail and the action taken are returned.
    """
    is_platform_admin = bool(current_user.is_admin)
    cid = _company_id()

    # Resolve caller role
    is_company_admin = False
    if not is_platform_admin and cid:
        from models import UserCompanyAccess
        uca = UserCompanyAccess.query.filter_by(
            user_id=current_user.id, company_id=cid
        ).first()
        is_company_admin = bool(uca and uca.can_admin())

    if not is_platform_admin and not is_company_admin:
        abort(403)

    body = request.get_json(silent=True) or {}
    key   = (body.get("key")   or "").strip()
    value = (body.get("value") or "").strip()

    if not key or not value:
        return jsonify({"ok": False, "error": "key and value are required"}), 400

    # Resolve provider-declared scope from metadata (not from caller role)
    all_providers = list(_HUB_PROVIDERS) + list(_COMPANY_PROVIDERS)
    provider_scope = next((sc for pslug, _n, _c, sc, _ic, _ek in all_providers if pslug == slug), None)
    if provider_scope is None:
        return jsonify({"ok": False, "error": "unknown provider"}), 400

    # Authorization: company admins may only save company-scoped providers
    if not is_platform_admin and provider_scope != "company":
        abort(403)

    credential_scope = provider_scope
    save_company_id  = cid if credential_scope == "company" else None

    from services.provider_config import save_provider_credential, mask_secret
    ok, action_or_err = save_provider_credential(
        provider       = slug,
        scope          = credential_scope,
        key            = key,
        plaintext_value= value,
        company_id     = save_company_id,
        actor_user_id  = current_user.id,
        source         = "manual",
        notes          = f"saved via API Hub UI by user_id={current_user.id}",
    )

    if not ok:
        return jsonify({"ok": False, "error": action_or_err}), 500

    return jsonify({
        "ok":     True,
        "action": action_or_err,  # "created" or "updated"
        "masked": mask_secret(value),
    })


@integrations_bp.route("/api/api-hub/<slug>/delete", methods=["POST"])
@login_required
def api_hub_delete(slug):
    """
    Soft-delete (deactivate) a credential entry.

    platform_admin → can delete any platform-scoped credential
    company_admin  → can delete only their company's company-scoped credentials

    Expects JSON body:
        { "key": "ENV_VAR_NAME" }

    This never deletes the actual encrypted row — it sets is_active=False so
    the audit trail is preserved.  The env fallback will continue to work.
    """
    is_platform_admin = bool(current_user.is_admin)
    cid = _company_id()

    is_company_admin = False
    if not is_platform_admin and cid:
        from models import UserCompanyAccess
        uca = UserCompanyAccess.query.filter_by(
            user_id=current_user.id, company_id=cid
        ).first()
        is_company_admin = bool(uca and uca.can_admin())

    if not is_platform_admin and not is_company_admin:
        abort(403)

    # Resolve provider-declared scope from metadata (not from caller role)
    all_providers_del = list(_HUB_PROVIDERS) + list(_COMPANY_PROVIDERS)
    provider_scope_del = next((sc for pslug, _n, _c, sc, _ic, _ek in all_providers_del if pslug == slug), None)
    if provider_scope_del is None:
        return jsonify({"ok": False, "error": "unknown provider"}), 400

    # Company admins may only delete company-scoped providers
    if not is_platform_admin and provider_scope_del != "company":
        abort(403)

    body = request.get_json(silent=True) or {}
    key  = (body.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "key is required"}), 400

    credential_scope  = provider_scope_del
    delete_company_id = cid if credential_scope == "company" else None

    from services.provider_config import delete_provider_credential
    ok, msg = delete_provider_credential(
        provider      = slug,
        scope         = credential_scope,
        key           = key,
        company_id    = delete_company_id,
        actor_user_id = current_user.id,
    )

    if not ok:
        return jsonify({"ok": False, "error": msg}), 404 if "not found" in msg else 500

    return jsonify({"ok": True, "message": msg})


@integrations_bp.route("/api/api-hub/import-from-env", methods=["POST"])
@login_required
def api_hub_import_from_env():
    """
    Trigger the backfill script programmatically (platform admin only).
    Imports all env-based credentials into the DB — idempotent.
    Returns counts of created/skipped rows.
    Audit logs an 'imported' action per provider.
    """
    if not current_user.is_admin:
        abort(403)

    import os
    from services.secret_vault import vault
    from models import ProviderCredential, ApiHubAuditLog
    from extensions import db
    from services.provider_config import write_audit_log

    created = 0
    skipped = 0
    errors  = []

    for slug, _name, _cat, scope, _icon, env_keys in _HUB_PROVIDERS:
        for env_key in env_keys:
            try:
                from services.provider_config import get_provider_config as _gpc_imp
                val = _gpc_imp(slug, scope, field=env_key, key=env_key)
            except Exception:
                val = None
            if not val:
                skipped += 1
                continue
            try:
                existing = ProviderCredential.query.filter_by(
                    provider_slug=slug, scope=scope, key=env_key,
                    company_id=None, is_active=True,
                ).first()
                if existing:
                    skipped += 1
                    continue
                encrypted = vault.encrypt(val)
                row = ProviderCredential(
                    provider_slug   = slug,
                    scope           = scope,
                    key             = env_key,
                    company_id      = None,
                    encrypted_value = encrypted,
                    source          = "env",
                    is_active       = True,
                    audit_notes     = "imported via API Hub UI",
                )
                db.session.add(row)
                created += 1
                # Per-credential audit row — written before commit so failures are traceable
                write_audit_log(slug, "imported", scope,
                                actor_user_id=current_user.id,
                                result="ok",
                                notes=f"key={env_key} source=env")
            except Exception as e:
                errors.append(f"{slug}/{env_key}: {str(e)[:100]}")
                write_audit_log(slug, "imported", scope,
                                actor_user_id=current_user.id,
                                result="error",
                                notes=f"key={env_key} error={str(e)[:120]}")

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

    # Per-credential audit rows are written inside the loop above (one per imported key).
    # Write a summary row so the aggregate is also queryable.
    write_audit_log("*", "import_summary", "platform", actor_user_id=current_user.id,
                    result="ok",
                    notes=f"created={created} skipped={skipped} errors={len(errors)}")

    return jsonify({
        "ok": True,
        "created": created,
        "skipped": skipped,
        "errors":  errors[:10],
    })


# ============================================================
# Admin UI — /platform/integrations
# ============================================================

@integrations_bp.route("/platform/integrations")
@login_required
def platform_integrations():
    from models import IntegrationConnection
    # Pull last-known statuses from DB (fast — no live network call)
    rows = IntegrationConnection.query.filter_by(company_id=None).all()
    statuses = {r.provider: r for r in rows}

    providers = [
        {
            "slug": "twilio",
            "name": "Twilio",
            "icon": "phone",
            "description": "SMS sending, inbound SMS handling, opt-in/out consent management, and voice routing.",
            "use_cases": ["Campaign SMS", "Inbound lead capture", "STOP/START handling"],
        },
        {
            "slug": "github",
            "name": "GitHub",
            "icon": "github",
            "description": "Repository status, deployment visibility, issue tracking, release notes, and feature requests.",
            "use_cases": ["Create issues from bug reports", "Track releases", "Platform Console status"],
            "platform_only": True,
        },
        {
            "slug": "outlook",
            "name": "Microsoft Outlook",
            "icon": "mail",
            "description": "Transactional email delivery, calendar events, onboarding reminders, and implementation calls.",
            "use_cases": ["Onboarding emails", "Meeting invites", "Billing reminders"],
        },
        {
            "slug": "revenuecat",
            "name": "RevenueCat",
            "icon": "credit-card",
            "description": "Plan entitlement checks, subscription access control, and upgrade/paywall prompts.",
            "use_cases": ["Gate features by plan", "Subscription status", "Paywall prompts"],
        },
        {
            "slug": "airtable",
            "name": "Airtable",
            "icon": "grid",
            "description": "Optional external sync for leads, onboarding pipeline, and licensing data.",
            "use_cases": ["Lead sync", "Onboarding checklist", "Customer success notes"],
        },
    ]

    for p in providers:
        row = statuses.get(p["slug"])
        p["status"]          = row.status         if row else "unknown"
        p["last_tested_at"]  = row.last_tested_at  if row else None
        p["last_success_at"] = row.last_success_at if row else None
        p["last_error"]      = (row.last_error or "")[:120] if (row and current_user.is_admin) else ""
        p["enabled"]         = row.enabled         if row else True

    return render_template(
        "platform/integrations.html",
        providers=providers,
        is_platform_admin=current_user.is_admin,
    )
