"""GTM SMS campaign workflow: edit/schedule/send rules, fail-closed scheduling,
send-time STOP precedence, batching without duplicates, roles, tenant isolation.

Every provider call is faked (SMSService.send_sms); nothing reaches Twilio.
"""
import os
from datetime import datetime, timedelta

import pytest
from flask import g

from app import create_app
from extensions import db
from models import (
    Company, Contact, MarketingAuditLog, Segment, SMSCampaign, SMSRecipient, TwilioAccount,
    TwilioPhoneNumber, User, UserCompanyAccess,
)
from services.contact_audience import RecipientsLocked, resolve_sms_campaign_recipients
from services.sms_service import SMSService


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _tenant(name):
    co = Company(name=name)
    db.session.add(co)
    db.session.flush()
    acct = TwilioAccount(company_id=co.id, from_phone="+14155559000", is_active=True)
    acct.set_account_sid("AC" + name.replace(" ", "")[:10])
    acct.set_auth_token("token")
    line = TwilioPhoneNumber(company_id=co.id, phone_number=f"+1415555{co.id:04d}", is_active=True, sms_enabled=True,
                             campaign_sender_enabled=True, is_primary=True)
    db.session.add_all([acct, line])
    db.session.flush()
    return co, line


def _user(co, role, name=None):
    u = User(username=name or f"{role}-{co.id}", email=f"{name or role}-{co.id}@example.test", is_admin=False,
             default_company_id=co.id, password_hash="x")
    db.session.add(u)
    db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role=role, is_default=True, is_active=True))
    db.session.flush()
    return u


def _contact(co, phone, tag, **kw):
    values = dict(company_id=co.id, phone=phone, tags=tag, is_active=True,
                  sms_marketing_opt_in=True, sms_consent_status="opted_in")
    values.update(kw)
    c = Contact(**values)
    db.session.add(c)
    return c


def _segment(co, name):
    s = Segment(company_id=co.id, name=name)
    db.session.add(s)
    db.session.flush()
    return s


def _campaign(co, seg, line, **kw):
    values = dict(company_id=co.id, name="GTM", message="Hello Reply STOP to opt out.", segment=seg.name,
                  selected_tag_ids=[seg.id], audience_filter={"selected_tag_ids": [seg.id]},
                  from_phone_number_id=line.id, from_phone_number=line.phone_number, status="draft")
    values.update(kw)
    c = SMSCampaign(**values)
    db.session.add(c)
    db.session.commit()
    return c


class Client:
    """Test client acting as one user; clears flask-login's per-context cache
    before every request (the fixture keeps one app context open)."""

    def __init__(self, app, user):
        self.c = app.test_client()
        with self.c.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

    def __getattr__(self, method):
        def call(*a, **kw):
            g.pop("_login_user", None)
            return getattr(self.c, method)(*a, **kw)
        return call


@pytest.fixture
def fake_send(monkeypatch):
    attempts = []

    def _send(cls, to, message, **kw):
        attempts.append({"to": to, "from": kw.get("from_phone"), "company_id": kw.get("company_id")})
        return {"success": True, "message_sid": f"SMGTM{len(attempts):04d}"}

    monkeypatch.setattr(SMSService, "send_sms", classmethod(_send))
    return attempts


@pytest.fixture
def world(app):
    co, line = _tenant("Tenant A")
    other, other_line = _tenant("Tenant B")
    vip = _segment(co, "vip")
    empty = _segment(co, "nobody")
    for i in range(5):
        _contact(co, f"+1415444{i:04d}", "vip")
    _contact(other, "+14153330001", "vip")
    _segment(other, "vip")
    users = {r: _user(co, r) for r in ("owner", "admin", "manager", "editor", "viewer", "inbox_only")}
    users["outsider"] = _user(other, "admin", name="outsider")
    db.session.commit()
    return dict(co=co, line=line, other=other, other_line=other_line, vip=vip, empty=empty, users=users)


# ---------------------------------------------------------------------------
# Batching, duplicate prevention, scheduled dispatch
# ---------------------------------------------------------------------------

def test_scheduled_campaign_larger_than_one_batch_sends_everyone_exactly_once(app, world, fake_send):
    c = _campaign(world["co"], world["vip"], world["line"], batch_size=2, status="scheduled",
                  scheduled_at=datetime.utcnow() - timedelta(minutes=1))
    first = SMSService.execute_scheduled_campaign(c.id)
    assert first["sent"] == 5, first
    db.session.refresh(c)
    assert c.status == "completed"
    assert sorted(a["to"] for a in fake_send) == sorted(f"+1415444{i:04d}" for i in range(5))
    assert {a["from"] for a in fake_send} == {world["line"].phone_number}
    # A second trigger (retry, second worker, re-run) cannot re-send anyone.
    again = SMSService.execute_scheduled_campaign(c.id)
    assert again["success"] is False
    assert len(fake_send) == 5
    assert SMSRecipient.query.filter_by(campaign_id=c.id).count() == 5


