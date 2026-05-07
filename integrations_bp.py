"""
Integration Blueprint — /api/integrations/* and /platform/integrations

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

  GET  /api/airtable/records
  POST /api/airtable/records
  PATCH /api/airtable/records/<record_id>
  DELETE /api/airtable/records/<record_id>

  GET  /api/github/repos
  GET  /api/github/<owner>/<repo>/issues
  POST /api/github/<owner>/<repo>/issues
  GET  /api/github/<owner>/<repo>/pulls
  GET  /api/github/<owner>/<repo>/latest-commit

  POST /api/webhooks/revenuecat

  GET  /platform/integrations  (admin UI)
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
