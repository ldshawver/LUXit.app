import base64
import os

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    Company,
    Contact,
    Notification,
    PhoneNumberUserPermission,
    PushSubscription,
    TwilioCallLog,
    TwilioConversation,
    TwilioPhoneNumber,
    User,
    UserCompanyAccess,
    VoiceVoicemailMessage,
)


@pytest.fixture
def pwa_app():
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    os.environ.setdefault("TWILIO_AUTH_TOKEN", "authtest")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company = Company(name="PWA Communications Co", is_active=True)
        db.session.add(company); db.session.flush()
        admin = User(username="pwa_admin", email="pwa_admin@example.com", password_hash=generate_password_hash("pw"), default_company_id=company.id)
        staff = User(username="pwa_staff", email="pwa_staff@example.com", password_hash=generate_password_hash("pw"), default_company_id=company.id)
        denied = User(username="pwa_denied", email="pwa_denied@example.com", password_hash=generate_password_hash("pw"), default_company_id=company.id)
        db.session.add_all([admin, staff, denied]); db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=admin.id, company_id=company.id, role="admin", is_default=True, can_access_mobile_inbox=True),
            UserCompanyAccess(user_id=staff.id, company_id=company.id, role="staff", is_default=True, can_access_mobile_inbox=True),
            UserCompanyAccess(user_id=denied.id, company_id=company.id, role="staff", is_default=True, can_access_mobile_inbox=True),
        ])
        line = TwilioPhoneNumber(company_id=company.id, phone_number="+15550001000", friendly_name="Shared", sms_enabled=True, voice_enabled=True, is_active=True)
        other_line = TwilioPhoneNumber(company_id=company.id, phone_number="+15550002000", friendly_name="Other", sms_enabled=True, voice_enabled=True, is_active=True)
        db.session.add_all([line, other_line]); db.session.flush()
        db.session.add_all([
            PhoneNumberUserPermission(company_id=company.id, user_id=staff.id, phone_number_id=line.id, can_access_pwa=True, can_view_sms=True, can_view_calls=True, can_view_voicemail=True),
            PhoneNumberUserPermission(company_id=company.id, user_id=denied.id, phone_number_id=other_line.id, can_access_pwa=True, can_view_sms=True, can_view_calls=True, can_view_voicemail=True),
        ])
        db.session.commit()
        yield app, app.test_client(), {"company": company.id, "admin": admin.id, "staff": staff.id, "denied": denied.id, "line": line.id, "other_line": other_line.id}
        db.session.remove(); db.drop_all()


