import os
from datetime import datetime, timezone
from types import SimpleNamespace

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
    PhoneNumberUserPermission,
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
        sess.clear()
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


def disable_identity_collection(monkeypatch):
    # These routing tests predate contact identity collection and are not
    # intended to assert its prompt. Keep that independent workflow from
    # consuming the single auto-reply slot under test.
    monkeypatch.setattr("services.contact_identity.apply_cached_google_match", lambda contact: "unmatched")
    monkeypatch.setattr("services.contact_identity.process_identity_message", lambda *args, **kwargs: {"reply": None})
    monkeypatch.setattr("services.contact_identity.should_request_identity", lambda contact: False)


def capture_conversation_sms(monkeypatch, sent, formatter):
    """Capture the canonical conversation sender without calling Twilio."""
    disable_identity_collection(monkeypatch)

    def fake_send(conversation_id, body, **kwargs):
        conv = db.session.get(TwilioConversation, conversation_id)
        sent.append(formatter(conv, body, kwargs))
        return {"success": True, "sid": "SMtest"}

    monkeypatch.setattr("twilio_sms.sendConversationSms", fake_send)


def stub_conversation_sms(monkeypatch):
    capture_conversation_sms(monkeypatch, [], lambda conv, body, kwargs: None)


def stub_twilio_sms_client(monkeypatch):
    """Prevent unit tests from ever reaching the Twilio network."""
    messages = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(sid="SMtest", status="queued"))
    monkeypatch.setattr("twilio_sms._build_client", lambda account: SimpleNamespace(messages=messages))


def track_awareness_calls(monkeypatch):
    """Record every call to the inbound-awareness side effects (Tuya, SSE,
    Web Push) so tests can assert they fire exactly once per inbound message,
    independent of outbound auto-reply delivery outcome or webhook retries.

    Wraps (rather than replaces) the real functions so the actual
    Notification row still gets created -- callers can assert on both the
    call count and the persisted Notification state.
    """
    import inbox_pwa
    import services.tuya_notification as tuya_notification

    calls = {"push": [], "sse": [], "tuya": []}
    real_push = inbox_pwa._fire_push_notification
    real_sse = inbox_pwa._push_sse_event
    real_tuya = tuya_notification.accept_inbound

    def fake_push(*a, **k):
        calls["push"].append((a, k))
        return real_push(*a, **k)

    def fake_sse(*a, **k):
        calls["sse"].append((a, k))
        return real_sse(*a, **k)

    def fake_tuya(*a, **k):
        calls["tuya"].append((a, k))
        return real_tuya(*a, **k)

    monkeypatch.setattr("inbox_pwa._fire_push_notification", fake_push)
    monkeypatch.setattr("inbox_pwa._push_sse_event", fake_sse)
    monkeypatch.setattr("services.tuya_notification.accept_inbound", fake_tuya)
    return calls


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
    token_body = token_resp.get_json()
    identity = token_body["identity"]
    assert identity.startswith(f"luxit_c{world['co_a']}_")
    assert identity != f"luxit_company_{world['co_a']}_pwa"
    assert token_body["calling_number"] == "+15550001000"
    assert token_body["permitted_numbers"] == ["+15550001000"]






def test_voice_client_error_log_endpoint_requires_auth_and_returns_json(client, world):
    resp = client.post("/api/phone/voice-client-error", json={"code": "SDK_MISSING", "message": "Calling SDK failed to load"})
    assert resp.status_code == 401

    login(client, world["alice"])
    resp = client.post("/api/phone/voice-client-error", json={"code": "SDK_MISSING", "message": "Calling SDK failed to load"})

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}


def test_voice_token_requires_call_permission_for_explicit_number_grant(client, app, world):
    with app.app_context():
        pn = TwilioPhoneNumber(company_id=world["co_a"], phone_number="+15550001111", voice_enabled=True, is_active=True)
        db.session.add(pn)
        db.session.flush()
        db.session.add(PhoneNumberUserPermission(
            company_id=world["co_a"],
            phone_number_id=pn.id,
            user_id=world["alice"],
            can_access_pwa=True,
            can_call=False,
        ))
        db.session.commit()
    login(client, world["alice"])

    token_resp = client.get("/api/phone/voice-token")

    assert token_resp.status_code == 403
    body = token_resp.get_json()
    assert body["code"] == "NO_ASSIGNED_NUMBER"
    assert body["error"] == "No calling number assigned"


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
    capture_conversation_sms(monkeypatch, sent, lambda conv, body, kw: (conv.from_number, body))
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
    capture_conversation_sms(monkeypatch, sent, lambda conv, body, kw: (conv.from_number, body))
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
    capture_conversation_sms(monkeypatch, sent, lambda conv, body, kw: (conv.company_id, conv.to_number, conv.from_number, body))
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
    assert sent == [(world["co_a"], "+15550001000", "+15551110001", "fallback A")]


def test_inbound_sms_uses_company_b_number_business_hours_and_auto_reply(app, client, world, monkeypatch):
    sent = []
    capture_conversation_sms(monkeypatch, sent, lambda conv, body, kw: (conv.company_id, conv.to_number, conv.from_number, body))
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
    assert sent == [(world["co_b"], "+15550002000", "+15551110002", "fallback B")]


def test_company_a_after_hours_does_not_affect_company_b_open_number(app, client, world, monkeypatch):
    sent = []
    capture_conversation_sms(monkeypatch, sent, lambda conv, body, kw: (conv.company_id, body))
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


