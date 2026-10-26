"""Unified communications routes (M4): identities, send, email log, caller context.

Scoped to the caller's workspace. Identities need admin rights; sending and
logging need edit rights; caller context needs membership.
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, jsonify, request
from flask_login import current_user, login_required

from extensions import db, limiter
from services import unified_comms as comms
from services.tenant_scope import current_company_id

unified_comms_bp = Blueprint("unified_comms", __name__)

_REFUSAL_STATUS = {"invalid": 400, "not_configured": 409, "no_conversation": 409, "conflict": 409}


def _company_id():
    company_id = current_company_id()
    if company_id is None or not (current_user.is_admin or current_user.get_company_access(company_id)):
        abort(404)
    return company_id


def _payload():
    return request.get_json(silent=True) or request.form.to_dict()


def _refused(exc):
    return jsonify({"success": False, "code": exc.code, "error": str(exc)}), _REFUSAL_STATUS.get(exc.code, 422)


def _forbidden():
    return jsonify({"success": False, "error": "You do not have permission to do that."}), 403


@unified_comms_bp.route("/api/comms/identities")
@login_required
def list_identities():
    from models import BusinessIdentity
    company_id = _company_id()
    rows = BusinessIdentity.query.filter_by(company_id=company_id).order_by(BusinessIdentity.name).all()
    return jsonify({"success": True, "identities": [comms.identity_dict(i) for i in rows]})


@unified_comms_bp.route("/api/comms/identities", methods=["POST"])
@unified_comms_bp.route("/api/comms/identities/<int:identity_id>", methods=["POST", "PATCH"])
@login_required
def save_identity(identity_id=None):
    from models import BusinessIdentity
    company_id = _company_id()
    if not current_user.can_admin_company(company_id):
        return _forbidden()
    identity = None
    if identity_id is not None:
        identity = BusinessIdentity.query.filter_by(id=identity_id, company_id=company_id).first()
        if identity is None:
            abort(404)
    try:
        identity = comms.save_identity(company_id, _payload(), identity)
    except comms.CommsError as exc:
        db.session.rollback()
        return _refused(exc)
    db.session.commit()
    return jsonify({"success": True, "identity": comms.identity_dict(identity)}), 201 if identity_id is None else 200


@unified_comms_bp.route("/api/contacts/<int:contact_id>/send", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def send_to_contact(contact_id):
    from services.contact_timeline import canonical_contact
    company_id = _company_id()
    if not current_user.can_edit_company(company_id):
        return _forbidden()
    contact = canonical_contact(company_id, contact_id)
    if contact is None:
        abort(404)
    data = _payload()
    try:
        result = comms.send_to_contact(
            company_id, contact, channel=data.get("channel"), body=data.get("body"), user=current_user,
            identity_id=data.get("identity_id"), subject=data.get("subject"),
            purpose=data.get("purpose") or "conversational",
            idempotency_key=request.headers.get("Idempotency-Key") or data.get("idempotency_key"),
        )
    except comms.CommsError as exc:
        db.session.rollback()
        return _refused(exc)
    db.session.commit()
    return jsonify(result), 200 if result.get("success") else 502


@unified_comms_bp.route("/api/contacts/<int:contact_id>/emails", methods=["POST"])
@login_required
def log_email(contact_id):
    """Record an email exchanged outside LUXit (until a mailbox is connected)."""
    from services.contact_timeline import canonical_contact
    company_id = _company_id()
    if not current_user.can_edit_company(company_id):
        return _forbidden()
    contact = canonical_contact(company_id, contact_id)
    if contact is None:
        abort(404)
    data = _payload()
    direction = data.get("direction") or "outbound"
    occurred_at = None
    if data.get("occurred_at"):
        try:
            occurred_at = datetime.fromisoformat(data["occurred_at"])
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date."}), 400
    try:
        msg, _ = comms.record_email(
            company_id, direction=direction, from_address=data.get("from_address") or (
                contact.email if direction == "inbound" else current_user.email),
            to_addresses=[contact.email] if direction == "outbound" else [current_user.email],
            subject=data.get("subject"), body_text=data.get("body"), provider="manual",
            occurred_at=occurred_at, user_id=current_user.id, contact_id=contact.id,
            status="logged",
        )
    except comms.CommsError as exc:
        db.session.rollback()
        return _refused(exc)
    db.session.commit()
    return jsonify({"success": True, "email_message_id": msg.id}), 201


@unified_comms_bp.route("/api/comms/caller-context")
@login_required
@limiter.limit("120 per minute")
def caller_context():
    """LUXit Phone: who is calling, on which business line, what to know."""
    company_id = _company_id()
    from_number = request.args.get("from")
    if not from_number:
        return jsonify({"success": False, "error": "from is required"}), 400
    return jsonify({"success": True, **comms.caller_context(company_id, from_number=from_number,
                                                            to_number=request.args.get("to"))})