def login(client, user_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def test_google_synced_contact_name_refreshes_conversation_display(pwa_app):
    app, client, ids = pwa_app
    with app.app_context():
        db.session.add(Contact(company_id=ids["company"], first_name="John", last_name="Smith", phone="+14155551212", source="google_contacts", is_active=True))
        conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155551212", to_number="+15550001000", contact_name=None, last_message_preview="Hi", message_count=1)
        db.session.add(conv); db.session.commit(); conv_id = conv.id
    login(client, ids["staff"])
    resp = client.get("/api/inbox/conversations?filter=all")
    assert resp.status_code == 200
    data = resp.get_json()
    row = next(c for c in data["conversations"] if c["id"] == conv_id)
    assert row["display_name"] == "John Smith"
    assert row["contact_name"] == "John Smith"
    assert row["from_number"] == "+14155551212"
    with app.app_context():
        assert db.session.get(TwilioConversation, conv_id).contact_name == "John Smith"


def test_call_list_uses_contact_name_and_voicemail_audio_proxies_without_twilio_login(pwa_app):
    app, client, ids = pwa_app
    audio = base64.b64encode(b"FAKEAUDIO").decode()
    with app.app_context():
        db.session.add(Contact(company_id=ids["company"], first_name="John", last_name="Smith", phone="+14155551212", source="google_contacts", is_active=True))
        call = TwilioCallLog(company_id=ids["company"], phone_number_id=ids["line"], twilio_sid="CAvm", direction="inbound", from_number="+14155551212", to_number="+15550001000", status="voicemail", voicemail_url=f"data:audio/mpeg;base64,{audio}")
        db.session.add(call); db.session.flush()
        db.session.add(VoiceVoicemailMessage(company_id=ids["company"], call_log_id=call.id, phone_number_id=ids["line"], recording_url=f"data:audio/mpeg;base64,{audio}", transcript="Please call me back"))
        db.session.commit(); call_id = call.id
    login(client, ids["staff"])
    recent = client.get("/api/calls/recent?tab=voicemail")
    assert recent.status_code == 200
    row = next(c for c in recent.get_json()["calls"] if c["id"] == call_id)
    assert row["caller_name"] == "John Smith"
    assert row["voicemail_exists"] is True
    media = client.get(f"/api/calls/{call_id}/voicemail/audio")
    assert media.status_code == 200
    assert media.mimetype == "audio/mpeg"
    assert media.data == b"FAKEAUDIO"


def test_push_subscription_and_notifications_are_permission_scoped(pwa_app, monkeypatch):
    app, client, ids = pwa_app
    login(client, ids["staff"])
    resp = client.post("/api/pwa/push/subscribe", json={"endpoint": "https://push.example/sub/1", "keys": {"p256dh": "p", "auth": "a"}, "device_key": "dev1"})
    assert resp.status_code == 200
    with app.app_context():
        assert PushSubscription.query.filter_by(user_id=ids["staff"], company_id=ids["company"], is_active=True).count() == 1
    sent = {}
    import inbox_pwa
    monkeypatch.setattr(inbox_pwa, "send_pwa_push_notification", lambda company_id, **kw: sent.setdefault("payload", kw) or {"sent": 1, "errors": []})
    with app.app_context():
        conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155551212", to_number="+15550001000", contact_name="John Smith")
        db.session.add(conv); db.session.commit()
        inbox_pwa._fire_push_notification(ids["company"], conv, "Hello")
        assert Notification.query.filter_by(user_id=ids["staff"], event_type="incoming_sms").count() == 1
        assert Notification.query.filter_by(user_id=ids["denied"], event_type="incoming_sms").count() == 0
        assert ids["staff"] in sent["payload"]["user_ids"]
        assert ids["denied"] not in sent["payload"]["user_ids"]
        inbox_pwa.create_pwa_notification(ids["company"], event_type="voicemail", title="New voicemail", body="Voicemail", phone_number_id=ids["line"], link="/app/calls")
        assert Notification.query.filter_by(user_id=ids["staff"], event_type="voicemail").count() == 1


def test_pwa_static_assets_support_unified_theme_voicemail_and_push_behaviors():
    index = open("templates/inbox_pwa/index.html", encoding="utf-8").read()
    calls = open("templates/inbox_pwa/calls.html", encoding="utf-8").read()
    sw = open("static/sw.js", encoding="utf-8").read()
    assert "Twilio password" not in calls and "Twilio login" not in calls and "twilio_email" not in calls
    assert "--pwa-primary" in calls and "pwa-card" in calls and "voicemail/audio" in calls
    assert "Play Voicemail" in calls and "Call Recording" in calls
    assert "self.addEventListener('push'" in sw
    assert "self.registration.setAppBadge" in sw and "self.registration.clearAppBadge" in sw
    assert "notificationclick" in sw and "clients.openWindow" in sw
    assert "function playSound" in index and "notificationSoundsEnabled" in index
    assert "vibrate" in index and "EventSource('/api/inbox/stream')" in index


def test_google_sync_backfill_updates_existing_phone_number_contact_names(pwa_app):
    app, _client, ids = pwa_app
    from services.google_contacts import backfill_conversation_contact_names, lookup_contact_for_phone
    with app.app_context():
        contact = Contact(
            company_id=ids["company"],
            name="John Smith",
            first_name="John",
            last_name="Smith",
            phone="(415) 555-1212",
            normalized_phone=None,
            source="google_contacts",
            is_active=True,
        )
        conv = TwilioConversation(
            company_id=ids["company"],
            phone_number_id=ids["line"],
            from_number="4155551212",
            to_number="+15550001000",
            contact_name="4155551212",
            last_message_preview="Existing thread",
            message_count=2,
        )
        db.session.add_all([contact, conv]); db.session.commit(); conv_id = conv.id
        info = lookup_contact_for_phone(ids["company"], "+14155551212")
        assert info["name"] == "John Smith"
        assert info["contact_id"] == contact.id
        result = backfill_conversation_contact_names(company_id=ids["company"])
        assert result["matched"] >= 1
        assert result["updated"] >= 1
        refreshed = db.session.get(TwilioConversation, conv_id)
        assert refreshed.contact_id == contact.id
        assert refreshed.contact_name == "John Smith"
        assert refreshed.contact_source == "google_contacts"
        assert contact.normalized_phone == "+14155551212"


def test_google_contacts_sync_creates_contact_cache_fields_and_links_conversation(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts

    class DummyToken:
        last_sync_at = None
        contacts_synced = 0
        sync_error = None

    with app.app_context():
        conv = TwilioConversation(
            company_id=ids["company"],
            phone_number_id=ids["line"],
            from_number="+14155551212",
            to_number="+15550001000",
            contact_name=None,
        )
        db.session.add(conv); db.session.commit(); conv_id = conv.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": "John Smith"})
        result = google_contacts.sync_contacts(ids["staff"], ids["company"])
        assert result["synced"] == 1
        contact = Contact.query.filter_by(company_id=ids["company"], normalized_phone="+14155551212").one()
        assert contact.name == "John Smith"
        assert contact.phone == "+14155551212"
        assert contact.source == "google_contacts"
        refreshed = db.session.get(TwilioConversation, conv_id)
        assert refreshed.contact_id == contact.id
        assert refreshed.contact_name == "John Smith"


def test_push_setup_status_preferences_and_unread_reminder_lifecycle(pwa_app, monkeypatch):
    app, client, ids = pwa_app
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_SUBJECT", raising=False)
    login(client, ids["staff"])
    status = client.get("/api/pwa/push/status")
    assert status.status_code == 200
    assert status.get_json()["configured"] is False
    assert "VAPID_PUBLIC_KEY" in status.get_json()["missing"]
    prefs = client.patch("/api/pwa/preferences", json={
        "textAlertsEnabled": True,
        "callAlertsEnabled": True,
        "voicemailAlertsEnabled": True,
        "unreadReminderAlertsEnabled": True,
        "notificationSoundsEnabled": True,
        "vibrationEnabled": True,
        "businessHoursOnly": True,
        "unreadRepeatMinutes": 1,
    })
    assert prefs.status_code == 200
    with app.app_context():
        conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155551212", to_number="+15550001000", contact_name="John Smith", is_read=False, last_message_preview="Please reply")
        db.session.add(conv); db.session.commit()
        import inbox_pwa
        first = inbox_pwa.create_unread_message_reminders()
        second = inbox_pwa.create_unread_message_reminders()
        assert first["created"] >= 1
        assert second["created"] == 0
        conv.is_read = True
        db.session.commit()
        assert inbox_pwa.create_unread_message_reminders()["created"] == 0


def test_voice_greeting_crud_is_per_phone_number(pwa_app):
    app, client, ids = pwa_app
    login(client, ids["staff"])
    create = client.post(f"/api/phone/numbers/{ids['line']}/greetings", json={
        "name": "AI after-hours greeting",
        "greeting_type": "text_to_speech",
        "text_body": "Thanks for calling LUXit. Please leave a message.",
        "voice_name": "alloy",
        "applies_to": "after_hours",
        "is_active": True,
    })
    assert create.status_code == 201
    greeting = create.get_json()["greeting"]
    assert greeting["is_active"] is True
    listed = client.get(f"/api/phone/numbers/{ids['line']}/greetings")
    assert listed.status_code == 200
    assert listed.get_json()["greetings"][0]["phone_number_id"] == ids["line"]
    activate = client.post(f"/api/phone/greetings/{greeting['id']}/activate")
    assert activate.status_code == 200
    with app.app_context():
        from models import VoiceGreeting
        assert VoiceGreeting.query.filter_by(phone_number_id=ids["line"], is_active=True, applies_to="after_hours").count() == 1


def test_pwa_calls_visual_nav_sdk_and_voicemail_static_requirements():
    calls = open("templates/inbox_pwa/calls.html", encoding="utf-8").read()
    index = open("templates/inbox_pwa/index.html", encoding="utf-8").read()
    assert "#7c3aed" not in calls and "rgba(124,58,237" not in calls
    assert "#7c3aed" not in index and "rgba(124,58,237" not in index
    for token in ("--pwa-primary", "--pwa-primary-contrast", "--pwa-accent", "--pwa-card-bg", "--pwa-surface", "--pwa-border", "--pwa-text", "--pwa-muted"):
        assert token in index
        assert token in calls
    nav = open("templates/inbox_pwa/_bottom_nav.html", encoding="utf-8").read()
    assert 'data-test="shared-pwa-bottom-nav"' in nav
    assert "position: fixed" in nav and "bottom: 0" in nav and "env(safe-area-inset-bottom)" in nav
    assert "min-width: 44px" in nav and "min-height: 52px" in nav
    for label in ("Dial Pad", "Recents", "Settings", "Conversations", "New Text"):
        assert label in nav
    for route in ("/app/dial-pad", "/app/recents", "/app/settings", "/app/inbox", "/app/new-text"):
        assert route in nav
    assert "Play" in calls and "Transcript" in calls and "Twilio login" not in calls
    assert "Greeting management" in calls and "text_to_speech" in calls and "recorded" in calls and "upload" in calls
    assert "SDK_MISSING" in calls and "TOKEN_ENDPOINT_FAILED" in calls and "NO_ASSIGNED_NUMBER" in calls
    assert "favoritesScreen" in index and "saveFavorite" in index and "removeFavorite" in index and "moveFavorite" in index
    assert "setPalette('slate')" in index and "setPalette('rose')" in index
    assert "mark-unread" in calls and "/api/calls/${id}/${read?'mark-read':'mark-unread'}" in calls


def test_unread_reminders_obey_business_hours_and_stop_conditions(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import inbox_pwa
    with app.app_context():
        open_line = db.session.get(TwilioPhoneNumber, ids["line"])
        open_line.business_hours = {str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)}
        closed_line = TwilioPhoneNumber(company_id=ids["company"], phone_number="+15550003000", sms_enabled=True, voice_enabled=True, is_active=True, business_hours={str(i): {"is_open": False} for i in range(7)})
        db.session.add(closed_line); db.session.flush()
        open_conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155550001", to_number="+15550001000", is_read=False, last_message_preview="Open unread")
        closed_conv = TwilioConversation(company_id=ids["company"], phone_number_id=closed_line.id, from_number="+14155550002", to_number="+15550003000", is_read=False, last_message_preview="Closed unread")
        replied_conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155550003", to_number="+15550001000", is_read=False, last_message_preview="Already replied")
        auto_conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155550004", to_number="+15550001000", is_read=False, last_message_preview="Auto replied")
        db.session.add_all([open_conv, closed_conv, replied_conv, auto_conv]); db.session.flush()
        from models import TwilioMessage
        db.session.add_all([
            TwilioMessage(conversation_id=open_conv.id, company_id=ids["company"], direction="inbound", body="Open unread"),
            TwilioMessage(conversation_id=closed_conv.id, company_id=ids["company"], direction="inbound", body="Closed unread"),
            TwilioMessage(conversation_id=replied_conv.id, company_id=ids["company"], direction="outbound", body="Human reply"),
            TwilioMessage(conversation_id=auto_conv.id, company_id=ids["company"], direction="outbound", body="Auto reply", is_auto_reply=True),
        ])
        db.session.commit()
        sent = []
        monkeypatch.setattr(inbox_pwa, "send_pwa_push_notification", lambda company_id, **kw: sent.append(kw) or {"sent": 1, "errors": []})
        result = inbox_pwa.create_unread_message_reminders()
        assert result["created"] >= 1
        reminders = Notification.query.filter_by(event_type="unread_message_reminder").all()
        assert reminders
        assert {r.link for r in reminders} == {f"/app/inbox?conv={open_conv.id}"}
        assert sent and sent[0]["event_type"] == "unread_message_reminder"


def test_after_hours_sms_push_is_not_silent_and_defers_skip_decision_to_user_preferences(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import inbox_pwa
    with app.app_context():
        user = db.session.get(User, ids["staff"])
        user.pwa_after_hours_push_enabled = False
        line = db.session.get(TwilioPhoneNumber, ids["line"])
        line.business_hours = {str(i): {"is_open": False} for i in range(7)}
        conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155559999", to_number="+15550001000", is_read=False)
        db.session.add(conv); db.session.commit()
        sent = []
        monkeypatch.setattr(inbox_pwa, "send_pwa_push_notification", lambda company_id, **kw: sent.append(kw) or {"sent": len(kw.get("user_ids", [])), "errors": []})
        inbox_pwa._fire_push_notification(ids["company"], conv, "after hours", silent=None)
        assert sent[-1]["silent"] is False
        assert sent[-1]["event_type"] == "incoming_sms"
        assert sent[-1]["in_business_hours"] is False
        assert ids["staff"] in sent[-1]["user_ids"]


def test_service_worker_and_in_app_alerts_honor_silent_sound_vibration_flags():
    index = open("templates/inbox_pwa/index.html", encoding="utf-8").read()
    sw = open("static/sw.js", encoding="utf-8").read()
    assert "if (!data.silent)" in index
    assert "vibrateIfEnabled([80, 40, 80])" in index
    assert "playSound('sms-'" in index
    assert "navigator.setAppBadge" in index and "navigator.clearAppBadge" in index
    assert "silent:  false" in sw
    assert "renotify: data.renotify !== false" in sw
    assert "vibrate: [200, 100, 200]" in sw


def test_call_missed_voicemail_and_reminder_notifications_emit_push_and_sse(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import inbox_pwa
    with app.app_context():
        sent_push = []
        sent_sse = []
        monkeypatch.setattr(inbox_pwa, "send_pwa_push_notification", lambda company_id, **kw: sent_push.append(kw) or {"sent": 1, "errors": []})
        monkeypatch.setattr(inbox_pwa, "_push_sse_event", lambda company_id, event_type, data: sent_sse.append((event_type, data)))
        for event_type in ("missed_call", "voicemail", "unread_message_reminder"):
            inbox_pwa.create_pwa_notification(
                ids["company"],
                event_type=event_type,
                title=f"Test {event_type}",
                body="Alert body",
                link="/app/inbox?tab=calls" if event_type != "unread_message_reminder" else "/app/inbox?conv=1",
                phone_number_id=ids["line"],
            )
        assert [p["event_type"] for p in sent_push] == ["missed_call", "voicemail", "unread_message_reminder"]
        assert [e[0] for e in sent_sse] == ["missed_call", "voicemail", "unread_message_reminder"]
        assert all(e[1]["silent"] is False for e in sent_sse)


def test_unread_reminder_stops_for_resolved_or_closed_tags(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import inbox_pwa
    with app.app_context():
        line = db.session.get(TwilioPhoneNumber, ids["line"])
        line.business_hours = {str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)}
        resolved_conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155550101", to_number="+15550001000", is_read=False, tags=["resolved"], last_message_preview="Resolved")
        closed_conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155550102", to_number="+15550001000", is_read=False, tags=["closed"], last_message_preview="Closed")
        db.session.add_all([resolved_conv, closed_conv]); db.session.commit()
        monkeypatch.setattr(inbox_pwa, "send_pwa_push_notification", lambda company_id, **kw: {"sent": 1, "errors": []})
        result = inbox_pwa.create_unread_message_reminders()
        assert result["created"] == 0
        assert Notification.query.filter_by(event_type="unread_message_reminder").count() == 0

def test_pwa_badge_count_endpoint_is_user_and_company_scoped(pwa_app):
    app, client, ids = pwa_app
    with app.app_context():
        conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155550001", to_number="+15550001000", is_read=False, last_message_preview="Unread")
        hidden_conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["other_line"], from_number="+14155550002", to_number="+15550002000", is_read=False, last_message_preview="Hidden")
        call = TwilioCallLog(company_id=ids["company"], phone_number_id=ids["line"], twilio_sid="CAmissed", direction="inbound", from_number="+14155550003", to_number="+15550001000", status="missed", is_read=False)
        vm_call = TwilioCallLog(company_id=ids["company"], phone_number_id=ids["line"], twilio_sid="CAvoice", direction="inbound", from_number="+14155550004", to_number="+15550001000", status="voicemail", is_read=False)
        db.session.add_all([conv, hidden_conv, call, vm_call]); db.session.flush()
        vm = VoiceVoicemailMessage(company_id=ids["company"], call_log_id=vm_call.id, phone_number_id=ids["line"], recording_url="https://example.test/vm.mp3", is_read=False)
        note = Notification(user_id=ids["staff"], company_id=ids["company"], event_type="missed_call", title="Pending", message="Pending", is_read=False)
        db.session.add_all([vm, note]); db.session.commit()
        call_id = call.id
        conv_id = conv.id
    login(client, ids["staff"])
    data = client.get("/api/pwa/badge-count").get_json()
    assert data == {"count": 4, "smsUnread": 1, "missedCalls": 1, "voicemails": 1, "notifications": 1}
    client.patch(f"/api/inbox/conversations/{conv_id}/read", json={"is_read": True})
    client.post(f"/api/calls/{call_id}/mark-read")
    data = client.get("/api/pwa/badge-count").get_json()
    assert data["smsUnread"] == 0
    assert data["missedCalls"] == 0
    assert data["count"] == 2


def test_push_payload_non_silent_vibration_badge_and_quiet_hours(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import inbox_pwa
    with app.app_context():
        user = db.session.get(User, ids["staff"])
        db.session.add(PushSubscription(user_id=ids["staff"], company_id=ids["company"], endpoint="https://push.example/sub/ios", p256dh="p", auth_key="a", is_active=True))
        db.session.add(TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], from_number="+14155550005", to_number="+15550001000", is_read=False))
        db.session.commit()
        payloads = []
        monkeypatch.setattr(inbox_pwa, "_send_web_push_to_subscriptions", lambda subs, payload: payloads.append(payload) or {"sent": len(subs), "errors": []})
        inbox_pwa.send_pwa_push_notification(ids["company"], user_ids=[ids["staff"]], title="SMS", body="Body", event_type="incoming_sms", phone_number_id=ids["line"])
        assert payloads[-1]["silent"] is False
        assert payloads[-1]["vibrate"] == [200, 100, 200]
        assert payloads[-1]["renotify"] is True
        assert payloads[-1]["data"]["badgeCount"] >= 1
        user.pwa_vibration_enabled = False
        db.session.commit()
        inbox_pwa.send_pwa_push_notification(ids["company"], user_ids=[ids["staff"]], title="iOS", body="Body", event_type="missed_call", phone_number_id=ids["line"])
        assert payloads[-1]["silent"] is False
        assert payloads[-1]["vibrate"] == []
        user.pwa_quiet_hours_start = "00:00"
        user.pwa_quiet_hours_end = "23:59"
        db.session.commit()
        inbox_pwa.send_pwa_push_notification(ids["company"], user_ids=[ids["staff"]], title="Quiet", body="Body", event_type="voicemail", phone_number_id=ids["line"])
        assert payloads[-1]["silent"] is False
        assert payloads[-1]["data"]["debug_reason"] == "quiet_hours"



def test_push_subscribe_creates_device_and_debug_counts_device_subscription(pwa_app):
    app, client, ids = pwa_app
    login(client, ids["staff"])
    resp = client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example/sub/device-debug",
        "keys": {"p256dh": "p", "auth": "a"},
        "device_key": "repair-device",
        "device_label": "Repair Phone",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["active_subscriptions"] == 1
    assert data["device_active_subscriptions"] == 1

    debug = client.get("/api/pwa/push/debug", headers={"X-PWA-Device-Key": "repair-device"})
    assert debug.status_code == 200
    payload = debug.get_json()
    assert payload["active_subscriptions"] == 1
    assert payload["device_active_subscriptions"] == 1
    assert payload["device_key"] == "repair-device"
    assert payload["vapid_public_key_present"] in {True, False}
    assert payload["subscriptions"][0]["device_key"] == "repair-device"

    with app.app_context():
        from models import PWADevice
        device = PWADevice.query.filter_by(company_id=ids["company"], user_id=ids["staff"], device_key="repair-device").one()
        assert device.push_enabled is True



def test_push_unsubscribe_auth_and_permission_match_push_routes(pwa_app):
    app, client, ids = pwa_app
    unauth = client.post("/api/pwa/push/unsubscribe", json={"endpoint": "https://push.example/sub/auth"})
    assert unauth.status_code == 401

    with app.app_context():
        blocked = User(username="pwa_unsub_blocked", email="pwa_unsub_blocked@example.com", password_hash=generate_password_hash("pw"), default_company_id=ids["company"])
        db.session.add(blocked)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=blocked.id, company_id=ids["company"], role="staff", is_default=True, can_access_mobile_inbox=False, pwa_access_enabled=False))
        db.session.commit()
        blocked_id = blocked.id

    login(client, blocked_id)
    forbidden = client.post("/api/pwa/push/unsubscribe", json={"endpoint": "https://push.example/sub/auth"})
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"] == "Mobile inbox access is not enabled for this account."

    login(client, ids["staff"])
    sub = client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example/sub/auth",
        "keys": {"p256dh": "p", "auth": "a"},
        "device_key": "auth-device",
    })
    assert sub.status_code == 200
    ok = client.post("/api/pwa/push/unsubscribe", json={"endpoint": "https://push.example/sub/auth"})
    assert ok.status_code == 200
    assert ok.get_json()["disabled"] == 1

