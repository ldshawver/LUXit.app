from datetime import datetime, timedelta, timezone

import pytest
from twilio.request_validator import RequestValidator
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    Company,
    TuyaNotificationActivation,
    TuyaNotificationEvent,
    TuyaNotificationWorkerHeartbeat,
    TwilioAccount,
    TwilioMessage,
    TwilioPhoneNumber,
    User,
    UserCompanyAccess,
)
from services.tuya_client import TuyaError
import services.tuya_notification as notification

DESTINATION = "+19165989519"
OTHER_DESTINATION = "+15550001111"


@pytest.fixture
def app(monkeypatch):
    for key, value in {
        "TUYA_NOTIFICATION_ENABLED": "true",
        "TUYA_ACCESS_ID": "test-access-id",
        "TUYA_ACCESS_SECRET": "test-access-secret",
        "TUYA_NOTIFICATION_DEVICE_ID": "test-device-id",
        "TUYA_NOTIFICATION_PHONE": DESTINATION,
        "TUYA_NOTIFICATION_DURATION_SECONDS": "60",
        "TWILIO_STRICT_SIGNATURE": "true",
    }.items():
        monkeypatch.setenv(key, value)
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="tuya-integration-test",
        SERVER_NAME="localhost",
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def world(app):
    company_a = Company(name="Tuya Tenant A")
    company_b = Company(name="Tuya Tenant B")
    db.session.add_all([company_a, company_b])
    db.session.flush()
    account_a = TwilioAccount(
        company_id=company_a.id,
        from_phone=DESTINATION,
        webhook_base_url="http://localhost",
        automation_enabled=False,
        is_active=True,
    )
    account_a.set_auth_token("twilio-test-token")
    account_a.set_account_sid("AC_TEST_A")
    account_b = TwilioAccount(
        company_id=company_b.id,
        from_phone="+15550002222",
        webhook_base_url="http://localhost",
        automation_enabled=False,
        is_active=True,
    )
    account_b.set_auth_token("twilio-test-token-b")
    account_b.set_account_sid("AC_TEST_B")
    db.session.add_all([account_a, account_b])
    db.session.flush()
    db.session.add_all(
        [
            TwilioPhoneNumber(
                company_id=company_a.id,
                twilio_account_id=account_a.id,
                phone_number=DESTINATION,
                is_active=True,
                sms_enabled=True,
            ),
            TwilioPhoneNumber(
                company_id=company_a.id,
                twilio_account_id=account_a.id,
                phone_number=OTHER_DESTINATION,
                is_active=True,
                sms_enabled=True,
            ),
        ]
    )
    owner_a = User(
        username="tuya-owner-a",
        email="tuya-owner-a@example.test",
        password_hash=generate_password_hash("pw"),
        default_company_id=company_a.id,
    )
    staff_a = User(
        username="tuya-staff-a",
        email="tuya-staff-a@example.test",
        password_hash=generate_password_hash("pw"),
        default_company_id=company_a.id,
    )
    owner_b = User(
        username="tuya-owner-b",
        email="tuya-owner-b@example.test",
        password_hash=generate_password_hash("pw"),
        default_company_id=company_b.id,
    )
    db.session.add_all([owner_a, staff_a, owner_b])
    db.session.flush()
    db.session.add_all(
        [
            UserCompanyAccess(
                user_id=owner_a.id,
                company_id=company_a.id,
                role="owner",
                is_default=True,
            ),
            UserCompanyAccess(
                user_id=staff_a.id,
                company_id=company_a.id,
                role="staff",
                is_default=True,
            ),
            UserCompanyAccess(
                user_id=owner_b.id,
                company_id=company_b.id,
                role="owner",
                is_default=True,
            ),
        ]
    )
    db.session.commit()
    return {
        "company_a": company_a.id,
        "company_b": company_b.id,
        "owner_a": owner_a.id,
        "staff_a": staff_a.id,
        "owner_b": owner_b.id,
    }


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, user_id):
    from flask import g, has_app_context

    if has_app_context() and hasattr(g, "_login_user"):
        del g._login_user
    with client.session_transaction() as session:
        session.clear()
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def inbound_payload(sid, *, to=DESTINATION, from_number="+14155550123"):
    return {
        "MessageSid": sid,
        "To": to,
        "From": from_number,
        "Body": "notification test",
        "NumMedia": "0",
    }


def signed_post(client, payload, *, valid=True):
    signature = RequestValidator("twilio-test-token").compute_signature(
        "http://localhost/twilio/sms/inbound", payload
    )
    if not valid:
        signature = "invalid-signature"
    return client.post(
        "/twilio/sms/inbound",
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )


def test_signed_inbound_matches_to_and_persists_one_event(client, world):
    response = signed_post(
        client, inbound_payload("SM_TUYA_1", from_number=DESTINATION)
    )
    assert response.status_code == 200
    assert (
        TwilioMessage.query.filter_by(
            twilio_sid="SM_TUYA_1", direction="inbound"
        ).count()
        == 1
    )
    event = TuyaNotificationEvent.query.filter_by(message_sid="SM_TUYA_1").one()
    assert event.company_id == world["company_a"]
    assert event.trigger_source == "inbound_sms"
    assert (
        TuyaNotificationActivation.query.filter_by(company_id=world["company_a"])
        .one()
        .desired_state
    )


def test_invalid_signature_and_other_destination_do_not_trigger(client, world):
    assert (
        signed_post(client, inbound_payload("SM_BAD_SIG"), valid=False).status_code
        == 403
    )
    assert TuyaNotificationEvent.query.filter_by(message_sid="SM_BAD_SIG").count() == 0
    other = inbound_payload("SM_OTHER_TO", to=OTHER_DESTINATION)
    other_signature = RequestValidator("twilio-test-token").compute_signature(
        "http://localhost/twilio/sms/inbound", other
    )
    response = client.post(
        "/twilio/sms/inbound",
        data=other,
        headers={"X-Twilio-Signature": other_signature},
    )
    assert response.status_code == 200
    assert TwilioMessage.query.filter_by(twilio_sid="SM_OTHER_TO").count() == 1
    assert TuyaNotificationEvent.query.filter_by(message_sid="SM_OTHER_TO").count() == 0


def test_status_callback_does_not_trigger(client):
    response = client.post(
        "/twilio/sms/status",
        data={"MessageSid": "SM_STATUS", "MessageStatus": "delivered"},
    )
    assert response.status_code in {200, 204, 403, 404}
    assert TuyaNotificationEvent.query.filter_by(message_sid="SM_STATUS").count() == 0


def test_duplicate_and_new_message_deadlines(world, monkeypatch):
    monkeypatch.setattr(
        notification, "company_owns_integration", lambda company_id: True
    )
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert notification.accept_inbound(
        world["company_a"], "SM_DUP", DESTINATION, now=start
    )
    assert not notification.accept_inbound(
        world["company_a"], "SM_DUP", DESTINATION, now=start + timedelta(seconds=30)
    )
    activation = TuyaNotificationActivation.query.filter_by(
        company_id=world["company_a"]
    ).one()
    assert notification.as_utc(activation.off_deadline) == start + timedelta(seconds=60)
    assert notification.accept_inbound(
        world["company_a"], "SM_NEW", DESTINATION, now=start + timedelta(seconds=30)
    )
    assert notification.as_utc(activation.off_deadline) == start + timedelta(seconds=90)
    assert (
        TuyaNotificationEvent.query.filter_by(company_id=world["company_a"]).count()
        == 2
    )