def test_inbound_sms_notification_awareness_does_not_depend_on_auto_reply_delivery(app, client, world, monkeypatch):
    """Regression for the notification-delivery coupling found during the
    LUXit Development acceptance run: an inbound SMS that is durably
    persisted and linked to its conversation/contact must still make the
    agent aware of it (unread state + a communications Notification row)
    even when the *optional* outbound auto-reply fails to send.

    Fixed by moving the Tuya/PostHog/push-notification block ahead of
    _deliver_and_finalize_inbound() in twilio_sms.inbound_sms, so it runs
    unconditionally right after the inbound message is persisted and
    committed, independent of whether the auto-reply delivery that follows
    succeeds, fails, or needs a retry.
    """
    from models import Notification

    class _FailingTwilioError(Exception):
        status = 400
        code = "21211"

    def _failing_client(_account):
        def _raise(**kwargs):
            raise _FailingTwilioError("Invalid 'To' Phone Number")
        return SimpleNamespace(messages=SimpleNamespace(create=_raise))

    monkeypatch.setattr("twilio_sms._build_client", _failing_client)
    disable_identity_collection(monkeypatch)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110099", "To": "+15550001000", "Body": "hello", "MessageSid": "SMCOUPLING"})
    assert resp.status_code == 200

    with app.app_context():
        msg = TwilioMessage.query.filter_by(twilio_sid="SMCOUPLING").first()
        assert msg is not None, "inbound message must persist even if the auto-reply fails to send"
        assert msg.processing_status == "failed_terminal"
        conv = db.session.get(TwilioConversation, msg.conversation_id)
        assert conv.is_read is False

        note = Notification.query.filter_by(company_id=world["co_a"], event_type="incoming_sms").first()
        assert note is not None, (
            "inbound SMS notification/push awareness must not depend on "
            "whether the outbound auto-reply succeeded (notification-delivery coupling bug)"
        )


def test_inbound_sms_awareness_fires_once_on_successful_auto_reply(app, client, world, monkeypatch):
    """A: persisted inbound + successful auto-reply -> awareness exactly once."""
    from models import Notification

    calls = track_awareness_calls(monkeypatch)
    stub_twilio_sms_client(monkeypatch)
    disable_identity_collection(monkeypatch)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110100", "To": "+15550001000", "Body": "hello", "MessageSid": "SMSUCCESS1"})
    assert resp.status_code == 200
    assert len(calls["push"]) == 1
    assert len(calls["sse"]) == 1

    with app.app_context():
        msg = TwilioMessage.query.filter_by(twilio_sid="SMSUCCESS1").first()
        assert msg.processing_status == "completed"
        assert Notification.query.filter_by(company_id=world["co_a"], event_type="incoming_sms").count() == 1


def test_inbound_sms_awareness_fires_exactly_once_on_failed_auto_reply(app, client, world, monkeypatch):
    """B: persisted inbound + failed (terminal) auto-reply -> awareness still exactly once."""
    from models import Notification

    calls = track_awareness_calls(monkeypatch)
    disable_identity_collection(monkeypatch)

    class _FailingTwilioError(Exception):
        status = 400
        code = "21211"

    def _failing_client(_account):
        def _raise(**kwargs):
            raise _FailingTwilioError("Invalid 'To' Phone Number")
        return SimpleNamespace(messages=SimpleNamespace(create=_raise))

    monkeypatch.setattr("twilio_sms._build_client", _failing_client)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110101", "To": "+15550001000", "Body": "hello", "MessageSid": "SMFAIL1"})
    assert resp.status_code == 200
    assert len(calls["push"]) == 1
    assert len(calls["sse"]) == 1

    with app.app_context():
        msg = TwilioMessage.query.filter_by(twilio_sid="SMFAIL1").first()
        assert msg.processing_status == "failed_terminal"
        assert Notification.query.filter_by(company_id=world["co_a"], event_type="incoming_sms").count() == 1


def test_inbound_sms_awareness_fires_once_when_outbound_is_retryable(app, client, world, monkeypatch):
    """C: persisted inbound + retryable outbound condition -> awareness exactly once,
    and the response is 500 so Twilio knows to retry the *delivery*, not the awareness."""
    from models import Notification

    calls = track_awareness_calls(monkeypatch)
    disable_identity_collection(monkeypatch)

    class _RetryableTwilioError(Exception):
        status = 500
        code = "20500"

    def _retryable_client(_account):
        def _raise(**kwargs):
            raise _RetryableTwilioError("Twilio internal error")
        return SimpleNamespace(messages=SimpleNamespace(create=_raise))

    monkeypatch.setattr("twilio_sms._build_client", _retryable_client)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110102", "To": "+15550001000", "Body": "hello", "MessageSid": "SMRETRY1"})
    assert resp.status_code == 500
    assert len(calls["push"]) == 1
    assert len(calls["sse"]) == 1

    with app.app_context():
        msg = TwilioMessage.query.filter_by(twilio_sid="SMRETRY1").first()
        assert msg.processing_status == "failed_retryable"
        assert Notification.query.filter_by(company_id=world["co_a"], event_type="incoming_sms").count() == 1


def test_inbound_sms_retry_of_same_message_sid_does_not_duplicate_awareness(app, client, world, monkeypatch):
    """D: a Twilio webhook retry of the same MessageSid (after a retryable
    outbound failure that resolves on the retry) must not fire the
    Notification/Web-Push/SSE awareness a second time."""
    from models import Notification

    calls = track_awareness_calls(monkeypatch)
    disable_identity_collection(monkeypatch)

    attempt = {"n": 0}

    class _RetryableTwilioError(Exception):
        status = 500
        code = "20500"

    def _client_factory(_account):
        attempt["n"] += 1
        if attempt["n"] == 1:
            def _raise(**kwargs):
                raise _RetryableTwilioError("Twilio internal error")
            return SimpleNamespace(messages=SimpleNamespace(create=_raise))
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(sid="SMtest", status="queued")))

    monkeypatch.setattr("twilio_sms._build_client", _client_factory)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.commit()

    first = client.post("/twilio/sms/inbound", data={"From": "+15551110103", "To": "+15550001000", "Body": "hello", "MessageSid": "SMRETRY2"})
    assert first.status_code == 500
    assert len(calls["push"]) == 1
    assert len(calls["sse"]) == 1

    # Twilio retries the identical webhook (same MessageSid) after the 500.
    second = client.post("/twilio/sms/inbound", data={"From": "+15551110103", "To": "+15550001000", "Body": "hello", "MessageSid": "SMRETRY2"})
    assert second.status_code == 200

    assert len(calls["push"]) == 1, "webhook retry must not re-fire Web Push"
    assert len(calls["sse"]) == 1, "webhook retry must not re-fire SSE"

    with app.app_context():
        msg = TwilioMessage.query.filter_by(twilio_sid="SMRETRY2").first()
        assert msg.processing_status == "completed"
        assert Notification.query.filter_by(company_id=world["co_a"], event_type="incoming_sms").count() == 1, (
            "webhook retry must not create a duplicate Notification row"
        )


