"""Workflow builder (M5) on top of ``services.crm_automation``.

Rules are ``Segment`` rows of type ``automation_rule`` (the engine's existing
storage). Rules made here carry ``category='workflow'`` and are editable;
system rules (e.g. My Order Customer) are listed read-only. Every save goes
through the engine's ``validate_definition`` (fail closed).

Also here: dry-run preview (no writes), execution history, failure
reporting, approval of held messages, and the scheduled inactivity scan.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func

from extensions import db
from services import crm_automation as engine

logger = logging.getLogger(__name__)

WORKFLOW_CATEGORY = "workflow"
INACTIVITY_SCAN_LIMIT = 500


class BuilderError(ValueError):
    pass


def _rules_q(company_id):
    from models import Segment
    return Segment.query.filter_by(company_id=company_id, segment_type="automation_rule")


def rule_dict(rule, with_stats=True):
    from models import CRMAutomationExecution
    out = {
        "id": rule.id, "name": rule.name, "description": rule.description, "is_active": rule.is_active,
        "editable": rule.category == WORKFLOW_CATEGORY, "triggers": rule.triggers or [],
        "conditions": rule.conditions or {}, "actions": rule.actions or [], "match_mode": rule.match_mode,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }
    if with_stats:
        rows = (db.session.query(CRMAutomationExecution.status, func.count(CRMAutomationExecution.id))
                .filter(CRMAutomationExecution.company_id == rule.company_id,
                        CRMAutomationExecution.rule_id == rule.id)
                .group_by(CRMAutomationExecution.status).all())
        out["stats"] = {status: n for status, n in rows}
    return out


def list_rules(company_id):
    return [rule_dict(r) for r in _rules_q(company_id).order_by(engine.Segment.name).all()]


def get_rule(company_id, rule_id):
    return _rules_q(company_id).filter_by(id=rule_id).first()


def _check_refs(company_id, definition):
    """Segment/tag ids used by a rule must belong to the workspace."""
    from models import Segment
    ids = set(definition["conditions"].get("segment_ids_any") or [])
    ids |= set(definition["conditions"].get("tag_ids") or [])
    for action in definition["actions"]:
        for key in ("segment_id", "tag_id"):
            if key in action:
                ids.add(action[key])
    if ids:
        owned = {r[0] for r in db.session.query(Segment.id).filter(Segment.company_id == company_id,
                                                                  Segment.id.in_(ids))}
        if ids - owned:
            raise BuilderError("A referenced segment or tag doesn't belong to this workspace.")
    if "owner_user_id" in definition["conditions"]:
        from models import UserCompanyAccess
        if not UserCompanyAccess.query.filter_by(company_id=company_id, is_active=True,
                                                 user_id=definition["conditions"]["owner_user_id"]).first():
            raise BuilderError("The owner must be a member of this workspace.")


def validate(company_id, data):
    try:
        definition = engine.validate_definition(
            data.get("triggers"), data.get("conditions") or {}, data.get("actions"), data.get("match_mode") or "all")
    except engine.AutomationContractError as exc:
        raise BuilderError(str(exc))
    _check_refs(company_id, definition)
    return definition


def save_rule(company_id, data, rule=None):
    from models import Segment
    if rule is not None and rule.category != WORKFLOW_CATEGORY:
        raise BuilderError("System automations can't be edited here.")
    name = (data.get("name") or (rule.name if rule else "") or "").strip()
    if not name:
        raise BuilderError("Enter a name.")
    merged = {k: data.get(k, getattr(rule, k, None) if rule else None)
              for k in ("triggers", "conditions", "actions", "match_mode")}
    definition = validate(company_id, merged)
    clash = _rules_q(company_id).filter(func.lower(Segment.name) == name.lower())
    if rule is not None:
        clash = clash.filter(Segment.id != rule.id)
    if clash.first():
        raise BuilderError("Another automation already has that name.")
    if rule is None:
        rule = Segment(company_id=company_id, segment_type="automation_rule", category=WORKFLOW_CATEGORY,
                       is_dynamic=True, is_active=bool(data.get("is_active", False)))
        db.session.add(rule)
    rule.name = name[:255]
    if "description" in data:
        rule.description = (data.get("description") or "")[:2000] or None
    rule.triggers, rule.conditions = definition["triggers"], definition["conditions"]
    rule.actions, rule.match_mode = definition["actions"], definition["match_mode"]
    if "is_active" in data:
        rule.is_active = bool(data["is_active"])
    db.session.flush()
    return rule


def dry_run(company_id, data, contact_id, event=None):
    """Would this rule fire for this contact and event? Writes nothing."""
    from models import Contact
    definition = validate(company_id, data)
    contact = Contact.query.filter_by(id=contact_id, company_id=company_id).first()
    if contact is None:
        raise BuilderError("Contact not found.")
    event = dict(event or {})
    trigger = event.pop("trigger", None) or definition["triggers"][0]
    if trigger not in definition["triggers"]:
        raise BuilderError("That trigger isn't part of this automation.")
    ctx = {"company_id": company_id, "contact_id": contact.id, "contact": contact, **event,
           "rule_id": event.get("rule_id") or -1}
    checks = []
    for key, expected in definition["conditions"].items():
        try:
            ok = engine._condition_matches(key, expected, ctx)
        except Exception as exc:  # noqa: BLE001
            ok, expected = False, f"error: {exc}"
        checks.append({"condition": key, "expected": expected, "passed": bool(ok)})
    results = [c["passed"] for c in checks]
    would_fire = (any(results) if definition["match_mode"] == "any" else all(results)) if results else True
    if not contact.is_active or contact.merged_into_contact_id:
        would_fire = False
        checks.append({"condition": "contact_state", "expected": "canonical_active", "passed": False})
    planned = [{"type": a["type"], "description": _describe(a, contact)} for a in definition["actions"]]
    return {"trigger": trigger, "would_fire": would_fire, "conditions": checks,
            "actions": planned if would_fire else []}


def _describe(action, contact):
    kind = action["type"]
    if kind in {"task.create", "follow_up.create"}:
        return f"Create task “{engine._render(action['title'], contact)}”"
    if kind == "note.add":
        return "Add note to the timeline"
    if kind == "opportunity.update":
        return "Update the opportunity: " + ", ".join(f"{k}={action[k]}" for k in ("stage", "next_action") if k in action)
    if kind == "message.send":
        return f"Draft {action['channel'].upper()} for approval: “{engine._render(action['body'], contact)}”"
    if kind == "contact.add_tag":
        return f"Add tag #{action['tag_id']}"
    return f"Add to segment #{action.get('segment_id')}"


def history(company_id, *, rule_id=None, status=None, limit=100):
    from models import Contact, CRMAutomationExecution, Segment
    q = (db.session.query(CRMAutomationExecution, Segment.name, Contact.first_name, Contact.last_name)
         .join(Segment, Segment.id == CRMAutomationExecution.rule_id)
         .outerjoin(Contact, Contact.id == CRMAutomationExecution.contact_id)
         .filter(CRMAutomationExecution.company_id == company_id, Segment.company_id == company_id))
    if rule_id:
        q = q.filter(CRMAutomationExecution.rule_id == rule_id)
    if status:
        q = q.filter(CRMAutomationExecution.status == status)
    out = []
    for ex, rule_name, first, last in q.order_by(CRMAutomationExecution.created_at.desc()).limit(min(limit, 500)):
        details = ex.details or {}
        out.append({
            "id": ex.id, "rule_id": ex.rule_id, "rule_name": rule_name, "contact_id": ex.contact_id,
            "contact_name": " ".join(filter(None, [first, last])) or None, "trigger": ex.trigger_name,
            "event_key": ex.event_key, "action_index": ex.action_index, "status": ex.status,
            "action": (details.get("action") or {}).get("type"), "error": details.get("error"),
            "message": details.get("message"), "created_at": ex.created_at.isoformat(),
        })
    return out


def failure_summary(company_id, since_days=7):
    from models import CRMAutomationExecution
    since = datetime.utcnow() - timedelta(days=since_days)
    rows = (db.session.query(CRMAutomationExecution.rule_id, func.count(CRMAutomationExecution.id))
            .filter(CRMAutomationExecution.company_id == company_id, CRMAutomationExecution.status == "failed",
                    CRMAutomationExecution.created_at >= since)
            .group_by(CRMAutomationExecution.rule_id).all())
    return {"since_days": since_days, "failed_by_rule": {rule_id: n for rule_id, n in rows},
            "recent": history(company_id, status="failed", limit=20)}


def decide_message(company_id, execution_id, user, approve):
    """Approve (send now, consent re-checked) or reject a held message."""
    from models import Contact, CRMAutomationExecution
    from services import unified_comms
    ex = CRMAutomationExecution.query.filter_by(id=execution_id, company_id=company_id).first()
    if ex is None:
        raise BuilderError("Not found.")
    if ex.status != "awaiting_approval":
        raise BuilderError("This message is no longer waiting for approval.")
    details = dict(ex.details or {})
    details["decided_by_user_id"] = getattr(user, "id", None)
    details["decided_at"] = datetime.utcnow().isoformat()
    if not approve:
        ex.status, ex.details = "rejected", details
        return ex
    contact = Contact.query.filter_by(id=ex.contact_id, company_id=company_id).first()
    msg = details.get("message") or {}
    try:
        result = unified_comms.send_to_contact(
            company_id, contact, channel=msg.get("channel"), body=msg.get("body"), user=user,
            idempotency_key=f"automation:{ex.id}")
        ex.status = "completed" if result.get("success") else "failed"
        details["send"] = {k: result.get(k) for k in ("status", "sid", "conversation_id", "email_message_id", "error")}
        if not result.get("success"):
            details["error"] = result.get("error") or "send failed"
    except unified_comms.CommsError as exc:
        ex.status = "failed"
        details["error"] = f"{exc.code}: {exc}"
    ex.details = details
    return ex


def scan_inactive(company_id, now=None):
    """Raise ``contact.inactive`` for contacts silent longer than an active
    rule's ``no_communication_days``. Each silence period fires once per rule
    (the event key carries the contact's last-contact date)."""
    from models import Contact
    from services.crm_events import emit
    now = now or datetime.utcnow()
    rules = [r for r in _rules_q(company_id).filter_by(is_active=True).all()
             if engine.INACTIVE_TRIGGER in (r.triggers or [])]
    fired = 0
    for days in sorted({(r.conditions or {}).get("no_communication_days") for r in rules} - {None}):
        cutoff = now - timedelta(days=days)
        last = func.coalesce(Contact.last_contacted_at, Contact.last_activity_at, Contact.created_at)
        contacts = (Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True),
                                         Contact.merged_into_contact_id.is_(None), last < cutoff)
                    .order_by(Contact.id.asc()).limit(INACTIVITY_SCAN_LIMIT).all())
        for c in contacts:
            marker = (c.last_contacted_at or c.last_activity_at or c.created_at)
            key = f"contact.inactive:{days}d:{c.id}:{marker:%Y%m%d}" if marker else f"contact.inactive:{days}d:{c.id}:never"
            result = emit(company_id, engine.INACTIVE_TRIGGER, {"contact_id": c.id, "event_key": key, "now": now})
            fired += sum(1 for r in (result or {}).get("results", []) if not r.get("duplicate"))
    return fired


def scan_inactive_all(now=None):
    from models import Segment
    company_ids = {r[0] for r in db.session.query(Segment.company_id).filter(
        Segment.segment_type == "automation_rule", Segment.is_active.is_(True), Segment.company_id.isnot(None))}
    total = 0
    for company_id in sorted(company_ids):
        try:
            total += scan_inactive(company_id, now)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("inactivity scan failed for company_id=%s", company_id)
    return total
