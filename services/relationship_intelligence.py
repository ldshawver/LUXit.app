"""Relationship intelligence (M2): briefings, commitments, follow-ups, memory.

Everything here is deterministic and grounded in the M1 timeline: each
recommendation and each detected memory carries the ``source_refs`` of the
rows it came from. Detected items are stored ``pending`` in
``RelationshipMemory`` and only count as known once a person confirms or
corrects them; rejected items are kept (so a rescan never re-suggests them)
but never shown as fact.

AI summaries are an optional layer on top (see ``ai_status``); the briefing
is complete without them.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from extensions import db

logger = logging.getLogger(__name__)

REVIEW_STATES = ("pending", "confirmed", "rejected")
MEMORY_KINDS = ("commitment", "follow_up", "fact", "summary")
STALE_AFTER = timedelta(days=30)
DUE_SOON = timedelta(days=2)
SCAN_WINDOW = 200

# We promised something (outbound text, or a note we logged).
_OUR_PROMISE = re.compile(
    r"\b(?:i'll|i will|we'll|we will|let me|i'm going to|we're going to)\s+"
    r"(?:send|call|get back|follow up|email|text|check|confirm|have|get you|reach out|look into)\b[^.!?\n]*",
    re.IGNORECASE,
)
# They asked us for something (inbound text, voicemail, call transcript).
_THEIR_ASK = re.compile(
    r"\b(?:can you|could you|would you|please|call me back|give me a call|let me know|get back to me|"
    r"send me|text me|email me)\b[^.!?\n]*",
    re.IGNORECASE,
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _due_from_text(text, at):
    """Best-effort due date from phrases like 'tomorrow' or 'by Friday'."""
    t = text.lower()
    day_end = lambda d: datetime(d.year, d.month, d.day, 23, 59)  # noqa: E731
    if re.search(r"\b(today|tonight|this afternoon|this evening)\b", t):
        return day_end(at)
    if "tomorrow" in t:
        return day_end(at + timedelta(days=1))
    if "next week" in t:
        return day_end(at + timedelta(days=7))
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", t):
            delta = (i - at.weekday()) % 7 or 7
            return day_end(at + timedelta(days=delta))
    if "end of the week" in t or "this week" in t:
        return day_end(at + timedelta(days=(4 - at.weekday()) % 7))
    return None


def _parse_at(value):
    return datetime.fromisoformat(value) if value else None


def _ref(entry):
    return {"table": entry["source"]["table"], "id": entry["source"]["id"]}


def _contact_or_none(company_id, contact_id):
    from services.contact_timeline import canonical_contact
    if not company_id:
        return None
    return canonical_contact(company_id, contact_id)


# --------------------------------------------------------------------- scan

def detect_candidates(entries):
    """Yield (kind, text, due_at, entry, confidence) found in timeline entries."""
    for e in entries:
        text = e.get("summary") or ""
        if not text:
            continue
        at = _parse_at(e["at"]) or datetime.utcnow()
        outbound = e.get("direction") == "outbound" or e["kind"] == "activity"
        inbound = e.get("direction") == "inbound" or e["kind"] == "voicemail"
        if outbound:
            m = _OUR_PROMISE.search(text)
            if m:
                phrase = m.group(0).strip()
                yield "commitment", phrase, _due_from_text(phrase, at), e, 0.6
                continue
        if inbound:
            m = _THEIR_ASK.search(text)
            if m:
                phrase = m.group(0).strip()
                yield "follow_up", phrase, _due_from_text(phrase, at) or at + timedelta(days=1), e, 0.5


def scan_contact(company_id, contact_id):
    """Store new pending commitments/follow-ups for a contact. Idempotent:
    one memory per source row, and rejected suggestions never come back."""
    from models import RelationshipMemory
    from services.contact_timeline import build_timeline

    timeline = build_timeline(company_id, contact_id, limit=SCAN_WINDOW,
                              kinds=["message", "voicemail", "call", "activity"])
    if timeline is None:
        return None
    canonical_id = timeline["canonical_contact_id"]
    created = []
    for kind, text, due_at, entry, confidence in detect_candidates(timeline["entries"]):
        key = f"{kind}:{entry['source']['table']}:{entry['source']['id']}"
        exists = RelationshipMemory.query.filter_by(company_id=company_id, dedupe_key=key).first()
        if exists:
            continue
        memory = RelationshipMemory(
            company_id=company_id, contact_id=canonical_id, kind=kind, text=text[:1000], due_at=due_at,
            origin="rule", confidence=confidence, review_state="pending", source_refs=[_ref(entry)],
            dedupe_key=key,
        )
        db.session.add(memory)
        created.append(memory)
    db.session.flush()
    return created


# ------------------------------------------------------------------ review

class ReviewError(ValueError):
    pass


def get_memory(company_id, contact_id, memory_id):
    from models import RelationshipMemory
    contact = _contact_or_none(company_id, contact_id)
    if contact is None:
        return None
    return RelationshipMemory.query.filter_by(id=memory_id, company_id=company_id, contact_id=contact.id).first()


def review(memory, user, action, text=None):
    """confirm | reject | correct (with text) | reopen."""
    now = datetime.utcnow()
    if action == "confirm":
        memory.review_state = "confirmed"
    elif action == "reject":
        memory.review_state = "rejected"
    elif action == "correct":
        text = (text or "").strip()
        if not text:
            raise ReviewError("Enter the corrected text.")
        memory.corrected_text = text[:1000]
        memory.review_state = "confirmed"
    elif action == "reopen":
        memory.review_state = "pending"
    else:
        raise ReviewError("Unknown review action.")
    memory.reviewed_by_user_id = getattr(user, "id", None)
    memory.reviewed_at = now
    return memory


def add_memory(company_id, contact_id, user, kind, text, due_at=None):
    """A fact or follow-up entered by a person: confirmed from the start."""
    from models import RelationshipMemory
    contact = _contact_or_none(company_id, contact_id)
    if contact is None:
        return None
    if kind not in MEMORY_KINDS:
        raise ReviewError("Unknown memory type.")
    text = (text or "").strip()
    if not text:
        raise ReviewError("Enter some text.")
    memory = RelationshipMemory(
        company_id=company_id, contact_id=contact.id, kind=kind, text=text[:1000], due_at=due_at,
        origin="user", review_state="confirmed", source_refs=[], created_by_user_id=getattr(user, "id", None),
        reviewed_by_user_id=getattr(user, "id", None), reviewed_at=datetime.utcnow(),
    )
    db.session.add(memory)
    db.session.flush()
    return memory


def create_task_from_memory(memory, user):
    """Turn a commitment/follow-up into a ContactTask (confirms the memory)."""
    from models import ContactTask
    if memory.task_id:
        return db.session.get(ContactTask, memory.task_id)
    if memory.review_state == "rejected":
        raise ReviewError("This suggestion was rejected.")
    task = ContactTask(
        company_id=memory.company_id, contact_id=memory.contact_id,
        assigned_user_id=getattr(user, "id", None), title=memory.effective_text[:255],
        due_at=memory.due_at, priority="normal", status="open",
    )
    db.session.add(task)
    db.session.flush()
    memory.task_id = task.id
    if memory.review_state == "pending":
        review(memory, user, "confirm")
    return task


# ---------------------------------------------------------------- briefing

def memory_dict(m):
    return {
        "id": m.id, "kind": m.kind, "text": m.effective_text, "original_text": m.text,
        "corrected": bool(m.corrected_text), "due_at": m.due_at.isoformat() if m.due_at else None,
        "origin": m.origin, "confidence": m.confidence, "review_state": m.review_state,
        "source_refs": m.source_refs or [], "task_id": m.task_id,
    }


def _consent(contact):
    from services import contact_consent
    sms_out = contact_consent.has_sms_opt_out(contact)
    return {
        "do_not_contact": bool(getattr(contact, "do_not_contact", False)),
        "sms_opted_out": bool(sms_out),
        "sms_consent_status": getattr(contact, "sms_consent_status", None),
        "email_contactable": bool(contact_consent.is_email_contactable(contact)),
    }


def ai_status():
    """AI summaries are optional; report why they aren't in the briefing."""
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        return "not_configured"
    return "available_on_request"