def test_dispatcher_runs_only_due_campaigns_and_claims_once(app, world, fake_send):
    due = _campaign(world["co"], world["vip"], world["line"], status="scheduled",
                    scheduled_at=datetime.utcnow() - timedelta(minutes=5))
    future = _campaign(world["co"], world["vip"], world["line"], status="scheduled",
                       scheduled_at=datetime.utcnow() + timedelta(days=1))
    results = SMSService.dispatch_due_campaigns()
    assert set(results) == {due.id}
    assert SMSService.dispatch_due_campaigns() == {}
    db.session.refresh(future)
    assert future.status == "scheduled"
    assert len(fake_send) == 5


def test_rebuild_is_refused_once_any_recipient_was_attempted(app, world, fake_send):
    c = _campaign(world["co"], world["vip"], world["line"])
    resolve_sms_campaign_recipients(c, materialize=True)
    db.session.commit()
    SMSService.send_campaign(c.id)
    with pytest.raises(RecipientsLocked):
        resolve_sms_campaign_recipients(c, materialize=True)
    assert SMSRecipient.query.filter_by(campaign_id=c.id, status="sent").count() == 5


def test_stop_after_scheduling_wins_at_send_time(app, world, fake_send):
    c = _campaign(world["co"], world["vip"], world["line"])
    admin = Client(app, world["users"]["admin"])
    when = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    assert admin.post(f"/api/marketing/sms-campaigns/{c.id}/schedule", json={"scheduled_at": when}).status_code == 200
    assert SMSRecipient.query.filter_by(campaign_id=c.id, status="pending").count() == 5
    stopped = Contact.query.filter_by(company_id=world["co"].id, phone="+14154440000").one()
    stopped.sms_opted_out = True
    stopped.sms_opt_out_at = datetime.utcnow()
    db.session.commit()
    result = SMSService.send_campaign(c.id, transition=False)  # pending list built before STOP
    assert result["sent"] == 4 and result["skipped"] == 1
    assert "+14154440000" not in [a["to"] for a in fake_send]
    skipped = SMSRecipient.query.filter_by(campaign_id=c.id, contact_id=stopped.id).one()
    assert skipped.status == "skipped" and "opted_out" in skipped.error_message


def test_pause_halts_remaining_batches(app, world, monkeypatch):
    c = _campaign(world["co"], world["vip"], world["line"], batch_size=2)
    resolve_sms_campaign_recipients(c, materialize=True)
    db.session.commit()
    calls = []

    def _send(cls, to, message, **kw):
        calls.append(to)
        if len(calls) == 2:
            db.session.get(SMSCampaign, c.id).status = "paused"
        return {"success": True, "message_sid": f"SMP{len(calls)}"}

    monkeypatch.setattr(SMSService, "send_sms", classmethod(_send))
    result = SMSService.send_campaign(c.id)
    assert result["halted"] == "paused"
    assert len(calls) == 2
    assert SMSRecipient.query.filter_by(campaign_id=c.id, status="pending").count() == 3


# ---------------------------------------------------------------------------
# Edit / schedule rules
# ---------------------------------------------------------------------------

def test_scheduled_campaign_edit_rebuilds_pending_and_refuses_empty_audience(app, world, fake_send):
    co = world["co"]
    gold = _segment(co, "gold")
    _contact(co, "+14154449001", "gold")
    _contact(co, "+14154449002", "gold", sms_marketing_opt_in=False, sms_consent_status="unknown")
    db.session.commit()
    c = _campaign(co, world["vip"], world["line"])
    admin = Client(app, world["users"]["admin"])
    when = (datetime.utcnow() + timedelta(hours=2)).isoformat()
    assert admin.post(f"/api/marketing/sms-campaigns/{c.id}/schedule", json={"scheduled_at": when}).status_code == 200

    refused = admin.put(f"/api/marketing/sms-campaigns/{c.id}", json={"segment_id": world["empty"].id})
    assert refused.status_code == 400 and refused.get_json()["code"] == "no_eligible_recipients"
    db.session.expire_all()
    c = db.session.get(SMSCampaign, c.id)
    assert c.selected_tag_ids == [world["vip"].id]
    assert SMSRecipient.query.filter_by(campaign_id=c.id, status="pending").count() == 5

    ok = admin.put(f"/api/marketing/sms-campaigns/{c.id}",
                   json={"segment_id": gold.id, "message": "New copy", "media_urls": ["https://cdn.example.test/a.png"]})
    body = ok.get_json()
    assert ok.status_code == 200, body
    assert body["audience"]["eligible_recipients"] == 1
    assert body["audience"]["missing_sms_consent"] == 1
    assert body["campaign"]["media_urls"] == ["https://cdn.example.test/a.png"]
    assert "STOP" in body["campaign"]["message"]
    assert SMSRecipient.query.filter_by(campaign_id=c.id, status="pending").count() == 1


