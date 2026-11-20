"""Account status API (M7): plan, limits and feature switches for the current workspace.

Any member may read it (the phone app and the web app hide what the plan
doesn't include); billing changes stay on the owner/admin billing routes.
"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify
from flask_login import current_user, login_required

from extensions import db
from services.tenant_scope import current_company_id

account_bp = Blueprint("account", __name__)


@account_bp.route("/api/account/entitlements")
@login_required
def api_entitlements():
    from models import Company
    from services.account_status import account_status
    company_id = current_company_id()
    if company_id is None or not (current_user.is_admin or current_user.get_company_access(company_id)):
        abort(404)
    company = db.session.get(Company, company_id)
    if company is None:
        abort(404)
    return jsonify({"success": True, **account_status(company, current_user)})


def _current_workspace_or_404():
    from models import Company
    company_id = current_company_id()
    company = db.session.get(Company, company_id) if company_id is not None else None
    if company is None or not (current_user.is_admin or current_user.get_company_access(company_id)):
        abort(404)
    return company


@account_bp.route("/api/account/export")
@login_required
def api_export():
    """Owner/admin download of everything this workspace holds (ZIP of JSON lines)."""
    import tempfile

    from flask import send_file

    from saas_mgmt import _user_can_manage_billing
    from services import security_audit
    from services.workspace_lifecycle import write_export

    company = _current_workspace_or_404()
    if not _user_can_manage_billing(current_user, company):
        security_audit.record("account.data_export", outcome="denied", company_id=company.id)
        return jsonify({"success": False, "error": "Only workspace owners and admins can export data."}), 403
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    manifest = write_export(company.id, spool)
    spool.seek(0)
    security_audit.record("account.data_export", company_id=company.id,
                          detail={"fields": sorted(manifest["tables"])[:20]})
    stamp = manifest["generated_at"][:10]
    return send_file(spool, mimetype="application/zip", as_attachment=True,
                     download_name=f"luxit-workspace-{company.id}-{stamp}.zip", max_age=0)


@account_bp.route("/api/account/close", methods=["POST"])
@login_required
def api_close():
    """Owner closes (cancels) the workspace. JSON: {confirm_name, reason?}."""
    from flask import request

    from services import security_audit
    from services.workspace_lifecycle import ClosureError, can_close, close_workspace

    company = _current_workspace_or_404()
    if not can_close(current_user, company.id):
        security_audit.record("account.closed", outcome="denied", company_id=company.id)
        return jsonify({"success": False, "error": "Only the workspace owner can close it."}), 403
    body = request.get_json(silent=True) or request.form
    try:
        result = close_workspace(company, current_user, confirm_name=body.get("confirm_name", ""),
                                 reason=body.get("reason"))
    except ClosureError as exc:
        db.session.rollback()
        return jsonify({"success": False, "code": exc.code, "error": exc.message}), exc.status
    security_audit.record("account.closed", company_id=company.id, detail={"reason": (body.get("reason") or "")[:60] or None,
                                  "purge_after": result["purge_after"]})
    return jsonify({"success": True, **result})


@account_bp.route("/account/closed")
@login_required
def closed_page():
    from flask import render_template_string
    has_other = bool(current_user.default_company_id)
    return render_template_string(_CLOSED_HTML, has_other=has_other), 200


_CLOSED_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Workspace closed</title>
<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;line-height:1.5}
a{color:#4f46e5}</style></head><body>
<h1>This workspace has been closed</h1>
<p>Its owner closed the workspace. Its data is kept for a limited time and then deleted.
If this was a mistake, the owner can contact support to reopen it before then.</p>
{% if has_other %}<p><a href="/dashboard">Continue to your other workspace</a></p>{% endif %}
<p><a href="/auth/logout">Sign out</a></p>
</body></html>"""


# ── bring-your-own Twilio (T-32) ───────────────────────────────────────────── #

def _comms_admin_workspace():
    from saas_mgmt import _user_can_manage_billing
    company = _current_workspace_or_404()
    if not _user_can_manage_billing(current_user, company):
        abort(403)
    return company


def _connect_call(fn, *args, audit=None, **kwargs):
    from services import security_audit
    from services.twilio_connect import ConnectError
    try:
        result = fn(*args, **kwargs)
    except ConnectError as exc:
        db.session.rollback()
        if audit:
            security_audit.record(audit[0], outcome="denied", company_id=audit[1], detail={"reason": exc.code})
        return jsonify({"success": False, "code": exc.code, "error": exc.message}), exc.status
    if audit:
        security_audit.record(audit[0], company_id=audit[1])
    return jsonify({"success": True, **(result if isinstance(result, dict) else {"numbers": result})})


@account_bp.route("/api/comms/twilio")
@login_required
def api_twilio_status():
    from services.twilio_connect import status
    return jsonify({"success": True, **status(_comms_admin_workspace())})


@account_bp.route("/api/comms/twilio/connect", methods=["POST"])
@login_required
def api_twilio_connect():
    from flask import request

    from services.twilio_connect import connect
    company = _comms_admin_workspace()
    body = request.get_json(silent=True) or {}
    return _connect_call(connect, company, current_user, body.get("account_sid"), body.get("auth_token"),
                         audit=("comms.twilio.connected", company.id))


@account_bp.route("/api/comms/twilio/numbers")
@login_required
def api_twilio_numbers():
    from services.twilio_connect import available_numbers
    return _connect_call(available_numbers, _comms_admin_workspace())


@account_bp.route("/api/comms/twilio/numbers/attach", methods=["POST"])
@login_required
def api_twilio_attach():
    from flask import request

    from services.twilio_connect import attach_number
    company = _comms_admin_workspace()
    body = request.get_json(silent=True) or {}
    return _connect_call(attach_number, company, body.get("number_sid"),
                         audit=("comms.twilio.number_attached", company.id))


@account_bp.route("/api/comms/twilio/disconnect", methods=["POST"])
@login_required
def api_twilio_disconnect():
    from services.twilio_connect import disconnect
    company = _comms_admin_workspace()
    return _connect_call(disconnect, company, current_user, audit=("comms.twilio.disconnected", company.id))
