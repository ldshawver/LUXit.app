import pytest

from app import create_app
from extensions import db
from models import Company, Contact, SMSCampaign, SMSRecipient, User, UserCompanyAccess
from services.sms_service import SMSService


@pytest.fixture
def app_client():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company = Company(name="Double Send Tenant")
        db.session.add(company)
        db.session.flush()
        user = User(username="double-admin", email="double-admin@example.com", is_admin=True, default_company_id=company.id, password_hash="test-hash")
        db.session.add(user)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="admin", is_default=True))
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield app, client, company
        db.session.remove()
        db.drop_all()


def _campaign_with_recipients(company, count):
    campaign = SMSService.create_campaign("Double Send", "Hello", company_id=company.id)
    contacts = []
    for i in range(count):
        contact = Contact(company_id=company.id, phone=f"+1555000{i:04d}", sms_marketing_opt_in=True, sms_consent_status="opted_in")
        contacts.append(contact)
    db.session.add_all(contacts)
    db.session.flush()
    SMSService.add_recipients(campaign.id, [c.id for c in contacts])
    return campaign


def test_two_send_attempts_do_not_duplicate_recipient_sends(app_client, monkeypatch):
    app, client, company = app_client
    sent_to = []
    with app.app_context():
        campaign = _campaign_with_recipients(company, 2)

        def fake_send(to_number, message, company_id=None):
            sent_to.append(to_number)
            return {"success": True, "message_sid": f"SM{len(sent_to)}", "status": "queued"}

        monkeypatch.setattr(SMSService, "send_sms", staticmethod(fake_send))
        first = client.post(f"/sms/campaign/{campaign.id}/send", follow_redirects=False)
        second = client.post(f"/sms/campaign/{campaign.id}/send", follow_redirects=False)

        assert first.status_code in (302, 303)
        assert second.status_code == 409
        assert len(sent_to) == 2
        assert SMSRecipient.query.filter_by(campaign_id=campaign.id).count() == 2
        assert SMSRecipient.query.filter_by(campaign_id=campaign.id, status="sent").count() == 2


def test_large_campaign_route_queues_without_looping_recipients(app_client, monkeypatch):
    app, client, company = app_client
    with app.app_context():
        campaign = _campaign_with_recipients(company, SMSService.ASYNC_RECIPIENT_THRESHOLD + 1)
        called = {}

        def fake_queue(campaign_id, app=None):
            called["campaign_id"] = campaign_id
            return {"success": True, "queued": True, "campaign_id": campaign_id}

        def fail_send(*args, **kwargs):
            raise AssertionError("send_campaign should not run inside large request")

        monkeypatch.setattr(SMSService, "queue_campaign_send", staticmethod(fake_queue))
        monkeypatch.setattr(SMSService, "send_campaign", staticmethod(fail_send))
        response = client.post(f"/sms/campaign/{campaign.id}/send", follow_redirects=False)

        assert response.status_code in (302, 303)
        assert called["campaign_id"] == campaign.id
        assert SMSRecipient.query.filter_by(campaign_id=campaign.id, status="pending").count() == SMSService.ASYNC_RECIPIENT_THRESHOLD + 1


def test_queued_campaign_cannot_be_queued_twice_server_side(app_client):
    app, _, company = app_client
    with app.app_context():
        campaign = _campaign_with_recipients(company, SMSService.ASYNC_RECIPIENT_THRESHOLD + 1)

        _, first = SMSService.begin_send(campaign.id, queued=True)
        _, second = SMSService.begin_send(campaign.id, queued=True)

        assert first["success"] is True
        assert second["success"] is False
        assert second["status_code"] == 409
        assert SMSRecipient.query.filter_by(campaign_id=campaign.id).count() == SMSService.ASYNC_RECIPIENT_THRESHOLD + 1


@pytest.mark.parametrize("status", ["queued", "sending", "processing", "scheduled", "sent", "completed", "failed", "canceled", "cancelled", "archived"])
def test_protected_campaign_statuses_reject_begin_send(app_client, status):
    app, _, company = app_client
    with app.app_context():
        campaign = _campaign_with_recipients(company, 1)
        campaign.status = status
        db.session.commit()

        _, result = SMSService.begin_send(campaign.id)

        assert result["success"] is False
        assert result["status_code"] == 409
        assert SMSRecipient.query.filter_by(campaign_id=campaign.id).count() == 1