def test_edits_refused_after_sending_begins(app, world, fake_send):
    c = _campaign(world["co"], world["vip"], world["line"])
    admin = Client(app, world["users"]["admin"])
    assert admin.post(f"/api/marketing/sms-campaigns/{c.id}/send", json={}).status_code == 200
    for payload in ({"message": "changed"}, {"segment_id": world["empty"].id}, {"status": "draft"}):
        r = admin.put(f"/api/marketing/sms-campaigns/{c.id}", json=payload)
        assert r.status_code == 409 and r.get_json()["code"] == "campaign_locked"
    assert admin.post(f"/api/marketing/sms-campaigns/{c.id}/schedule", json={}).status_code == 409
    assert len(fake_send) == 5


def test_status_cannot_be_forced_through_update(app, world):
    c = _campaign(world["co"], world["vip"], world["line"])
    admin = Client(app, world["users"]["admin"])
    for status in ("scheduled", "sent", "sending", "completed"):
        r = admin.put(f"/api/marketing/sms-campaigns/{c.id}", json={"status": status})
        assert r.status_code == 400
    db.session.refresh(c)
    assert c.status == "draft"


@pytest.mark.parametrize("case", ["no_sender", "foreign_sender", "no_provider", "no_eligible", "past_time", "no_audience"])
def test_scheduling_fails_closed(app, world, case):
    co = world["co"]
    admin = Client(app, world["users"]["admin"])
    c = _campaign(co, world["vip"], world["line"])
    payload = {"scheduled_at": (datetime.utcnow() + timedelta(hours=1)).isoformat()}
    if case == "no_sender":
        world["line"].is_primary = False
        c.from_phone_number_id = None
        c.from_phone_number = None
    elif case == "foreign_sender":
        payload["from_phone_number_id"] = world["other_line"].id
    elif case == "no_provider":
        TwilioAccount.query.filter_by(company_id=co.id).delete()
    elif case == "no_eligible":
        c.selected_tag_ids = [world["empty"].id]
        c.audience_filter = {"selected_tag_ids": [world["empty"].id]}
        c.segment = "nobody"
    elif case == "past_time":
        payload["scheduled_at"] = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    elif case == "no_audience":
        c.selected_tag_ids = []
        c.audience_filter = {}
        c.segment = None
    db.session.commit()
    r = admin.post(f"/api/marketing/sms-campaigns/{c.id}/schedule", json=payload)
    assert r.status_code in (400, 403, 409), r.get_json()
    db.session.expire_all()
    c = db.session.get(SMSCampaign, c.id)
    assert c.status == "draft"
    assert SMSRecipient.query.filter_by(campaign_id=c.id).count() == 0


def test_html_create_route_rejects_unknown_free_text_audience_and_never_targets_everyone(app, world):
    admin = Client(app, world["users"]["admin"])
    at = datetime.utcnow() + timedelta(days=1)
    for extra in ({"segment_tags": "typo-segment"}, {}):
        r = admin.post("/sms/create", data={"name": "Free text", "message": "Hi", "send_option": "scheduled",
                                            "scheduled_date": at.strftime("%Y-%m-%d"), "scheduled_time": at.strftime("%H:%M"),
                                            "from_phone_number_id": str(world["line"].id), **extra})
        assert r.status_code in (302, 303)
    assert SMSCampaign.query.filter_by(name="Free text").count() == 0
    assert SMSRecipient.query.count() == 0


# ---------------------------------------------------------------------------
# Roles, purpose, tenant isolation
# ---------------------------------------------------------------------------

