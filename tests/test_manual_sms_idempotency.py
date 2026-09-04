"""Regression coverage for manual (operator-initiated) SMS send idempotency.

Launch-critical requirement: a browser/network retry (double-tap, timeout-
and-resubmit, a duplicate/concurrent request) of the same logical manual send
must produce exactly one canonical outbound record and exactly one provider
submission. ``services.sms_send_idempotency.send_with_idempotency`` is the
guard (see its module docstring); ``twilio_sms.sendConversationSms`` also now
rejects a send into a STOP'd/opted-out conversation before touching the
provider or the database, closing a separate pre-existing gap on the manual
send paths (automated paths already pre-checked this).
"""
import os

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    Company, SmsSendIdempotencyRecord, TwilioAccount, TwilioConversation,
    TwilioMessage, User, UserCompanyAccess, TwilioPhoneNumber, user_company,
)


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


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def world(app):
    co = Company(name="Idempotency Co", is_active=True)
    db.session.add(co)
    db.session.flush()
    user = User(username="idem-user", email="idem@example.com",
               password_hash=generate_password_hash("x"), default_company_id=co.id)
    db.session.add(user)
    db.session.flush()
    db.session.add_all([
        UserCompanyAccess(user_id=user.id, company_id=co.id, role="owner", is_default=True, pwa_access_enabled=True),
        TwilioAccount(company_id=co.id, from_phone="+15550001111", _account_sid="ACtest", _auth_token="auth"),
    ])
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=co.id, is_default=True))
    pn = TwilioPhoneNumber(company_id=co.id, phone_number="+15550001111", friendly_name="Main", is_active=True,
                           business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)})
    db.session.add(pn)
    db.session.flush()
    conv = TwilioConversation(company_id=co.id, from_number="+15551230001", to_number=pn.phone_number)
    stopped_conv = TwilioConversation(company_id=co.id, from_number="+15551230099", to_number=pn.phone_number, is_opted_out=True)
    db.session.add_all([conv, stopped_conv])
    db.session.commit()
    return {"co": co.id, "user": user.id, "conv": conv.id, "stopped_conv": stopped_conv.id}


class _FakeMessages:
    """Records every create() call; returns a distinct SID per call."""
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        sid = f"SM{len(self.calls):04d}"
        return type("Msg", (), {"sid": sid, "status": "queued"})()


class _RaisingMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("Twilio provider exploded")


def _patch_client(monkeypatch, messages):
    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = messages
    import twilio.rest
    monkeypatch.setattr(twilio.rest, "Client", FakeClient)
    monkeypatch.setenv("LUXIT_TWILIO_MODE", "live")


def _send(world, body="hello", idempotency_key="key-a"):
    from services.sms_send_idempotency import send_with_idempotency
    ta = TwilioAccount.query.filter_by(company_id=world["co"]).first()
    return send_with_idempotency(
        company_id=world["co"], user_id=world["user"], conversation_id=world["conv"],
        body=body, twilio_account=ta, idempotency_key=idempotency_key,
    )


# --------------------------------------------------------------------------
# Sequential duplicate
# --------------------------------------------------------------------------

