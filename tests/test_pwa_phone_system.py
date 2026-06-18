import os
from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import (
    CallEvent,
    Company,
    Contact,
    PhoneSettings,
    AutoReplyRule,
    TwilioAccount,
    TwilioCallLog,
    TwilioPhoneNumber,
    PWADevice,
    User,
    UserCompanyAccess,
    VoiceVoicemailMessage,
    TwilioConversation,
    TwilioMessage,
)


@pytest.fixture
def app():
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    os.environ.setdefault("TWILIO_API_KEY", "SKtest")
    os.environ.setdefault("TWILIO_API_SECRET", "secret")
    os.environ.setdefault("DEFAULT_PHONE_TIMEZONE", "America/New_York")
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def world(app):
    with app.app_context():
        co_a = Company(name="Phone Co A", is_active=True)
        co_b = Company(name="Phone Co B", is_active=True)
        db.session.add_all([co_a, co_b])
        db.session.flush()
        alice = User(username="phone_alice", email="phone_alice@test.com", password_hash="x", default_company_id=co_a.id)
        bob = User(username="phone_bob", email="phone_bob@test.com", password_hash="x", default_company_id=co_b.id)
        db.session.add_all([alice, bob])
        db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=alice.id, company_id=co_a.id, role=UserCompanyAccess.ROLE_ADMIN, is_default=True, can_access_mobile_inbox=True),
            UserCompanyAccess(user_id=bob.id, company_id=co_b.id, role=UserCompanyAccess.ROLE_ADMIN, is_default=True, can_access_mobile_inbox=True),
            TwilioAccount(company_id=co_a.id, from_phone="+15550001000", _account_sid="ACtest", _auth_token="auth", call_forward_to="+15550009999", voice_forwarding_enabled=True),
            TwilioAccount(company_id=co_b.id, from_phone="+15550002000", _account_sid="ACtest", _auth_token="auth"),
        ])
        db.session.commit()
        yield {"co_a": co_a.id, "co_b": co_b.id, "alice": alice.id, "bob": bob.id}


def login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def save_settings(company_id, **overrides):
    defaults = {
        "company_id": company_id,
        "timezone": "UTC",
        "business_hours": {str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)},
        "during_hours_route": "ring_pwa",
        "after_hours_route": "voicemail",
        "recording_enabled": True,
        "transcription_enabled": True,
    }
    defaults.update(overrides)
    s = PhoneSettings(**defaults)
    db.session.add(s)
    db.session.commit()
    return s


def post_incoming(client, to="+15550001000", sid="CAin", **extra):
    payload = {"From": "+15551112222", "To": to, "CallSid": sid, "CallStatus": "ringing"}
    payload.update(extra)
    return client.post("/api/twilio/voice/incoming", data=payload)


def test_business_hours_route_rings_pwa_with_client_twiml(app, client, world):
    with app.app_context():
        save_settings(world["co_a"], during_hours_route="ring_pwa")
    resp = post_incoming(client, sid="CApwa")
    assert resp.status_code == 200
    assert b"<Client>" in resp.data
    assert b"luxit_c" in resp.data
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CApwa", company_id=world["co_a"]).one()
        assert log.status == "ringing"
        assert CallEvent.query.filter_by(call_log_id=log.id, event_type="incoming").count() == 1


def test_after_hours_route_goes_to_voicemail(app, client, world):
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)}, after_hours_route="voicemail")
    resp = post_incoming(client, sid="CAafter")
    assert resp.status_code == 200
    assert b"<Record" in resp.data
    assert b"<Client>" not in resp.data


def test_forwarding_route_logs_destination_and_records(app, client, world):
    with app.app_context():
        save_settings(world["co_a"], during_hours_route="forward", forward_number="+15554443333", fallback_forward_number="+15554444444", recording_enabled=True)
    resp = post_incoming(client, sid="CAfwd")
    assert resp.status_code == 200
    assert b"+15554443333" in resp.data
    assert b"record-from-answer" in resp.data
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CAfwd").one()
        assert log.status == "forwarded"
        assert log.forwarded_to_number == "+15554443333"


