"""Regression coverage for out-of-order Twilio SMS delivery-status callbacks.

Original gap: every write path that applies a Twilio ``StatusCallback``
(``TwilioMessage.status`` via the ``/twilio/sms/status`` route,
``SMSRecipient.status`` via ``services.sms_keyword_engine.update_delivery_status``,
and ``PromotionalOptInSolicitation.delivery_status`` via
``services.promotional_optin.sync_solicitation_delivery_status``) blindly
overwrote the stored status with whatever arrived last. Twilio's webhook is
not guaranteed to arrive in order, so a late/stale earlier status (e.g.
"sent") landing after a later one ("delivered") had already been applied
would silently regress the row back to a non-terminal state -- a false,
stale "sent" UI state for a message that was already confirmed delivered.

``services.sms_status.is_forward_status_transition`` centralizes the guard;
these tests pin its own behavior and confirm all three write paths apply it,
while a legitimate lateral correction between two already-final outcomes
(e.g. "delivered" -> "failed", which pre-existing tests rely on) is still
allowed.
"""
import os

import pytest

from app import create_app
from extensions import db
from models import (
    Company, Contact, PromotionalOptInSolicitation, SMSRecipient,
    TwilioAccount, TwilioConversation, TwilioMessage, User, user_company,
)
from services.sms_status import is_forward_status_transition


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
def tenant_user(app, client):
    company = Company(name="SMS Status Guard Co")
    user = User(username="status-guard-user", email="status-guard@example.com", is_admin=True)
    user.password_hash = "x"
    db.session.add_all([company, user])
    db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return user, company


# --------------------------------------------------------------------------
# Unit coverage of the shared guard
# --------------------------------------------------------------------------

@pytest.mark.parametrize("current,incoming,expected", [
    ("queued", "sent", True),          # normal forward progression
    ("sent", "delivered", True),
    ("delivered", "sent", False),      # stale/out-of-order regression
    ("delivered", "queued", False),
    ("failed", "sent", False),
    ("delivered", "failed", True),     # lateral correction between finals
    ("failed", "delivered", True),
    ("delivered", "delivered", True),  # idempotent replay
    ("", "queued", True),              # first status ever recorded
    ("sent", "", True),                # empty incoming is a safe no-op
    ("sent", "some_future_status", True),  # unknown status: never block
])
def test_is_forward_status_transition(current, incoming, expected):
    assert is_forward_status_transition(current, incoming) is expected


# --------------------------------------------------------------------------
# SMSRecipient (services.sms_keyword_engine.update_delivery_status)
# --------------------------------------------------------------------------

def test_campaign_recipient_late_sent_after_delivered_does_not_regress(app, tenant_user):
    _, company = tenant_user
    recipient = SMSRecipient(company_id=company.id, provider_message_sid="SMLATE1", status="sent")
    db.session.add(recipient)
    db.session.commit()

    from services.sms_keyword_engine import update_delivery_status

    update_delivery_status("SMLATE1", "delivered")
    db.session.commit()
    db.session.refresh(recipient)
    assert recipient.status == "delivered"
    delivered_at = recipient.delivered_at

    # A stale "sent" callback arrives late (out-of-order network delivery).
    update_delivery_status("SMLATE1", "sent")
    db.session.commit()
    db.session.refresh(recipient)
    assert recipient.status == "delivered"
    assert recipient.delivered_at == delivered_at


def test_campaign_recipient_forward_progression_still_applies(app, tenant_user):
    _, company = tenant_user
    recipient = SMSRecipient(company_id=company.id, provider_message_sid="SMFWD1", status="sent")
    db.session.add(recipient)
    db.session.commit()

    from services.sms_keyword_engine import update_delivery_status

    update_delivery_status("SMFWD1", "delivered")
    db.session.commit()
    db.session.refresh(recipient)
    assert recipient.status == "delivered"


# --------------------------------------------------------------------------
# TwilioMessage via the live /twilio/sms/status route
# --------------------------------------------------------------------------

