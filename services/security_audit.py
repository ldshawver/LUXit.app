"""Security audit trail (T-26): who signed in, changed a role, invited or
removed someone, opened billing, or edited a tenant from the platform console.

``record()`` is called *after* the action's own commit, writes in a savepoint
and never raises: a missing table (migration not applied yet) or a DB error
logs an error and leaves the request alone. Only allowlisted ``detail`` keys
with short scalar values are kept, so passwords, tokens, identifiers typed at
login and message bodies can't end up in the table.

``GET /api/security/audit-events`` lets workspace owners/admins read their own
workspace's events; platform admins may read any workspace or all of them.
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from extensions import db

logger = logging.getLogger(__name__)

OUTCOMES = ("success", "denied", "failure")

# detail keys we keep; anything else is dropped.
DETAIL_KEYS = frozenset({
    "reason", "role", "role_from", "role_to", "is_admin_from", "is_admin_to",
    "fields", "invitation_id", "license_id", "lookup_key", "include_setup_fee",
    "session_id", "status", "via", "device_id",
    "purge_after",
})
_MAX_STR = 120
_MAX_FIELDS = 20

security_audit_bp = Blueprint("security_audit", __name__)


def _clean_detail(detail):
    if not detail:
        return None
    out = {}
    for key, value in detail.items():
        if key not in DETAIL_KEYS or value is None:
            continue
        if isinstance(value, bool) or isinstance(value, int):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:_MAX_STR]
        elif isinstance(value, (list, tuple, set)):
            out[key] = sorted(str(v)[:_MAX_STR] for v in value)[:_MAX_FIELDS]
    return json.dumps(out, sort_keys=True, separators=(",", ":")) if out else None


def _request_ip():
    try:
        from extensions import _client_ip
        return (_client_ip() or "")[:64] or None
    except RuntimeError:  # outside a request (jobs, scripts)
        return None


def _actor_id(actor):
    if actor is None:
        try:
            actor = current_user
        except Exception:
            return None
    try:
        if not getattr(actor, "is_authenticated", True):
            return None
        return getattr(actor, "id", None)
    except Exception:
        return None


def record(action, *, outcome="success", actor=None, company_id=None, target_user_id=None, detail=None):
    """Append one audit event. Call after the action has committed. Never raises."""
    from models import SecurityAuditEvent

    if outcome not in OUTCOMES:
        outcome = "failure"
    actor_user_id = _actor_id(actor)
    logger.info("security_audit action=%s outcome=%s actor=%s company=%s target=%s",
                action, outcome, actor_user_id, company_id, target_user_id)
    try:
        with db.session.begin_nested():
            db.session.add(SecurityAuditEvent(
                action=str(action)[:64],
                outcome=outcome,
                actor_user_id=actor_user_id,
                company_id=company_id,
                target_user_id=target_user_id,
                ip=_request_ip(),
                detail=_clean_detail(detail),
            ))
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        logger.error("security_audit: could not record %s", action, exc_info=True)
        return False


def _serialize(evt):
    return {
        "id": evt.id,
        "created_at": evt.created_at.isoformat() + "Z" if evt.created_at else None,
        "action": evt.action,
        "outcome": evt.outcome,
        "actor_user_id": evt.actor_user_id,
        "company_id": evt.company_id,
        "target_user_id": evt.target_user_id,
        "ip": evt.ip,
        "detail": json.loads(evt.detail) if evt.detail else None,
    }


@security_audit_bp.route("/api/security/audit-events")
@login_required
def list_events():
    from models import SecurityAuditEvent

    limit = min(max(request.args.get("limit", 100, type=int) or 100, 1), 500)
    before_id = request.args.get("before_id", type=int)
    requested = request.args.get("company_id", type=int)

    if getattr(current_user, "is_admin", False):
        company_id = requested  # None = every workspace
    else:
        company = current_user.get_default_company()
        company_id = company.id if company else None
        if company_id is None or (requested is not None and requested != company_id) \
                or not current_user.can_admin_company(company_id):
            return jsonify({"error": "forbidden"}), 403

    q = SecurityAuditEvent.query
    if company_id is not None:
        q = q.filter(SecurityAuditEvent.company_id == company_id)
    if before_id:
        q = q.filter(SecurityAuditEvent.id < before_id)
    action = (request.args.get("action") or "").strip()
    if action:
        q = q.filter(SecurityAuditEvent.action == action[:64])
    rows = q.order_by(SecurityAuditEvent.id.desc()).limit(limit).all()
    return jsonify({
        "events": [_serialize(r) for r in rows],
        "next_before_id": rows[-1].id if len(rows) == limit else None,
    })