def test_sequential_duplicate_same_key_sends_exactly_once(app, world, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        first = _send(world)
        second = _send(world)  # same key, sequential retry
        assert first["success"] is True and second["success"] is True
        assert first["sid"] == second["sid"]
        assert second["idempotent"] is True
        assert len(messages.calls) == 1  # exactly one provider submission
        assert TwilioMessage.query.filter_by(company_id=world["co"], direction="outbound").count() == 1


def test_different_keys_are_independent_sends(app, world, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        first = _send(world, idempotency_key="key-a")
        second = _send(world, idempotency_key="key-b")
        assert first["sid"] != second["sid"]
        assert len(messages.calls) == 2
        assert TwilioMessage.query.filter_by(company_id=world["co"], direction="outbound").count() == 2


# --------------------------------------------------------------------------
# Concurrent duplicate (simulated: a claim already exists mid-flight)
# --------------------------------------------------------------------------

def test_concurrent_duplicate_collapses_to_one_send(app, world, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        # Simulate another request having just won the claim and being
        # mid-flight (Twilio call not yet returned).
        db.session.add(SmsSendIdempotencyRecord(
            company_id=world["co"], user_id=world["user"], conversation_id=world["conv"],
            idempotency_key="key-inflight", status="sending",
        ))
        db.session.commit()

        result = _send(world, idempotency_key="key-inflight")
        assert result["success"] is False
        assert result["status"] == "delivery_unknown"
        assert result["idempotent"] is True
        assert len(messages.calls) == 0  # never sent a second time
        assert TwilioMessage.query.filter_by(company_id=world["co"], direction="outbound").count() == 0


# --------------------------------------------------------------------------
# Timeout after provider submission -- process died mid-flight
# --------------------------------------------------------------------------

def test_timeout_after_provider_submission_never_resends(app, world, monkeypatch):
    """A row stuck at status='sending' forever (process died after Twilio
    accepted but before the row could be marked terminal) must never be
    resent by a later request for the same key -- provider acceptance in
    that state is genuinely unknown."""
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        db.session.add(SmsSendIdempotencyRecord(
            company_id=world["co"], user_id=world["user"], conversation_id=world["conv"],
            idempotency_key="key-crashed", status="sending",
        ))
        db.session.commit()

        result = _send(world, idempotency_key="key-crashed")
        assert result["status"] == "delivery_unknown"
        assert len(messages.calls) == 0


# --------------------------------------------------------------------------
# Retry after SID persistence -- client lost the response but the send
# already succeeded
# --------------------------------------------------------------------------

def test_retry_after_sid_persistence_returns_cached_result_no_resend(app, world, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        first = _send(world, idempotency_key="key-lost-response")
        assert first["success"] is True
        assert len(messages.calls) == 1

        # Client never saw the HTTP response (network drop) and retries with
        # the same key it generated for this logical send.
        retry = _send(world, idempotency_key="key-lost-response")
        assert retry["success"] is True
        assert retry["sid"] == first["sid"]
        assert retry["idempotent"] is True
        assert len(messages.calls) == 1  # still exactly one


# --------------------------------------------------------------------------
# STOP / suppressed contact
# --------------------------------------------------------------------------

def test_stopped_conversation_is_rejected_before_any_provider_call(app, world, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        from services.sms_send_idempotency import send_with_idempotency
        ta = TwilioAccount.query.filter_by(company_id=world["co"]).first()
        result = send_with_idempotency(
            company_id=world["co"], user_id=world["user"], conversation_id=world["stopped_conv"],
            body="are you still interested", twilio_account=ta, idempotency_key="key-stop",
        )
        assert result["success"] is False
        assert "STOP" in (result.get("error") or "")
        assert len(messages.calls) == 0
        assert TwilioMessage.query.filter_by(conversation_id=world["stopped_conv"]).count() == 0


def test_stopped_conversation_rejected_via_sendConversationSms_directly(app, world):
    """The guard lives in sendConversationSms itself -- the single entry
    point -- so it protects every caller, not just the idempotency wrapper."""
    with app.app_context():
        from twilio_sms import sendConversationSms
        ta = TwilioAccount.query.filter_by(company_id=world["co"]).first()
        result = sendConversationSms(world["stopped_conv"], "hi", twilio_account=ta)
        assert result["success"] is False
        assert result.get("error_code") == "recipient_opted_out"


# --------------------------------------------------------------------------
# Provider failure still records exactly one failed attempt per key
# --------------------------------------------------------------------------

def test_provider_failure_is_not_silently_retried_as_success(app, world, monkeypatch):
    messages = _RaisingMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        result = _send(world, idempotency_key="key-fails")
        assert result["success"] is False
        assert len(messages.calls) == 1
        record = SmsSendIdempotencyRecord.query.filter_by(
            company_id=world["co"], idempotency_key="key-fails",
        ).one()
        assert record.status == "failed"


# --------------------------------------------------------------------------
# Route-level wiring: PWA send endpoint honors a client-supplied key and
# preserves pre-existing tenant/authorization checks
# --------------------------------------------------------------------------

def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def test_pwa_route_double_submit_with_same_key_sends_once(client, world, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    _login(client, world["user"])

    body = {"body": "route double submit", "idempotency_key": "route-key-1"}
    first = client.post(f"/api/inbox/conversations/{world['conv']}/messages", json=body)
    second = client.post(f"/api/inbox/conversations/{world['conv']}/messages", json=body)

    assert first.status_code == 200 and first.json["success"] is True
    assert second.status_code == 200 and second.json["success"] is True
    assert first.json["message"]["twilio_sid"] == second.json["message"]["twilio_sid"]
    assert len(messages.calls) == 1


def test_pwa_route_wrong_tenant_conversation_rejected(client, world, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    _login(client, world["user"])
    resp = client.post("/api/inbox/conversations/999999/messages", json={"body": "hi"})
    assert resp.status_code == 404
    assert len(messages.calls) == 0
