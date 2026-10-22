"""Regression coverage for the inbound-keyword -> outbound-SMS -> delivery-
status lifecycle.

Root cause under test: ``twilio_sms.sendConversationSms`` sent conversational
replies (keyword auto-replies, campaign replies, manual replies) with an
explicit ``from_=<number>`` and no ``status_callback``. Twilio only reports a
message's real delivery outcome to a callback URL that was actually supplied
-- either per-message, or as a default configured on the sending Messaging
Service. A number-scoped send with no per-message callback and no Messaging
Service involved gets no callback at all, so the local record was frozen
forever at whatever status the synchronous API response happened to carry
(hardcoded to "sent" regardless), even though the customer's real delivery
outcome (delivered / undelivered / failed) was never learned.

These tests use a disposable test tenant and neutral synthetic keyword text
only -- never the production tenant/content that surfaced this defect.
"""
import os

import pytest

from app import create_app
from extensions import db
from models import (
    Company, Contact, SMSKeywordRule, SMSOutboundIntent, TwilioAccount,
    TwilioConversation, TwilioMessage, TwilioPhoneNumber, User, user_company,
)

WEBHOOK_BASE_URL = "https://test.example.invalid"
BUSINESS_NUMBER = "+15559999999"
CUSTOMER_NUMBER = "+14155550199"
NEUTRAL_MENU_REPLY = "Thank you for contacting us. Here is our menu."


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
def tenant(app, client):
    company = Company(name="Status Callback Test Co")
    user = User(username="status-cb-user", email="status-cb@example.com",
                default_company_id=None, is_admin=True)
    user.password_hash = "testpass"
    db.session.add_all([company, user])
    db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))

    account = TwilioAccount(company_id=company.id, from_phone=BUSINESS_NUMBER,
                             webhook_base_url=WEBHOOK_BASE_URL, is_active=True)
    account.set_account_sid("ACtest")
    account.set_auth_token("token")
    db.session.add(account)
    db.session.flush()

    pn = TwilioPhoneNumber(
        company_id=company.id, phone_number=BUSINESS_NUMBER, friendly_name="Main",
        is_active=True,
        business_hours={str(i): {"is_open": True, "open": "00:00", "close": "23:59"} for i in range(7)},
    )
    db.session.add(pn)

    rule = SMSKeywordRule(
        company_id=company.id, keyword="menu", match_type="exact",
        reply_message=NEUTRAL_MENU_REPLY, priority=1, is_active=True,
    )
    db.session.add(rule)

    # An already-known, identity-confirmed customer, so the keyword-reply
    # path under test fires instead of the (separate, higher-precedence)
    # first-contact identity-collection flow.
    contact = Contact(
        company_id=company.id, first_name="Test", last_name="Customer",
        phone=CUSTOMER_NUMBER, normalized_phone=CUSTOMER_NUMBER,
        tags="sms_consent", is_active=True, is_subscribed=True,
        identity_status="confirmed", google_match_status="not_checked",
    )
    db.session.add(contact)
    db.session.commit()

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return {"company": company, "rule": rule}