def test_recording_callback_associates_recording_and_voicemail(app, client, world):
    with app.app_context():
        log = TwilioCallLog(company_id=world["co_a"], twilio_sid="CArec", direction="inbound", status="missed")
        db.session.add(log); db.session.commit()
    resp = client.post("/api/twilio/voice/recording", data={"To": "+15550001000", "CallSid": "CArec", "RecordingSid": "RE1", "RecordingUrl": "https://rec", "RecordingDuration": "12", "RecordingSource": "RecordVerb"})
    assert resp.status_code == 204
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CArec").one()
        assert log.voicemail_url == "https://rec"
        assert log.recording_sid == "RE1"
        assert VoiceVoicemailMessage.query.filter_by(call_log_id=log.id, recording_sid="RE1").count() == 1


def test_transcription_callback_updates_call_log(app, client, world):
    with app.app_context():
        log = TwilioCallLog(company_id=world["co_a"], twilio_sid="CAtr", direction="inbound", status="voicemail", voicemail_sid="RE2")
        db.session.add(log); db.session.commit()
    resp = client.post("/api/twilio/voice/transcription", data={"To": "+15550001000", "CallSid": "CAtr", "RecordingSid": "RE2", "TranscriptionSid": "TR1", "TranscriptionStatus": "completed", "TranscriptionText": "Call me back"})
    assert resp.status_code == 204
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CAtr").one()
        assert log.transcription_text == "Call me back"
        assert log.transcription_status == "complete"


def test_duplicate_incoming_webhook_does_not_duplicate_call_or_event(app, client, world):
    with app.app_context():
        save_settings(world["co_a"])
    post_incoming(client, sid="CAdup")
    post_incoming(client, sid="CAdup")
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CAdup").one()
        assert TwilioCallLog.query.filter_by(twilio_sid="CAdup").count() == 1
        assert CallEvent.query.filter_by(call_log_id=log.id, event_type="incoming").count() == 1


def test_settings_save_load_and_voice_token(client, world):
    login(client, world["alice"])
    resp = client.put("/api/phone/settings", json={"timezone": "America/Chicago", "during_hours_route": "forward", "forward_number": "+15551230000", "missed_call_sms_enabled": True, "missed_call_sms_body": "Sorry we missed you"})
    assert resp.status_code == 200
    body = client.get("/api/phone/settings").get_json()["settings"]
    assert body["timezone"] == "America/Chicago"
    assert body["missed_call_sms_enabled"] is True
    token_resp = client.get("/api/phone/voice-token")
    assert token_resp.status_code == 200
    identity = token_resp.get_json()["identity"]
    assert identity.startswith(f"luxit_c{world['co_a']}_")
    assert identity != f"luxit_company_{world['co_a']}_pwa"


def test_tenant_isolation_for_calls_and_archive_read(client, app, world):
    with app.app_context():
        mine = TwilioCallLog(company_id=world["co_a"], direction="inbound", status="voicemail", from_number="+1", to_number="+2")
        other = TwilioCallLog(company_id=world["co_b"], direction="inbound", status="missed", from_number="+3", to_number="+4")
        db.session.add_all([mine, other]); db.session.commit(); mine_id, other_id = mine.id, other.id
    login(client, world["alice"])
    recent = client.get("/api/calls/recent").get_json()["calls"]
    assert [c["id"] for c in recent] == [mine_id]
    assert client.post(f"/api/calls/{other_id}/archive").status_code == 404
    assert client.post(f"/api/calls/{other_id}/accept").status_code == 404
    assert client.post(f"/api/calls/{mine_id}/mark-read").status_code == 200
    assert client.post(f"/api/calls/{mine_id}/archive").status_code == 200


def test_accept_decline_voicemail_end_actions_log_answered_user(client, app, world):
    with app.app_context():
        call = TwilioCallLog(company_id=world["co_a"], direction="inbound", status="ringing")
        db.session.add(call); db.session.commit(); call_id = call.id
    login(client, world["alice"])
    assert client.post(f"/api/calls/{call_id}/accept").status_code == 200
    with app.app_context():
        call = db.session.get(TwilioCallLog, call_id)
        assert call.status == "answered"
        assert call.answered_by_user_id == world["alice"]
        assert call.answered_at is not None
    assert client.post(f"/api/calls/{call_id}/end").status_code == 200


def test_twilio_signature_strict_rejects_unsigned(client, app, world, monkeypatch):
    monkeypatch.setenv("TWILIO_STRICT_SIGNATURE", "1")
    with app.app_context():
        save_settings(world["co_a"])
    resp = post_incoming(client, sid="CAbad")
    assert resp.status_code == 403