def test_stop_start_help_unaffected_by_awareness_reordering(app, client, world, monkeypatch):
    """F: STOP/START/HELP keep their synchronous-TwiML precedence and
    behavior, untouched by the awareness-block reorder."""
    disable_identity_collection(monkeypatch)
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000")
        db.session.commit()

    stop_resp = client.post("/twilio/sms/inbound", data={"From": "+15551110104", "To": "+15550001000", "Body": "STOP", "MessageSid": "SMSTOPX"})
    assert stop_resp.status_code == 200
    assert b"unsubscribed" in stop_resp.data.lower()

    start_resp = client.post("/twilio/sms/inbound", data={"From": "+15551110104", "To": "+15550001000", "Body": "START", "MessageSid": "SMSTARTX"})
    assert start_resp.status_code == 200
    assert b"subscribed" in start_resp.data.lower()

    help_resp = client.post("/twilio/sms/inbound", data={"From": "+15551110104", "To": "+15550001000", "Body": "HELP", "MessageSid": "SMHELPX"})
    assert help_resp.status_code == 200
    assert b"stop to opt out" in help_resp.data.lower()

    with app.app_context():
        conv = TwilioConversation.query.filter_by(company_id=world["co_a"], from_number="+15551110104").first()
        assert conv.is_opted_out is False  # STOP then START nets to opted back in


def test_badge_count_still_correct_after_awareness_reordering(app, client, world, monkeypatch):
    """G: the PWA badge fix and the notification-delivery-coupling fix
    compose correctly -- a failed auto-reply now produces exactly one
    incoming_sms Notification row (previously zero), and the badge still
    counts that unread SMS exactly once, not twice."""
    disable_identity_collection(monkeypatch)

    class _FailingTwilioError(Exception):
        status = 400
        code = "21211"

    def _failing_client(_account):
        def _raise(**kwargs):
            raise _FailingTwilioError("Invalid 'To' Phone Number")
        return SimpleNamespace(messages=SimpleNamespace(create=_raise))

    monkeypatch.setattr("twilio_sms._build_client", _failing_client)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110105", "To": "+15550001000", "Body": "hello", "MessageSid": "SMBADGE1"})
    assert resp.status_code == 200

    login(client, world["alice"])
    data = client.get("/api/pwa/badge-count").get_json()
    assert data["smsUnread"] == 1
    assert data["notifications"] == 0  # incoming_sms rows are excluded from this counter
    assert data["count"] == 1  # not 2 -- proves the two fixes compose without double-counting


def test_disabled_twilio_mode_blocks_send_but_inbound_processing_still_works(app, client, world, monkeypatch):
    """Twilio safety gate, A+B+C: with LUXIT_TWILIO_MODE=disabled, the
    outbound auto-reply send is blocked at the centralized gate (A), the
    inbound webhook still returns successfully and persists the message
    (B), and the notification-delivery-coupling fix still makes the agent
    aware of the message even though the send was blocked (C)."""
    from models import Notification

    monkeypatch.setenv("LUXIT_TWILIO_MODE", "disabled")
    calls = track_awareness_calls(monkeypatch)
    disable_identity_collection(monkeypatch)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000", after_hours_text="Company A after-hours SMS")
        db.session.add(AutoReplyRule(company_id=world["co_a"], name="A after hours", trigger_type="after_hours", action="reply", response="fallback A", is_active=True))
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551110106", "To": "+15550001000", "Body": "hello", "MessageSid": "SMGATE1"})
    assert resp.status_code == 200  # the blocked send classifies as terminal (400-499), not retryable

    assert len(calls["push"]) == 1
    assert len(calls["sse"]) == 1

    with app.app_context():
        msg = TwilioMessage.query.filter_by(twilio_sid="SMGATE1").first()
        assert msg is not None, "inbound message must persist while sends are disabled"
        assert msg.processing_status == "failed_terminal"
        conv = db.session.get(TwilioConversation, msg.conversation_id)
        assert conv.is_read is False

        outbound = TwilioMessage.query.filter_by(conversation_id=conv.id, direction="outbound").all()
        assert outbound == [], "no outbound TwilioMessage record should exist -- the send never reached Twilio"

        assert Notification.query.filter_by(company_id=world["co_a"], event_type="incoming_sms").count() == 1


def test_stop_start_help_unaffected_by_disabled_twilio_mode(app, client, world, monkeypatch):
    """Twilio safety gate, F: STOP/START/HELP use a synchronous TwiML reply
    that never touches the Twilio REST client, so they must behave
    identically whether LUXIT_TWILIO_MODE is disabled or live."""
    monkeypatch.setenv("LUXIT_TWILIO_MODE", "disabled")
    disable_identity_collection(monkeypatch)
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)})
        add_phone_number(world["co_a"], "+15550001000")
        db.session.commit()

    stop_resp = client.post("/twilio/sms/inbound", data={"From": "+15551110107", "To": "+15550001000", "Body": "STOP", "MessageSid": "SMSTOPGATE"})
    assert stop_resp.status_code == 200
    assert b"unsubscribed" in stop_resp.data.lower()

    start_resp = client.post("/twilio/sms/inbound", data={"From": "+15551110107", "To": "+15550001000", "Body": "START", "MessageSid": "SMSTARTGATE"})
    assert start_resp.status_code == 200
    assert b"subscribed" in start_resp.data.lower()

    help_resp = client.post("/twilio/sms/inbound", data={"From": "+15551110107", "To": "+15550001000", "Body": "HELP", "MessageSid": "SMHELPGATE"})
    assert help_resp.status_code == 200
    assert b"stop to opt out" in help_resp.data.lower()


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
    stub_conversation_sms(monkeypatch)
    with app.app_context():
        add_phone_number(world["co_a"], "+15550001000")
        add_phone_number(world["co_b"], "+15550002000")
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={"From": "+15551119999", "To": "+15550002000", "Body": "route me", "MessageSid": "SMROUTE"})

    assert resp.status_code == 200
    with app.app_context():
        assert TwilioConversation.query.filter_by(company_id=world["co_b"], from_number="+15551119999").one()
        assert TwilioConversation.query.filter_by(company_id=world["co_a"], from_number="+15551119999").first() is None



