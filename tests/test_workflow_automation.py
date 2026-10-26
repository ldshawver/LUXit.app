"""Workflow automation (M5): triggers, conditions, actions, idempotency, failure
isolation, approvals, dry-run, history, permissions. No provider is called."""
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import (
    Company, Contact, ContactActivity, ContactTask, CRMAutomationExecution, Opportunity, Segment, SegmentMember,
    TwilioConversation, User, UserCompanyAccess,
)
from services import automation_builder as builder
from services import crm_automation as engine
from services import crm_workspace as crm
from services.crm_events import emit


class _FreshUserClient(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env(monkeypatch):
    sent = []
    import services.sms_send_idempotency as idem
    monkeypatch.setattr(idem, "send_with_idempotency",
                        lambda **kw: sent.append(kw) or {"success": True, "sid": "SM1", "status": "sent"})
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        a, b = Company(name=f"WF A {tag}"), Company(name=f"WF B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        ids = {"a": a.id, "b": b.id}
        for key, company, role in (("admin", a, "admin"), ("editor", a, "editor"), ("viewer", a, "viewer"),
                                   ("other", b, "owner")):
            u = User(username=f"wf-{key}-{tag}", email=f"wf-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            ids[key] = u.id
        c = Contact(company_id=a.id, first_name="Riley", phone="+19168675351", is_active=True, tags="vip",
                    owner_user_id=ids["editor"], lifecycle_stage="customer")
        db.session.add(c)
        db.session.flush()
        db.session.add(TwilioConversation(company_id=a.id, contact_id=c.id, from_number="+19168675351",
                                          to_number="+19165550121"))
        seg = Segment(company_id=a.id, name="VIPs", segment_type="custom")
        db.session.add(seg)
        db.session.flush()
        db.session.add(SegmentMember(segment_id=seg.id, contact_id=c.id))
        db.session.commit()
        ids.update(contact=c.id, segment=seg.id)
        yield app, ids, sent
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def _rule(company_id, name, triggers, conditions, actions, active=True):
    r = builder.save_rule(company_id, {"name": name, "triggers": triggers, "conditions": conditions,
                                       "actions": actions, "is_active": active})
    db.session.commit()
    return r


def test_missed_call_creates_follow_up_once(env):
    app, ids, _ = env
    _rule(ids["a"], "Missed call follow-up", ["call.missed"],
          {"consent": "contactable", "contact_tags_any": ["VIP"]},
          [{"type": "task.create", "title": "Call {first_name} back", "due_in_hours": 2, "assign_to": "owner"}])
    payload = {"contact_id": ids["contact"], "event_key": "twilio-call:CA1:missed"}
    emit(ids["a"], "call.missed", payload)
    emit(ids["a"], "call.missed", payload)  # Twilio retry
    db.session.commit()
    tasks = ContactTask.query.filter_by(contact_id=ids["contact"]).all()
    assert [(t.title, t.assigned_user_id) for t in tasks] == [("Call Riley back", ids["editor"])]
    assert CRMAutomationExecution.query.filter_by(company_id=ids["a"], status="completed").count() == 1


def test_stage_change_trigger_and_loop_guard(env):
    app, ids, _ = env
    _rule(ids["a"], "Proposal note", ["opportunity.stage_changed"], {"to_stage": ["proposal"]},
          [{"type": "note.add", "text": "Send the proposal checklist"},
           {"type": "opportunity.update", "stage": "negotiation"}])
    _rule(ids["a"], "Back to proposal", ["opportunity.stage_changed"], {"to_stage": ["negotiation"]},
          [{"type": "opportunity.update", "stage": "proposal"}])
    opp = crm.create_opportunity(ids["a"], None, {"contact_id": ids["contact"], "name": "Upgrade"})
    crm.update_opportunity(opp, None, {"stage": "proposal"})
    db.session.commit()
    notes = ContactActivity.query.filter_by(contact_id=ids["contact"], activity_type="note").count()
    assert notes == 1
    # The ping-pong stops at the depth limit instead of looping forever.
    assert opp.stage in {"negotiation", "proposal"}
    assert CRMAutomationExecution.query.filter_by(company_id=ids["a"]).count() <= 4


def test_task_completed_and_conditions(env):
    app, ids, _ = env
    _rule(ids["a"], "After follow-up", ["task.completed"],
          {"segment_ids_any": [ids["segment"]], "lifecycle_stage": ["customer"], "owner_user_id": ids["editor"],
           "no_open_tasks": True},
          [{"type": "follow_up.create", "title": "Check satisfaction", "due_in_days": 7}])
    task = crm.create_task(ids["a"], None, {"contact_id": ids["contact"], "title": "Install"})
    db.session.commit()
    crm.complete_task(task, None)
    db.session.commit()
    follow = ContactTask.query.filter_by(contact_id=ids["contact"], title="Check satisfaction").one()
    assert follow.due_at > datetime.utcnow() + timedelta(days=6)


def test_failed_action_is_reported_and_others_still_run(env):
    app, ids, _ = env
    _rule(ids["a"], "Mixed", ["inbound_sms.received"], {},
          [{"type": "opportunity.update", "next_action": "x"},  # no opportunity on this event → fails
           {"type": "note.add", "text": "Texted us"}])
    emit(ids["a"], "inbound_sms.received", {"contact_id": ids["contact"], "event_key": "twilio:SM9:received"})
    db.session.commit()
    rows = {r.action_index: r for r in CRMAutomationExecution.query.filter_by(company_id=ids["a"]).all()}
    assert rows[0].status == "failed" and "opportunity" in rows[0].details["error"]
    assert rows[1].status == "completed"
    summary = builder.failure_summary(ids["a"])
    assert sum(summary["failed_by_rule"].values()) == 1 and summary["recent"][0]["error"]


def test_message_waits_for_approval_then_sends_with_consent(env):
    app, ids, sent = env
    _rule(ids["a"], "Missed call text", ["call.missed"], {"consent": "sms_allowed"},
          [{"type": "message.send", "channel": "sms", "body": "Sorry we missed you, {first_name}!",
            "approval": "required"}])
    emit(ids["a"], "call.missed", {"contact_id": ids["contact"], "event_key": "twilio-call:CA2:missed"})
    db.session.commit()
    assert sent == []
    waiting = builder.history(ids["a"], status="awaiting_approval")
    assert waiting[0]["message"]["body"] == "Sorry we missed you, Riley!"
    ex_id = waiting[0]["id"]
    assert _as(app, ids["viewer"]).post(f"/api/automations/approvals/{ex_id}", json={"decision": "approve"}).status_code == 403
    assert _as(app, ids["other"]).post(f"/api/automations/approvals/{ex_id}", json={"decision": "approve"}).status_code == 404
    r = _as(app, ids["editor"]).post(f"/api/automations/approvals/{ex_id}", json={"decision": "approve"})
    assert r.get_json()["status"] == "completed" and sent[0]["idempotency_key"] == f"automation:{ex_id}"
    again = _as(app, ids["editor"]).post(f"/api/automations/approvals/{ex_id}", json={"decision": "approve"})
    assert again.status_code == 409 and len(sent) == 1


def test_approval_rechecks_consent(env):
    app, ids, sent = env
    _rule(ids["a"], "Text", ["call.missed"], {}, [{"type": "message.send", "channel": "sms", "body": "Hi",
                                                    "approval": "required"}])
    emit(ids["a"], "call.missed", {"contact_id": ids["contact"], "event_key": "k-consent"})
    db.session.get(Contact, ids["contact"]).sms_opted_out = True
    db.session.commit()
    ex_id = builder.history(ids["a"], status="awaiting_approval")[0]["id"]
    r = _as(app, ids["editor"]).post(f"/api/automations/approvals/{ex_id}", json={"decision": "approve"})
    assert r.get_json()["status"] == "failed" and "sms_opted_out" in r.get_json()["error"] and sent == []


def test_inactivity_scan_fires_once_per_quiet_period(env):
    app, ids, _ = env
    c = db.session.get(Contact, ids["contact"])
    c.last_contacted_at = datetime.utcnow() - timedelta(days=45)
    db.session.commit()
    _rule(ids["a"], "Win-back", ["contact.inactive"], {"no_communication_days": 30},
          [{"type": "task.create", "title": "Reconnect with {name}"}])
    assert builder.scan_inactive(ids["a"]) == 1
    assert builder.scan_inactive(ids["a"]) == 0
    db.session.commit()
    assert ContactTask.query.filter_by(contact_id=ids["contact"], title="Reconnect with Riley").count() == 1


def test_cooldown_condition(env):
    app, ids, _ = env
    _rule(ids["a"], "Once a week", ["inbound_sms.received"], {"cooldown_days": 7},
          [{"type": "note.add", "text": "hello"}])
    emit(ids["a"], "inbound_sms.received", {"contact_id": ids["contact"], "event_key": "e1"})
    emit(ids["a"], "inbound_sms.received", {"contact_id": ids["contact"], "event_key": "e2"})
    db.session.commit()
    assert ContactActivity.query.filter_by(contact_id=ids["contact"], activity_type="note").count() == 1


@pytest.mark.parametrize("definition,error", [
    ({"triggers": ["call.missed"], "conditions": {}, "actions": [{"type": "message.send", "channel": "sms",
                                                                  "body": "x", "approval": "auto"}]}, "approval"),
    ({"triggers": ["contact.inactive"], "conditions": {}, "actions": [{"type": "note.add", "text": "x"}]},
     "no_communication_days"),
    ({"triggers": ["call.missed"], "conditions": {"to_stage": ["won"]}, "actions": [{"type": "note.add", "text": "x"}]},
     "stage"),
    ({"triggers": ["call.missed"], "conditions": {"bogus": 1}, "actions": [{"type": "note.add", "text": "x"}]},
     "unsupported"),
    ({"triggers": ["call.missed"], "conditions": {}, "actions": [{"type": "task.create"}]}, "title"),
    ({"triggers": ["nope"], "conditions": {}, "actions": [{"type": "note.add", "text": "x"}]}, "unsupported"),
])
def test_invalid_definitions_rejected(env, definition, error):
    app, ids, _ = env
    with pytest.raises(builder.BuilderError) as exc:
        builder.save_rule(ids["a"], {"name": "bad", **definition})
    assert error in str(exc.value)


def test_cross_tenant_references_rejected(env):
    app, ids, _ = env
    other_seg = Segment(company_id=ids["b"], name="theirs", segment_type="custom")
    db.session.add(other_seg)
    db.session.commit()
    with pytest.raises(builder.BuilderError):
        builder.save_rule(ids["a"], {"name": "x", "triggers": ["call.missed"],
                                     "conditions": {"segment_ids_any": [other_seg.id]},
                                     "actions": [{"type": "note.add", "text": "x"}]})


def test_builder_api_permissions_dry_run_and_history(env):
    app, ids, _ = env
    body = {"name": "API rule", "triggers": ["call.missed"], "conditions": {"contact_tags_any": ["vip"]},
            "actions": [{"type": "task.create", "title": "Call back"}], "is_active": True}
    assert _as(app, ids["editor"]).post("/api/automations", json=body).status_code == 403
    r = _as(app, ids["admin"]).post("/api/automations", json=body)
    assert r.status_code == 201
    rule_id = r.get_json()["rule"]["id"]
    dry = _as(app, ids["editor"]).post("/api/automations/dry-run",
                                       json={"rule_id": rule_id, "contact_id": ids["contact"]}).get_json()
    assert dry["would_fire"] is True and dry["actions"][0]["description"] == "Create task “Call back”"
    assert ContactTask.query.count() == 0  # preview wrote nothing
    miss = _as(app, ids["editor"]).post("/api/automations/dry-run", json={
        "rule": {**body, "conditions": {"contact_tags_any": ["nobody"]}}, "contact_id": ids["contact"]}).get_json()
    assert miss["would_fire"] is False and miss["conditions"][0]["passed"] is False
    assert _as(app, ids["other"]).get("/api/automations").get_json()["rules"] == []
    assert _as(app, ids["other"]).post(f"/api/automations/{rule_id}", json={"is_active": False}).status_code == 404
    page = _as(app, ids["viewer"]).get("/automations/workflows")
    assert page.status_code == 200 and b"wfRules" in page.data and b'id="wfSave"' not in page.data


def test_system_rules_are_read_only_and_legacy_flow_unchanged(env):
    app, ids, _ = env
    setup = engine.ensure_my_order_automation(ids["a"], create_missing=True)
    db.session.commit()
    listed = {r["name"]: r for r in builder.list_rules(ids["a"])}
    system = listed["My Order Customer — First inbound SMS"]
    assert system["editable"] is False
    with pytest.raises(builder.BuilderError):
        builder.save_rule(ids["a"], {"name": "hack"}, builder.get_rule(ids["a"], system["id"]))
    result = engine.dispatch_event(ids["a"], ids["contact"], engine.FIRST_INBOUND_TRIGGER, "twilio:SMlegacy",
                                   direction="inbound", channel="sms", first_inbound_sms=True)
    assert result["results"] and "my order customer" in db.session.get(Contact, ids["contact"]).tags.lower()
    assert setup["segment"] is not None
