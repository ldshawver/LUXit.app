"""Workflow builder routes (M5).

Managing automations needs admin rights in the workspace; approving a held
message needs edit rights; reading needs membership.
"""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from extensions import db
from services import automation_builder as builder
from services import crm_automation as engine
from services.tenant_scope import current_company_id

automation_builder_bp = Blueprint("automation_builder", __name__)

CATALOG = {
    "triggers": {
        engine.INBOUND_SMS_TRIGGER: "A text message arrives",
        engine.MISSED_CALL_TRIGGER: "A call is missed",
        engine.OPPORTUNITY_CREATED_TRIGGER: "An opportunity is created",
        engine.STAGE_CHANGED_TRIGGER: "An opportunity changes stage",
        engine.TASK_COMPLETED_TRIGGER: "A follow-up task is completed",
        engine.INACTIVE_TRIGGER: "A contact has been quiet for a number of days (checked daily)",
        engine.TAG_ADDED_TRIGGER: "A tag is added",
    },
    "conditions": {
        "contact_tags_any": "Contact has any of these tags (list)",
        "segment_ids_any": "Contact is in any of these segments (ids)",
        "lifecycle_stage": "Lifecycle stage is one of (list)",
        "owner_user_id": "Contact owner is (user id)",
        "consent": "sms_allowed | email_allowed | contactable",
        "business_hours": "inside | outside",
        "opportunity_stage": "Opportunity stage is one of (list)",
        "from_stage": "Stage changed from (list; stage-changed trigger only)",
        "to_stage": "Stage changed to (list; stage-changed trigger only)",
        "no_communication_days": "No communication for at least N days",
        "cooldown_days": "This automation hasn't run for the contact in N days",
        "no_open_tasks": "Contact has no open tasks (true)",
    },
    "actions": {
        "task.create": "Create a task {title, due_in_hours?, priority?, assign_to: owner|unassigned}",
        "follow_up.create": "Create a follow-up {title, due_in_days?}",
        "note.add": "Add an internal note {text}",
        "opportunity.update": "Update the event's opportunity {stage?, next_action?}",
        "message.send": "Draft a text/email for approval {channel: sms|email, body, approval: required}",
    },
    "placeholders": ["{first_name}", "{name}"],
}


def _company_id():
    company_id = current_company_id()
    if company_id is None or not (current_user.is_admin or current_user.get_company_access(company_id)):
        abort(404)
    return company_id


def _payload():
    return request.get_json(silent=True) or {}


def _err(exc, status=400):
    return jsonify({"success": False, "error": str(exc)}), status


def _forbidden():
    return jsonify({"success": False, "error": "You do not have permission to manage automations."}), 403


@automation_builder_bp.route("/automations/workflows")
@login_required
def workflows_page():
    company_id = _company_id()
    return render_template("automations/workflows.html", catalog=CATALOG,
                           can_manage=current_user.can_admin_company(company_id),
                           can_approve=current_user.can_edit_company(company_id))


@automation_builder_bp.route("/api/automations/catalog")
@login_required
def catalog():
    _company_id()
    return jsonify({"success": True, **CATALOG})


@automation_builder_bp.route("/api/automations")
@login_required
def list_rules():
    return jsonify({"success": True, "rules": builder.list_rules(_company_id())})


@automation_builder_bp.route("/api/automations", methods=["POST"])
@automation_builder_bp.route("/api/automations/<int:rule_id>", methods=["POST", "PATCH"])
@login_required
def save_rule(rule_id=None):
    company_id = _company_id()
    if not current_user.can_admin_company(company_id):
        return _forbidden()
    rule = None
    if rule_id is not None:
        rule = builder.get_rule(company_id, rule_id)
        if rule is None:
            abort(404)
    try:
        rule = builder.save_rule(company_id, _payload(), rule)
    except builder.BuilderError as exc:
        db.session.rollback()
        return _err(exc)
    db.session.commit()
    return jsonify({"success": True, "rule": builder.rule_dict(rule)}), 201 if rule_id is None else 200


@automation_builder_bp.route("/api/automations/dry-run", methods=["POST"])
@login_required
def dry_run():
    company_id = _company_id()
    data = _payload()
    definition = data.get("rule")
    if data.get("rule_id"):
        rule = builder.get_rule(company_id, int(data["rule_id"]))
        if rule is None:
            abort(404)
        # Unsaved edits win; the saved rule supplies cooldown history.
        definition = definition or {"triggers": rule.triggers, "conditions": rule.conditions,
                                    "actions": rule.actions, "match_mode": rule.match_mode}
    definition = definition or {}
    event = dict(data.get("event") or {})
    if data.get("rule_id"):
        event["rule_id"] = int(data["rule_id"])
    try:
        result = builder.dry_run(company_id, definition, data.get("contact_id"), event)
    except builder.BuilderError as exc:
        return _err(exc)
    finally:
        db.session.rollback()  # a preview never writes
    return jsonify({"success": True, **result})


@automation_builder_bp.route("/api/automations/history")
@login_required
def history():
    company_id = _company_id()
    return jsonify({"success": True, "executions": builder.history(
        company_id, rule_id=request.args.get("rule_id", type=int), status=request.args.get("status"),
        limit=request.args.get("limit", 100, type=int))})


@automation_builder_bp.route("/api/automations/failures")
@login_required
def failures():
    return jsonify({"success": True, **builder.failure_summary(_company_id())})


@automation_builder_bp.route("/api/automations/approvals/<int:execution_id>", methods=["POST"])
@login_required
def decide(execution_id):
    company_id = _company_id()
    if not current_user.can_edit_company(company_id):
        return _forbidden()
    decision = _payload().get("decision")
    if decision not in {"approve", "reject"}:
        return _err("decision must be approve or reject")
    try:
        ex = builder.decide_message(company_id, execution_id, current_user, decision == "approve")
    except builder.BuilderError as exc:
        db.session.rollback()
        return _err(exc, 404 if str(exc) == "Not found." else 409)
    db.session.commit()
    return jsonify({"success": True, "status": ex.status, "error": (ex.details or {}).get("error")})
