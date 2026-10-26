"""Unified relationship timeline for one canonical contact (M1).

A read model only: it merges what the existing tables already record about a
person, newest first, without copying anything. Every entry names the row it
came from (``source.table`` / ``source.id``) so a UI, an export or an AI
summary can cite it.

Identity: the requested contact is resolved to its canonical record by
following ``merged_into_contact_id``; records still attached to contacts that
were merged into it are included and marked ``merged_from``. Phone-only
records (voicemail, unlinked calls) are matched on the cluster's normalized
numbers. Every query is filtered by ``company_id``.

``ActivityLog`` is a per-user audit log with no contact reference, so it is
not part of a contact's timeline.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from extensions import db

logger = logging.getLogger(__name__)

KINDS = ("activity", "message", "email", "call", "voicemail", "task", "opportunity", "deal", "touchpoint", "source")
MAX_LIMIT = 200
_MAX_MERGE_HOPS = 10
_SUMMARY_LEN = 280


def _summary(text):
    text = (text or "").strip()
    return text if len(text) <= _SUMMARY_LEN else text[: _SUMMARY_LEN - 1] + "…"


def _iso(value):
    return value.isoformat() if value else None


def canonical_contact(company_id, contact_id):
    """The surviving contact for ``contact_id`` within ``company_id`` (or None)."""
    from models import Contact
    contact = Contact.query.filter_by(id=contact_id, company_id=company_id).first()
    hops = 0
    while contact is not None and contact.merged_into_contact_id and hops < _MAX_MERGE_HOPS:
        nxt = Contact.query.filter_by(id=contact.merged_into_contact_id, company_id=company_id).first()
        if nxt is None:
            break
        contact, hops = nxt, hops + 1
    return contact


def identity_cluster(company_id, canonical):
    """Contact ids merged into ``canonical`` (transitively) plus its identifiers."""
    from models import Contact, ContactEmailAddress, ContactPhoneNumber
    from services.phone_normalization import normalize_phone_e164

    ids, frontier = {canonical.id}, [canonical.id]
    while frontier and len(ids) < 500:
        rows = (
            db.session.query(Contact.id)
            .filter(Contact.company_id == company_id, Contact.merged_into_contact_id.in_(frontier))
            .all()
        )
        frontier = [r[0] for r in rows if r[0] not in ids]
        ids.update(frontier)

    phones, emails = set(), set()
    for c in Contact.query.filter(Contact.company_id == company_id, Contact.id.in_(ids)).all():
        if c.phone:
            phones.add(normalize_phone_e164(c.phone) or c.phone)
        if c.email:
            emails.add(c.email.strip().lower())
    for (value,) in db.session.query(ContactPhoneNumber.normalized_value).filter(
            ContactPhoneNumber.company_id == company_id, ContactPhoneNumber.contact_id.in_(ids)):
        if value:
            phones.add(value)
    for (value,) in db.session.query(ContactEmailAddress.normalized_value).filter(
            ContactEmailAddress.company_id == company_id, ContactEmailAddress.contact_id.in_(ids)):
        if value:
            emails.add(value.strip().lower())
    phones.discard("")
    return ids, sorted(phones), sorted(emails)


def _entry(kind, table, pk, at, *, contact_id, canonical_id, title, summary=None, channel=None,
           direction=None, actor_user_id=None, status=None, provenance=None, extra=None):
    entry = {
        "id": f"{table}:{pk}",
        "kind": kind,
        "channel": channel,
        "direction": direction,
        "at": _iso(at),
        "title": title,
        "summary": _summary(summary),
        "status": status,
        "actor_user_id": actor_user_id,
        "source": {"table": table, "id": pk},
        "provenance": provenance,
        "merged_from": contact_id if contact_id and contact_id != canonical_id else None,
    }
    if extra:
        entry["extra"] = extra
    return entry


def _since(query, column, before):
    return query.filter(column < before) if before else query


# --- per-source collectors: (company_id, ids, phones, emails, before, limit, canonical_id) -> [entry]

def _activities(cid, ids, phones, emails, before, limit, canon):
    from models import ContactActivity
    q = ContactActivity.query.filter(ContactActivity.company_id == cid, ContactActivity.contact_id.in_(ids))
    out = []
    for a in _since(q, ContactActivity.created_at, before).order_by(ContactActivity.created_at.desc()).limit(limit):
        extra = a.extra_data if isinstance(a.extra_data, dict) else {}
        out.append(_entry(
            "activity", "contact_activity", a.id, a.created_at, contact_id=a.contact_id, canonical_id=canon,
            title=a.title or (a.activity_type or "activity").replace("_", " ").title(),
            summary=a.description, channel=a.touchpoint, actor_user_id=extra.get("user_id"),
            status=extra.get("outcome"), provenance=a.source or "manual",
        ))
    return out


def _messages(cid, ids, phones, emails, before, limit, canon):
    from models import TwilioConversation, TwilioMessage
    q = (
        db.session.query(TwilioMessage, TwilioConversation.contact_id)
        .join(TwilioConversation, TwilioConversation.id == TwilioMessage.conversation_id)
        .filter(TwilioMessage.company_id == cid, TwilioConversation.company_id == cid)
    )
    match = [TwilioConversation.contact_id.in_(ids)]
    if phones:
        match.append(db.and_(TwilioConversation.contact_id.is_(None), TwilioConversation.from_number.in_(phones)))
    q = q.filter(or_(*match))
    out = []
    for m, contact_id in _since(q, TwilioMessage.created_at, before).order_by(TwilioMessage.created_at.desc()).limit(limit):
        inbound = (m.direction or "").lower() == "inbound"
        out.append(_entry(
            "message", "twilio_message", m.id, m.created_at, contact_id=contact_id, canonical_id=canon,
            title="SMS received" if inbound else ("Auto-reply sent" if m.is_auto_reply else "SMS sent"),
            summary=m.body, channel="sms", direction=m.direction, status=m.status,
            provenance="twilio" if contact_id else "phone_match",
        ))
    return out


def _emails(cid, ids, phones, emails, before, limit, canon):
    from models import EmailMessage
    q = EmailMessage.query.filter(EmailMessage.company_id == cid, EmailMessage.contact_id.in_(ids))
    out = []
    for m in _since(q, EmailMessage.occurred_at, before).order_by(EmailMessage.occurred_at.desc()).limit(limit):
        inbound = m.direction == "inbound"
        out.append(_entry(
            "email", "email_message", m.id, m.occurred_at, contact_id=m.contact_id, canonical_id=canon,
            title=("Email received" if inbound else "Email sent") + (f": {m.subject}" if m.subject else ""),
            summary=m.body_text, channel="email", direction=m.direction, status=m.status,
            actor_user_id=m.user_id, provenance=m.provider or "email",
        ))
    return out


def _calls(cid, ids, phones, emails, before, limit, canon):
    from models import TwilioCallLog
    match = [TwilioCallLog.contact_id.in_(ids)]
    if phones:
        match.append(db.and_(TwilioCallLog.contact_id.is_(None),
                             or_(TwilioCallLog.from_number.in_(phones), TwilioCallLog.to_number.in_(phones))))
    q = TwilioCallLog.query.filter(TwilioCallLog.company_id == cid, or_(*match))
    out = []
    for c in _since(q, TwilioCallLog.created_at, before).order_by(TwilioCallLog.created_at.desc()).limit(limit):
        status = c.final_status or c.status
        inbound = (c.direction or "").lower() == "inbound"
        missed = (status or "").lower() in {"missed", "no-answer", "busy", "failed", "canceled"}
        title = ("Missed call" if missed else "Inbound call") if inbound else "Outbound call"
        out.append(_entry(
            "call", "twilio_call_log", c.id, c.created_at, contact_id=c.contact_id, canonical_id=canon,
            title=title, summary=c.transcription_text, channel="voice", direction=c.direction,
            status=status, provenance="twilio" if c.contact_id else "phone_match",
            extra={"duration_secs": c.duration or 0},
        ))
    return out


def _voicemails(cid, ids, phones, emails, before, limit, canon):
    from models import VoiceVoicemailMessage
    if not phones:
        return []
    q = VoiceVoicemailMessage.query.filter(VoiceVoicemailMessage.company_id == cid,
                                           VoiceVoicemailMessage.from_number.in_(phones))
    out = []
    for v in _since(q, VoiceVoicemailMessage.created_at, before).order_by(VoiceVoicemailMessage.created_at.desc()).limit(limit):
        out.append(_entry(
            "voicemail", "voice_voicemail_message", v.id, v.created_at, contact_id=None, canonical_id=canon,
            title="Voicemail", summary=v.transcription_text or v.transcript, channel="voice", direction="inbound",
            status=v.transcription_status, provenance="phone_match",
            extra={"duration_secs": v.duration_secs or 0},
        ))
    return out


def _tasks(cid, ids, phones, emails, before, limit, canon):
    from models import ContactTask
    q = ContactTask.query.filter(ContactTask.company_id == cid, ContactTask.contact_id.in_(ids))
    out = []
    for t in _since(q, ContactTask.created_at, before).order_by(ContactTask.created_at.desc()).limit(limit):
        out.append(_entry(
            "task", "contact_task", t.id, t.created_at, contact_id=t.contact_id, canonical_id=canon,
            title=f"Task: {t.title}", status=t.status, actor_user_id=t.assigned_user_id, provenance="crm",
            extra={"due_at": _iso(t.due_at), "completed_at": _iso(t.completed_at), "priority": t.priority},
        ))
    return out


def _opportunities(cid, ids, phones, emails, before, limit, canon):
    from models import Opportunity
    q = Opportunity.query.filter(Opportunity.company_id == cid, Opportunity.contact_id.in_(ids))
    out = []
    for o in _since(q, Opportunity.created_at, before).order_by(Opportunity.created_at.desc()).limit(limit):
        out.append(_entry(
            "opportunity", "opportunity", o.id, o.created_at, contact_id=o.contact_id, canonical_id=canon,
            title=f"Opportunity: {o.name}", summary=o.next_action, status=f"{o.status}/{o.stage}",
            actor_user_id=o.owner_user_id, provenance="crm",
            extra={"pipeline": o.pipeline, "estimated_value": float(o.estimated_value) if o.estimated_value is not None else None},
        ))
    return out


def _deals(cid, ids, phones, emails, before, limit, canon):
    from models import Deal, DealActivity
    q = (
        db.session.query(DealActivity, Deal)
        .join(Deal, Deal.id == DealActivity.deal_id)
        .filter(Deal.company_id == cid, Deal.contact_id.in_(ids))
    )
    out = []
    for a, deal in _since(q, DealActivity.activity_date, before).order_by(DealActivity.activity_date.desc()).limit(limit):
        out.append(_entry(
            "deal", "deal_activity", a.id, a.activity_date, contact_id=deal.contact_id, canonical_id=canon,
            title=f"Deal {deal.name or deal.id}: {(a.activity_type or 'note').replace('_', ' ')}",
            summary=a.description, status=deal.stage, actor_user_id=a.user_id, provenance="crm",
            extra={"deal_id": deal.id},
        ))
    return out


def _touchpoints(cid, ids, phones, emails, before, limit, canon):
    from models import TouchpointEvent
    q = TouchpointEvent.query.filter(TouchpointEvent.company_id == cid, TouchpointEvent.contact_id.in_(ids))
    out = []
    for t in _since(q, TouchpointEvent.created_at, before).order_by(TouchpointEvent.created_at.desc()).limit(limit):
        out.append(_entry(
            "touchpoint", "touchpoint_event", t.id, t.created_at, contact_id=t.contact_id, canonical_id=canon,
            title=(t.touchpoint_type or "touchpoint").replace("_", " ").title(), summary=t.page_url,
            channel="web", provenance=t.source or "tracking",
        ))
    return out


def _sources(cid, ids, phones, emails, before, limit, canon):
    from models import ContactSourceEvent
    q = ContactSourceEvent.query.filter(ContactSourceEvent.company_id == cid, ContactSourceEvent.contact_id.in_(ids))
    out = []
    for s in _since(q, ContactSourceEvent.event_at, before).order_by(ContactSourceEvent.event_at.desc()).limit(limit):
        detail = " · ".join(filter(None, [s.source_detail, s.campaign]))
        out.append(_entry(
            "source", "contact_source_event", s.id, s.event_at, contact_id=s.contact_id, canonical_id=canon,
            title=f"Source: {s.source} ({s.event_type})", summary=detail, actor_user_id=s.created_by_user_id,
            provenance=s.source,
        ))
    return out


_COLLECTORS = {
    "activity": _activities, "message": _messages, "email": _emails, "call": _calls, "voicemail": _voicemails,
    "task": _tasks, "opportunity": _opportunities, "deal": _deals, "touchpoint": _touchpoints,
    "source": _sources,
}


def build_timeline(company_id, contact_id, *, limit=50, before=None, kinds=None):
    """Return the merged timeline, or None when the contact isn't in this tenant.

    ``before`` (datetime) pages backwards; pass the previous ``next_before``.
    A source that fails to load is listed under ``unavailable`` instead of
    failing the whole timeline.
    """
    if not company_id:
        return None
    canonical = canonical_contact(company_id, contact_id)
    if canonical is None:
        return None
    limit = max(1, min(int(limit or 50), MAX_LIMIT))
    wanted = [k for k in (kinds or KINDS) if k in _COLLECTORS]
    ids, phones, emails = identity_cluster(company_id, canonical)

    entries, unavailable = [], []
    for kind in wanted:
        try:
            with db.session.begin_nested():
                entries.extend(_COLLECTORS[kind](company_id, ids, phones, emails, before, limit + 1, canonical.id))
        except SQLAlchemyError:
            logger.exception("timeline: %s source unavailable for company_id=%s", kind, company_id)
            unavailable.append(kind)

    entries.sort(key=lambda e: (e["at"] or "", e["id"]), reverse=True)
    page, more = entries[:limit], len(entries) > limit
    return {
        "contact_id": contact_id,
        "canonical_contact_id": canonical.id,
        "merged_contact_ids": sorted(ids - {canonical.id}),
        "identifiers": {"phones": phones, "emails": emails},
        "entries": page,
        "next_before": page[-1]["at"] if more and page else None,
        "unavailable": unavailable,
    }


def parse_before(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