class _FakeMessages:
    """Records every create() call; returns a distinct SID + a realistic
    Twilio-style synchronous status ("queued") per call -- never "sent" or
    "delivered", since those are never known synchronously."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        sid = f"SM{len(self.calls):04d}"
        return type("Msg", (), {"sid": sid, "status": "queued"})()


class _RaisingMessages:
    def __init__(self, status=None, code=None):
        self.calls = []
        self.status = status
        self.code = code

    def create(self, **kwargs):
        self.calls.append(kwargs)
        exc = RuntimeError("Twilio provider rejected the request")
        exc.status = self.status
        exc.code = self.code
        raise exc


def _patch_client(monkeypatch, messages):
    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = messages
    import twilio.rest
    monkeypatch.setattr(twilio.rest, "Client", FakeClient)
    monkeypatch.setenv("LUXIT_TWILIO_MODE", "live")


def _post_inbound(client, body, message_sid):
    return client.post("/twilio/sms/inbound", data={
        "From": CUSTOMER_NUMBER, "To": BUSINESS_NUMBER, "Body": body, "MessageSid": message_sid,
    })


# ---------------------------------------------------------------------------
# Keyword matching: case-insensitivity and surrounding whitespace
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", ["Menu", "MENU", " menu ", "menu"])
def test_menu_keyword_matches_case_and_whitespace_variants(app, client, tenant, monkeypatch, body):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        resp = _post_inbound(client, body, f"SMIN-{abs(hash(body))}")
        assert resp.status_code == 200
        outbound = TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).all()
        assert len(outbound) == 1
        assert outbound[0].body == NEUTRAL_MENU_REPLY


# ---------------------------------------------------------------------------
# Root-cause fix: status_callback attached, real provider status persisted
# ---------------------------------------------------------------------------

def test_keyword_reply_submission_attaches_status_callback_url(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        resp = _post_inbound(client, "menu", "SMIN0001")
        assert resp.status_code == 200
        assert len(messages.calls) == 1
        assert messages.calls[0]["status_callback"] == f"{WEBHOOK_BASE_URL}/twilio/sms/status"


def test_accepted_submission_persists_real_provider_status_and_sid(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        resp = _post_inbound(client, "menu", "SMIN0002")
        assert resp.status_code == 200
        outbound = TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).one()
        # Twilio's synchronous response was "queued" -- the record must reflect
        # that, not a hardcoded "sent" the app never actually confirmed.
        assert outbound.status == "queued"
        assert outbound.twilio_sid == "SM0001"
        intent = SMSOutboundIntent.query.filter_by(company_id=tenant["company"].id).one()
        assert intent.provider_sid == "SM0001"
        assert intent.status == "delivered"  # internal outbox state: "provider accepted"


# ---------------------------------------------------------------------------
# Exactly-once delivery + idempotency
# ---------------------------------------------------------------------------

def test_exactly_one_outbound_submission_for_one_eligible_inbound(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        resp = _post_inbound(client, "menu", "SMIN0003")
        assert resp.status_code == 200
        assert len(messages.calls) == 1
        assert TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).count() == 1


def test_duplicate_inbound_message_sid_does_not_send_a_second_reply(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        first = _post_inbound(client, "menu", "SMIN-DUP")
        second = _post_inbound(client, "menu", "SMIN-DUP")
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(messages.calls) == 1
        assert TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).count() == 1


# ---------------------------------------------------------------------------
# Provider submission failure must not look like success
# ---------------------------------------------------------------------------

def test_provider_rejection_yields_failed_state_not_false_success(app, client, tenant, monkeypatch):
    messages = _RaisingMessages(status=400, code="21211")
    _patch_client(monkeypatch, messages)
    with app.app_context():
        resp = _post_inbound(client, "menu", "SMIN-REJECT")
        # The webhook itself still ACKs Twilio's inbound delivery (inbound was
        # received); it is the reply submission that failed.
        assert resp.status_code == 200
        assert len(messages.calls) == 1
        intent = SMSOutboundIntent.query.filter_by(company_id=tenant["company"].id).one()
        assert intent.status in {"failed_retryable", "failed_terminal", "delivery_unknown"}
        assert intent.status != "delivered"
        # No conversation bubble may claim a reply was sent/delivered when it wasn't.
        assert TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).count() == 0


def test_provider_5xx_is_retryable_not_terminal(app, client, tenant, monkeypatch):
    messages = _RaisingMessages(status=500, code="20500")
    _patch_client(monkeypatch, messages)
    with app.app_context():
        _post_inbound(client, "menu", "SMIN-5XX")
        intent = SMSOutboundIntent.query.filter_by(company_id=tenant["company"].id).one()
        assert intent.status == "failed_retryable"


# ---------------------------------------------------------------------------
# Delivery-status callback correlates to the correct outbound record
# ---------------------------------------------------------------------------

def test_delivery_callback_updates_the_correct_keyword_reply_message(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        _post_inbound(client, "menu", "SMIN-CB-1")
        outbound = TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).one()
        assert outbound.status == "queued"

        callback = client.post("/twilio/sms/status", data={
            "MessageSid": outbound.twilio_sid, "MessageStatus": "delivered",
        })
        assert callback.status_code == 204
        db.session.refresh(outbound)
        assert outbound.status == "delivered"


def test_failure_callback_updates_the_correct_keyword_reply_message(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        _post_inbound(client, "menu", "SMIN-CB-2")
        outbound = TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).one()

        callback = client.post("/twilio/sms/status", data={
            "MessageSid": outbound.twilio_sid, "MessageStatus": "undelivered",
            "ErrorCode": "30003",
        })
        assert callback.status_code == 204
        db.session.refresh(outbound)
        assert outbound.status == "undelivered"
        assert outbound.error_code == "30003"


def test_delivery_callback_for_unknown_sid_does_not_touch_other_messages(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        _post_inbound(client, "menu", "SMIN-CB-3")
        outbound = TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).one()
        before = outbound.status

        callback = client.post("/twilio/sms/status", data={
            "MessageSid": "SM-does-not-correlate-to-anything", "MessageStatus": "delivered",
        })
        assert callback.status_code == 204
        db.session.refresh(outbound)
        assert outbound.status == before


# ---------------------------------------------------------------------------
# Suppressed / opted-out recipient fails safe
# ---------------------------------------------------------------------------

def test_opted_out_conversation_never_gets_a_keyword_reply(app, client, tenant, monkeypatch):
    messages = _FakeMessages()
    _patch_client(monkeypatch, messages)
    with app.app_context():
        conv = TwilioConversation(company_id=tenant["company"].id, from_number=CUSTOMER_NUMBER,
                                   to_number=BUSINESS_NUMBER, is_opted_out=True)
        db.session.add(conv)
        db.session.commit()

        resp = _post_inbound(client, "menu", "SMIN-OPTOUT")
        assert resp.status_code == 200
        assert len(messages.calls) == 0
        assert TwilioMessage.query.filter_by(direction="outbound", company_id=tenant["company"].id).count() == 0