def test_push_subscribe_rejects_incomplete_browser_subscription(pwa_app):
    app, client, ids = pwa_app
    login(client, ids["staff"])
    resp = client.post("/api/pwa/push/subscribe", json={"endpoint": "https://push.example/sub/missing-keys"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "MISSING_SUBSCRIPTION_KEYS"

def test_push_debug_uses_logged_in_pwa_user_session(pwa_app):
    app, client, ids = pwa_app
    login(client, ids["staff"])
    with app.app_context():
        db.session.add(PushSubscription(
            user_id=ids["staff"],
            company_id=ids["company"],
            endpoint="https://push.example/sub/debug",
            p256dh="p",
            auth_key="a",
            is_active=True,
        ))
        db.session.commit()
    resp = client.get("/api/pwa/push/debug")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["user"]["id"] == ids["staff"]
    assert data["company"]["id"] == ids["company"]
    assert data["mobile_inbox_access"] is True
    assert data["active_subscriptions"] == 1
    assert data["decision"]["event_type"] == "incoming_sms"


def test_push_debug_returns_clear_auth_and_permission_responses(pwa_app):
    app, client, ids = pwa_app
    unauth = client.get("/api/pwa/push/debug")
    assert unauth.status_code == 401
    assert unauth.get_json()["code"] == "AUTHENTICATION_REQUIRED"
    with app.app_context():
        blocked = User(username="pwa_blocked", email="pwa_blocked@example.com", password_hash=generate_password_hash("pw"), default_company_id=ids["company"])
        db.session.add(blocked)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=blocked.id, company_id=ids["company"], role="staff", is_default=True, can_access_mobile_inbox=False, pwa_access_enabled=False))
        db.session.commit()
        blocked_id = blocked.id
    login(client, blocked_id)
    forbidden = client.get("/api/pwa/push/debug")
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"] == "Mobile inbox access is not enabled for this account."


