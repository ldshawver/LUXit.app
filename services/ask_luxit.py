"""Ask LUXit (M6): tenant-safe questions over the workspace's own CRM data.

Deterministic first: a question is routed to an intent (tasks, pipeline,
needs-attention, quiet contacts, commitments, one person, or keyword search)
and answered only from rows read inside ``company_id``. Every answer sentence
cites the results it was built from (``[n]`` → ``results[n-1]``), and every
result names its source row, so nothing in an answer is unsupported.

An AI layer may later rephrase an answer, but only from these results; it is
not wired while no AI provider is available (T-20).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import or_

from extensions import db

MAX_QUESTION = 500
MAX_RESULTS = 25
_SNIPPET = 200

_STOPWORDS = set("""
a an and any are about at be by can could did do does for from get give had has have how i in is it its last
list me my of on or our please recent recently s show tell that the their them there they this to us was we
were what when where which who whom why with you your all find search anything everything everyone someone
say said says mention mentioned talk talked discuss discussed latest
""".split())

_STAGE_WORDS = {
    "new lead": "new_lead", "new leads": "new_lead", "qualified": "qualified", "proposal": "proposal",
    "proposals": "proposal", "negotiation": "negotiation", "negotiating": "negotiation", "won": "won", "lost": "lost",
}
_UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30}

_RE_TASKS = re.compile(r"\b(tasks?|to-?dos?|follow[- ]?ups? (?:due|overdue|today))\b")
_RE_PIPELINE = re.compile(r"\b(pipeline|deals?|opportunit(?:y|ies))\b")
_RE_UNANSWERED = re.compile(r"\b(unanswered|unreplied|un-replied|need(?:s)? (?:a )?repl(?:y|ies)|waiting (?:on|for) (?:a )?repl)")
_RE_MISSED = re.compile(r"\bmissed calls?\b")
_RE_ATTENTION = re.compile(r"\b(needs? (?:my )?attention|what should i do|what(?:'s| is) next|my day)\b")
_RE_QUIET = re.compile(
    r"\b(?:haven'?t|have not|hasn'?t|not|no one|nobody)\b.*?\b(?:contact(?:ed)?|talk(?:ed)?|spoken|spoke|"
    r"heard|reach(?:ed)?|text(?:ed)?|call(?:ed)?)\b(?:.*?\b(\d{1,3})\s*(days?|weeks?|months?))?")
_RE_COMMIT = re.compile(r"\b(commitments?|promis(?:e|es|ed)|owe|i said i(?:'d| would))\b")


class AskError(ValueError):
    pass


def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"


def _snip(text):
    text = " ".join((text or "").split())
    return text if len(text) <= _SNIPPET else text[: _SNIPPET - 1] + "…"


def _iso(value):
    return value.isoformat() if value else None


def keywords(question):
    words = re.findall(r"[a-z0-9@.+'-]{2,}", question.lower())
    out = []
    for w in words:
        w = w.strip(".'-")
        if len(w) >= 2 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return out[:6]


class _Answer:
    def __init__(self, intent):
        self.intent = intent
        self.results = []
        self.sentences = []

    def cite(self, kind, table, pk, *, title, snippet=None, at=None, contact_id=None, link=None, extra=None):
        for i, r in enumerate(self.results):
            if r["source"] == {"table": table, "id": pk}:
                return i + 1
        self.results.append({
            "kind": kind, "title": title, "snippet": _snip(snippet), "at": _iso(at) if isinstance(at, datetime) else at,
            "contact_id": contact_id, "link": link or (f"/contacts/{contact_id}/activities" if contact_id else None),
            "source": {"table": table, "id": pk}, **({"extra": extra} if extra else {}),
        })
        return len(self.results)

    def say(self, text, refs=()):
        refs = sorted(set(r for r in refs if r))
        self.sentences.append({"text": text, "refs": refs})

    def to_dict(self, question):
        text = " ".join(s["text"] + "".join(f" [{n}]" for n in s["refs"][:8]) for s in self.sentences)
        return {
            "question": question, "intent": self.intent, "answer": text, "sentences": self.sentences,
            "results": self.results[:MAX_RESULTS], "grounded": True, "mode": "deterministic",
        }


# ------------------------------------------------------------------ intents

def _tasks(company_id, user_id, q, now, ans):
    from services import crm_workspace as crm
    bucket = "overdue" if re.search(r"\b(overdue|late|past due)\b", q) else "today" if "today" in q else \
        "upcoming" if re.search(r"\b(upcoming|this week|next)\b", q) else "open"
    mine = bool(re.search(r"\b(my|mine|me|i)\b", q)) and "everyone" not in q and "team" not in q
    rows = crm.list_tasks(company_id, assigned_user_id=user_id if mine else None, bucket=bucket, now=now, limit=MAX_RESULTS)
    label = {"open": "open", "overdue": "overdue", "today": "due today", "upcoming": "upcoming"}[bucket]
    whose = "You have" if mine else "The workspace has"
    if not rows:
        ans.say(f"{whose} no {label} tasks.")
        return
    refs = [ans.cite("task", "contact_task", t["id"], title=t["title"],
                     snippet=f"{t['contact_name']} · due {t['due_at'][:10] if t['due_at'] else 'no date'}"
                             + (" · overdue" if t["overdue"] else ""),
                     at=t["due_at"], contact_id=t["contact_id"]) for t in rows]
    more = " (showing the first 25)" if len(rows) >= MAX_RESULTS else ""
    ans.say(f"{whose} {len(rows)} {label} task{'s' if len(rows) != 1 else ''}{more}.", refs)


def _pipeline(company_id, user_id, q, now, ans):
    from services import crm_workspace as crm
    board = crm.pipeline_board(company_id)
    stage = next((v for k, v in _STAGE_WORDS.items() if re.search(rf"\b{k}\b", q)), None)
    stages = [s for s in board["stages"] if (s["stage"] == stage if stage else s["stage"] not in crm.CLOSED_STAGES)]
    items = [(s["stage"], i) for s in stages for i in s["items"]]
    if not items:
        ans.say(f"There are no {'opportunities in ' + stage.replace('_', ' ') if stage else 'open opportunities'}.")
        return
    items.sort(key=lambda x: -x[1]["value"])
    refs = [ans.cite("opportunity", "opportunity", i["id"], title=i["name"],
                     snippet=f"{i['contact_name']} · {st.replace('_', ' ')} · ${i['value']:,.0f}"
                             + (" · follow-up overdue" if i["follow_up_overdue"] else ""),
                     contact_id=i["contact_id"], extra={"stage": st, "value": i["value"]})
            for st, i in items[:MAX_RESULTS]]
    total = sum(i["value"] for _, i in items)
    where = f"in {stage.replace('_', ' ')}" if stage else "open"
    ans.say(f"{len(items)} opportunit{'y' if len(items) == 1 else 'ies'} {where}, worth ${total:,.0f} in total.", refs)
    if not stage:
        ans.say(f"Weighted by probability, the open pipeline is ${board['weighted_open_value']:,.0f}.", refs)
    overdue = [r for r, (_, i) in zip(refs, items) if i["follow_up_overdue"]]
    if overdue:
        ans.say(f"{len(overdue)} of them have an overdue follow-up.", overdue)


def _attention(company_id, user_id, q, now, ans, only=None):
    from services import crm_workspace as crm
    items = crm.needs_attention(company_id, user_id=None if only else user_id, now=now, limit=200)
    if only:
        items = [i for i in items if i["type"] in only]
    if not items:
        ans.say({"unanswered_text": "No texts are waiting for a reply in the last 14 days.",
                 "missed_call": "No missed calls are waiting to be returned in the last 7 days."}.get(
                 next(iter(only)) if only and len(only) == 1 else None, "Nothing needs attention right now."))
        return
    refs = []
    for i in items[:MAX_RESULTS]:
        ref = i.get("source_ref") or {}
        table, pk = ref.get("table") or i["type"], ref.get("id") or i["contact_id"]
        refs.append(ans.cite(i["type"], table, pk, title=f"{i['action']} — {i['contact_name']}",
                             snippet=i.get("reason"), at=i.get("at"), contact_id=i["contact_id"], link=i.get("link")))
    noun = {"unanswered_text": ("text is", "texts are", "waiting for a reply"),
            "missed_call": ("missed call is", "missed calls are", "waiting to be returned")}
    if only and len(only) == 1:
        one, many, tail = noun[next(iter(only))]
        ans.say(f"{len(items)} {one if len(items) == 1 else many} {tail}.", refs)
    else:
        ans.say(f"{len(items)} item{'s' if len(items) != 1 else ''} need attention, most urgent first.", refs)


def _quiet(company_id, user_id, q, now, ans, m):
    from models import Contact
    from services.crm_workspace import contact_name
    n, unit = (int(m.group(1)), m.group(2)) if m.group(1) else (30, "days")
    days = max(1, min(n * _UNITS.get(unit, 1), 3650))
    cutoff = now - timedelta(days=days)
    base = Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True),
                                Contact.merged_into_contact_id.is_(None), Contact.do_not_contact.is_(False))
    if re.search(r"\b(my|mine)\b", q):
        base = base.filter(Contact.owner_user_id == user_id)
    rows = (base.filter(Contact.last_contacted_at.isnot(None), Contact.last_contacted_at < cutoff)
            .order_by(Contact.last_contacted_at.asc()).limit(MAX_RESULTS).all())
    total = base.filter(Contact.last_contacted_at.isnot(None), Contact.last_contacted_at < cutoff).count()
    if not rows:
        ans.say(f"Every contact with a recorded last contact has been reached in the last {days} days.")
        return
    refs = [ans.cite("contact", "contact", c.id, title=contact_name(c),
                     snippet=f"last contacted {c.last_contacted_at.date().isoformat()}"
                             + (f" · {c.lifecycle_stage.replace('_', ' ')}" if c.lifecycle_stage else ""),
                     at=c.last_contacted_at, contact_id=c.id) for c in rows]
    more = f" (showing the {len(rows)} quietest)" if total > len(rows) else ""
    ans.say(f"{total} contact{'s have' if total != 1 else ' has'} not been contacted in over {days} days{more}.", refs)
    never = base.filter(Contact.last_contacted_at.is_(None)).count()
    if never:
        ans.say(f"{never} more have no recorded contact at all.")


def _commitments(company_id, user_id, q, now, ans, contact_ids=None):
    from models import Contact, RelationshipMemory
    from services.crm_workspace import contact_name
    query = (db.session.query(RelationshipMemory, Contact).join(Contact, Contact.id == RelationshipMemory.contact_id)
             .filter(RelationshipMemory.company_id == company_id, Contact.company_id == company_id,
                     RelationshipMemory.kind.in_(("commitment", "follow_up")),
                     RelationshipMemory.review_state != "rejected", RelationshipMemory.task_id.is_(None)))
    if contact_ids:
        query = query.filter(RelationshipMemory.contact_id.in_(contact_ids))
    rows = query.order_by(RelationshipMemory.due_at.asc().nullslast(), RelationshipMemory.id.desc()).limit(MAX_RESULTS).all()
    if not rows:
        ans.say("No open commitments or follow-ups are recorded.")
        return
    refs, overdue = [], []
    for mem, c in rows:
        text = mem.corrected_text or mem.text
        state = "" if mem.review_state == "confirmed" else " · unreviewed"
        r = ans.cite(mem.kind, "relationship_memory", mem.id,
                     title=f"{'Commitment' if mem.kind == 'commitment' else 'Follow-up'} — {contact_name(c)}",
                     snippet=text + (f" · due {mem.due_at.date().isoformat()}" if mem.due_at else "") + state,
                     at=mem.due_at or mem.created_at, contact_id=c.id)
        refs.append(r)
        if mem.due_at and mem.due_at < now:
            overdue.append(r)
    ans.say(f"{len(rows)} open commitment{'s' if len(rows) != 1 else ''} or follow-up{'s' if len(rows) != 1 else ''}"
            " not yet turned into a task.", refs)
    if overdue:
        ans.say(f"{len(overdue)} {'is' if len(overdue) == 1 else 'are'} past due.", overdue)


def match_contacts(company_id, phrase, limit=5):
    """Contacts in this tenant whose name/company matches every word of ``phrase``."""
    from models import Contact
    words = [w for w in re.findall(r"[a-z0-9'@.+-]{2,}", phrase.lower()) if w not in _STOPWORDS][:4]
    if not words:
        return []
    q = Contact.query.filter(Contact.company_id == company_id, Contact.merged_into_contact_id.is_(None))
    for w in words:
        pat = _like(w)
        q = q.filter(or_(Contact.first_name.ilike(pat, escape="\\"), Contact.last_name.ilike(pat, escape="\\"),
                         Contact.display_name.ilike(pat, escape="\\"), Contact.name.ilike(pat, escape="\\"),
                         Contact.company.ilike(pat, escape="\\"), Contact.business_name.ilike(pat, escape="\\"),
                         Contact.email.ilike(pat, escape="\\")))
    return q.order_by(Contact.last_contacted_at.desc().nullslast(), Contact.id.asc()).limit(limit).all()


def resolve_contact(company_id, terms):
    """The one contact most of ``terms`` point at, or None when no single contact stands out.

    Each term is matched on its own; a term matching many contacts is too
    generic to identify anyone and is ignored.
    """
    scores, by_id = {}, {}
    for term in terms:
        matches = match_contacts(company_id, term, limit=6)
        if not matches or len(matches) > 5:
            continue
        for c in matches:
            scores[c.id] = scores.get(c.id, 0) + 1
            by_id[c.id] = c
    if not scores:
        return None
    best = max(scores.values())
    top = [cid for cid, n in scores.items() if n == best]
    return by_id[top[0]] if len(top) == 1 else None


def _person(company_id, user_id, q, now, ans, contact, terms):
    from services.contact_timeline import build_timeline
    from services.crm_workspace import contact_name
    from services.relationship_intelligence import build_briefing
    name = contact_name(contact)
    tl = build_timeline(company_id, contact.id, limit=200) or {"entries": []}
    entries = tl["entries"]
    name_words = set(re.findall(r"[a-z0-9]+", name.lower()))
    topic = [t for t in terms if t not in name_words]
    if topic:
        hits = [e for e in entries if all(t in f"{e['title']} {e['summary'] or ''}".lower() for t in topic)] or \
               [e for e in entries if any(t in f"{e['title']} {e['summary'] or ''}".lower() for t in topic)]
        if hits:
            refs = [ans.cite(e["kind"], e["source"]["table"], e["source"]["id"], title=f"{e['title']} — {name}",
                             snippet=e["summary"], at=e["at"], contact_id=contact.id) for e in hits[:MAX_RESULTS]]
            ans.say(f"{len(hits)} record{'s' if len(hits) != 1 else ''} with {name} mention "
                    f"{' and '.join(repr(t) for t in topic)}; the newest is from {hits[0]['at'][:10] if hits[0]['at'] else 'an unknown date'}.",
                    refs)
            return
        ans.say(f"Nothing in {name}'s history mentions {' or '.join(repr(t) for t in topic)}.")
    brief = build_briefing(company_id, contact.id, now=now) or {}
    contact_ref = ans.cite("contact", "contact", contact.id, title=name, contact_id=contact.id,
                           snippet=" · ".join(filter(None, [contact.company or contact.business_name,
                                                            (contact.lifecycle_stage or "").replace("_", " ")])))
    activity = brief.get("activity") or {}
    last_in, last_out = activity.get("last_inbound"), activity.get("last_outbound")
    for label, e in (("heard from", last_in), ("reached out to", last_out)):
        if e:
            r = ans.cite(e["kind"], e["source"]["table"], e["source"]["id"], title=f"{e['title']} — {name}",
                         snippet=e.get("summary"), at=e.get("at"), contact_id=contact.id)
            ans.say(f"You last {label} {name} on {(e.get('at') or '')[:10]} ({e['title'].lower()}).", [r])
    if not (last_in or last_out):
        ans.say(f"No messages, calls or emails with {name} are recorded.", [contact_ref])
    by_source = {(e["source"]["table"], str(e["source"]["id"])): e for e in entries}
    for rec in (brief.get("recommendations") or [])[:3]:
        refs = []
        for sref in rec.get("source_refs") or []:
            table, pk = sref.get("table"), sref.get("id")
            e = by_source.get((table, str(pk)))
            refs.append(ans.cite(e["kind"] if e else table, table, pk, contact_id=contact.id,
                                 title=(e["title"] if e else table.replace("_", " ").capitalize()) + f" — {name}",
                                 snippet=e["summary"] if e else rec.get("reason"), at=e["at"] if e else None))
        reason = rec.get("reason") or ""
        ans.say(f"Suggested next step: {rec['action']}" + (f" — {reason.rstrip('.')}" if reason else "") + ".",
                refs or [contact_ref])


def _search(company_id, user_id, q, now, ans, terms):
    """Keyword search across the workspace's records; rows must contain every term."""
    from models import (Contact, ContactActivity, ContactTask, EmailMessage, Opportunity, RelationshipMemory,
                        TwilioConversation, TwilioMessage)
    from services.crm_workspace import contact_name
    if not terms:
        ans.say("Ask about a person, a topic, your tasks, your pipeline, or who needs a reply.")
        return
    pats = [_like(t) for t in terms]

    def all_of(*cols):
        return [or_(*[c.ilike(p, escape="\\") for c in cols]) for p in pats]

    found = []
    contacts = (Contact.query.filter(Contact.company_id == company_id, Contact.merged_into_contact_id.is_(None),
                                     *all_of(Contact.first_name, Contact.last_name, Contact.display_name, Contact.name,
                                             Contact.company, Contact.business_name, Contact.email, Contact.phone,
                                             Contact.tags))
                .limit(10).all())
    for c in contacts:
        found.append((c.last_contacted_at or c.created_at, "contact", "contact", c.id, contact_name(c),
                      " · ".join(filter(None, [c.company or c.business_name, c.email, c.phone])), c.id))
    msgs = (db.session.query(TwilioMessage, TwilioConversation.contact_id)
            .join(TwilioConversation, TwilioConversation.id == TwilioMessage.conversation_id)
            .filter(TwilioMessage.company_id == company_id, TwilioConversation.company_id == company_id,
                    *all_of(TwilioMessage.body))
            .order_by(TwilioMessage.created_at.desc()).limit(15).all())
    for m, cid in msgs:
        found.append((m.created_at, "message", "twilio_message", m.id,
                      "SMS received" if (m.direction or "").lower() == "inbound" else "SMS sent", m.body, cid))
    for e in (EmailMessage.query.filter(EmailMessage.company_id == company_id,
                                        *all_of(EmailMessage.subject, EmailMessage.body_text))
              .order_by(EmailMessage.occurred_at.desc()).limit(10)):
        found.append((e.occurred_at, "email", "email_message", e.id, f"Email: {e.subject or '(no subject)'}",
                      e.body_text, e.contact_id))
    for a in (ContactActivity.query.filter(ContactActivity.company_id == company_id,
                                           *all_of(ContactActivity.title, ContactActivity.description))
              .order_by(ContactActivity.created_at.desc()).limit(10)):
        found.append((a.created_at, "activity", "contact_activity", a.id, a.title or "Note", a.description, a.contact_id))
    for t in (ContactTask.query.filter(ContactTask.company_id == company_id, *all_of(ContactTask.title))
              .order_by(ContactTask.created_at.desc()).limit(10)):
        found.append((t.due_at or t.created_at, "task", "contact_task", t.id, f"Task: {t.title}", t.status, t.contact_id))
    for o in (Opportunity.query.filter(Opportunity.company_id == company_id,
                                       *all_of(Opportunity.name, Opportunity.next_action))
              .order_by(Opportunity.updated_at.desc()).limit(10)):
        found.append((o.updated_at, "opportunity", "opportunity", o.id, f"Opportunity: {o.name}",
                      f"{o.stage.replace('_', ' ')} · {o.next_action or ''}", o.contact_id))
    for r in (RelationshipMemory.query.filter(RelationshipMemory.company_id == company_id,
                                              RelationshipMemory.review_state != "rejected",
                                              *all_of(RelationshipMemory.text, RelationshipMemory.corrected_text))
              .order_by(RelationshipMemory.created_at.desc()).limit(10)):
        found.append((r.created_at, r.kind, "relationship_memory", r.id, r.kind.replace("_", " ").title(),
                      r.corrected_text or r.text, r.contact_id))

    # A row is only ever attached to a contact of this tenant.
    contact_ids = {f[6] for f in found if f[6]}
    names = {c.id: contact_name(c) for c in Contact.query.filter(Contact.company_id == company_id,
                                                                  Contact.id.in_(contact_ids))} if contact_ids else {}
    found = [f for f in found if not f[6] or f[6] in names]
    if not found:
        ans.say(f"Nothing in this workspace matches {' and '.join(repr(t) for t in terms)}.")
        return
    found.sort(key=lambda f: f[0] or datetime.min, reverse=True)
    refs = [ans.cite(kind, table, pk, title=title + (f" — {names[cid]}" if cid in names and kind != "contact" else ""),
                     snippet=snippet, at=at, contact_id=cid) for at, kind, table, pk, title, snippet, cid in found[:MAX_RESULTS]]
    by_kind = {}
    for f in found:
        by_kind[f[1]] = by_kind.get(f[1], 0) + 1
    parts = ", ".join(f"{n} {k.replace('_', ' ')}{'s' if n != 1 else ''}" for k, n in sorted(by_kind.items(), key=lambda x: -x[1]))
    ans.say(f"Found {len(found)} record{'s' if len(found) != 1 else ''} matching "
            f"{' and '.join(repr(t) for t in terms)}: {parts}. Newest first.", refs)


