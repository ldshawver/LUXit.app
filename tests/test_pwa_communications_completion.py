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
    assert "silent:  data.silent === true" in sw
    assert "renotify: data.renotify !== false" in sw
    assert "vibrate: data.silent === true ? [] : (data.vibrate || [200, 100, 200])" in sw


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
        assert payloads[-1]["silent"] is True