def test_tenant_pwa_identity_is_scoped_to_called_number(app, client, world):
    with app.app_context():
        save_settings(world["co_a"], during_hours_route="ring_pwa")
        save_settings(world["co_b"], during_hours_route="ring_pwa")
        from services.phone_identity import pwa_voice_identity
        a_identity = pwa_voice_identity(world["co_a"]).encode()
        b_identity = pwa_voice_identity(world["co_b"]).encode()
    resp = post_incoming(client, to="+15550002000", sid="CAbtenant")
    assert resp.status_code == 200
    assert b_identity in resp.data
    assert a_identity not in resp.data


def test_call_action_idempotency_does_not_duplicate_audit_rows(client, app, world):
    with app.app_context():
        call = TwilioCallLog(company_id=world["co_a"], direction="inbound", status="ringing")
        db.session.add(call); db.session.commit(); call_id = call.id
    login(client, world["alice"])
    assert client.post(f"/api/calls/{call_id}/accept").status_code == 200
    assert client.post(f"/api/calls/{call_id}/accept").status_code == 200
    with app.app_context():
        call = db.session.get(TwilioCallLog, call_id)
        assert call.status == "answered"
        assert CallEvent.query.filter_by(call_log_id=call_id, event_type="pwa_action", provider_event_id=f"pwa:answered:{world['alice']}").count() == 1


def test_after_hours_sms_auto_reply_respects_opt_out_and_cooldown(app, client, world, monkeypatch):
    sent = []
    monkeypatch.setattr("twilio_sms._send_sms", lambda ta, to, body: sent.append((to, body)) or {"success": True})
    with app.app_context():
        save_settings(
            world["co_a"],
            business_hours={str(i): {"is_open": False} for i in range(7)},
            after_hours_sms_enabled=True,
            after_hours_sms_body="We are closed",
        )
        db.session.add(TwilioConversation(company_id=world["co_a"], from_number="+15551112222", to_number="+15550001000", is_opted_out=True))
        db.session.commit()
    post_incoming(client, sid="CAsmsOpt")
    assert sent == []
    with app.app_context():
        TwilioConversation.query.filter_by(company_id=world["co_a"], from_number="+15551112222").delete()
        db.session.commit()
    post_incoming(client, sid="CAsms1")
    post_incoming(client, sid="CAsms2")
    assert sent == [("+15551112222", "We are closed")]


def test_missed_call_sms_only_sends_when_enabled(app, client, world, monkeypatch):
    sent = []
    monkeypatch.setattr("twilio_sms._send_sms", lambda ta, to, body: sent.append((to, body)) or {"success": True})
    with app.app_context():
        save_settings(world["co_a"], missed_call_sms_enabled=False, missed_call_sms_body="Missed you")
        log = TwilioCallLog(company_id=world["co_a"], twilio_sid="CAnoanswer", direction="inbound", status="ringing", from_number="+15551112222", to_number="+15550001000")
        db.session.add(log); db.session.commit()
    client.post("/twilio/voice/no-answer", data={"To": "+15550001000", "From": "+15551112222", "CallSid": "CAnoanswer", "DialCallStatus": "no-answer"})
    assert sent == []


def test_inbox_conversations_all_excludes_archived_json_tags(client, app, world):
    with app.app_context():
        visible = TwilioConversation(
            company_id=world["co_a"],
            from_number="+15551000001",
            to_number="+15550001000",
            contact_name="Visible SMS",
            tags=["lead"],
            last_message_at=datetime(2026, 1, 2),
        )
        archived = TwilioConversation(
            company_id=world["co_a"],
            from_number="+15551000002",
            to_number="+15550001000",
            contact_name="Archived SMS",
            tags=["archived"],
            last_message_at=datetime(2026, 1, 3),
        )
        db.session.add_all([visible, archived])
        db.session.commit()
    login(client, world["alice"])

    resp = client.get("/api/inbox/conversations?filter=all")

    assert resp.status_code == 200
    body = resp.get_json()
    names = {c["contact_name"] for c in body["conversations"]}
    assert "Visible SMS" in names
    assert "Archived SMS" not in names