def test_pwa_sound_forwarding_autoreply_static_requirements():
    sw = open("static/sw.js", encoding="utf-8").read()
    html = open("templates/inbox_pwa/index.html", encoding="utf-8").read()
    nav = open("templates/inbox_pwa/_bottom_nav.html", encoding="utf-8").read()
    migration = open("migrations/20260705_pwa_sound_forwarding_autoreply.sql", encoding="utf-8").read()
    assert "20260710-push-receipt-ack" in sw
    assert "silent:  false" in sw
    assert "renotify: data.renotify !== false" in sw
    assert "[200, 100, 200]" in sw
    assert "high_priority_messages" in sw and "channel_id" in sw and "importance" in sw
    assert "requireInteraction: data.requireInteraction !== false" in sw
    assert "PUSH_DIAGNOSTICS" in sw and "lastNotificationSilent" in sw
    assert "Call Forwarding" in html and "Business-hours SMS auto reply" in html
    assert "Push Diagnostics" in html and "lastNotificationSilent" in html
    assert "buildPushLifecycleAudit" in html and "browserPushAudit" in html
    assert "database_insert_update_confirmed" in html
    assert "Clear Push Diagnostic History" in html and "CLEAR_PUSH_DIAGNOSTICS" in sw
    clear_history_body = html.split("function clearPushDiagnosticHistory()", 1)[1].split("function recordPushLifecycle", 1)[0]
    assert "subscription.unsubscribe" not in clear_history_body
    assert "/api/pwa/push/unsubscribe" not in clear_history_body
    assert "installPwaNavPerformanceHandlers" in html
    assert "pwa-tab-loading" in nav
    assert "env(safe-area-inset-bottom)" in nav
    assert "visualViewport" in html
    assert "call_forwarding_enabled" in migration
    assert "business_hours_auto_reply_enabled" in migration
    assert "final_status" in migration