# -------------------------------------------------------------------- entry

def ask(company_id, user, question, now=None):
    """Answer ``question`` from ``company_id``'s data only. Raises AskError on bad input."""
    if not company_id:
        raise AskError("No workspace selected.")
    question = " ".join((question or "").split())
    if not question:
        raise AskError("Ask a question.")
    if len(question) > MAX_QUESTION:
        raise AskError(f"Questions are limited to {MAX_QUESTION} characters.")
    now = now or datetime.utcnow()
    q = question.lower()
    user_id = getattr(user, "id", None)

    if _RE_UNANSWERED.search(q) or (re.search(r"\btexts?\b", q) and re.search(r"\brepl", q)):
        ans = _Answer("unanswered_texts")
        _attention(company_id, user_id, q, now, ans, only={"unanswered_text"})
    elif _RE_MISSED.search(q):
        ans = _Answer("missed_calls")
        _attention(company_id, user_id, q, now, ans, only={"missed_call"})
    elif _RE_ATTENTION.search(q):
        ans = _Answer("needs_attention")
        _attention(company_id, user_id, q, now, ans)
    elif _RE_TASKS.search(q):
        ans = _Answer("tasks")
        _tasks(company_id, user_id, q, now, ans)
    elif _RE_PIPELINE.search(q):
        ans = _Answer("pipeline")
        _pipeline(company_id, user_id, q, now, ans)
    elif _RE_QUIET.search(q):
        ans = _Answer("quiet_contacts")
        _quiet(company_id, user_id, q, now, ans, _RE_QUIET.search(q))
    elif _RE_COMMIT.search(q):
        ans = _Answer("commitments")
        _commitments(company_id, user_id, q, now, ans)
    else:
        terms = keywords(q)
        contact = resolve_contact(company_id, terms)
        if contact is not None:
            ans = _Answer("contact")
            _person(company_id, user_id, q, now, ans, contact, terms)
        else:
            ans = _Answer("search")
            _search(company_id, user_id, q, now, ans, terms)
    return ans.to_dict(question)
