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
    AutoReplyRule,
    SMSCampaign,
    Notification,
    PushSubscription,
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





def test_outbound_sms_body_excludes_notification_debug_text(client, comms_world, monkeypatch):
    sent = {}

    class FakeMessages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return type("Msg", (), {"sid": "SMBODYONLY", "status": "sent"})()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    import twilio.rest
    monkeypatch.setattr(twilio.rest, "Client", FakeClient)
    notification_debug_text = "Notifications (sounds and alerts) are still not pushing through"

    login(client, comms_world["admin"])
    resp = client.post(
        f"/api/inbox/conversations/{comms_world['c1']}/messages",
        json={"body": "Actual customer reply"},
    )
    assert resp.status_code == 200
    assert sent["body"] == "Actual customer reply"
    assert notification_debug_text not in sent["body"]


def test_pwa_notifications_and_push_subscription_are_scoped_by_number(client, comms_world):
    login(client, comms_world["staff"])
    sub = client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example.test/staff-device",
        "device_key": "staff-phone",
        "keys": {"p256dh": "key", "auth": "auth"},
    })
    assert sub.status_code == 200
    assert sub.json["success"] is True
    with client.application.app_context():
        saved = PushSubscription.query.filter_by(endpoint="https://push.example.test/staff-device").one()
        assert saved.user_id == comms_world["staff"]
        assert saved.device_key == "staff-phone"

    with client.application.app_context():
        from inbox_pwa import create_pwa_notification
        create_pwa_notification(
            comms_world["co"],
            event_type="inbound_sms",
            title="New message from +15551230001",
            body="Customer message only",
            phone_number_id=comms_world["pn1"],
            link="/app/inbox?conv=1",
        )
        create_pwa_notification(
            comms_world["co"],
            event_type="missed_call",
            title="Missed call",
            body="Missed call from +15551230001",
            phone_number_id=comms_world["pn1"],
            link="/app/inbox?tab=calls",
        )
        create_pwa_notification(
            comms_world["co"],
            event_type="voicemail",
            title="New voicemail",
            body="Voicemail from +15551230001",
            phone_number_id=comms_world["pn1"],
            link="/app/inbox?tab=voicemail",
        )
        create_pwa_notification(
            comms_world["co"],
            event_type="inbound_sms",
            title="Restricted message",
            body="Should not be visible",
            phone_number_id=comms_world["pn2"],
            link="/app/inbox?conv=2",
        )

    notifications = client.get("/api/pwa/notifications").json["notifications"]
    assert any(n["message"] == "Customer message only" and n["event_type"] == "inbound_sms" for n in notifications)
    assert any(n["event_type"] == "missed_call" for n in notifications)
    assert any(n["event_type"] == "voicemail" for n in notifications)
    assert all(n["message"] != "Should not be visible" for n in notifications)
    read = client.post("/api/pwa/notifications/read", json={"notification_id": "all"})
    assert read.status_code == 200
    assert read.json["updated"] >= 1
    assert client.get("/api/pwa/notifications?filter=unread").json["unread_count"] == 0


def test_pwa_push_test_reports_missing_configuration_cleanly(client, comms_world, monkeypatch):
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    login(client, comms_world["staff"])
    client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example.test/no-vapid",
        "device_key": "staff-phone",
        "keys": {"p256dh": "key", "auth": "auth"},
    })
    resp = client.post("/api/pwa/push/test")
    assert resp.status_code == 200
    assert resp.json["success"] is False
    assert resp.json["configured"] is False


def test_pwa_inbox_all_unread_archived_and_number_filters(client, comms_world):
    login(client, comms_world["admin"])
    all_convs = client.get("/api/inbox/conversations?filter=all").json["conversations"]
    assert {c["id"] for c in all_convs} == {comms_world["c1"], comms_world["c2"]}

    unread = client.get("/api/inbox/conversations?filter=unread").json["conversations"]
    assert {c["id"] for c in unread} == {comms_world["c2"]}

    detail = client.get(f"/api/inbox/conversations/{comms_world['c2']}")
    assert detail.status_code == 200
    after_read_all = client.get("/api/inbox/conversations?filter=all").json["conversations"]
    assert {c["id"] for c in after_read_all} == {comms_world["c1"], comms_world["c2"]}
    after_read_unread = client.get("/api/inbox/conversations?filter=unread").json["conversations"]
    assert after_read_unread == []

    archive = client.patch(f"/api/inbox/conversations/{comms_world['c1']}/archive", json={"archived": True})
    assert archive.status_code == 200
    visible_all = client.get("/api/inbox/conversations?filter=all").json["conversations"]
    assert {c["id"] for c in visible_all} == {comms_world["c2"]}
    archived = client.get("/api/inbox/conversations?filter=archived").json["conversations"]
    assert {c["id"] for c in archived} == {comms_world["c1"]}

    selected = client.get("/api/inbox/conversations?filter=all&number=+15550002222").json["conversations"]
    assert {c["id"] for c in selected} == {comms_world["c2"]}