def test_inbox_conversations_all_includes_read_and_unread_non_archived(client, app, world):
    with app.app_context():
        rows = [
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551001001",
                to_number="+15550001000",
                contact_name="Unread Persistent SMS",
                is_read=False,
                tags=[],
                last_message_at=datetime(2026, 1, 5),
            ),
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551001002",
                to_number="+15550001000",
                contact_name="Read Persistent SMS",
                is_read=True,
                tags=[],
                last_message_at=datetime(2026, 1, 4),
            ),
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551001003",
                to_number="+15550001000",
                contact_name="Archived Persistent SMS",
                is_read=False,
                tags=["archived"],
                last_message_at=datetime(2026, 1, 6),
            ),
        ]
        db.session.add_all(rows)
        db.session.commit()
    login(client, world["alice"])

    resp = client.get("/api/inbox/conversations?filter=all")

    assert resp.status_code == 200
    body = resp.get_json()
    names = {c["contact_name"] for c in body["conversations"]}
    assert "Unread Persistent SMS" in names
    assert "Read Persistent SMS" in names
    assert "Archived Persistent SMS" not in names


def test_opening_and_marking_conversation_read_keeps_it_in_all(client, app, world):
    with app.app_context():
        conv = TwilioConversation(
            company_id=world["co_a"],
            from_number="+15551001004",
            to_number="+15550001000",
            contact_name="Read Stable SMS",
            is_read=False,
            tags=["lead"],
            last_message_at=datetime(2026, 1, 7),
        )
        db.session.add(conv)
        db.session.flush()
        db.session.add(TwilioMessage(
            conversation_id=conv.id,
            company_id=world["co_a"],
            direction="inbound",
            from_number="+15551001004",
            to_number="+15550001000",
            body="hello",
            status="received",
            created_at=datetime(2026, 1, 7),
        ))
        db.session.commit()
        conv_id = conv.id
    login(client, world["alice"])

    opened = client.get(f"/api/inbox/conversations/{conv_id}")
    marked = client.patch(f"/api/inbox/conversations/{conv_id}/read", json={"is_read": True})
    again = client.get("/api/inbox/conversations?filter=all")

    assert opened.status_code == 200
    assert marked.status_code == 200
    names = {c["contact_name"] for c in again.get_json()["conversations"]}
    assert "Read Stable SMS" in names
    with app.app_context():
        persisted = db.session.get(TwilioConversation, conv_id)
        assert persisted.is_read is True
        assert persisted.tags == ["lead"]
        assert persisted.company_id == world["co_a"]
        assert persisted.assigned_user_id is None
        assert TwilioMessage.query.filter_by(conversation_id=conv_id).count() == 1


def test_inbox_conversations_archived_filter_uses_python_json_tag_check(client, app, world):
    with app.app_context():
        visible = TwilioConversation(
            company_id=world["co_a"],
            from_number="+15551000003",
            to_number="+15550001000",
            contact_name="Visible SMS 2",
            tags=[],
            last_message_at=datetime(2026, 1, 2),
        )
        archived = TwilioConversation(
            company_id=world["co_a"],
            from_number="+15551000004",
            to_number="+15550001000",
            contact_name="Archived SMS 2",
            tags=["archived", "vip"],
            last_message_at=datetime(2026, 1, 3),
        )
        db.session.add_all([visible, archived])
        db.session.commit()
    login(client, world["alice"])

    resp = client.get("/api/inbox/conversations?filter=archived")

    assert resp.status_code == 200
    body = resp.get_json()
    names = {c["contact_name"] for c in body["conversations"]}
    assert "Archived SMS 2" in names
    assert "Visible SMS 2" not in names


@pytest.mark.parametrize(
    ("filter_name", "expected", "excluded"),
    [
        ("unread", "Unread SMS", "Read SMS"),
        ("mine", "Assigned SMS", "Unassigned SMS"),
        ("opted_out", "Opted Out SMS", "Opted In SMS"),
    ],
)
def test_inbox_conversations_non_archived_filters_still_work(
    client, app, world, filter_name, expected, excluded
):
    with app.app_context():
        rows = [
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551000101",
                to_number="+15550001000",
                contact_name="Unread SMS",
                is_read=False,
                tags=[],
                last_message_at=datetime(2026, 1, 4),
            ),
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551000102",
                to_number="+15550001000",
                contact_name="Read SMS",
                is_read=True,
                tags=[],
                last_message_at=datetime(2026, 1, 3),
            ),
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551000103",
                to_number="+15550001000",
                contact_name="Assigned SMS",
                assigned_user_id=world["alice"],
                tags=[],
                last_message_at=datetime(2026, 1, 2),
            ),
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551000104",
                to_number="+15550001000",
                contact_name="Unassigned SMS",
                tags=[],
                last_message_at=datetime(2026, 1, 1),
            ),
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551000105",
                to_number="+15550001000",
                contact_name="Opted Out SMS",
                is_opted_out=True,
                tags=[],
                last_message_at=datetime(2026, 1, 6),
            ),
            TwilioConversation(
                company_id=world["co_a"],
                from_number="+15551000106",
                to_number="+15550001000",
                contact_name="Opted In SMS",
                is_opted_out=False,
                tags=[],
                last_message_at=datetime(2026, 1, 5),
            ),
        ]
        db.session.add_all(rows)
        db.session.commit()
    login(client, world["alice"])

    resp = client.get(f"/api/inbox/conversations?filter={filter_name}")

    assert resp.status_code == 200
    names = {c["contact_name"] for c in resp.get_json()["conversations"]}
    assert expected in names
    assert excluded not in names


