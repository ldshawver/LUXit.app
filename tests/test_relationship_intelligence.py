"""Relationship intelligence (M2): briefings, detection, review, tenant safety."""
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import (
    Company, Contact, ContactTask, Opportunity, RelationshipMemory, TwilioCallLog, TwilioConversation, TwilioMessage,
    User, UserCompanyAccess, VoiceVoicemailMessage,
)
from services import relationship_intelligence as ri

PHONE = "+19168675321"
NOW = datetime(2026, 3, 2, 9, 0)  # a Monday


class _FreshUserClient(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        a, b = Company(name=f"RI A {tag}"), Company(name=f"RI B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        c = Contact(company_id=a.id, first_name="Dana", last_name="Lee", phone=PHONE, email=f"dana-{tag}@example.com")
        db.session.add(c)
        db.session.flush()
        conv = TwilioConversation(company_id=a.id, contact_id=c.id, from_number=PHONE, to_number="+19165550100")
        db.session.add(conv)
        db.session.flush()
        users = {}
        for key, company, role in (("editor", a, "editor"), ("viewer", a, "viewer"), ("other", b, "owner")):
            u = User(username=f"ri-{key}-{tag}", email=f"ri-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            users[key] = u.id
        db.session.add_all([
            TwilioMessage(conversation_id=conv.id, company_id=a.id, direction="outbound",
                          body="Thanks Dana! I'll send the estimate by Friday.", created_at=NOW - timedelta(days=3)),
            TwilioMessage(conversation_id=conv.id, company_id=a.id, direction="inbound",
                          body="Great. Can you also include the tile options?", created_at=NOW - timedelta(days=1)),
            TwilioCallLog(company_id=a.id, contact_id=c.id, direction="inbound", from_number=PHONE,
                          to_number="+19165550100", status="no-answer", created_at=NOW - timedelta(hours=5)),
            VoiceVoicemailMessage(company_id=a.id, from_number=PHONE, to_number="+19165550100",
                                  transcription_text="Hi it's Dana, please call me back tomorrow.",
                                  created_at=NOW - timedelta(hours=4)),
            ContactTask(company_id=a.id, contact_id=c.id, title="Measure kitchen", status="open",
                        due_at=NOW - timedelta(days=2)),
            Opportunity(company_id=a.id, contact_id=c.id, name="Kitchen remodel", status="open"),
        ])
        db.session.commit()
        yield app, {"a": a.id, "b": b.id, "contact": c.id, **users}
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def test_scan_detects_commitments_and_follow_ups_with_sources(env):
    app, ids = env
    created = ri.scan_contact(ids["a"], ids["contact"])
    kinds = sorted((m.kind, m.text) for m in created)
    assert ("commitment", "I'll send the estimate by Friday") in kinds
    assert ("follow_up", "Can you also include the tile options?") in kinds or \
        ("follow_up", "Can you also include the tile options") in kinds
    assert any(k == "follow_up" and "call me back tomorrow" in t for k, t in kinds)
    commit = next(m for m in created if m.kind == "commitment")
    assert commit.review_state == "pending" and commit.origin == "rule"
    assert commit.source_refs[0]["table"] == "twilio_message"
    assert commit.due_at.weekday() == 4  # Friday
    db.session.commit()
    # Idempotent: rescanning adds nothing.
    assert ri.scan_contact(ids["a"], ids["contact"]) == []


def test_rejected_suggestion_never_returns(env):
    app, ids = env
    created = ri.scan_contact(ids["a"], ids["contact"])
    for m in created:
        ri.review(m, None, "reject")
    db.session.commit()
    assert ri.scan_contact(ids["a"], ids["contact"]) == []
    briefing = ri.build_briefing(ids["a"], ids["contact"], now=NOW)
    assert briefing["memories"] == []


def test_briefing_recommends_reply_return_call_and_overdue_task(env):
    app, ids = env
    b = ri.build_briefing(ids["a"], ids["contact"], now=NOW)
    actions = [r["action"] for r in b["recommendations"]]
    assert "Reply to their text" in actions
    assert "Return their call" in actions
    assert "Complete overdue task: Measure kitchen" in actions
    for r in b["recommendations"]:
        assert r["source_refs"], r
    assert b["consent"]["do_not_contact"] is False
    assert b["open_opportunities"][0]["name"] == "Kitchen remodel"
    assert b["activity"]["last_inbound"]["kind"] in ("voicemail", "call", "message")
    assert "Last 30 days" in b["summary"] or "No conversations" in b["summary"]


def test_do_not_contact_suppresses_outreach(env):
    app, ids = env
    c = db.session.get(Contact, ids["contact"])
    c.do_not_contact = True
    db.session.commit()
    b = ri.build_briefing(ids["a"], ids["contact"], now=NOW)
    assert [r["action"] for r in b["recommendations"]] == ["Do not contact"]


def test_review_correct_and_convert_to_task(env):
    app, ids = env
    created = ri.scan_contact(ids["a"], ids["contact"])
    db.session.commit()
    commit = next(m for m in created if m.kind == "commitment")
    c = _as(app, ids["editor"])
    r = c.post(f"/api/contacts/{ids['contact']}/memories/{commit.id}/review",
               json={"action": "correct", "text": "Send estimate with tile options by Friday"})
    body = r.get_json()
    assert r.status_code == 200 and body["memory"]["review_state"] == "confirmed" and body["memory"]["corrected"]
    r = c.post(f"/api/contacts/{ids['contact']}/memories/{commit.id}/task")
    task = db.session.get(ContactTask, r.get_json()["task_id"])
    assert task.title == "Send estimate with tile options by Friday" and task.company_id == ids["a"]
    # Converting twice returns the same task.
    again = c.post(f"/api/contacts/{ids['contact']}/memories/{commit.id}/task").get_json()
    assert again["task_id"] == task.id


def test_api_permissions_and_tenant_isolation(env):
    app, ids = env
    contact = ids["contact"]
    assert _as(app, ids["viewer"]).get(f"/api/contacts/{contact}/briefing").status_code == 200
    assert _as(app, ids["viewer"]).post(f"/api/contacts/{contact}/memories/scan").status_code == 403
    assert _as(app, ids["other"]).get(f"/api/contacts/{contact}/briefing").status_code == 404
    assert _as(app, ids["other"]).post(f"/api/contacts/{contact}/memories/scan").status_code == 404
    r = _as(app, ids["editor"]).post(f"/api/contacts/{contact}/memories/scan")
    assert r.status_code == 200 and r.get_json()["created"]
    mid = r.get_json()["created"][0]["id"]
    assert _as(app, ids["other"]).post(f"/api/contacts/{contact}/memories/{mid}/review",
                                       json={"action": "confirm"}).status_code == 404
    assert db.session.get(RelationshipMemory, mid).review_state == "pending"


def test_user_added_fact_is_confirmed(env):
    app, ids = env
    r = _as(app, ids["editor"]).post(f"/api/contacts/{ids['contact']}/memories",
                                     json={"kind": "fact", "text": "Prefers texts after 5pm"})
    assert r.status_code == 201
    m = r.get_json()["memory"]
    assert m["origin"] == "user" and m["review_state"] == "confirmed"
    bad = _as(app, ids["editor"]).post(f"/api/contacts/{ids['contact']}/memories", json={"kind": "fact", "text": " "})
    assert bad.status_code == 400


def test_due_date_phrases():
    at = datetime(2026, 3, 2, 10, 0)  # Monday
    assert ri._due_from_text("call me back tomorrow", at).day == 3
    assert ri._due_from_text("by friday", at).weekday() == 4
    assert ri._due_from_text("next week", at).day == 9
    assert ri._due_from_text("whenever", at) is None


def test_sms_opt_out_switches_reply_channel(env):
    app, ids = env
    c = db.session.get(Contact, ids["contact"])
    c.sms_opted_out = True
    db.session.commit()
    actions = [r["action"] for r in ri.build_briefing(ids["a"], ids["contact"], now=NOW)["recommendations"]]
    assert "Reply to their text" not in actions and "Reply by phone or email" in actions


def test_contact_page_shows_briefing_and_hides_edit_for_viewers(env):
    app, ids = env
    editor = _as(app, ids["editor"]).get(f"/contacts/{ids['contact']}/activities")
    assert editor.status_code == 200 and b"relationshipBriefing" in editor.data and b'id="brScan"' in editor.data
    viewer = _as(app, ids["viewer"]).get(f"/contacts/{ids['contact']}/activities")
    assert viewer.status_code == 200 and b'id="brScan"' not in viewer.data