def test_legacy_communications_routes_redirect_to_hub(client, comms_world):
    login(client, comms_world["admin"])
    expected = {
        "/twilio/hours": "/twilio/comms?tab=hours",
        "/twilio/inbox": "/twilio/comms?tab=inbox",
        "/twilio/rules": "/twilio/comms?tab=auto",
        "/twilio/settings": "/twilio/comms?tab=integrations",
        "/twilio/calls": "/twilio/comms?tab=calls",
    }
    for old_route, new_path in expected.items():
        resp = client.get(old_route, follow_redirects=False)
        assert resp.status_code in (301, 302), old_route
        assert new_path in resp.headers["Location"], old_route


def test_comms_settings_tab_save_label_and_number_settings_persist(client, comms_world):
    login(client, comms_world["admin"])
    page = client.get(f"/twilio/comms?tab=settings&number_id={comms_world['pn1']}")
    assert page.status_code == 200
    assert b"Save Settings Settings" not in page.data
    assert b"Save Settings" in page.data

    resp = client.post(
        f"/twilio/numbers/{comms_world['pn1']}/edit",
        data={
            "return_to": "comms",
            "friendly_name": "Sales Main",
            "caller_id_display_name": "Sales Main",
            "timezone": "America/Los_Angeles",
            "during_hours_route": "ring_pwa",
            "after_hours_route": "voicemail",
            "sms_forward_to": "+15551239999",
            "call_forward_to": "+15551238888",
            "voicemail_greeting_text": "Please leave a message",
            "after_hours_text": "We are closed",
            "browser_calling_enabled": "1",
            "cell_callback_enabled": "1",
            "mobile_data_allowed": "1",
            "sms_forwarding_enabled": "1",
            "voice_forwarding_enabled": "1",
            "auto_reply_enabled": "1",
            "after_hours_sms_enabled": "1",
            "after_hours_voicemail_enabled": "1",
            "fallback_behavior": "voicemail",
            "bh_0_open": "1",
            "bh_0_start": "08:30",
            "bh_0_end": "17:30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with client.application.app_context():
        pn = db.session.get(TwilioPhoneNumber, comms_world["pn1"])
        assert pn.friendly_name == "Sales Main"
        assert pn.business_hours["0"]["open"] == "08:30"


def test_comms_users_permissions_form_persists_and_reloads(client, comms_world):
    login(client, comms_world["admin"])
    resp = client.post(
        f"/twilio/comms/numbers/{comms_world['pn2']}/permissions",
        data={
            "user_id": comms_world["staff"],
            "can_access_pwa": "1",
            "can_view_sms": "1",
            "can_send_sms": "1",
            "can_view_calls": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Permissions updated" in resp.data
    with client.application.app_context():
        perm = PhoneNumberUserPermission.query.filter_by(
            phone_number_id=comms_world["pn2"], user_id=comms_world["staff"]
        ).one()
        assert perm.can_access_pwa is True
        assert perm.can_view_sms is True
        assert perm.can_call is False
    page = client.get(f"/twilio/comms?tab=users&number_id={comms_world['pn2']}")
    assert page.status_code == 200


def test_comms_auto_replies_inline_crud(client, comms_world):
    login(client, comms_world["admin"])
    auto_page = client.get(f"/twilio/comms?tab=auto&number_id={comms_world['pn1']}")
    assert auto_page.status_code == 200
    assert b"Full rule editor" not in auto_page.data
    create = client.post(
        "/twilio/rules/create",
        data={
            "phone_number_id": comms_world["pn1"],
            "name": "Booking Reply",
            "trigger_type": "keyword_exact",
            "keywords": "BOOK",
            "response": "Booking link",
            "priority": "7",
            "action": "reply",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert create.status_code == 302
    with client.application.app_context():
        rule = AutoReplyRule.query.filter_by(company_id=comms_world["co"], name="Booking Reply").one()
        assert rule.phone_number_id == comms_world["pn1"]
        rule_id = rule.id
    edit = client.post(
        f"/twilio/rules/{rule_id}/edit",
        data={
            "phone_number_id": comms_world["pn1"],
            "name": "Booking Reply Updated",
            "trigger_type": "keyword_contains",
            "keywords": "BOOK, RESERVE",
            "response": "Updated link",
            "priority": "9",
            "action": "reply",
        },
        follow_redirects=False,
    )
    assert edit.status_code == 302
    with client.application.app_context():
        rule = db.session.get(AutoReplyRule, rule_id)
        assert rule.name == "Booking Reply Updated"
        assert rule.is_active is False
    delete = client.post(f"/twilio/rules/{rule_id}/delete", follow_redirects=False)
    assert delete.status_code == 302
    with client.application.app_context():
        assert db.session.get(AutoReplyRule, rule_id) is None


def test_duplicate_communications_nav_links_removed():
    from pathlib import Path
    base = Path("templates/base.html").read_text()
    hub = Path("templates/twilio/comms_hub.html").read_text()
    assert "/twilio/hours" not in base
    assert "/twilio/rules" not in base
    assert 'href="/twilio/settings"' not in base
    assert "url_for('twilio.settings')" not in base
    assert "Save {{ active_section }} Settings" not in hub
    assert "Full rule editor" not in hub

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