def test_inbound_sms_to_formatted_number_variants_routes_by_to_number(app, client, world, monkeypatch):
    stub_conversation_sms(monkeypatch)
    with app.app_context():
        pn = add_phone_number(world["co_a"], "(916) 598-9519", friendly_name="Sacramento")
        db.session.commit()

    resp = client.post("/twilio/sms", data={
        "From": "+19166066620",
        "To": "+19165989519",
        "Body": "production repro",
        "MessageSid": "SMFORMAT",
        "MessagingServiceSid": "MGa8routebyto",
    })

    assert resp.status_code == 200
    with app.app_context():
        conv = TwilioConversation.query.filter_by(company_id=world["co_a"], from_number="+19166066620").one()
        assert conv.to_number == "+19165989519"
        assert conv.phone_number_id == pn.id
        msg = TwilioMessage.query.filter_by(twilio_sid="SMFORMAT").one()
        assert msg.conversation_id == conv.id
        assert msg.direction == "inbound"


def test_messaging_service_inbound_routes_by_to_number_before_service_sid(app, client, world, monkeypatch):
    stub_conversation_sms(monkeypatch)
    with app.app_context():
        account_a = TwilioAccount.query.filter_by(company_id=world["co_a"]).one()
        account_b = TwilioAccount.query.filter_by(company_id=world["co_b"]).one()
        account_a.messaging_service_sid = "MGa8shared"
        account_b.messaging_service_sid = "MGa8shared"
        pn_b = add_phone_number(world["co_b"], "9165989519", friendly_name="Tenant B Sac")
        db.session.commit()

    resp = client.post("/twilio/sms/inbound", data={
        "From": "+19166066620",
        "To": "+19165989519",
        "Body": "route by to",
        "MessageSid": "SMTOSERVICE",
        "MessagingServiceSid": "MGa8shared",
    })

    assert resp.status_code == 200
    with app.app_context():
        conv = TwilioConversation.query.filter_by(from_number="+19166066620").one()
        assert conv.company_id == world["co_b"]
        assert conv.phone_number_id == pn_b.id
        assert TwilioMessage.query.filter_by(twilio_sid="SMTOSERVICE", company_id=world["co_b"]).one()


def test_inbound_sms_visibility_respects_number_permissions(app, client, world, monkeypatch):
    stub_conversation_sms(monkeypatch)
    with app.app_context():
        pn_allowed = add_phone_number(world["co_a"], "+19165989519", friendly_name="Allowed")
        pn_denied = add_phone_number(world["co_a"], "+19165550000", friendly_name="Denied")
        allowed_user = User(username="allowed_sms", email="allowed_sms@test.com", password_hash="x", default_company_id=world["co_a"])
        denied_user = User(username="denied_sms", email="denied_sms@test.com", password_hash="x", default_company_id=world["co_a"])
        db.session.add_all([allowed_user, denied_user])
        db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=allowed_user.id, company_id=world["co_a"], role=UserCompanyAccess.ROLE_STAFF, is_default=True, can_access_mobile_inbox=True),
            UserCompanyAccess(user_id=denied_user.id, company_id=world["co_a"], role=UserCompanyAccess.ROLE_STAFF, is_default=True, can_access_mobile_inbox=True),
            PhoneNumberUserPermission(company_id=world["co_a"], phone_number_id=pn_allowed.id, user_id=allowed_user.id, can_access_pwa=True, can_view_sms=True),
            PhoneNumberUserPermission(company_id=world["co_a"], phone_number_id=pn_denied.id, user_id=denied_user.id, can_access_pwa=True, can_view_sms=True),
        ])
        db.session.commit()
        allowed_id = allowed_user.id
        denied_id = denied_user.id

    resp = client.post("/twilio/sms/inbound", data={"From": "+19166066620", "To": "+19165989519", "Body": "visible", "MessageSid": "SMVISIBLE"})
    assert resp.status_code == 200

    with app.test_client() as allowed_client:
        login(allowed_client, allowed_id)
        allowed_resp = allowed_client.get("/api/inbox/conversations")
    assert allowed_resp.status_code == 200
    assert any(c["from_number"] == "+19166066620" for c in allowed_resp.get_json()["conversations"])

    with app.app_context():
        from services.comms_permissions import filter_conversations_for_user
        denied_user = db.session.get(User, denied_id)
        hidden = filter_conversations_for_user(
            TwilioConversation.query.filter_by(company_id=world["co_a"]), denied_user, world["co_a"]
        ).filter_by(from_number="+19166066620").first()
        assert hidden is None

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