def test_google_contacts_enriches_phone_only_contact_and_preserves_existing_tags(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None

    with app.app_context():
        contact = Contact(company_id=ids["company"], phone="4155551212", normalized_phone="+14155551212", tags="MyOrder Customer", is_active=True)
        db.session.add(contact); db.session.commit(); contact_id = contact.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {
            "+14155551212": {"resource_name": "people/g1", "name": "Jane Doe", "first_name": "Jane", "last_name": "Doe", "email": "jane@example.com", "phone": "+14155551212", "normalized_phone": "+14155551212", "company": "Acme"}
        })
        result = google_contacts.sync_contacts(ids["staff"], ids["company"])
        enriched = db.session.get(Contact, contact_id)
        assert result["updated"] == 1
        assert enriched.first_name == "Jane"
        assert enriched.last_name == "Doe"
        assert enriched.email == "jane@example.com"
        assert enriched.company == "Acme"
        assert enriched.source == "google_contacts"
        assert enriched.source_detail == "Google Contacts sync"
        assert enriched.external_google_contact_id == "people/g1"
        assert "MyOrder Customer" in enriched.tags and "Google Contact" in enriched.tags


def test_google_contacts_enriches_email_only_contact_without_overwriting_existing_name(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None

    with app.app_context():
        contact = Contact(company_id=ids["company"], email="person@example.com", first_name="Existing", name="Existing Name", is_active=True)
        db.session.add(contact); db.session.commit(); contact_id = contact.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {
            "+14155550000": {"resource_name": "people/g2", "name": "Google Name", "first_name": "Google", "last_name": "Name", "email": "person@example.com", "phone": "+14155550000", "normalized_phone": "+14155550000"}
        })
        google_contacts.sync_contacts(ids["staff"], ids["company"])
        enriched = db.session.get(Contact, contact_id)
        assert enriched.first_name == "Existing"
        assert enriched.name == "Existing Name"
        assert enriched.last_name == "Name"
        assert enriched.phone == "+14155550000"