def build_briefing(company_id, contact_id, now=None):
    """Everything a person needs before reaching out, with sources."""
    from models import ContactTask, Opportunity, RelationshipMemory
    from services.contact_timeline import build_timeline

    now = now or datetime.utcnow()
    timeline = build_timeline(company_id, contact_id, limit=SCAN_WINDOW)
    if timeline is None:
        return None
    canonical_id = timeline["canonical_contact_id"]
    from models import Contact
    contact = db.session.get(Contact, canonical_id)
    entries = timeline["entries"]

    comms = [e for e in entries if e["kind"] in ("message", "call", "voicemail")]
    inbound = [e for e in comms if e.get("direction") == "inbound"]
    outbound = [e for e in comms if e.get("direction") == "outbound"]
    last_in = inbound[0] if inbound else None
    last_out = outbound[0] if outbound else None
    last_touch = comms[0] if comms else None
    since = now - STALE_AFTER
    recent = [e for e in comms if _parse_at(e["at"]) and _parse_at(e["at"]) >= since]
    counts = {}
    for e in recent:
        counts[e["channel"] or e["kind"]] = counts.get(e["channel"] or e["kind"], 0) + 1

    tasks = (ContactTask.query
             .filter(ContactTask.company_id == company_id, ContactTask.contact_id.in_(
                 [canonical_id] + timeline["merged_contact_ids"]), ContactTask.status == "open")
             .order_by(ContactTask.due_at.asc().nullslast()).all())
    opps = (Opportunity.query
            .filter(Opportunity.company_id == company_id, Opportunity.contact_id.in_(
                [canonical_id] + timeline["merged_contact_ids"]), Opportunity.status == "open")
            .order_by(Opportunity.created_at.desc()).all())
    memories = (RelationshipMemory.query
                .filter(RelationshipMemory.company_id == company_id, RelationshipMemory.contact_id == canonical_id,
                        RelationshipMemory.review_state != "rejected")
                .order_by(RelationshipMemory.created_at.desc()).all())

    consent = _consent(contact)
    recs = []

    def rec(priority, action, reason, refs, channel=None):
        recs.append({"priority": priority, "action": action, "reason": reason, "source_refs": refs,
                     "channel": channel})

    if consent["do_not_contact"]:
        rec(0, "Do not contact", "This contact is marked do-not-contact.", [])
    else:
        later_out = lambda e: last_out and last_out["at"] > e["at"]  # noqa: E731
        last_text = next((e for e in inbound if e["kind"] == "message"), None)
        if last_text and not later_out(last_text):
            if consent["sms_opted_out"]:
                rec(1, "Reply by phone or email", "They texted but have opted out of SMS.", [_ref(last_text)], "voice")
            else:
                rec(1, "Reply to their text", f"Unanswered message: “{last_text['summary'] or ''}”",
                    [_ref(last_text)], "sms")
        missed = next((e for e in inbound if e["kind"] == "voicemail" or e["title"] == "Missed call"), None)
        if missed and not later_out(missed):
            rec(1, "Return their call", f"{missed['title']} not returned yet.", [_ref(missed)], "voice")
        for t in tasks:
            if t.due_at and t.due_at < now:
                rec(2, f"Complete overdue task: {t.title}", f"Due {t.due_at:%Y-%m-%d}.",
                    [{"table": "contact_task", "id": t.id}])
        for m in memories:
            if m.kind in ("commitment", "follow_up") and not m.task_id and m.due_at and m.due_at <= now + DUE_SOON:
                verb = "Keep your commitment" if m.kind == "commitment" else "Follow up"
                rec(2, f"{verb}: {m.effective_text}", f"Due {m.due_at:%Y-%m-%d}"
                    + (" (unconfirmed suggestion)" if m.review_state == "pending" else "") + ".",
                    m.source_refs or [{"table": "relationship_memory", "id": m.id}])
        last_touch_at = _parse_at(last_touch["at"]) if last_touch else None
        if opps and (last_touch_at is None or last_touch_at < since):
            o = opps[0]
            rec(3, f"Check in about {o.name}", "Open opportunity and no conversation in 30+ days.",
                [{"table": "opportunity", "id": o.id}])
    recs.sort(key=lambda r: r["priority"])

    name = " ".join(filter(None, [contact.first_name, contact.last_name])) or contact.email or contact.phone
    return {
        "contact": {"id": canonical_id, "name": name, "identifiers": timeline["identifiers"],
                    "merged_contact_ids": timeline["merged_contact_ids"]},
        "consent": consent,
        "activity": {
            "last_inbound": last_in, "last_outbound": last_out,
            "days_since_contact": (now - _parse_at(last_touch["at"])).days if last_touch else None,
            "last_30_days": counts,
        },
        "summary": _summary_line(counts, last_in, last_out),
        "open_tasks": [{"id": t.id, "title": t.title, "due_at": t.due_at.isoformat() if t.due_at else None,
                        "overdue": bool(t.due_at and t.due_at < now)} for t in tasks],
        "open_opportunities": [{"id": o.id, "name": o.name, "stage": o.stage, "next_action": o.next_action}
                               for o in opps],
        "memories": [memory_dict(m) for m in memories],
        "recommendations": recs,
        "ai": {"status": ai_status()},
        "unavailable": timeline["unavailable"],
    }


def _summary_line(counts, last_in, last_out):
    if not counts and not last_in and not last_out:
        return "No conversations yet."
    parts = []
    if counts:
        parts.append("Last 30 days: " + ", ".join(f"{n} {ch}" for ch, n in sorted(counts.items())) + ".")
    else:
        parts.append("No conversations in the last 30 days.")
    if last_in:
        parts.append(f"Last heard from them {last_in['at'][:10]} ({last_in['title'].lower()}).")
    if last_out:
        parts.append(f"Last reached out {last_out['at'][:10]} ({last_out['title'].lower()}).")
    return " ".join(parts)