def _create_staff_mobile_inbox_user(app, company_id, *, with_number=True):
    from werkzeug.security import generate_password_hash
    with app.app_context():
        user = User(
            username=f"staff_mobile_{with_number}",
            email=f"staff_mobile_{str(with_number).lower()}@test.com",
            password_hash=generate_password_hash("secret"),
            default_company_id=company_id,
        )
        db.session.add(user)
        db.session.flush()
        acc = UserCompanyAccess(
            user_id=user.id,
            company_id=company_id,
            role=UserCompanyAccess.ROLE_STAFF,
            is_default=True,
            can_access_mobile_inbox=True,
            pwa_access_enabled=True,
            can_access_full_app=False,
            comms_hub_enabled=False,
            manage_users_enabled=False,
        )
        db.session.add(acc)
        pn = TwilioPhoneNumber(company_id=company_id, phone_number="+15550007777", sms_enabled=True, voice_enabled=True, is_active=True)
        db.session.add(pn)
        db.session.flush()
        if with_number:
            db.session.add(PhoneNumberUserPermission(
                company_id=company_id,
                phone_number_id=pn.id,
                user_id=user.id,
                can_access_pwa=True,
                can_view_sms=True,
                can_send_sms=True,
                can_view_calls=True,
                can_call=True,
            ))
        conv = TwilioConversation(
            company_id=company_id,
            from_number="+15551110000",
            to_number="+15550007777",
            contact_name="Assigned Customer",
            last_message_at=datetime(2026, 1, 8),
        )
        db.session.add(conv)
        db.session.flush()
        db.session.add(TwilioMessage(
            conversation_id=conv.id,
            company_id=company_id,
            direction="inbound",
            from_number="+15551110000",
            to_number="+15550007777",
            body="assigned hello",
            status="received",
            created_at=datetime(2026, 1, 8),
        ))
        db.session.commit()
        return user.id, conv.id


