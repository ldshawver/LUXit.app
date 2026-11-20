"""Actionable CRM (M3): pipeline, tasks and reminders, and the daily
"needs attention" view.

Canonical models only: ``Opportunity`` (pipeline) and ``ContactTask``
(tasks/follow-ups). Changes that matter to a relationship are written to
``ContactActivity`` so they appear on the M1 timeline, and are published as
CRM events (``services.crm_events``) for automation.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from extensions import db

logger = logging.getLogger(__name__)

DEFAULT_PIPELINE = "sales"
PIPELINE_STAGES = {
    "sales": ["new_lead", "qualified", "proposal", "negotiation", "won", "lost"],
}
CLOSED_STAGES = {"won", "lost"}
TASK_PRIORITIES = ("low", "normal", "high", "urgent")
STALE_AFTER = timedelta(days=30)
UNANSWERED_WINDOW = timedelta(days=14)
MISSED_CALL_WINDOW = timedelta(days=7)


class CRMError(ValueError):
    pass


def _event(company_id, event_type, payload):
    from services.crm_events import emit
    emit(company_id, event_type, payload)


def _log(contact_id, company_id, activity_type, title, description=None, **extra):
    from models import ContactActivity
    db.session.add(ContactActivity(
        contact_id=contact_id, company_id=company_id, activity_type=activity_type, title=title[:255],
        description=description, source="crm", extra_data=extra,
    ))


def _member_ids(company_id):
    from models import UserCompanyAccess
    return {r[0] for r in db.session.query(UserCompanyAccess.user_id).filter(
        UserCompanyAccess.company_id == company_id, UserCompanyAccess.is_active.is_(True))}


def _contact(company_id, contact_id):
    from models import Contact
    contact = Contact.query.filter_by(id=contact_id, company_id=company_id).first() if contact_id else None
    if contact is None:
        raise CRMError("Contact not found.")
    return contact


def _parse_dt(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        raise CRMError(f"Invalid {field}.")


def _parse_date(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise CRMError(f"Invalid {field}.")


def _owner(company_id, value):
    if value in (None, ""):
        return None
    try:
        uid = int(value)
    except (TypeError, ValueError):
        raise CRMError("Invalid owner.")
    if uid not in _member_ids(company_id):
        raise CRMError("The owner must be a member of this workspace.")
    return uid


def contact_name(contact):
    return (" ".join(filter(None, [contact.first_name, contact.last_name]))
            or getattr(contact, "display_name", None) or contact.email or contact.phone or f"Contact {contact.id}")


# ----------------------------------------------------------------- pipeline

def stages_for(pipeline):
    return PIPELINE_STAGES.get(pipeline or DEFAULT_PIPELINE, PIPELINE_STAGES[DEFAULT_PIPELINE])


def _apply_stage(opp, stage, reason=None):
    if stage not in stages_for(opp.pipeline):
        raise CRMError("Unknown stage for this pipeline.")
    old = opp.stage
    opp.stage = stage
    if stage in CLOSED_STAGES:
        opp.status = stage
        opp.won_lost_reason = (reason or opp.won_lost_reason or "")[:255] or None
        opp.probability = 100 if stage == "won" else 0
    else:
        opp.status = "open"
    return old


def create_opportunity(company_id, user, data):
    from models import Opportunity
    contact = _contact(company_id, data.get("contact_id"))
    name = (data.get("name") or "").strip()
    if not name:
        raise CRMError("Enter a name for the opportunity.")
    opp = Opportunity(
        company_id=company_id, contact_id=contact.id, name=name[:255],
        pipeline=data.get("pipeline") or DEFAULT_PIPELINE, stage="new_lead", status="open",
        owner_user_id=_owner(company_id, data.get("owner_user_id")) or getattr(user, "id", None),
        estimated_value=_money(data.get("estimated_value")),
        expected_close_date=_parse_date(data.get("expected_close_date"), "close date"),
        next_action=(data.get("next_action") or "")[:255] or None,
        follow_up_at=_parse_dt(data.get("follow_up_at"), "follow-up date"),
        probability=_probability(data.get("probability"), 10),
    )
    if data.get("stage"):
        _apply_stage(opp, data["stage"])
    db.session.add(opp)
    db.session.flush()
    _log(contact.id, company_id, "opportunity_created", f"Opportunity created: {opp.name}",
         user_id=getattr(user, "id", None), opportunity_id=opp.id, stage=opp.stage)
    _event(company_id, "opportunity.created", {"opportunity_id": opp.id, "contact_id": contact.id, "stage": opp.stage})
    return opp


def _money(value):
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise CRMError("Invalid amount.")
    if amount < 0:
        raise CRMError("Amount can't be negative.")
    return amount


def _probability(value, default):
    if value in (None, ""):
        return default
    try:
        p = int(value)
    except (TypeError, ValueError):
        raise CRMError("Invalid probability.")
    if not 0 <= p <= 100:
        raise CRMError("Probability must be 0–100.")
    return p


def update_opportunity(opp, user, data):
    changed_stage = None
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            raise CRMError("Enter a name for the opportunity.")
        opp.name = name[:255]
    if "estimated_value" in data:
        opp.estimated_value = _money(data.get("estimated_value"))
    if "probability" in data:
        opp.probability = _probability(data.get("probability"), opp.probability)
    if "expected_close_date" in data:
        opp.expected_close_date = _parse_date(data.get("expected_close_date"), "close date")
    if "next_action" in data:
        opp.next_action = (data.get("next_action") or "")[:255] or None
    if "follow_up_at" in data:
        opp.follow_up_at = _parse_dt(data.get("follow_up_at"), "follow-up date")
    if "owner_user_id" in data:
        opp.owner_user_id = _owner(opp.company_id, data.get("owner_user_id"))
    if data.get("stage") and data["stage"] != opp.stage:
        old = _apply_stage(opp, data["stage"], data.get("reason"))
        changed_stage = (old, opp.stage)
    if changed_stage:
        old, new = changed_stage
        _log(opp.contact_id, opp.company_id, "stage_change", f"{opp.name}: {old} → {new}",
             description=opp.won_lost_reason if new in CLOSED_STAGES else None,
             user_id=getattr(user, "id", None), opportunity_id=opp.id, from_stage=old, to_stage=new)
        _event(opp.company_id, "opportunity.stage_changed", {
            "opportunity_id": opp.id, "contact_id": opp.contact_id, "from_stage": old, "to_stage": new})
    return opp


def pipeline_board(company_id, pipeline=DEFAULT_PIPELINE, owner_user_id=None):
    from models import Contact, Opportunity
    q = (db.session.query(Opportunity, Contact)
         .join(Contact, Contact.id == Opportunity.contact_id)
         .filter(Opportunity.company_id == company_id, Contact.company_id == company_id,
                 Opportunity.pipeline == pipeline))
    if owner_user_id:
        q = q.filter(Opportunity.owner_user_id == owner_user_id)
    columns = {stage: {"stage": stage, "items": [], "count": 0, "value": 0.0, "weighted_value": 0.0}
               for stage in stages_for(pipeline)}
    now = datetime.utcnow()
    for opp, contact in q.order_by(Opportunity.updated_at.desc()).limit(1000):
        col = columns.get(opp.stage)
        if col is None:
            continue
        value = float(opp.estimated_value or 0)
        col["items"].append({
            "id": opp.id, "name": opp.name, "contact_id": contact.id, "contact_name": contact_name(contact),
            "value": value, "probability": opp.probability, "owner_user_id": opp.owner_user_id,
            "next_action": opp.next_action, "follow_up_at": opp.follow_up_at.isoformat() if opp.follow_up_at else None,
            "follow_up_overdue": bool(opp.follow_up_at and opp.follow_up_at < now and opp.status == "open"),
        })
        col["count"] += 1
        col["value"] += value
        col["weighted_value"] += value * (opp.probability or 0) / 100
    open_cols = [c for s, c in columns.items() if s not in CLOSED_STAGES]
    return {
        "pipeline": pipeline, "stages": list(columns.values()),
        "open_value": sum(c["value"] for c in open_cols),
        "weighted_open_value": round(sum(c["weighted_value"] for c in open_cols), 2),
    }


# -------------------------------------------------------------------- tasks

def create_task(company_id, user, data):
    from models import ContactTask
    contact = _contact(company_id, data.get("contact_id"))
    title = (data.get("title") or "").strip()
    if not title:
        raise CRMError("Enter a task title.")
    priority = data.get("priority") or "normal"
    if priority not in TASK_PRIORITIES:
        raise CRMError("Invalid priority.")
    due_at = _parse_dt(data.get("due_at"), "due date")
    task = ContactTask(
        company_id=company_id, contact_id=contact.id, title=title[:255], priority=priority, status="open",
        assigned_user_id=_owner(company_id, data.get("assigned_user_id")) or getattr(user, "id", None),
        due_at=due_at, reminder_at=_parse_dt(data.get("reminder_at"), "reminder"),
    )
    db.session.add(task)
    db.session.flush()
    if due_at:
        contact.next_follow_up_at = min(filter(None, [contact.next_follow_up_at, due_at]))
    _event(company_id, "task.created", {"task_id": task.id, "contact_id": contact.id})
    return task


def update_task(task, user, data):
    if "title" in data:
        title = (data.get("title") or "").strip()
        if not title:
            raise CRMError("Enter a task title.")
        task.title = title[:255]
    if "priority" in data:
        if data["priority"] not in TASK_PRIORITIES:
            raise CRMError("Invalid priority.")
        task.priority = data["priority"]
    if "due_at" in data:
        task.due_at = _parse_dt(data.get("due_at"), "due date")
    if "reminder_at" in data:
        task.reminder_at = _parse_dt(data.get("reminder_at"), "reminder")
    if "assigned_user_id" in data:
        task.assigned_user_id = _owner(task.company_id, data.get("assigned_user_id"))
    return task


def complete_task(task, user):
    if task.status == "done":
        return task
    task.status = "done"
    task.completed_at = datetime.utcnow()
    task.completed_by_user_id = getattr(user, "id", None)
    task.reminder_at = None
    _log(task.contact_id, task.company_id, "task_completed", f"Completed: {task.title}",
         user_id=getattr(user, "id", None), task_id=task.id)
    _refresh_next_follow_up(task.company_id, task.contact_id)
    _event(task.company_id, "task.completed", {"task_id": task.id, "contact_id": task.contact_id})
    return task


def reopen_task(task, user):
    task.status = "open"
    task.completed_at = None
    task.completed_by_user_id = None
    _refresh_next_follow_up(task.company_id, task.contact_id)
    return task


def _refresh_next_follow_up(company_id, contact_id):
    from models import Contact, ContactTask
    contact = db.session.get(Contact, contact_id)
    if contact is None or contact.company_id != company_id:
        return
    db.session.flush()
    nxt = (db.session.query(db.func.min(ContactTask.due_at))
           .filter(ContactTask.company_id == company_id, ContactTask.contact_id == contact_id,
                   ContactTask.status == "open").scalar())
    contact.next_follow_up_at = nxt


def list_tasks(company_id, *, assigned_user_id=None, bucket="open", now=None, limit=200):
    from models import Contact, ContactTask
    now = now or datetime.utcnow()
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1)  # day boundaries are UTC
    q = (db.session.query(ContactTask, Contact).join(Contact, Contact.id == ContactTask.contact_id)
         .filter(ContactTask.company_id == company_id, Contact.company_id == company_id))
    if assigned_user_id:
        q = q.filter(ContactTask.assigned_user_id == assigned_user_id)
    if bucket == "done":
        q = q.filter(ContactTask.status == "done").order_by(ContactTask.completed_at.desc())
    else:
        q = q.filter(ContactTask.status == "open")
        if bucket == "overdue":
            q = q.filter(ContactTask.due_at < now)
        elif bucket == "today":
            q = q.filter(ContactTask.due_at >= max(start, now), ContactTask.due_at < end)
        elif bucket == "upcoming":
            q = q.filter((ContactTask.due_at >= end) | ContactTask.due_at.is_(None))
        q = q.order_by(ContactTask.due_at.asc().nullslast(), ContactTask.id.asc())
    return [task_dict(t, c, now) for t, c in q.limit(limit)]


def task_dict(task, contact, now=None):
    now = now or datetime.utcnow()
    return {
        "id": task.id, "title": task.title, "status": task.status, "priority": task.priority,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "overdue": bool(task.status == "open" and task.due_at and task.due_at < now),
        "reminder_at": task.reminder_at.isoformat() if task.reminder_at else None,
        "assigned_user_id": task.assigned_user_id, "contact_id": contact.id, "contact_name": contact_name(contact),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def send_due_reminders(now=None):
    """Notify assignees of open tasks whose reminder time has passed.

    Each reminder fires once: ``reminder_at`` is cleared when the
    notification is written (same transaction).
    """
    from models import Contact, ContactTask, Notification
    now = now or datetime.utcnow()
    due = (db.session.query(ContactTask, Contact).join(Contact, Contact.id == ContactTask.contact_id)
           .filter(ContactTask.status == "open", ContactTask.reminder_at.isnot(None),
                   ContactTask.reminder_at <= now, Contact.company_id == ContactTask.company_id)
           .limit(500).all())
    sent = 0
    for task, contact in due:
        if task.assigned_user_id and task.assigned_user_id in _member_ids(task.company_id):
            db.session.add(Notification(
                user_id=task.assigned_user_id, company_id=task.company_id, event_type="task_reminder",
                category="contact", icon="check-square", title=f"Reminder: {task.title}"[:200],
                message=f"Follow up with {contact_name(contact)}"
                        + (f" (due {task.due_at:%b %d %H:%M})" if task.due_at else ""),
                link=f"/contacts/{contact.id}/activities",
            ))
            sent += 1
        task.reminder_at = None
    db.session.commit()
    return sent


# ---------------------------------------------------------------- dashboard

def needs_attention(company_id, *, user_id=None, now=None, limit=50):
    """Cross-contact list of what to do next, newest/most urgent first."""
    from models import (Contact, ContactTask, Opportunity, RelationshipMemory, TwilioCallLog, TwilioConversation,
                        TwilioMessage)
    now = now or datetime.utcnow()
    items = []

    def add(kind, priority, contact, action, reason, at, ref):
        if contact is None or contact.company_id != company_id or getattr(contact, "do_not_contact", False):
            return
        items.append({"type": kind, "priority": priority, "contact_id": contact.id,
                      "contact_name": contact_name(contact), "action": action, "reason": reason,
                      "at": at.isoformat() if at else None, "link": f"/contacts/{contact.id}/activities",
                      "source_ref": ref})

    # Unanswered texts: the newest message in a conversation is inbound.
    # Automatic replies ("we're closed", missed-call texts) don't answer anyone.
    rows = (db.session.query(TwilioMessage.conversation_id, TwilioMessage.id, TwilioMessage.direction,
                             TwilioMessage.body, TwilioMessage.created_at, TwilioConversation.contact_id)
            .join(TwilioConversation, TwilioConversation.id == TwilioMessage.conversation_id)
            .filter(TwilioMessage.company_id == company_id, TwilioConversation.company_id == company_id,
                    TwilioMessage.created_at >= now - UNANSWERED_WINDOW,
                    db.or_(TwilioMessage.is_auto_reply.is_(None), TwilioMessage.is_auto_reply.is_(False)))
            .order_by(TwilioMessage.created_at.desc()).limit(5000).all())
    seen = set()
    latest_out_by_contact = {}
    for conv_id, msg_id, direction, body, at, contact_id in rows:
        if direction == "outbound" and contact_id and contact_id not in latest_out_by_contact:
            latest_out_by_contact[contact_id] = at
        if conv_id in seen:
            continue
        seen.add(conv_id)
        if direction == "inbound" and contact_id:
            add("unanswered_text", 1, db.session.get(Contact, contact_id), "Reply to their text",
                (body or "")[:140], at, {"table": "twilio_message", "id": msg_id})

    # Missed calls not followed by any outbound text or call.
    calls = (TwilioCallLog.query
             .filter(TwilioCallLog.company_id == company_id, TwilioCallLog.created_at >= now - MISSED_CALL_WINDOW,
                     TwilioCallLog.contact_id.isnot(None))
             .order_by(TwilioCallLog.created_at.desc()).limit(2000).all())
    last_out_call = {}
    for c in calls:
        if (c.direction or "") == "outbound" and c.contact_id not in last_out_call:
            last_out_call[c.contact_id] = c.created_at
    flagged = set()
    for c in calls:
        status = (c.final_status or c.status or "").lower()
        if (c.direction or "") != "inbound" or status not in {"missed", "no-answer", "busy", "canceled"}:
            continue
        if c.contact_id in flagged:
            continue
        followed = max(filter(None, [last_out_call.get(c.contact_id), latest_out_by_contact.get(c.contact_id)]),
                       default=None)
        if followed and followed > c.created_at:
            continue
        flagged.add(c.contact_id)
        add("missed_call", 1, db.session.get(Contact, c.contact_id), "Return their call", "Missed call",
            c.created_at, {"table": "twilio_call_log", "id": c.id})

    tq = ContactTask.query.filter(ContactTask.company_id == company_id, ContactTask.status == "open",
                                  ContactTask.due_at < now + timedelta(days=1))
    if user_id:
        tq = tq.filter((ContactTask.assigned_user_id == user_id) | ContactTask.assigned_user_id.is_(None))
    for t in tq.order_by(ContactTask.due_at.asc()).limit(200):
        overdue = t.due_at < now
        add("task_overdue" if overdue else "task_due_today", 2 if overdue else 3, db.session.get(Contact, t.contact_id),
            t.title, "Overdue" if overdue else "Due today", t.due_at, {"table": "contact_task", "id": t.id})

    for m in (RelationshipMemory.query
              .filter(RelationshipMemory.company_id == company_id, RelationshipMemory.review_state != "rejected",
                      RelationshipMemory.kind.in_(["commitment", "follow_up"]), RelationshipMemory.task_id.is_(None),
                      RelationshipMemory.due_at.isnot(None), RelationshipMemory.due_at <= now + timedelta(days=2))
              .order_by(RelationshipMemory.due_at.asc()).limit(200)):
        verb = "Keep your commitment" if m.kind == "commitment" else "Follow up"
        add(m.kind, 2, db.session.get(Contact, m.contact_id), f"{verb}: {m.effective_text}"[:200],
            "Unconfirmed suggestion" if m.review_state == "pending" else "Confirmed", m.due_at,
            {"table": "relationship_memory", "id": m.id})

    oq = (db.session.query(Opportunity, Contact).join(Contact, Contact.id == Opportunity.contact_id)
          .filter(Opportunity.company_id == company_id, Opportunity.status == "open"))
    if user_id:
        oq = oq.filter((Opportunity.owner_user_id == user_id) | Opportunity.owner_user_id.is_(None))
    for o, contact in oq.limit(500):
        if o.follow_up_at and o.follow_up_at < now:
            add("opportunity_follow_up", 2, contact, f"Follow up on {o.name}", o.next_action or "Follow-up date passed",
                o.follow_up_at, {"table": "opportunity", "id": o.id})
        elif contact.last_contacted_at is None or contact.last_contacted_at < now - STALE_AFTER:
            if not o.follow_up_at:
                add("stale_opportunity", 4, contact, f"Check in about {o.name}", "No contact in 30+ days",
                    contact.last_contacted_at, {"table": "opportunity", "id": o.id})

    items.sort(key=lambda i: (i["priority"], i["at"] or ""))
    return items[:limit]


def dashboard(company_id, *, user_id=None, now=None):
    now = now or datetime.utcnow()
    board = pipeline_board(company_id)
    return {
        "needs_attention": needs_attention(company_id, user_id=user_id, now=now),
        "tasks": {
            "overdue": list_tasks(company_id, assigned_user_id=user_id, bucket="overdue", now=now, limit=50),
            "today": list_tasks(company_id, assigned_user_id=user_id, bucket="today", now=now, limit=50),
        },
        "pipeline": {
            "stages": [{k: v for k, v in s.items() if k != "items"} for s in board["stages"]],
            "open_value": board["open_value"], "weighted_open_value": board["weighted_open_value"],
        },
    }
