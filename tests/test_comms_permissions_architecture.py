from datetime import datetime, timedelta

import os
import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    Company,
    PhoneNumberUserPermission,
    TwilioAccount,
    TwilioCallLog,
    TwilioConversation,
    TwilioMessage,
    TwilioPhoneNumber,
    SMSCampaign,
    User,
    UserCompanyAccess,
)


@pytest.fixture
def app():
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


@pytest.fixture
def comms_world(app):
    with app.app_context():
        co = Company(name="Comms Perm Co", is_active=True)
        other = Company(name="Other Comms Co", is_active=True)
        db.session.add_all([co, other])
        db.session.flush()
        owner = User(username="perm_owner", email="owner@perms.test", password_hash=generate_password_hash("x"), default_company_id=co.id)
        admin = User(username="perm_admin", email="info@adiken.com", password_hash=generate_password_hash("x"), default_company_id=co.id)
        staff = User(username="perm_staff", email="staff@perms.test", password_hash=generate_password_hash("x"), default_company_id=co.id)
        blocked = User(username="perm_blocked", email="blocked@perms.test", password_hash=generate_password_hash("x"), default_company_id=co.id)
        outsider = User(username="perm_out", email="out@perms.test", password_hash=generate_password_hash("x"), default_company_id=other.id)
        db.session.add_all([owner, admin, staff, blocked, outsider])
        db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=owner.id, company_id=co.id, role="owner", is_default=True),
            UserCompanyAccess(user_id=admin.id, company_id=co.id, role="admin", is_default=True, manage_users_enabled=True),
            UserCompanyAccess(user_id=staff.id, company_id=co.id, role="staff", is_default=True, pwa_access_enabled=True),
            UserCompanyAccess(user_id=blocked.id, company_id=co.id, role="viewer", is_default=True),
            UserCompanyAccess(user_id=outsider.id, company_id=other.id, role="owner", is_default=True),
            TwilioAccount(company_id=co.id, from_phone="+15550001111", _account_sid="ACtest", _auth_token="auth"),
        ])
        pn1 = TwilioPhoneNumber(company_id=co.id, phone_number="+15550001111", friendly_name="Sales", is_active=True, business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)})
        pn2 = TwilioPhoneNumber(company_id=co.id, phone_number="+15550002222", friendly_name="Support", is_active=True, business_hours={str(i): {"is_open": False} for i in range(7)})
        db.session.add_all([pn1, pn2])
        db.session.flush()
        db.session.add(PhoneNumberUserPermission(company_id=co.id, phone_number_id=pn1.id, user_id=staff.id, can_access_pwa=True))
        c1 = TwilioConversation(company_id=co.id, from_number="+15551230001", to_number=pn1.phone_number, is_read=True, last_message_at=datetime.utcnow(), last_message_preview="read stays")
        c2 = TwilioConversation(company_id=co.id, from_number="+15551230002", to_number=pn2.phone_number, is_read=False, last_message_at=datetime.utcnow(), last_message_preview="restricted")
        db.session.add_all([c1, c2])
        db.session.flush()
        db.session.add_all([
            TwilioMessage(conversation_id=c1.id, company_id=co.id, direction="inbound", from_number=c1.from_number, to_number=c1.to_number, body="read stays", status="received"),
            TwilioMessage(conversation_id=c2.id, company_id=co.id, direction="inbound", from_number=c2.from_number, to_number=c2.to_number, body="restricted", status="received"),
            TwilioCallLog(company_id=co.id, twilio_sid="CA1", direction="inbound", from_number="+15551230001", to_number=pn1.phone_number, status="completed", duration=12),
            TwilioCallLog(company_id=co.id, twilio_sid="CA2", direction="inbound", from_number="+15551230002", to_number=pn2.phone_number, status="voicemail", voicemail_url="https://example.test/vm.mp3"),
        ])
        db.session.commit()
        return {"co": co.id, "other": other.id, "owner": owner.id, "admin": admin.id, "staff": staff.id, "blocked": blocked.id, "outsider": outsider.id, "pn1": pn1.id, "pn2": pn2.id, "c1": c1.id, "c2": c2.id}


def test_owner_can_edit_all_tenant_users_and_roles(client, comms_world):
    login(client, comms_world["owner"])
    resp = client.post(f"/api/user/{comms_world['staff']}/access", json={"role": "supervisor", "manage_users_enabled": True})
    assert resp.status_code == 200
    assert resp.json["success"] is True
    assert resp.json["role"] == "manager"
    assert resp.json["manage_users_enabled"] is True


def test_admin_with_manage_users_can_manage_users(client, comms_world):
    login(client, comms_world["admin"])
    resp = client.post(f"/api/user/{comms_world['staff']}/access", json={"can_access_mobile_inbox": True})
    assert resp.status_code == 200
    assert resp.json["success"] is True