def test_staff_mobile_inbox_user_can_log_in_and_open_inbox(client, app, world):
    user_id, _ = _create_staff_mobile_inbox_user(app, world["co_a"])

    resp = client.post("/auth/login", data={"email": "staff_mobile_true@test.com", "password": "secret"}, follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/inbox")
    inbox = client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126 Mobile"})
    assert inbox.status_code == 200


def test_staff_mobile_inbox_user_can_subscribe_to_push(client, app, world):
    user_id, _ = _create_staff_mobile_inbox_user(app, world["co_a"])
    login(client, user_id)

    resp = client.post("/api/push/subscribe", json={"endpoint": "https://push.test/1", "keys": {"p256dh": "p", "auth": "a"}})

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_staff_mobile_inbox_user_can_read_assigned_number_conversations(client, app, world):
    user_id, conv_id = _create_staff_mobile_inbox_user(app, world["co_a"])
    login(client, user_id)

    listed = client.get("/api/inbox/conversations").get_json()
    opened = client.get(f"/api/inbox/conversations/{conv_id}")
    messages = client.get("/api/inbox/messages").get_json()

    assert listed["success"] is True
    assert [c["contact_name"] for c in listed["conversations"]] == ["Assigned Customer"]
    assert opened.status_code == 200
    assert opened.get_json()["messages"][0]["body"] == "assigned hello"
    assert messages["messages"][0]["body"] == "assigned hello"


def test_staff_mobile_inbox_user_cannot_access_campaigns_or_manage(client, app, world):
    user_id, _ = _create_staff_mobile_inbox_user(app, world["co_a"])
    login(client, user_id)

    assert client.get("/campaigns").status_code == 403
    manage_resp = client.get("/user/manage-users")
    assert manage_resp.status_code in (302, 403)
    assert manage_resp.status_code != 200


def test_staff_mobile_inbox_no_number_returns_clear_empty_state(client, app, world):
    user_id, _ = _create_staff_mobile_inbox_user(app, world["co_a"], with_number=False)
    login(client, user_id)

    resp = client.get("/api/inbox/conversations")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == "NO_ASSIGNED_NUMBER"
    assert body["empty_title"] == "No phone number assigned"
    assert body["conversations"] == []


def test_staff_mobile_inbox_direct_link_does_not_loop_on_desktop_safari(client, app, world):
    user_id, _ = _create_staff_mobile_inbox_user(app, world["co_a"])
    login(client, user_id)

    resp = client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 Safari/605.1.15"}, follow_redirects=False)

    assert resp.status_code == 200
    assert b"LUXit" in resp.data or b"Inbox" in resp.data


def test_staff_mobile_inbox_google_contacts_callback_returns_to_pwa(client, app, world, monkeypatch):
    user_id, _ = _create_staff_mobile_inbox_user(app, world["co_a"])
    login(client, user_id)
    calls = []
    monkeypatch.setattr("services.google_contacts.exchange_code", lambda user_id_arg, code: calls.append((user_id_arg, code)) or {"access_token": "tok"})
    monkeypatch.setattr("services.google_contacts.sync_contacts", lambda user_id_arg, company_id: {"synced": 0, "matched": 0, "error": None})

    resp = client.get("/twilio/google-contacts/callback?code=ok", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/inbox")
    assert calls == [(user_id, "ok")]


def test_staff_mobile_inbox_google_contacts_callback_handles_sync_exception(client, app, world, monkeypatch):
    user_id, _ = _create_staff_mobile_inbox_user(app, world["co_a"])
    login(client, user_id)
    monkeypatch.setattr("services.google_contacts.exchange_code", lambda user_id_arg, code: {"access_token": "tok"})
    def boom(user_id_arg, company_id):
        raise RuntimeError("people api unavailable")
    monkeypatch.setattr("services.google_contacts.sync_contacts", boom)

    resp = client.get("/twilio/google-contacts/callback?code=ok", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/inbox")


def test_number_settings_save_greeting_and_auto_reply_messages(app, client, world):
    login(client, world["alice"])
    with app.app_context():
        pn = add_phone_number(world["co_a"], "+15550003000")
        pn_id = pn.id
        db.session.commit()
    resp = client.put(f"/api/phone/numbers/{pn_id}/settings", json={
        "auto_reply_enabled": True,
        "number_auto_reply_text": "Open hours line reply",
        "after_hours_sms_enabled": True,
        "after_hours_text": "Closed line reply",
        "missed_call_text": "Missed you from this line",
        "voicemail_greeting_text": "Line-specific voicemail greeting",
    })
    assert resp.status_code == 200
    body = resp.get_json()["settings"]
    assert body["number_auto_reply_text"] == "Open hours line reply"
    assert body["after_hours_text"] == "Closed line reply"
    assert body["missed_call_text"] == "Missed you from this line"
    assert body["voicemail_greeting_text"] == "Line-specific voicemail greeting"
    again = client.get(f"/api/phone/numbers/{pn_id}/settings")
    assert again.get_json()["settings"]["voicemail_greeting_text"] == "Line-specific voicemail greeting"


def test_per_number_business_and_after_hours_auto_replies_work_without_rules(app, client, world, monkeypatch):
    sent = []
    capture_conversation_sms(monkeypatch, sent, lambda conv, body, kw: (conv.company_id, conv.to_number, conv.from_number, body, kw.get("is_auto_reply")))
    with app.app_context():
        add_phone_number(
            world["co_a"],
            "+15550003001",
            business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)},
            number_auto_reply_text="Open hours line reply",
            auto_reply_enabled=True,
        )
        add_phone_number(
            world["co_a"],
            "+15550003002",
            business_hours={str(i): {"is_open": False} for i in range(7)},
            after_hours_text="Closed line reply",
            after_hours_sms_enabled=True,
            auto_reply_enabled=True,
        )
        db.session.commit()
    open_resp = client.post("/twilio/sms/inbound", data={"From": "+15551113001", "To": "+15550003001", "Body": "hello", "MessageSid": "SMOPENREPLY"})
    closed_resp = client.post("/twilio/sms/inbound", data={"From": "+15551113002", "To": "+15550003002", "Body": "hello", "MessageSid": "SMCLOSEDREPLY"})
    assert open_resp.status_code == 200
    assert closed_resp.status_code == 200
    assert (world["co_a"], "+15550003001", "+15551113001", "Open hours line reply", True) in sent
    assert (world["co_a"], "+15550003002", "+15551113002", "Closed line reply", True) in sent


def test_cross_midnight_business_hours_two_pm_to_two_am(app, world):
    import twilio_sms
    with app.app_context():
        pn = TwilioPhoneNumber(
            company_id=world["co_a"],
            phone_number="+15550003333",
            timezone="America/New_York",
            business_hours={str(i): {"is_open": True, "open": "14:00", "close": "02:00"} for i in range(7)},
        )
        db.session.add(pn); db.session.commit()
        cases = [
            (datetime(2026, 6, 22, 15, 0), False),  # 11 AM ET
            (datetime(2026, 6, 22, 17, 59), False), # 1:59 PM ET
            (datetime(2026, 6, 22, 18, 0), True),   # 2 PM ET
            (datetime(2026, 6, 23, 3, 0), True),    # 11 PM ET
            (datetime(2026, 6, 23, 5, 59), True),   # 1:59 AM ET
            (datetime(2026, 6, 23, 6, 0), False),   # 2 AM ET
        ]
        for at_utc, expected in cases:
            assert twilio_sms._is_business_hours(world["co_a"], at_time=at_utc.replace(tzinfo=timezone.utc), phone_config=pn) is expected


def test_sms_keyword_engine_delegates_to_tz_aware_business_hours(app, world):
    """Regression test for the after-hours-during-business-hours bug.

    services.sms_keyword_engine used to have its own naive-UTC, no-wraparound
    _is_business_hours() (hardcoded 9-5 Mon-Fri) that disagreed with the
    correct twilio_sms._is_business_hours() used elsewhere, so campaign/
    keyword after-hours replies could fire during real business hours. It now
    delegates to the same tz-aware implementation; this proves that holds for
    the same midnight-crossing 2 PM-2 AM America/New_York schedule used above.
    """
    from services import sms_keyword_engine
    with app.app_context():
        pn = TwilioPhoneNumber(
            company_id=world["co_a"],
            phone_number="+15550003334",
            timezone="America/New_York",
            business_hours={str(i): {"is_open": True, "open": "14:00", "close": "02:00"} for i in range(7)},
        )
        db.session.add(pn); db.session.commit()
        cases = [
            (datetime(2026, 6, 22, 15, 0), False),  # 11 AM ET -- must NOT look like business hours
            (datetime(2026, 6, 22, 18, 0), True),   # 2 PM ET -- opening minute
            (datetime(2026, 6, 23, 3, 0), True),    # 11 PM ET
            (datetime(2026, 6, 23, 5, 59), True),   # 1:59 AM ET -- still open (crossed midnight)
            (datetime(2026, 6, 23, 6, 0), False),   # 2 AM ET -- closing minute
        ]
        for at_utc, expected in cases:
            assert sms_keyword_engine._is_business_hours(
                world["co_a"], phone_config=pn, at=at_utc.replace(tzinfo=timezone.utc)
            ) is expected


def test_number_after_hours_copy_alias_persists_and_reloads(client, app, world):
    login(client, world["alice"])
    required = "Thanks for reaching out. Our business hours are daily from 2 PM to 2 AM. We’ll respond as soon as we’re back online."
    with app.app_context():
        pn = TwilioPhoneNumber(company_id=world["co_a"], phone_number="+15550004444", sms_enabled=True, is_active=True)
        db.session.add(pn); db.session.commit(); number_id = pn.id
    resp = client.put(f"/api/phone/numbers/{number_id}/settings", json={"after_hours_sms_body": required, "after_hours_sms_enabled": True, "after_hours_cooldown_minutes": 30})
    assert resp.status_code == 200
    body = client.get(f"/api/phone/numbers/{number_id}/settings").get_json()["settings"]
    assert body["after_hours_text"] == required
    assert body["after_hours_sms_body"] == required
    assert body["after_hours_cooldown_minutes"] == 30


def test_after_hours_auto_reply_fallback_creates_outbound_and_marks_inbound(app, client, world, monkeypatch):
    import twilio_sms
    required = "Thanks for reaching out. Our business hours are daily from 2 PM to 2 AM. We’ll respond as soon as we’re back online."
    with app.app_context():
        ta = TwilioAccount.query.filter_by(company_id=world["co_a"]).one()
        ta._account_sid = "ACtest"; ta._auth_token = "auth"; ta.automation_enabled = True; ta.after_hours_sms_enabled = True
        pn = TwilioPhoneNumber(company_id=world["co_a"], twilio_account_id=ta.id, phone_number="+15550005555", sms_enabled=True, is_active=True, auto_reply_enabled=True, after_hours_sms_enabled=True, after_hours_text=required, business_hours={str(i): {"is_open": True, "open": "14:00", "close": "02:00"} for i in range(7)}, timezone="America/New_York")
        db.session.add(pn); db.session.commit()
    monkeypatch.setattr(twilio_sms, "_validate_twilio_signature", lambda *a, **k: True)
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **k: False)
    stub_twilio_sms_client(monkeypatch)
    disable_identity_collection(monkeypatch)
    resp = client.post("/twilio/sms/inbound", data={"From": "+15551112222", "To": "+15550005555", "Body": "hello", "MessageSid": "SMINAH1"})
    assert resp.status_code == 200
    with app.app_context():
        conv = TwilioConversation.query.filter_by(company_id=world["co_a"], from_number="+15551112222").one()
        inbound = TwilioMessage.query.filter_by(conversation_id=conv.id, direction="inbound").one()
        outbound = TwilioMessage.query.filter_by(conversation_id=conv.id, direction="outbound", is_auto_reply=True).one()
        assert inbound.auto_responded is True
        assert outbound.body == required
        assert outbound.to_number == "+15551112222"