def test_inbox_pwa_fetch_failure_does_not_clear_conversation_state():
    html = open("templates/inbox_pwa/index.html", encoding="utf-8").read()

    assert "Could not refresh conversations. Showing saved conversations." in html
    assert "state.conversations = data.conversations" not in html
    assert "localStorage.clear()" not in html
    assert "sessionStorage.clear()" not in html
    assert "indexedDB.deleteDatabase" not in html
    assert "mergeConversations(state.conversations, data.conversations)" in html


def add_phone_number(company_id, number, **overrides):
    ta = TwilioAccount.query.filter_by(company_id=company_id).one()
    defaults = {
        "company_id": company_id,
        "twilio_account_id": ta.id,
        "phone_number": number,
        "is_active": True,
        "sms_enabled": True,
        "voice_enabled": True,
    }
    defaults.update(overrides)
    pn = TwilioPhoneNumber(**defaults)
    db.session.add(pn)
    db.session.flush()
    return pn


def test_inbound_sms_uses_company_a_number_business_hours_and_auto_reply(app, client, world, monkeypatch):
    sent = []
    monkeypatch.setattr("twilio_sms._send_sms", lambda ta, to, body, **kw: sent.append((ta.company_id, ta.from_phone, to, body)) or {"success": True})
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        save_settings(world["co_b"], business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        add_phone_number(world["co_b"], "+15550002000", after_hours_text="Company B after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.add(AutoReplyRule(company_id=world["co_b"], name="B after hours", trigger_type="after_hours", action="reply", response="fallback B", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110001", "To": "+15550001000", "Body": "hello", "MessageSid": "SMA"})

    assert resp.status_code == 200
    assert sent == [(world["co_a"], "+15550001000", "+15551110001", "Company A after-hours SMS")]


def test_inbound_sms_uses_company_b_number_business_hours_and_auto_reply(app, client, world, monkeypatch):
    sent = []
    monkeypatch.setattr("twilio_sms._send_sms", lambda ta, to, body, **kw: sent.append((ta.company_id, ta.from_phone, to, body)) or {"success": True})
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)})
        save_settings(world["co_b"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        add_phone_number(world["co_b"], "+15550002000", after_hours_text="Company B after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.add(AutoReplyRule(company_id=world["co_b"], name="B after hours", trigger_type="after_hours", action="reply", response="fallback B", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110002", "To": "+1 (555) 000-2000", "Body": "hello", "MessageSid": "SMB"})

    assert resp.status_code == 200
    assert sent == [(world["co_b"], "+15550002000", "+15551110002", "Company B after-hours SMS")]


def test_company_a_after_hours_does_not_affect_company_b_open_number(app, client, world, monkeypatch):
    sent = []
    monkeypatch.setattr("twilio_sms._send_sms", lambda ta, to, body, **kw: sent.append((ta.company_id, body)) or {"success": True})
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        save_settings(world["co_b"], business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        add_phone_number(world["co_b"], "+15550002000", after_hours_text="Company B after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.add(AutoReplyRule(company_id=world["co_b"], name="B after hours", trigger_type="after_hours", action="reply", response="fallback B", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110003", "To": "+15550002000", "Body": "hello", "MessageSid": "SMBOPEN"})

    assert resp.status_code == 200
    assert sent == []


def test_inbound_voice_voicemail_greeting_is_selected_by_to_number_company(app, client, world):
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)}, after_hours_route="voicemail")
        save_settings(world["co_b"], business_hours={str(i): {"is_open": False} for i in range(7)}, after_hours_route="voicemail")
        add_phone_number(world["co_a"], "+15550001000", voicemail_greeting_text="Company A voicemail greeting")
        add_phone_number(world["co_b"], "+15550002000", voicemail_greeting_text="Company B voicemail greeting")
        db.session.commit()

    resp = client.post("/api/twilio/voice/incoming", data={"From": "+15552220000", "To": "+15550002000", "CallSid": "CABvm", "CallStatus": "ringing"})

    assert resp.status_code == 200
    assert b"Company B voicemail greeting" in resp.data
    assert b"Company A voicemail greeting" not in resp.data


