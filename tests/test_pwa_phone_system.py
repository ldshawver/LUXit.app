import os
from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import (
    CallEvent,
    Company,
    PhoneSettings,
    TwilioAccount,
    TwilioCallLog,
    User,
    UserCompanyAccess,
    VoiceVoicemailMessage,
    TwilioConversation,
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
