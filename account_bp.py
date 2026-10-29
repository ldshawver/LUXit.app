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