def test_stop_opt_out_does_not_receive_after_hours_auto_reply(app, client, world, monkeypatch):
    import twilio_sms
    with app.app_context():
        ta = TwilioAccount.query.filter_by(company_id=world["co_a"]).one()
        ta._account_sid = "ACtest"; ta._auth_token = "auth"; ta.automation_enabled = True; ta.after_hours_sms_enabled = True
        db.session.add(TwilioPhoneNumber(company_id=world["co_a"], twilio_account_id=ta.id, phone_number="+15550006666", sms_enabled=True, is_active=True, after_hours_sms_enabled=True, after_hours_text="closed")); db.session.commit()
    monkeypatch.setattr(twilio_sms, "_validate_twilio_signature", lambda *a, **k: True)
    resp = client.post("/twilio/sms/inbound", data={"From": "+15551113333", "To": "+15550006666", "Body": "STOP", "MessageSid": "SMSTOP1"})
    assert resp.status_code == 200
    with app.app_context():
        conv = TwilioConversation.query.filter_by(company_id=world["co_a"], from_number="+15551113333").one()
        assert conv.is_opted_out is True
        assert TwilioMessage.query.filter_by(conversation_id=conv.id, direction="outbound", is_auto_reply=True).count() == 0


def test_pwa_device_approval_gate_pending_approved_revoked(client, app, world):
    with app.app_context():
        company = db.session.get(Company, world["co_a"])
        company.require_approved_pwa_devices = True
        conv = TwilioConversation(company_id=world["co_a"], from_number="+15551234567", to_number="+15550001000", is_read=True, last_message_at=datetime.utcnow())
        db.session.add(conv)
        db.session.commit()
    login(client, world["alice"])
    reg = client.post("/api/pwa/devices/register", json={"device_key": "approval-device", "device_name": "Approval iPhone", "device_type": "iPhone"})
    assert reg.status_code == 200
    assert reg.get_json()["device"]["approved_status"] == "pending"

    pending = client.get("/api/inbox/conversations", headers={"X-PWA-Device-Key": "approval-device"})
    assert pending.status_code == 403
    assert pending.get_json()["code"] == "PWA_DEVICE_PENDING_APPROVAL"

    with app.app_context():
        device = PWADevice.query.filter_by(company_id=world["co_a"], device_key="approval-device").one()
        device.approved_status = "approved"
        db.session.commit()
    approved = client.get("/api/inbox/conversations", headers={"X-PWA-Device-Key": "approval-device"})
    assert approved.status_code == 200

    with app.app_context():
        device = PWADevice.query.filter_by(company_id=world["co_a"], device_key="approval-device").one()
        device.approved_status = "revoked"
        db.session.commit()
    revoked = client.get("/api/inbox/conversations", headers={"X-PWA-Device-Key": "approval-device"})
    assert revoked.status_code == 403
    assert revoked.get_json()["code"] == "PWA_DEVICE_REVOKED"


def test_pwa_device_approval_disabled_allows_registered_device(client, app, world):
    login(client, world["alice"])
    reg = client.post("/api/pwa/devices/register", json={"device_key": "auto-approved-device", "device_type": "Android"})
    assert reg.status_code == 200
    assert reg.get_json()["device"]["approved_status"] == "approved"
    ok = client.get("/api/inbox/conversations", headers={"X-PWA-Device-Key": "auto-approved-device"})
    assert ok.status_code == 200


def test_comms_device_controls_are_tenant_scoped_and_disable_subscriptions(client, app, world):
    from models import PushSubscription
    with app.app_context():
        db.session.get(User, world["alice"]).is_admin = True
        own = PWADevice(company_id=world["co_a"], user_id=world["alice"], device_key="own-device", device_name="Own", approved_status="approved", lifecycle_status="active", push_enabled=True)
        foreign = PWADevice(company_id=world["co_b"], user_id=world["bob"], device_key="foreign-device", device_name="Foreign", approved_status="approved", lifecycle_status="active", push_enabled=True)
        db.session.add_all([own, foreign]); db.session.flush()
        db.session.add(PushSubscription(company_id=world["co_a"], user_id=world["alice"], device_key="own-device", endpoint="https://push.test/own", p256dh="p", auth_key="a", is_active=True))
        db.session.commit(); own_id, foreign_id = own.id, foreign.id
    login(client, world["alice"])
    assert client.post(f"/twilio/comms/devices/{foreign_id}", data={"action": "disable"}).status_code == 404
    disabled = client.post(f"/twilio/comms/devices/{own_id}", data={"action": "disable"})
    assert disabled.status_code == 302
    with app.app_context():
        own = db.session.get(PWADevice, own_id)
        assert own.lifecycle_status == "disabled"
        assert own.push_enabled is False
        assert PushSubscription.query.filter_by(device_key="own-device", is_active=True).count() == 0


