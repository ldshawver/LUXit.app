"""Actionable CRM (M3): pipeline, tasks, reminders, needs-attention, segments."""
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import (
    Company, Contact, ContactActivity, ContactTask, Notification, Opportunity, Segment, TwilioCallLog,
    TwilioConversation, TwilioMessage, User, UserCompanyAccess,
)
from services import crm_workspace as crm
from services import segment_engine


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
        a, b = Company(name=f"CRM A {tag}"), Company(name=f"CRM B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        ids = {"a": a.id, "b": b.id}
        for key, company, role in (("editor", a, "editor"), ("viewer", a, "viewer"), ("other", b, "owner")):
            u = User(username=f"crm-{key}-{tag}", email=f"crm-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            ids[key] = u.id
        alice = Contact(company_id=a.id, first_name="Alice", phone="+19168675331", is_active=True)
        bob = Contact(company_id=a.id, first_name="Bob", phone="+19168675332", is_active=True,
                      last_contacted_at=datetime.utcnow() - timedelta(days=60), lifecycle_stage="customer",
                      owner_user_id=ids["editor"])
        carol_b = Contact(company_id=b.id, first_name="Carol", phone="+19168675333", is_active=True)
        db.session.add_all([alice, bob, carol_b])
        db.session.flush()
        ids.update(alice=alice.id, bob=bob.id, carol_b=carol_b.id)
        db.session.commit()
        yield app, ids
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def test_opportunity_lifecycle_logs_stage_changes(env):
    app, ids = env
    c = _as(app, ids["editor"])
    r = c.post("/api/crm/opportunities", json={"contact_id": ids["alice"], "name": "Patio", "estimated_value": "5000"})
    assert r.status_code == 201
    opp_id = r.get_json()["opportunity_id"]
    assert c.post(f"/api/crm/opportunities/{opp_id}", json={"stage": "proposal", "probability": 50}).status_code == 200
    assert c.post(f"/api/crm/opportunities/{opp_id}", json={"stage": "bogus"}).status_code == 400
    r = c.post(f"/api/crm/opportunities/{opp_id}", json={"stage": "won", "reason": "Signed"})
    assert r.get_json()["status"] == "won"
    changes = ContactActivity.query.filter_by(contact_id=ids["alice"], activity_type="stage_change").all()
    assert [(a.extra_data["from_stage"], a.extra_data["to_stage"]) for a in changes] == [("new_lead", "proposal"),
                                                                                      ("proposal", "won")]
    board = c.get("/api/crm/pipeline").get_json()
    won = next(s for s in board["stages"] if s["stage"] == "won")
    assert won["count"] == 1 and won["value"] == 5000.0 and board["open_value"] == 0


def test_opportunity_permissions_and_isolation(env):
    app, ids = env
    assert _as(app, ids["viewer"]).post("/api/crm/opportunities",
                                        json={"contact_id": ids["alice"], "name": "X"}).status_code == 403
    # Other tenant's contact can't be used, and other tenant can't edit ours.
    r = _as(app, ids["editor"]).post("/api/crm/opportunities", json={"contact_id": ids["carol_b"], "name": "X"})
    assert r.status_code == 400
    opp = Opportunity(company_id=ids["a"], contact_id=ids["alice"], name="Ours")
    db.session.add(opp)
    db.session.commit()
    assert _as(app, ids["other"]).post(f"/api/crm/opportunities/{opp.id}", json={"stage": "lost"}).status_code == 404
    # Owner must belong to the workspace.
    r = _as(app, ids["editor"]).post(f"/api/crm/opportunities/{opp.id}", json={"owner_user_id": ids["other"]})
    assert r.status_code == 400
    assert _as(app, ids["other"]).get("/api/crm/pipeline").get_json()["open_value"] == 0


def test_tasks_buckets_complete_and_next_follow_up(env):
    app, ids = env
    c = _as(app, ids["editor"])
    now = datetime.utcnow()
    overdue = c.post("/api/crm/tasks", json={"contact_id": ids["alice"], "title": "Send quote",
                                             "due_at": (now - timedelta(hours=3)).isoformat()}).get_json()["task_id"]
    c.post("/api/crm/tasks", json={"contact_id": ids["alice"], "title": "Call next week",
                                   "due_at": (now + timedelta(days=7)).isoformat()})
    assert [t["id"] for t in c.get("/api/crm/tasks?bucket=overdue").get_json()["tasks"]] == [overdue]
    assert len(c.get("/api/crm/tasks?bucket=upcoming").get_json()["tasks"]) == 1
    contact = db.session.get(Contact, ids["alice"])
    assert contact.next_follow_up_at < now
    assert c.post(f"/api/crm/tasks/{overdue}/complete", headers={"Accept": "application/json"}).get_json()["status"] == "done"
    db.session.refresh(contact)
    assert contact.next_follow_up_at > now
    assert ContactActivity.query.filter_by(contact_id=ids["alice"], activity_type="task_completed").count() == 1
    # Viewer can't complete someone else's task; other tenant gets 404.
    assert _as(app, ids["viewer"]).post(f"/api/crm/tasks/{overdue}/reopen").status_code == 403
    assert _as(app, ids["other"]).post(f"/api/crm/tasks/{overdue}/reopen").status_code == 404


def test_reminders_fire_once_to_assignee(env):
    app, ids = env
    task = ContactTask(company_id=ids["a"], contact_id=ids["alice"], title="Ping Alice", status="open",
                       assigned_user_id=ids["editor"], reminder_at=datetime.utcnow() - timedelta(minutes=1))
    future = ContactTask(company_id=ids["a"], contact_id=ids["alice"], title="Later", status="open",
                         assigned_user_id=ids["editor"], reminder_at=datetime.utcnow() + timedelta(hours=1))
    db.session.add_all([task, future])
    db.session.commit()
    assert crm.send_due_reminders() == 1
    assert crm.send_due_reminders() == 0
    notes = Notification.query.filter_by(user_id=ids["editor"], event_type="task_reminder").all()
    assert len(notes) == 1 and "Ping Alice" in notes[0].title and notes[0].company_id == ids["a"]


def test_needs_attention_lists_unanswered_missed_and_stale(env):
    app, ids = env
    now = datetime.utcnow()
    conv = TwilioConversation(company_id=ids["a"], contact_id=ids["alice"], from_number="+19168675331",
                              to_number="+19165550100")
    conv_b = TwilioConversation(company_id=ids["b"], contact_id=ids["carol_b"], from_number="+19168675333",
                                to_number="+19165550199")
    db.session.add_all([conv, conv_b])
    db.session.flush()
    db.session.add_all([
        TwilioMessage(conversation_id=conv.id, company_id=ids["a"], direction="inbound", body="Are you open Sat?",
                      created_at=now - timedelta(hours=2)),
        TwilioMessage(conversation_id=conv_b.id, company_id=ids["b"], direction="inbound", body="B secret",
                      created_at=now - timedelta(hours=2)),
        TwilioCallLog(company_id=ids["a"], contact_id=ids["bob"], direction="inbound", status="no-answer",
                      created_at=now - timedelta(hours=1)),
        Opportunity(company_id=ids["a"], contact_id=ids["bob"], name="Renewal", status="open",
                    owner_user_id=ids["editor"]),
    ])
    db.session.commit()
    items = crm.needs_attention(ids["a"], user_id=ids["editor"], now=now)
    types = {(i["type"], i["contact_name"]) for i in items}
    assert ("unanswered_text", "Alice") in types
    assert ("missed_call", "Bob") in types
    assert ("stale_opportunity", "Bob") in types
    assert "B secret" not in repr(items)
    # A reply clears the unanswered text.
    db.session.add(TwilioMessage(conversation_id=conv.id, company_id=ids["a"], direction="outbound", body="Yes!",
                                 created_at=now - timedelta(minutes=30)))
    db.session.commit()
    assert "unanswered_text" not in {i["type"] for i in crm.needs_attention(ids["a"], now=now)}
    page = _as(app, ids["editor"]).get("/crm/today")
    assert page.status_code == 200 and b"Needs attention" in page.data and b"Return their call" in page.data


def test_relationship_segment_fields(env):
    app, ids = env
    db.session.add_all([
        Opportunity(company_id=ids["a"], contact_id=ids["bob"], name="Renewal", stage="proposal", status="open"),
        ContactTask(company_id=ids["a"], contact_id=ids["alice"], title="late", status="open",
                    due_at=datetime.utcnow() - timedelta(days=1)),
    ])
    seg = Segment(company_id=ids["a"], name="Stale customers in proposal", segment_type="custom", is_dynamic=True,
                  match_mode="all", conditions=[
                      {"field": "open_opportunity_stage", "op": "equals", "value": "Proposal"},
                      {"field": "last_contacted_at", "op": "more_than_days_ago", "value": 30},
                      {"field": "lifecycle_stage", "op": "equals", "value": "customer"},
                      {"field": "owner_user_id", "op": "equals", "value": ids["editor"]},
                  ])
    overdue = Segment(company_id=ids["a"], name="Overdue", segment_type="custom", is_dynamic=True, match_mode="all",
                      conditions=[{"field": "has_overdue_task", "op": "equals", "value": True}])
    db.session.add_all([seg, overdue])
    db.session.commit()
    assert segment_engine.compute_membership_delta(seg).desired_member_ids == {ids["bob"]}
    assert segment_engine.compute_membership_delta(overdue).desired_member_ids == {ids["alice"]}
    assert segment_engine.evaluate(overdue, db.session.get(Contact, ids["alice"])) is True
    assert segment_engine.evaluate(overdue, db.session.get(Contact, ids["bob"])) is False


@pytest.mark.parametrize("rule", [
    {"field": "last_contacted_at", "op": "within_last_days", "value": "7"},
    {"field": "last_contacted_at", "op": "within_last_days", "value": -1},
    {"field": "has_overdue_task", "op": "equals", "value": "yes"},
    {"field": "has_overdue_task", "op": "contains", "value": True},
    {"field": "open_opportunity_stage", "op": "after", "value": 1},
    {"field": "first_name", "op": "within_last_days", "value": 3},
])
def test_invalid_relationship_rules_fail_closed(rule):
    with pytest.raises(segment_engine.SegmentConditionError):
        segment_engine.validate_conditions([rule], "all")