def test_non_authorized_user_denied_and_tenant_isolation(client, comms_world):
    login(client, comms_world["blocked"])
    resp = client.post(f"/api/user/{comms_world['staff']}/access", json={"role": "admin"})
    assert resp.status_code == 403

    login(client, comms_world["owner"])
    resp = client.post(f"/api/user/{comms_world['outsider']}/access", json={"role": "admin"})
    assert resp.status_code == 403
    assert UserCompanyAccess.query.filter_by(user_id=comms_world["outsider"], company_id=comms_world["co"]).first() is None
    assert UserCompanyAccess.query.filter_by(user_id=comms_world["outsider"], company_id=comms_world["other"], role="owner").first() is not None


def test_pwa_history_persists_and_is_scoped_to_assigned_number(client, comms_world):
    login(client, comms_world["staff"])
    first = client.get("/api/inbox/conversations").json["conversations"]
    second = client.get("/api/inbox/conversations").json["conversations"]
    assert [c["id"] for c in first] == [comms_world["c1"]]
    assert [c["id"] for c in second] == [comms_world["c1"]]

    detail = client.get(f"/api/inbox/conversations/{comms_world['c1']}")
    assert detail.status_code == 200
    assert client.get(f"/api/inbox/conversations/{comms_world['c2']}").status_code == 404


def test_authorized_admin_sees_all_number_history_and_voicemail_metadata(client, comms_world):
    login(client, comms_world["admin"])
    convs = client.get("/api/inbox/conversations").json["conversations"]
    assert {c["id"] for c in convs} == {comms_world["c1"], comms_world["c2"]}
    calls = client.get("/api/calls/recent?tab=all").json["calls"]
    assert {c["twilio_call_sid"] for c in calls} == {"CA1", "CA2"}
    vms = client.get("/api/calls/voicemails").json["voicemails"]
    assert len(vms) == 1
    assert vms[0]["voicemail_url"] == "https://example.test/vm.mp3"


def test_number_settings_are_independent_per_number(client, comms_world):
    login(client, comms_world["owner"])
    r1 = client.get(f"/api/phone/numbers/{comms_world['pn1']}/settings")
    r2 = client.get(f"/api/phone/numbers/{comms_world['pn2']}/settings")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json["settings"]["business_hours"]["0"]["is_open"] is True
    assert r2.json["settings"]["business_hours"]["0"]["is_open"] is False
    upd = client.put(f"/api/phone/numbers/{comms_world['pn2']}/settings", json={"caller_id_display_name": "Support Line", "wifi_only": True})
    assert upd.status_code == 200
    assert upd.json["settings"]["caller_id_display_name"] == "Support Line"
    assert client.get(f"/api/phone/numbers/{comms_world['pn1']}/settings").json["settings"]["caller_id_display_name"] is None


def test_left_nav_consolidates_sms_phone_duplicates():
    from pathlib import Path
    html = Path("templates/base.html").read_text()
    assert "Communications Hub" in html
    assert "SMS Campaigns" in html
    assert "SMS &amp; Calls Admin" not in html
    assert "> Phone Numbers" not in html


def test_info_adiken_admin_can_open_manage_users_and_edit(client, comms_world):
    login(client, comms_world["admin"])
    page = client.get("/user/manage-users")
    assert page.status_code == 200
    assert b"info@adiken.com" in page.data
    resp = client.post(f"/api/user/{comms_world['staff']}/access", json={"role": "editor"})
    assert resp.status_code == 200
    assert resp.json["role"] == "editor"


def test_sms_campaign_sender_number_permissions_and_calendar_visibility(client, comms_world):
    with client.application.app_context():
        campaign = SMSCampaign(
            company_id=comms_world["co"],
            created_by_user_id=comms_world["admin"],
            name="Scheduled SMS Line Check",
            message="Line-specific campaign",
            status="scheduled",
            scheduled_at=datetime.utcnow() + timedelta(days=1),
        )
        db.session.add(campaign)
        db.session.commit()
        campaign_id = campaign.id

    login(client, comms_world["staff"])
    blocked = client.post(
        f"/sms/campaign/{campaign_id}/send",
        data={"from_phone_number_id": comms_world["pn2"]},
        follow_redirects=False,
    )
    assert blocked.status_code == 403

    login(client, comms_world["admin"])
    calendar = client.get("/api/calendar/events?types=sms&range=7")
    assert calendar.status_code == 200
    events = calendar.get_json()
    assert any(e["id"] == f"sms_{campaign_id}" and e["content_type"] == "sms_campaign" for e in events)


def test_communications_hub_tabs_and_pwa_api_smoke(client, comms_world):
    login(client, comms_world["admin"])
    tabs = ["overview", "numbers", "settings", "users", "hours", "auto", "routing", "voicemail", "pwa", "devices", "calls", "inbox", "integrations", "reports"]
    for tab in tabs:
        resp = client.get(f"/twilio/comms?tab={tab}&number_id={comms_world['pn1']}")
        assert resp.status_code == 200, tab
    assert client.get("/api/phone/numbers").status_code == 200
    assert client.get(f"/api/phone/numbers/{comms_world['pn1']}/settings").status_code == 200
    assert client.get("/api/calls/recent?tab=all").status_code == 200
    assert client.get("/api/calls/voicemails").status_code == 200
    assert client.get("/api/pwa/devices").status_code == 200