def test_google_contacts_duplicate_merge_repoints_twilio_and_unions_tags(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None

    with app.app_context():
        survivor = Contact(company_id=ids["company"], phone="+14155551212", is_active=True, tags="MyOrder Customer")
        duplicate = Contact(company_id=ids["company"], phone="4155551212", email="jane@example.com", first_name="Jane", tags="VIP", is_active=True)
        db.session.add_all([survivor, duplicate]); db.session.flush()
        conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], contact_id=duplicate.id, from_number="+14155551212", to_number="+15550001000", contact_name="4155551212")
        db.session.add(conv); db.session.commit(); survivor_id = survivor.id; duplicate_id = duplicate.id; conv_id = conv.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": {"name": "Jane Doe", "email": "jane@example.com", "phone": "+14155551212", "normalized_phone": "+14155551212"}})
        result = google_contacts.sync_contacts(ids["staff"], ids["company"])
        kept = db.session.get(Contact, survivor_id)
        old = db.session.get(Contact, duplicate_id)
        refreshed = db.session.get(TwilioConversation, conv_id)
        assert result["merged"] == 1
        assert old.is_active is False
        assert refreshed.contact_id == survivor_id
        assert kept.name == "Jane Doe"
        assert "MyOrder Customer" in kept.tags and "VIP" in kept.tags and "Google Contact" in kept.tags