def test_role_matrix_for_sms_campaign_api(app, world, fake_send):
    u = world["users"]
    expectations = {
        # role: (list, create, schedule)
        "owner": (200, 201, 200), "admin": (200, 201, 200), "manager": (200, 201, 403),
        "editor": (200, 201, 403), "viewer": (200, 403, 403), "inbox_only": (403, 403, 403),
    }
    when = (datetime.utcnow() + timedelta(hours=3)).isoformat()
    for role, (list_s, create_s, sched_s) in expectations.items():
        client = Client(app, u[role])
        assert client.get("/api/marketing/sms-campaigns").status_code == list_s, role
        r = client.post("/api/marketing/sms-campaigns", json={"name": f"by-{role}", "message": "hi", "segment_id": world["vip"].id})
        assert r.status_code == create_s, (role, r.get_json())
        target = _campaign(world["co"], world["vip"], world["line"], name=f"sched-{role}")
        r = client.post(f"/api/marketing/sms-campaigns/{target.id}/schedule", json={"scheduled_at": when})
        assert r.status_code == sched_s, (role, r.get_json())
        # "Send now" on a scheduled campaign is an admin action; others are refused before any send.
        r = client.post(f"/api/marketing/sms-campaigns/{target.id}/send", json={})
        assert r.status_code == (200 if sched_s == 200 else 403), (role, r.get_json())
        r = client.post(f"/api/marketing/sms-campaigns/{target.id}/send", json={})
        assert r.status_code == (409 if sched_s == 200 else 403), (role, r.get_json())
    assert len(fake_send) == 10  # owner + admin campaigns, 5 recipients each, once


def test_other_tenant_cannot_read_edit_schedule_or_send(app, world, fake_send):
    c = _campaign(world["co"], world["vip"], world["line"])
    outsider = Client(app, world["users"]["outsider"])
    assert outsider.get(f"/api/marketing/sms-campaigns/{c.id}").status_code == 404
    assert outsider.put(f"/api/marketing/sms-campaigns/{c.id}", json={"message": "x"}).status_code == 404
    assert outsider.post(f"/api/marketing/sms-campaigns/{c.id}/schedule", json={}).status_code == 404
    assert outsider.post(f"/api/marketing/sms-campaigns/{c.id}/send", json={}).status_code == 404
    assert outsider.post(f"/api/marketing/sms-campaigns/{c.id}/preview").status_code == 404
    admin = Client(app, world["users"]["admin"])
    foreign_seg = Segment.query.filter_by(company_id=world["other"].id).first()
    r = admin.put(f"/api/marketing/sms-campaigns/{c.id}", json={"segment_id": foreign_seg.id})
    assert r.status_code in (400, 422)
    r = admin.put(f"/api/marketing/sms-campaigns/{c.id}", json={"from_phone_number_id": world["other_line"].id})
    assert r.status_code == 403
    assert fake_send == []


def test_non_promotional_purpose_is_admin_only_and_never_overrides_stop(app, world, fake_send):
    co = world["co"]
    editor = Client(app, world["users"]["editor"])
    c = _campaign(co, world["vip"], world["line"])
    r = editor.put(f"/api/marketing/sms-campaigns/{c.id}", json={"campaign_purpose": "transactional"})
    assert r.status_code == 403
    admin = Client(app, world["users"]["admin"])
    r = admin.put(f"/api/marketing/sms-campaigns/{c.id}", json={"campaign_purpose": "transactional"})
    assert r.status_code == 200 and r.get_json()["campaign"]["campaign_purpose"] == "transactional"
    stopped = Contact.query.filter_by(company_id=co.id, phone="+14154440001").one()
    stopped.sms_opted_out = True
    db.session.commit()
    counts = resolve_sms_campaign_recipients(db.session.get(SMSCampaign, c.id))["counts"]
    assert counts["opted_out_contacts"] == 1
    assert stopped.id not in [x.id for x, _ in resolve_sms_campaign_recipients(db.session.get(SMSCampaign, c.id))["recipients"]]


def test_schedule_and_edit_are_audited(app, world):
    c = _campaign(world["co"], world["vip"], world["line"])
    admin = Client(app, world["users"]["admin"])
    admin.put(f"/api/marketing/sms-campaigns/{c.id}", json={"name": "Renamed"})
    admin.post(f"/api/marketing/sms-campaigns/{c.id}/schedule",
               json={"scheduled_at": (datetime.utcnow() + timedelta(hours=1)).isoformat()})
    actions = [row.action for row in MarketingAuditLog.query.filter_by(company_id=world["co"].id, entity_id=c.id)]
    assert "sms_campaign_updated" in actions and "sms_campaign_scheduled" in actions
    # Audit details never carry the message body.
    for row in MarketingAuditLog.query.filter_by(entity_id=c.id):
        assert "Hello Reply STOP" not in str(row.details)