def test_old_and_final_off_and_expired_on_are_safe(world, monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    activation = TuyaNotificationActivation(
        company_id=world["company_a"],
        desired_state=True,
        off_deadline=start + timedelta(seconds=90),
        last_operation="on",
        last_operation_status="succeeded",
    )
    db.session.add(activation)
    db.session.commit()
    calls = []
    fake = type("Client", (), {"set_switch": lambda self, value: calls.append(value)})()
    monkeypatch.setattr(notification, "get_client", lambda **kwargs: fake)
    assert not notification.run_off_check(
        activation.id, now=start + timedelta(seconds=60)
    )
    assert calls == []
    assert notification.run_off_check(activation.id, now=start + timedelta(seconds=90))
    assert calls == [False]
    activation.desired_state = True
    activation.off_deadline = start
    activation.last_operation = "on"
    activation.last_operation_status = "queued"
    db.session.commit()
    assert notification.run_on(activation.id, now=start + timedelta(seconds=120))
    assert calls == [False, False]


def test_failures_retry_bounded_and_safety_pending(world, monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    activation = TuyaNotificationActivation(
        company_id=world["company_a"],
        desired_state=True,
        off_requested=True,
        off_deadline=start,
        last_operation="off",
        last_operation_status="queued",
    )
    db.session.add(activation)
    db.session.commit()

    class FailingClient:
        def set_switch(self, value):
            raise TuyaError("sanitized failure")

    monkeypatch.setattr(notification, "get_client", lambda **kwargs: FailingClient())
    for attempt in range(7):
        notification.run_off_check(
            activation.id, now=start + timedelta(minutes=attempt * 10)
        )
    db.session.refresh(activation)
    assert activation.retry_count == notification.MAX_RETRIES
    assert activation.desired_state is True
    assert activation.off_requested is True
    assert activation.last_operation_status == "failed"


def test_restart_reconciliation_off_and_no_repeated_on(world, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    overdue = TuyaNotificationActivation(
        company_id=world["company_a"],
        desired_state=True,
        off_deadline=now - timedelta(seconds=1),
        last_operation="on",
        last_operation_status="succeeded",
    )
    db.session.add(overdue)
    db.session.commit()
    calls = []
    fake = type("Client", (), {"set_switch": lambda self, value: calls.append(value)})()
    monkeypatch.setattr(notification, "get_client", lambda **kwargs: fake)
    notification.reconcile(now)
    assert calls == [False]
    assert overdue.desired_state is False


def test_two_workers_share_operation_claim(world, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    activation = TuyaNotificationActivation(
        company_id=world["company_a"],
        desired_state=True,
        off_deadline=now + timedelta(seconds=60),
        last_operation="on",
        last_operation_status="in_progress",
        last_attempt_at=now,
    )
    db.session.add(activation)
    db.session.commit()
    monkeypatch.setattr(
        notification,
        "get_client",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate provider call")
        ),
    )
    assert notification.run_on(activation.id, now=now + timedelta(seconds=1))


def test_inbound_survives_activation_failure(client, world, monkeypatch):
    monkeypatch.setattr(
        notification,
        "accept_inbound",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    response = signed_post(client, inbound_payload("SM_PERSIST_FAILURE"))
    assert response.status_code == 200
    assert TwilioMessage.query.filter_by(twilio_sid="SM_PERSIST_FAILURE").count() == 1


def test_admin_ui_manual_test_off_and_tenant_permissions(client, world, monkeypatch):
    heartbeat = TuyaNotificationWorkerHeartbeat(
        worker_id="test-worker",
        started_at=notification.utcnow(),
        last_seen_at=notification.utcnow(),
    )
    db.session.add(heartbeat)
    db.session.commit()
    login(client, world["owner_a"])
    monkeypatch.setattr(
        notification,
        "get_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("live request")),
    )
    page = client.get("/twilio/comms/tuya-notifications")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Tuya Notifications" in body
    query_page = client.get("/twilio/comms?tab=tuya_notifications")
    assert query_page.status_code == 200
    assert "Tuya Notifications" in query_page.get_data(as_text=True)
    assert client.get("/twilio/comms?tab=devices").status_code == 200
    for secret in ("test-access-secret", "test-device-id", "test-access-id"):
        assert secret not in body
    assert client.post("/twilio/comms/tuya-notifications/test").status_code == 302
    assert (
        TuyaNotificationEvent.query.filter_by(
            company_id=world["company_a"], trigger_source="manual_test"
        ).count()
        == 1
    )
    assert client.post("/twilio/comms/tuya-notifications/test").status_code == 302
    assert (
        TuyaNotificationEvent.query.filter_by(trigger_source="manual_test").count() == 1
    )
    activation = TuyaNotificationActivation.query.filter_by(
        company_id=world["company_a"]
    ).one()
    assert client.post("/twilio/comms/tuya-notifications/off").status_code == 302
    db.session.refresh(activation)
    assert activation.off_requested is True

    login(client, world["staff_a"])
    assert client.get("/twilio/comms/tuya-notifications").status_code == 403
    assert client.post("/twilio/comms/tuya-notifications/test").status_code == 403
    login(client, world["owner_b"])
    assert client.get("/twilio/comms/tuya-notifications").status_code == 200
    assert client.post("/twilio/comms/tuya-notifications/test").status_code == 404
    assert (
        TuyaNotificationEvent.query.filter_by(company_id=world["company_b"]).count()
        == 0
    )


def test_manual_action_is_post_only_and_csrf_protected(client, app, world):
    login(client, world["owner_a"])
    assert client.get("/twilio/comms/tuya-notifications/test").status_code == 405
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        before = TuyaNotificationEvent.query.count()
        assert client.post("/twilio/comms/tuya-notifications/test").status_code in {
            302,
            400,
        }
        assert TuyaNotificationEvent.query.count() == before
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_disabled_triggers_skip_but_safety_off_still_runs(world, monkeypatch):
    monkeypatch.setenv("TUYA_NOTIFICATION_ENABLED", "false")
    assert not notification.accepts_destination(DESTINATION)
    now = notification.utcnow()
    activation = TuyaNotificationActivation(
        company_id=world["company_a"],
        desired_state=True,
        off_requested=True,
        off_deadline=now,
        last_operation="off",
        last_operation_status="queued",
    )
    db.session.add(activation)
    db.session.commit()
    calls = []
    fake = type("Client", (), {"set_switch": lambda self, value: calls.append(value)})()
    monkeypatch.setattr(notification, "_client", fake)
    monkeypatch.setattr(notification, "get_client", lambda **kwargs: fake)
    assert notification.run_off_check(activation.id, now=now)
    assert calls == [False]