def test_route_drops_late_sent_callback_after_delivered(app, client, tenant_user, monkeypatch):
    monkeypatch.setattr("twilio_sms._validate_twilio_signature", lambda *a, **k: True)
    _, company = tenant_user
    conv = TwilioConversation(company_id=company.id, from_number="+15551230000", to_number="+15559999999")
    db.session.add(conv)
    db.session.flush()
    msg = TwilioMessage(
        conversation_id=conv.id, company_id=company.id, twilio_sid="SMROUTE1",
        direction="outbound", from_number="+15559999999", to_number="+15551230000",
        body="hi", status="sent",
    )
    db.session.add(msg)
    db.session.commit()

    resp = client.post("/twilio/sms/status", data={"MessageSid": "SMROUTE1", "MessageStatus": "delivered"})
    assert resp.status_code == 204
    db.session.refresh(msg)
    assert msg.status == "delivered"

    # Out-of-order straggler: an earlier "sent" callback lands after "delivered".
    resp = client.post("/twilio/sms/status", data={"MessageSid": "SMROUTE1", "MessageStatus": "sent"})
    assert resp.status_code == 204
    db.session.refresh(msg)
    assert msg.status == "delivered"
    assert msg.error_code is None
    assert msg.error_message is None


def test_route_forward_transition_to_failed_with_error_still_applies(app, client, tenant_user, monkeypatch):
    monkeypatch.setattr("twilio_sms._validate_twilio_signature", lambda *a, **k: True)
    _, company = tenant_user
    conv = TwilioConversation(company_id=company.id, from_number="+15551230001", to_number="+15559999999")
    db.session.add(conv)
    db.session.flush()
    msg = TwilioMessage(
        conversation_id=conv.id, company_id=company.id, twilio_sid="SMROUTE2",
        direction="outbound", from_number="+15559999999", to_number="+15551230001",
        body="hi", status="sent",
    )
    db.session.add(msg)
    db.session.commit()

    resp = client.post("/twilio/sms/status", data={
        "MessageSid": "SMROUTE2", "MessageStatus": "undelivered", "ErrorCode": "30003",
    })
    assert resp.status_code == 204
    db.session.refresh(msg)
    assert msg.status == "undelivered"
    assert msg.error_code == "30003"


# --------------------------------------------------------------------------
# PromotionalOptInSolicitation (services.promotional_optin.sync_solicitation_delivery_status)
# --------------------------------------------------------------------------

def test_solicitation_late_sent_after_delivered_does_not_regress(app, tenant_user):
    _, company = tenant_user
    contact = Contact(company_id=company.id, phone="+15551230002", normalized_phone="+15551230002", is_active=True)
    db.session.add(contact)
    db.session.flush()
    row = PromotionalOptInSolicitation(
        company_id=company.id, contact_id=contact.id, canonical_phone="+15551230002",
        solicitation_message_sid="SMPROMO1", delivery_status="delivered",
    )
    db.session.add(row)
    db.session.commit()

    from services.promotional_optin import sync_solicitation_delivery_status

    applied = sync_solicitation_delivery_status("SMPROMO1", "sent")
    db.session.commit()
    db.session.refresh(row)
    assert applied == 0
    assert row.delivery_status == "delivered"


def test_solicitation_delivered_to_failed_lateral_correction_still_applies(app, tenant_user):
    _, company = tenant_user
    contact = Contact(company_id=company.id, phone="+15551230003", normalized_phone="+15551230003", is_active=True)
    db.session.add(contact)
    db.session.flush()
    row = PromotionalOptInSolicitation(
        company_id=company.id, contact_id=contact.id, canonical_phone="+15551230003",
        solicitation_message_sid="SMPROMO2", delivery_status="delivered",
    )
    db.session.add(row)
    db.session.commit()

    from services.promotional_optin import sync_solicitation_delivery_status

    applied = sync_solicitation_delivery_status("SMPROMO2", "failed", "30008 unknown error")
    db.session.commit()
    db.session.refresh(row)
    assert applied == 1
    assert row.delivery_status == "failed"
