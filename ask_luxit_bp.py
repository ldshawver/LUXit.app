"""Ask LUXit routes (M6): a question box over the caller's own workspace.

Membership is enough to ask (viewers included); answers only ever read the
caller's current workspace.
"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from extensions import limiter
from services import ask_luxit
from services.tenant_scope import current_company_id

ask_luxit_bp = Blueprint("ask_luxit", __name__)

EXAMPLES = (
    "Which texts are waiting for a reply?",
    "What are my overdue tasks?",
    "Show open deals in proposal",
    "Who haven't we contacted in 60 days?",
    "What commitments do we owe?",
)


def _company_id():
    company_id = current_company_id()
    if company_id is None or not (current_user.is_admin or current_user.get_company_access(company_id)):
        abort(404)
    return company_id


@ask_luxit_bp.route("/ask")
@login_required
def ask_page():
    _company_id()
    return render_template("ask/ask.html", examples=EXAMPLES)


@ask_luxit_bp.route("/api/ask", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def api_ask():
    company_id = _company_id()
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        result = ask_luxit.ask(company_id, current_user, data.get("question"))
    except ask_luxit.AskError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, **result})