def test_resolve_sms_sender_and_send_sms_use_inbound_to_number_not_messaging_service(app, world, monkeypatch):
    import twilio_sms
    monkeypatch.setattr("services.license_service.has_feature", lambda *a, **k: True)
    stub_twilio_sms_client(monkeypatch)
    with app.app_context():
        ta = TwilioAccount.query.filter_by(company_id=world["co_a"]).first()
        ta.messaging_service_sid = "MG_should_not_be_used_for_replies"
        pn = add_phone_number(world["co_a"], "+15550009991")
        conv = TwilioConversation(
            company_id=world["co_a"],
            phone_number_id=pn.id,
            from_number="+15551119991",
            to_number="+15550009991",
        )
        db.session.add(conv)
        db.session.commit()
        result = twilio_sms.sendConversationSms(conv.id, "Reply from same line", twilio_account=ta, is_auto_reply=True)
        assert result["success"] is True
        outbound = TwilioMessage.query.filter_by(conversation_id=conv.id, direction="outbound").one()
        assert twilio_sms.resolve_sms_sender(conv) == "+15550009991"
        assert outbound.from_number == "+15550009991"
        assert outbound.from_number != ta.messaging_service_sid


def test_send_sms_blocks_cross_number_sender_leakage(app, world, monkeypatch):
    import twilio_sms
    monkeypatch.setattr("services.license_service.has_feature", lambda *a, **k: True)
    with app.app_context():
        ta = TwilioAccount.query.filter_by(company_id=world["co_a"]).first()
        line_a = add_phone_number(world["co_a"], "+15550009992")
        add_phone_number(world["co_a"], "+15550009993")
        conv = TwilioConversation(
            company_id=world["co_a"],
            phone_number_id=line_a.id,
            from_number="+15551119992",
            to_number="+15550009993",
        )
        db.session.add(conv)
        db.session.commit()
        result = twilio_sms.sendConversationSms(conv.id, "Wrong-line reply", twilio_account=ta, is_auto_reply=True)
        assert result["success"] is False
        assert "does not match" in result["error"]
        assert TwilioMessage.query.filter_by(conversation_id=conv.id, direction="outbound").count() == 0


def test_twilio_message_media_url_is_proxied_in_conversation_payload(client, app, world):
    login(client, world["alice"])
    with app.app_context():
        conv = TwilioConversation(
            company_id=world["co_a"],
            from_number="+15551112222",
            to_number="+15550001000",
            contact_name="Media Customer",
            last_message_at=datetime.utcnow(),
        )
        db.session.add(conv)
        db.session.flush()
        msg = TwilioMessage(
            conversation_id=conv.id,
            company_id=world["co_a"],
            direction="inbound",
            from_number="+15551112222",
            to_number="+15550001000",
            body="photo",
            media_urls=["https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM123/Media/ME123"],
            created_at=datetime.utcnow(),
        )
        db.session.add(msg)
        db.session.commit()
        conv_id = conv.id

    resp = client.get(f"/api/inbox/conversations/{conv_id}")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["messages"][0]["media_urls"] == [f"/api/inbox/messages/{payload['messages'][0]['id']}/media/0"]
    assert "api.twilio.com" not in payload["messages"][0]["media_urls"][0]


def test_auto_replies_editor_surfaces_canonical_after_hours_message_used_by_inbound_sms(app, client, world, monkeypatch):
    desired = "Thanks for reaching out. Our business hours are daily from 2 PM to 2 AM. We’ll respond as soon as we’re back online."
    updated = "Thanks for reaching out. Our business hours are daily from 2 PM to 2 AM. We’ll respond as soon as we’re back online."
    sent = []
    capture_conversation_sms(monkeypatch, sent, lambda conv, message, kw: message)

    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        pn = add_phone_number(world["co_a"], "+15550001000", after_hours_text=desired)
        db.session.commit()
        number_id = pn.id

    login(client, world["alice"])
    page = client.get(f"/twilio/comms?tab=auto&number_id={number_id}")
    assert page.status_code == 200
    assert desired.encode() in page.data
    assert b"Current effective message" in page.data
    assert b"migrated number setting" in page.data or b"number-specific AutoReplyRule" in page.data
    assert b"Number-specific" in page.data

    with app.app_context():
        rule = AutoReplyRule.query.filter_by(company_id=world["co_a"], phone_number_id=number_id, trigger_type="after_hours").one()
        assert rule.response == desired
        rule_id = rule.id

    resp = client.post(
        f"/twilio/rules/{rule_id}/edit",
        data={
            "name": "After Hours",
            "phone_number_id": str(number_id),
            "return_number_id": str(number_id),
            "trigger_type": "after_hours",
            "keywords": "",
            "priority": "50",
            "response": updated,
            "action": "reply",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert updated.encode() in resp.data

    sms = client.post("/twilio/sms/inbound", data={"From": "+15551117777", "To": "+15550001000", "Body": "hello", "MessageSid": "SMAUTOUI"})
    assert sms.status_code == 200
    assert sent == [updated]


def test_company_wide_after_hours_rule_is_labeled_and_used_for_selected_number(app, client, world, monkeypatch):
    sent = []
    capture_conversation_sms(monkeypatch, sent, lambda conv, message, kw: message)
    with app.app_context():
        save_settings(world["co_a"], business_hours={str(i): {"is_open": False} for i in range(7)})
        pn = add_phone_number(world["co_a"], "+15550001000")
        rule = AutoReplyRule(company_id=world["co_a"], name="After Hours Company", trigger_type="after_hours", action="reply", response="Company-wide closed", is_active=True, priority=50)
        db.session.add(rule)
        db.session.commit()
        number_id = pn.id

    login(client, world["alice"])
    page = client.get(f"/twilio/comms?tab=auto&number_id={number_id}")
    assert page.status_code == 200
    assert b"Company-wide" in page.data
    assert b"company-wide AutoReplyRule" in page.data
    assert b"Company-wide closed" in page.data

    sms = client.post("/twilio/sms/inbound", data={"From": "+15551118888", "To": "+15550001000", "Body": "hello", "MessageSid": "SMCOMPANYWIDE"})
    assert sms.status_code == 200
    assert sent == ["Company-wide closed"]