def test_inbound_voice_forwarding_number_is_selected_by_to_number_company(app, client, world):
    with app.app_context():
        save_settings(world["co_a"], during_hours_route="forward", forward_number="+15554440001")
        save_settings(world["co_b"], during_hours_route="forward", forward_number="+15554440002")
        add_phone_number(world["co_a"], "+15550001000", call_forward_to="+15553330001")
        add_phone_number(world["co_b"], "+15550002000", call_forward_to="+15553330002")
        db.session.commit()

    resp = client.post("/api/twilio/voice/incoming", data={"From": "+15552220001", "To": "+15550002000", "CallSid": "CABfwd", "CallStatus": "ringing"})

    assert resp.status_code == 200
    assert b"+15553330002" in resp.data
    assert b"+15553330001" not in resp.data
    assert b"+15554440002" not in resp.data


def test_inbound_sms_conversation_is_assigned_to_to_number_company(app, client, world, monkeypatch):
    monkeypatch.setattr("twilio_sms._send_sms", lambda *args, **kwargs: {"success": True})
    with app.app_context():
        add_phone_number(world["co_a"], "+15550001000")
        add_phone_number(world["co_b"], "+15550002000")
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551119999", "To": "+15550002000", "Body": "route me", "MessageSid": "SMROUTE"})

    assert resp.status_code == 200
    with app.app_context():
        assert TwilioConversation.query.filter_by(company_id=world["co_b"], from_number="+15551119999").one()
        assert TwilioConversation.query.filter_by(company_id=world["co_a"], from_number="+15551119999").first() is None


def test_pwa_palette_persists_server_side_across_sessions(client, app, world):
    login(client, world["alice"])
    resp = client.patch("/api/pwa/preferences", json={"palette_id": "forest"})
    assert resp.status_code == 200
    assert resp.get_json()["preferences"]["palette_id"] == "forest"
    with client.session_transaction() as sess:
        sess.clear()
    login(client, world["alice"])
    loaded = client.get("/api/pwa/preferences")
    assert loaded.status_code == 200
    assert loaded.get_json()["preferences"]["palette_id"] == "forest"
    with app.app_context():
        assert db.session.get(User, world["alice"]).pwa_palette_id == "forest"


def test_pwa_device_registration_heartbeat_settings_and_tenant_isolation(client, app, world):
    with app.app_context():
        pn = TwilioPhoneNumber(company_id=world["co_a"], phone_number="+15550001000", friendly_name="Main A", is_active=True, browser_calling_enabled=False, cell_callback_enabled=False, mobile_data_allowed=False, wifi_only=True)
        db.session.add(pn); db.session.commit(); pn_id = pn.id
    login(client, world["alice"])
    payload = {
        "device_key": "device-a",
        "device_name": "Alice Work iPhone",
        "phone_number_id": pn_id,
        "browser": "Safari",
        "device_type": "phone",
        "push_enabled": True,
        "microphone_permission": "granted",
        "pwa_installed": True,
        "wifi_only": True,
        "cellular_callback_enabled": False,
        "mobile_data_calling_allowed": False,
        "default_calling_method": "browser",
    }
    reg = client.post("/api/pwa/devices/register", json=payload)
    assert reg.status_code == 200
    device_id = reg.get_json()["device"]["id"]
    listed = client.get("/api/pwa/devices").get_json()["devices"]
    assert listed[0]["device_name"] == "Alice Work iPhone"
    assert listed[0]["assigned_phone_number"] == "+15550001000"
    hb = client.post("/api/pwa/devices/heartbeat", json={"device_key": "device-a", "phone_number_id": pn_id, "microphone_permission": "prompt"})
    assert hb.status_code == 200
    patch = client.patch(f"/api/pwa/devices/{device_id}/settings", json={"default_calling_method": "cell_callback", "mobile_data_calling_allowed": True})
    assert patch.status_code == 200
    assert patch.get_json()["device"]["default_calling_method"] == "cell_callback"
    with app.app_context():
        assert PWADevice.query.filter_by(company_id=world["co_a"]).count() == 1
        assert PWADevice.query.filter_by(company_id=world["co_b"]).count() == 0


