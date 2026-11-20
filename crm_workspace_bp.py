"""Actionable CRM routes (M3): today view, pipeline, opportunities, tasks.

Every route is scoped to the caller's workspace (``current_company_id``);
reads need membership, writes need edit rights (a task's assignee may also
complete it).
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from services import crm_workspace as crm
from services.tenant_scope import current_company_id

crm_workspace_bp = Blueprint("crm_workspace", __name__)


def _company_id():
    company_id = current_company_id()
    if company_id is None or not (current_user.is_admin or current_user.get_company_access(company_id)):
        abort(404)
    return company_id


def _can_edit(company_id):
    return current_user.can_edit_company(company_id)


def _payload():
    return request.get_json(silent=True) or request.form.to_dict()


def _err(exc, status=400):
    return jsonify({"success": False, "error": str(exc)}), status


def _forbidden():
    return jsonify({"success": False, "error": "You do not have permission to change CRM records."}), 403


def _wants_json():
    return request.is_json or request.accept_mimetypes.best == "application/json"


@crm_workspace_bp.route("/crm/today")
@login_required
def today():
    company_id = _company_id()
    mine = request.args.get("scope") != "all"
    data = crm.dashboard(company_id, user_id=current_user.id if mine else None)
    return render_template("crm/today.html", data=data, mine=mine, can_edit=_can_edit(company_id),
                           stages=crm.stages_for(crm.DEFAULT_PIPELINE))


@crm_workspace_bp.route("/api/crm/dashboard")
@login_required
def api_dashboard():
    company_id = _company_id()
    user_id = None if request.args.get("scope") == "all" else current_user.id
    return jsonify({"success": True, **crm.dashboard(company_id, user_id=user_id)})


@crm_workspace_bp.route("/api/crm/pipeline")
@login_required
def api_pipeline():
    company_id = _company_id()
    pipeline = request.args.get("pipeline") or crm.DEFAULT_PIPELINE
    if pipeline not in crm.PIPELINE_STAGES:
        return _err("Unknown pipeline.", 404)
    return jsonify({"success": True, **crm.pipeline_board(company_id, pipeline,
                                                         owner_user_id=request.args.get("owner_user_id", type=int))})


@crm_workspace_bp.route("/api/crm/opportunities", methods=["POST"])
@login_required
def api_create_opportunity():
    company_id = _company_id()
    if not _can_edit(company_id):
        return _forbidden()
    try:
        opp = crm.create_opportunity(company_id, current_user, _payload())
    except crm.CRMError as exc:
        db.session.rollback()
        return _err(exc)
    db.session.commit()
    return jsonify({"success": True, "opportunity_id": opp.id, "stage": opp.stage}), 201


@crm_workspace_bp.route("/api/crm/opportunities/<int:opp_id>", methods=["POST", "PATCH"])
@login_required
def api_update_opportunity(opp_id):
    from models import Opportunity
    company_id = _company_id()
    opp = Opportunity.query.filter_by(id=opp_id, company_id=company_id).first()
    if opp is None:
        return _err("Not found.", 404)
    if not _can_edit(company_id):
        return _forbidden()
    try:
        crm.update_opportunity(opp, current_user, _payload())
    except crm.CRMError as exc:
        db.session.rollback()
        if not _wants_json():
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("crm_workspace.today"))
        return _err(exc)
    db.session.commit()
    if not _wants_json():
        return redirect(request.referrer or url_for("crm_workspace.today"))
    return jsonify({"success": True, "stage": opp.stage, "status": opp.status})


@crm_workspace_bp.route("/api/crm/tasks")
@login_required
def api_list_tasks():
    company_id = _company_id()
    bucket = request.args.get("bucket", "open")
    if bucket not in {"open", "overdue", "today", "upcoming", "done"}:
        return _err("Unknown bucket.")
    assigned = None if request.args.get("scope") == "all" else current_user.id
    return jsonify({"success": True, "tasks": crm.list_tasks(company_id, assigned_user_id=assigned, bucket=bucket)})


@crm_workspace_bp.route("/api/crm/tasks", methods=["POST"])
@login_required
def api_create_task():
    company_id = _company_id()
    if not _can_edit(company_id):
        return _forbidden()
    try:
        task = crm.create_task(company_id, current_user, _payload())
    except crm.CRMError as exc:
        db.session.rollback()
        return _err(exc)
    db.session.commit()
    return jsonify({"success": True, "task_id": task.id}), 201


def _task_or_404(company_id, task_id):
    from models import ContactTask
    task = ContactTask.query.filter_by(id=task_id, company_id=company_id).first()
    if task is None:
        abort(404)
    return task


@crm_workspace_bp.route("/api/crm/tasks/<int:task_id>", methods=["POST", "PATCH"])
@login_required
def api_update_task(task_id):
    company_id = _company_id()
    task = _task_or_404(company_id, task_id)
    if not _can_edit(company_id):
        return _forbidden()
    try:
        crm.update_task(task, current_user, _payload())
    except crm.CRMError as exc:
        db.session.rollback()
        return _err(exc)
    db.session.commit()
    return jsonify({"success": True})


@crm_workspace_bp.route("/api/crm/tasks/<int:task_id>/<action>", methods=["POST"])
@login_required
def api_task_action(task_id, action):
    company_id = _company_id()
    task = _task_or_404(company_id, task_id)
    if action not in {"complete", "reopen"}:
        abort(404)
    if not (_can_edit(company_id) or task.assigned_user_id == current_user.id):
        return _forbidden()
    (crm.complete_task if action == "complete" else crm.reopen_task)(task, current_user)
    db.session.commit()
    if not _wants_json():
        return redirect(request.referrer or url_for("crm_workspace.today"))
    return jsonify({"success": True, "status": task.status})
