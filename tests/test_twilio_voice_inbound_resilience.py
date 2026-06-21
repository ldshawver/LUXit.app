from datetime import datetime
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, TwilioAccount, TwilioPhoneNumber, User, UserCompanyAccess, Notification, TwilioCallLog


@pytest.fixture
def voice_app():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company = Company(name="Voice Tenant")
        db.session.add(company); db.session.flush()
        account = TwilioAccount(company_id=company.id, from_phone="+19165989519", is_active=True)
        db.session.add(account); db.session.flush()
        forward_line = TwilioPhoneNumber(
            company_id=company.id,
            twilio_account_id=account.id,
            phone_number="+19165989519",
            friendly_name="Forward Line",
            voice_enabled=True,
            is_active=True,
            during_hours_route="forward",
            voice_forwarding_enabled=True,
            call_forward_to="+12792860000",
            after_hours_route="voicemail",
            voicemail_greeting_text="Please leave a message for Forward Line.",
        )
        pwa_line = TwilioPhoneNumber(
            company_id=company.id,
            twilio_account_id=account.id,
            phone_number="+18302591310",
            friendly_name="PWA Line",
            voice_enabled=True,
            is_active=True,
            during_hours_route="ring_pwa",
            after_hours_route="voicemail",
        )
        user = User(username="voice-admin", email="voice-admin@example.com", password_hash=generate_password_hash("pw"), is_admin=True, default_company_id=company.id)
        db.session.add_all([forward_line, pwa_line, user]); db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="admin", is_default=True))
        db.session.commit()
        yield app, app.test_client(), company, forward_line, pwa_line
        db.session.remove(); db.drop_all()


def _post_voice(client, to_number, call_sid):
    return client.post("/twilio/voice/inbound", data={
        "To": to_number,
        "From": "+14155551212",
        "CallSid": call_sid,
        "Direction": "inbound",
    })


def test_voice_inbound_during_hours_forward_returns_twiml(voice_app, monkeypatch):
    app, client, company, forward_line, _ = voice_app
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **kw: True)

    resp = _post_voice(client, "+19165989519", "TEST_FORWARD_DEBUG_005")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/xml")
    assert "<Dial" in body
    assert "<Number>+12792860000</Number>" in body
    assert "InFailedSqlTransaction" not in body
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="TEST_FORWARD_DEBUG_005").one()
        assert log.phone_number_id == forward_line.id
        assert log.forwarded_to_number == "+12792860000"


def test_voice_inbound_ring_pwa_returns_valid_client_twiml(voice_app, monkeypatch):
    _, client, _, _, pwa_line = voice_app
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **kw: True)

    resp = _post_voice(client, "+18302591310", "TEST_RING_PWA_001")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<Response>" in body
    assert "<Client>" in body
    assert "<Identity>" in body
    assert "InFailedSqlTransaction" not in body


def test_voice_inbound_after_hours_returns_voicemail_twiml(voice_app, monkeypatch):
    _, client, *_ = voice_app
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **kw: False)

    resp = _post_voice(client, "+19165989519", "TEST_AFTER_HOURS_VM_001")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<Record" in body
    assert "Please leave a message" in body
    assert "InFailedSqlTransaction" not in body


def test_voice_inbound_exception_rolls_back_and_returns_safe_twiml(voice_app, monkeypatch):
    _, client, *_ = voice_app
    import twilio_sms
    rollback_calls = []
    original_rollback = db.session.rollback

    def tracked_rollback():
        rollback_calls.append("rollback")
        return original_rollback()

    monkeypatch.setattr(twilio_sms, "_resolve_number", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("first root cause")))
    monkeypatch.setattr(db.session, "rollback", tracked_rollback)

    with patch.object(twilio_sms.logger, "exception") as log_exception:
        resp = _post_voice(client, "+19165989519", "TEST_ERROR_ROLLBACK_001")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<Response>" in body
    assert "<Record" in body
    assert rollback_calls
    assert log_exception.called


def test_voice_schema_migration_covers_notification_and_call_log_fields():
    sql = open("migrations/20260620_voice_notification_calllog_compat.sql", encoding="utf-8").read().lower()
    for phrase in [
        "alter table notification add column if not exists phone_number_id",
        "alter table notification add column if not exists event_type",
        "alter table twilio_call_log add column if not exists phone_number_id",
        "alter table twilio_call_log add column if not exists forwarded_to_number",
        "create index if not exists ix_notification_phone_number_id",
        "create index if not exists ix_twilio_call_log_phone_number_id",
    ]:
        assert phrase in sql
    assert " drop " not in sql
    assert "delete from" not in sql