def test_voicemail_transcription_metadata_and_read_state(client, app, world):
    with app.app_context():
        call = TwilioCallLog(company_id=world["co_a"], twilio_sid="CAvmmeta", direction="inbound", status="voicemail", from_number="+15551110000", to_number="+15550001000", voicemail_url="https://vm", duration=33)
        db.session.add(call); db.session.flush()
        vm = VoiceVoicemailMessage(company_id=world["co_a"], call_log_id=call.id, from_number=call.from_number, to_number=call.to_number, call_sid=call.twilio_sid, recording_sid="REvm", recording_url="https://vm", duration_secs=33)
        db.session.add(vm); db.session.commit(); call_id = call.id
    client.post("/api/twilio/voice/transcription", data={"To": "+15550001000", "CallSid": "CAvmmeta", "RecordingSid": "REvm", "TranscriptionSid": "TRvm", "TranscriptionStatus": "completed", "TranscriptionText": "Please call back"})
    login(client, world["alice"])
    data = client.get("/api/calls/voicemails").get_json()["voicemails"]
    vm_row = next(c for c in data if c["id"] == call_id)
    assert vm_row["voicemail_exists"] is True
    assert vm_row["transcription_text"] == "Please call back"
    assert vm_row["transcription_status"] == "complete"
    read = client.post(f"/api/calls/{call_id}/mark-read")
    assert read.status_code == 200
    assert read.get_json()["call"]["is_read"] is True
    with app.app_context():
        call = db.session.get(TwilioCallLog, call_id)
        vm = VoiceVoicemailMessage.query.filter_by(call_log_id=call_id).one()
        assert call.read_by_user_id == world["alice"]
        assert vm.read_by_user_id == world["alice"]
        assert vm.transcription_text == "Please call back"


def test_pwa_search_matches_contacts_conversations_and_call_logs(client, app, world):
    with app.app_context():
        db.session.add(Contact(company_id=world["co_a"], first_name="Jamie", last_name="Rivera", company="Rivera Spa", email="jamie@rivera.test", phone="+15558880000"))
        db.session.add(TwilioConversation(company_id=world["co_a"], from_number="+15558881111", to_number="+15550001000", contact_name="Recent SMS Guest", last_message_at=datetime.utcnow()))
        db.session.add(TwilioCallLog(company_id=world["co_a"], direction="inbound", status="missed", from_number="+15558882222", to_number="+15550001000", caller_name="Recent Caller"))
        db.session.commit()
    login(client, world["alice"])
    by_company = client.get("/api/inbox/contacts/search?q=Rivera").get_json()["contacts"]
    assert any(r["phone"] == "+15558880000" and r["company"] == "Rivera Spa" for r in by_company)
    by_sms = client.get("/api/inbox/contacts/search?q=Guest").get_json()["contacts"]
    assert any(r["phone"] == "+15558881111" and r["source"] == "conversation" for r in by_sms)
    by_call = client.get("/api/inbox/contacts/search?q=Caller").get_json()["contacts"]
    assert any(r["phone"] == "+15558882222" and r["source"] == "call_log" for r in by_call)


def test_disabled_calling_methods_are_blocked_per_number(client, app, world):
    with app.app_context():
        pn = TwilioPhoneNumber(company_id=world["co_a"], phone_number="+15550001000", friendly_name="Main A", is_active=True, browser_calling_enabled=False, cell_callback_enabled=False, mobile_data_allowed=False, wifi_only=True)
        db.session.add(pn); db.session.commit()
    login(client, world["alice"])
    browser = client.post("/api/inbox/call/dial", json={"to": "+15559990000", "selected_number": "+15550001000", "calling_method": "browser"})
    assert browser.status_code == 403
    assert "Browser/WiFi calling is disabled" in browser.get_json()["error"]
    cell = client.post("/api/inbox/call/dial", json={"to": "+15559990000", "selected_number": "+15550001000", "calling_method": "cell_callback"})
    assert cell.status_code == 403
    assert "Cell callback calling is disabled" in cell.get_json()["error"]