def test_google_contacts_cross_company_contacts_do_not_merge_and_preview_counts(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None

    with app.app_context():
        other = Company(name="Other Co", is_active=True); db.session.add(other); db.session.flush()
        db.session.add(Contact(company_id=other.id, phone="+14155551212", first_name="Other", is_active=True))
        local = Contact(company_id=ids["company"], phone="+14155551212", is_active=True)
        db.session.add(local); db.session.commit(); local_id = local.id; other_id = other.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": "Jane Doe", "+14155559999": "New Person"})
        preview = google_contacts.sync_contacts(ids["staff"], ids["company"], dry_run=True)
        assert preview["updated"] == 1
        assert preview["merged"] == 0
        assert preview["created"] == 1
        assert db.session.get(Contact, local_id).name is None
        assert Contact.query.filter_by(company_id=other_id, is_active=True).count() == 1


def test_google_contacts_status_reports_expired_token_reconnect_required(pwa_app):
    app, client, ids = pwa_app
    from datetime import datetime, timedelta
    from models import GoogleOAuthToken
    with app.app_context():
        db.session.add(GoogleOAuthToken(user_id=ids["staff"], access_token="old", refresh_token=None, token_expiry=datetime.utcnow() - timedelta(hours=1)))
        db.session.commit()
    login(client, ids["staff"])
    resp = client.get("/api/inbox/google-contacts/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["reconnect_required"] is True
    assert data["oauth_expired"] is True


def test_google_contacts_sync_job_and_merge_audit_records_created(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts
    from models import ContactMergeAudit, GoogleContactsSyncJob

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None; google_sync_token = None; google_account_email = "owner@example.com"

    with app.app_context():
        survivor = Contact(company_id=ids["company"], phone="+14155551212", normalized_phone="+14155551212", tags="MyOrder Customer", is_active=True)
        duplicate = Contact(company_id=ids["company"], phone="4155551212", email="audit@example.com", tags="VIP", is_active=True)
        db.session.add_all([survivor, duplicate]); db.session.flush()
        conv = TwilioConversation(company_id=ids["company"], phone_number_id=ids["line"], contact_id=duplicate.id, from_number="+14155551212", to_number="+15550001000")
        db.session.add(conv); db.session.commit(); survivor_id = survivor.id; duplicate_id = duplicate.id; conv_id = conv.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": {"resource_name": "people/audit", "name": "Audit Person", "email": "audit@example.com", "phone": "+14155551212", "normalized_phone": "+14155551212"}})
        result = google_contacts.sync_contacts(ids["staff"], ids["company"])
        job = db.session.get(GoogleContactsSyncJob, result["sync_job_id"])
        audit = ContactMergeAudit.query.filter_by(sync_job_id=job.id, source_contact_id=duplicate_id, destination_contact_id=survivor_id).one()
        assert job.status == "completed"
        assert job.google_account_email == "owner@example.com"
        assert job.contacts_merged == 1
        assert audit.google_resource_id == "people/audit"
        assert audit.phone_match is True
        assert audit.match_confidence >= 95
        assert audit.reference_mappings[0]["to_contact_id"] == survivor_id
        assert db.session.get(TwilioConversation, conv_id).contact_id == survivor_id


def test_google_contacts_low_confidence_email_merge_requires_review(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts
    from models import ContactMergeAudit

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None; google_sync_token = None

    with app.app_context():
        survivor = Contact(company_id=ids["company"], phone="+14155551212", normalized_phone="+14155551212", is_active=True)
        contact = Contact(company_id=ids["company"], email="Case@Test.COM", first_name="Case", is_active=True)
        db.session.add_all([survivor, contact]); db.session.commit(); contact_id = contact.id
        monkeypatch.setenv("GOOGLE_CONTACTS_AUTO_MERGE_THRESHOLD", "80")
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": {"resource_name": "people/email", "name": "Case Email", "email": " case@test.com ", "phone": "+14155551212", "normalized_phone": "+14155551212"}})
        result = google_contacts.sync_contacts(ids["staff"], ids["company"], dry_run=True)
        assert result["preview"]["will_update"]
        assert result["preview"]["possible_merge_requires_review"]
        assert result["preview"]["possible_merge_requires_review"][0]["confidence"] == 75
        assert db.session.get(Contact, contact_id).last_name is None
        assert ContactMergeAudit.query.count() == 0


def test_google_contacts_preview_payload_and_dry_run_do_not_modify_database(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts
    from models import GoogleContactsSyncJob

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None; google_sync_token = None

    with app.app_context():
        contact = Contact(company_id=ids["company"], phone="(415) 555-1212 x99", is_active=True)
        db.session.add(contact); db.session.commit(); contact_id = contact.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": "Dry Run"})
        result = google_contacts.sync_contacts(ids["staff"], ids["company"], dry_run=True)
        unchanged = db.session.get(Contact, contact_id)
        job = db.session.get(GoogleContactsSyncJob, result["sync_job_id"])
        assert result["preview"]["will_update"][0]["fields_to_update"]
        assert unchanged.name is None
        assert unchanged.normalized_phone is None
        assert job.dry_run is True and job.preview_payload["will_update"]


def test_google_contacts_incremental_sync_token_and_avatar_metadata_refresh(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts
    from models import GoogleOAuthToken

    with app.app_context():
        tok = GoogleOAuthToken(user_id=ids["staff"], access_token="tok", refresh_token="ref", google_sync_token="sync-1")
        db.session.add(tok); db.session.commit(); seen = {}
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: tok)
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        def fake_fetch(access, sync_token=None):
            seen["sync_token"] = sync_token
            return {"+14155550101": {"resource_name": "people/avatar", "name": "Avatar Person", "phone": "+14155550101", "normalized_phone": "+14155550101", "avatar_url": "https://lh3.googleusercontent.com/a/photo"}, "__meta__": {"next_sync_token": "sync-2", "incremental": bool(sync_token)}}
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", fake_fetch)
        result = google_contacts.sync_contacts(ids["staff"], ids["company"])
        contact = Contact.query.filter_by(company_id=ids["company"], external_google_contact_id="people/avatar").one()
        assert seen["sync_token"] == "sync-1"
        assert result["incremental"] is True
        assert tok.google_sync_token == "sync-2"
        assert contact.avatar_url == "https://lh3.googleusercontent.com/a/photo"


def test_google_contact_normalization_handles_phone_and_email_variants():
    from services.google_contacts import normalize_email, normalize_phone
    assert normalize_phone("001 44 7911 123456 ext. 9") == "+447911123456"
    assert normalize_phone("(415) 555-1212 x123") == "+14155551212"
    assert normalize_phone("+1 415-555-1212") == "+14155551212"
    assert normalize_email("  CASE@Example.COM ") == "case@example.com"


def test_google_contacts_default_phone_only_duplicate_requires_review(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts
    from models import ContactMergeAudit

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None; google_sync_token = None

    with app.app_context():
        survivor = Contact(company_id=ids["company"], phone="+14155551212", normalized_phone="+14155551212", is_active=True)
        duplicate = Contact(company_id=ids["company"], phone="4155551212", tags="VIP", is_active=True)
        db.session.add_all([survivor, duplicate]); db.session.commit(); duplicate_id = duplicate.id
        monkeypatch.delenv("GOOGLE_CONTACTS_AUTO_MERGE_THRESHOLD", raising=False)
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": "Phone Only"})
        result = google_contacts.sync_contacts(ids["staff"], ids["company"])
        assert result["merged"] == 0
        assert result["review_required"] is True
        assert result["preview"]["possible_merge_requires_review"][0]["confidence"] == 70
        assert db.session.get(Contact, duplicate_id).is_active is True
        assert ContactMergeAudit.query.count() == 0


def test_google_contacts_merge_repointing_is_company_scoped(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts

    class DummyToken:
        last_sync_at = None; contacts_synced = 0; sync_error = None; google_sync_token = None

    with app.app_context():
        other = Company(name="Scoped Other", is_active=True); db.session.add(other); db.session.flush()
        survivor = Contact(company_id=ids["company"], phone="+14155551212", normalized_phone="+14155551212", is_active=True)
        duplicate = Contact(company_id=ids["company"], phone="4155551212", email="scope@example.com", is_active=True)
        db.session.add_all([survivor, duplicate]); db.session.flush()
        same_company_call = TwilioCallLog(company_id=ids["company"], contact_id=duplicate.id, twilio_sid="CAsame", direction="inbound", from_number="+14155551212", to_number="+15550001000")
        other_company_call = TwilioCallLog(company_id=other.id, contact_id=duplicate.id, twilio_sid="CAother", direction="inbound", from_number="+14155551212", to_number="+15550001000")
        db.session.add_all([same_company_call, other_company_call]); db.session.commit()
        survivor_id = survivor.id; duplicate_id = duplicate.id; same_id = same_company_call.id; other_id = other_company_call.id
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: DummyToken())
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access: {"+14155551212": {"name": "Scoped", "email": "scope@example.com", "phone": "+14155551212", "normalized_phone": "+14155551212"}})
        result = google_contacts.sync_contacts(ids["staff"], ids["company"])
        assert result["merged"] == 1
        assert db.session.get(TwilioCallLog, same_id).contact_id == survivor_id
        assert db.session.get(TwilioCallLog, other_id).contact_id == duplicate_id


def test_google_contacts_preview_payload_is_bounded_and_token_metadata_unchanged_on_dry_run(pwa_app, monkeypatch):
    app, _client, ids = pwa_app
    import services.google_contacts as google_contacts
    from models import GoogleContactsSyncJob, GoogleOAuthToken

    with app.app_context():
        tok = GoogleOAuthToken(user_id=ids["staff"], access_token="secret-access", refresh_token="secret-refresh", contacts_synced=99, google_sync_token="sync-old")
        db.session.add(tok); db.session.commit()
        monkeypatch.setenv("GOOGLE_CONTACTS_PREVIEW_LIMIT", "2")
        monkeypatch.setattr(google_contacts, "get_token", lambda user_id: tok)
        monkeypatch.setattr(google_contacts, "_refresh_if_needed", lambda token: "access")
        monkeypatch.setattr(google_contacts, "_fetch_all_contacts", lambda access, sync_token=None: {
            f"+14155550{i:03d}": {"name": f"Person {i}", "phone": f"+14155550{i:03d}", "normalized_phone": f"+14155550{i:03d}"}
            for i in range(5)
        })
        result = google_contacts.sync_contacts(ids["staff"], ids["company"], dry_run=True)
        job = db.session.get(GoogleContactsSyncJob, result["sync_job_id"])
        assert len(result["preview"]["will_create"]) == 2
        assert result["preview"]["omitted_counts"]["will_create"] == 3
        assert job.dry_run is True
        assert tok.contacts_synced == 99
        assert tok.google_sync_token == "sync-old"
        serialized = str(result) + str(job.preview_payload) + str(job.errors or [])
        assert "secret-access" not in serialized and "secret-refresh" not in serialized


def test_push_subscribe_is_idempotent_per_endpoint_and_device(pwa_app):
    app, client, ids = pwa_app
    login(client, ids["staff"])
    payload = {
        "endpoint": "https://push.example/sub/idempotent",
        "keys": {"p256dh": "first-key", "auth": "first-auth"},
        "device_key": "same-device",
        "device_label": "Same Device",
    }
    first = client.post("/api/pwa/push/subscribe", json=payload)
    second_payload = dict(payload)
    second_payload["keys"] = {"p256dh": "rotated-key", "auth": "rotated-auth"}
    second = client.post("/api/pwa/push/subscribe", json=second_payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json["subscription_id"] == second.json["subscription_id"]
    assert second.json["active_subscriptions"] == 1
    assert second.json["device_active_subscriptions"] == 1
    with app.app_context():
        rows = PushSubscription.query.filter_by(endpoint="https://push.example/sub/idempotent", is_active=True).all()
        assert len(rows) == 1
        assert rows[0].user_id == ids["staff"]
        assert rows[0].company_id == ids["company"]
        assert rows[0].device_key == "same-device"
        assert rows[0].p256dh == "rotated-key"


def test_push_subscriptions_are_per_authenticated_user_not_shared_line(pwa_app, monkeypatch):
    app, client, ids = pwa_app
    with app.app_context():
        other = User(username="pwa_staff_2", email="pwa_staff_2@example.com", password_hash=generate_password_hash("pw"), default_company_id=ids["company"])
        db.session.add(other)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=other.id, company_id=ids["company"], role="staff", is_default=True, can_access_mobile_inbox=True))
        db.session.add(PhoneNumberUserPermission(company_id=ids["company"], user_id=other.id, phone_number_id=ids["line"], can_access_pwa=True, can_view_sms=True, can_view_calls=True, can_view_voicemail=True))
        db.session.commit()
        other_id = other.id

    import inbox_pwa
    current_user_id = {"id": ids["staff"]}
    monkeypatch.setattr(inbox_pwa, "_current_user", lambda: db.session.get(User, current_user_id["id"]))
    staff_resp = client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example/sub/shared-line-staff",
        "keys": {"p256dh": "staff-key", "auth": "staff-auth"},
        "device_key": "staff-device",
    })
    assert staff_resp.status_code == 200

    current_user_id["id"] = other_id
    other_resp = client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example/sub/shared-line-other",
        "keys": {"p256dh": "other-key", "auth": "other-auth"},
        "device_key": "other-device",
    })
    assert other_resp.status_code == 200
    assert other_resp.json["success"] is True
    assert other_resp.json["device_active_subscriptions"] == 1

    with app.app_context():
        staff_sub = PushSubscription.query.filter_by(user_id=ids["staff"], device_key="staff-device", is_active=True).one()
        other_sub = PushSubscription.query.filter_by(user_id=other_id, device_key="other-device", is_active=True).one()
        assert staff_sub.company_id == other_sub.company_id == ids["company"]
        assert staff_sub.endpoint != other_sub.endpoint
        assert PushSubscription.query.filter_by(company_id=ids["company"], is_active=True).count() == 2


def test_push_test_separates_provider_acceptance_from_delivery_confirmation(pwa_app, monkeypatch):
    app, client, ids = pwa_app
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")
    login(client, ids["staff"])
    client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example/sub/test-state",
        "keys": {"p256dh": "key", "auth": "auth"},
        "device_key": "test-device",
    })
    import inbox_pwa
    monkeypatch.setattr(inbox_pwa, "_send_web_push_to_subscriptions", lambda subs, payload: {"sent": len(subs), "errors": []})

    resp = client.post("/api/pwa/push/test")
    assert resp.status_code == 200
    assert resp.json["success"] is True
    assert resp.json["backend_persisted"] is True
    assert resp.json["provider_accepted"] is True
    assert resp.json["service_worker_receipt_confirmed"] is False
    assert resp.json["notification_display_confirmed"] is False
    assert "lastPushReceivedAt" in resp.json["delivery_note"]


def test_push_provider_expiration_deactivates_only_affected_subscription(pwa_app, monkeypatch):
    app, client, ids = pwa_app
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.com")
    with app.app_context():
        expired = PushSubscription(user_id=ids["staff"], company_id=ids["company"], endpoint="https://push.example/sub/expired", p256dh="p", auth_key="a", is_active=True)
        healthy = PushSubscription(user_id=ids["denied"], company_id=ids["company"], endpoint="https://push.example/sub/healthy", p256dh="p", auth_key="a", is_active=True)
        db.session.add_all([expired, healthy])
        db.session.commit()
        expired_id = expired.id
        healthy_id = healthy.id

    import inbox_pwa
    calls = []
    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims):
        calls.append(subscription_info["endpoint"])
        if subscription_info["endpoint"].endswith("/expired"):
            raise Exception("410 Gone")
    monkeypatch.setattr("pywebpush.webpush", fake_webpush)

    with app.app_context():
        result = inbox_pwa._send_web_push_to_subscriptions(PushSubscription.query.order_by(PushSubscription.id).all(), {"title": "test"})
        assert result["sent"] == 1
        assert any("410" in error for error in result["errors"])
        assert db.session.get(PushSubscription, expired_id).is_active is False
        assert db.session.get(PushSubscription, healthy_id).is_active is True
        assert calls == ["https://push.example/sub/expired", "https://push.example/sub/healthy"]


def test_push_receipt_endpoint_records_redacted_service_worker_delivery_state(pwa_app):
    app, client, ids = pwa_app
    login(client, ids["staff"])
    client.post("/api/pwa/push/subscribe", json={
        "endpoint": "https://push.example/sub/receipt-device",
        "keys": {"p256dh": "key", "auth": "auth"},
        "device_key": "receipt-device",
    })

    receipt = client.post("/api/pwa/push/receipt", json={
        "stage": "displayed",
        "received_at": "2026-07-10T13:30:00.000Z",
        "sw_version": "20260710-push-receipt-ack",
        "event_type": "push_test",
        "tag": "push-test",
        "silent": False,
        "renotify": True,
        "vibrate": [200, 100, 200],
        "requireInteraction": False,
        "subscription": {
            "endpoint": "https://push.example/sub/receipt-device",
        },
    })
    assert receipt.status_code == 200
    assert receipt.json["success"] is True
    assert receipt.json["device_key"] == "receipt-device"
    assert receipt.json["endpoint_redacted"].startswith("https://push.example/")
    assert "receipt-device" in receipt.json["endpoint_redacted"]

    debug = client.get("/api/pwa/push/debug", headers={"X-PWA-Device-Key": "receipt-device"})
    assert debug.status_code == 200
    payload = debug.get_json()
    assert payload["latest_push_receipt"]["stage"] == "displayed"
    assert payload["latest_push_receipt"]["sw_version"] == "20260710-push-receipt-ack"
    assert payload["latest_push_receipt"]["silent"] is False
    assert payload["latest_push_receipt"]["renotify"] is True
    assert payload["latest_push_receipt"]["vibrate"] == [200, 100, 200]
    assert payload["latest_push_receipt"]["endpoint_redacted"].startswith("https://push.example/")
    serialized = str(payload["push_receipts"])
    assert "key" not in serialized and "auth" not in serialized
