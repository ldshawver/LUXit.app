from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    AutoReplyRule,
    BusinessHours,
    Campaign,
    Company,
    Contact,
    SocialPost,
    SMSCampaign,
    SMSRecipient,
    TwilioAccount,
    TwilioConversation,
    TwilioMessage,
    User,
    UserCompanyAccess,
)
from services.sms_service import SMSService


@pytest.fixture
def app_client():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company = Company(name="SMS QA Tenant")
        db.session.add(company)
        db.session.flush()
        user = User(
            username="sms-admin",
            email="sms-admin@example.com",
            password_hash=generate_password_hash("password"),
            is_admin=True,
            default_company_id=company.id,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="admin", is_default=True))
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield app, client, company, user
        db.session.remove()
        db.drop_all()


def test_marketing_pages_render_without_500(app_client):
    _, client, _, _ = app_client
    for url in ("/sms/campaigns", "/app/sms-campaigns", "/campaigns", "/app/campaigns", "/social-media", "/app/social"):
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code, response.get_data(as_text=True)[:500])
        assert b"Internal Server Error" not in response.data


def test_campaign_creation_excludes_opted_out_and_unknown_contacts(app_client):
    app, client, company, _ = app_client
    with app.app_context():
        opted_in = Contact(
            company_id=company.id,
            first_name="Opt",
            phone="+15550000001",
            sms_marketing_opt_in=True,
            sms_marketing_opt_in_at=datetime.utcnow(),
            sms_marketing_opt_in_source="pytest",
            sms_consent_status="opted_in",
        )
        opted_out = Contact(
            company_id=company.id,
            first_name="Out",
            phone="+15550000002",
            sms_marketing_opt_in=True,
            sms_opt_out_at=datetime.utcnow(),
            sms_consent_status="opted_in",
        )
        unknown = Contact(company_id=company.id, first_name="Unknown", phone="+15550000003")
        db.session.add_all([opted_in, opted_out, unknown])
        db.session.commit()

    response = client.post(
        "/sms/create",
        data={"name": "Compliance QA", "message": "Spring offer", "send_option": "draft"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with app.app_context():
        campaign = SMSCampaign.query.filter_by(name="Compliance QA").one()
        recipients = SMSRecipient.query.filter_by(campaign_id=campaign.id).all()
        assert campaign.company_id == company.id
        assert campaign.message.endswith("Reply STOP to opt out.")
        assert [r.phone_number for r in recipients] == ["+15550000001"]


def test_campaign_send_with_mocked_twilio_continues_on_recipient_failure(app_client, monkeypatch):
    app, _, company, _ = app_client
    with app.app_context():
        campaign = SMSService.create_campaign("Mocked Send", "Hello opted in", company_id=company.id)
        good = Contact(company_id=company.id, phone="+15550000011", sms_marketing_opt_in=True, sms_consent_status="opted_in")
        bad = Contact(company_id=company.id, phone="+15550000012", sms_marketing_opt_in=True, sms_consent_status="opted_in")
        db.session.add_all([good, bad])
        db.session.flush()
        SMSService.add_recipients(campaign.id, [good.id, bad.id])

        def fake_send(to_number, message, company_id=None):
            if to_number.endswith("12"):
                return {"success": False, "error": "mock Twilio failure"}
            return {"success": True, "message_sid": "SM123", "status": "queued"}

        monkeypatch.setattr(SMSService, "send_sms", staticmethod(fake_send))
        result = SMSService.send_campaign(campaign.id)
        assert result == {"success": True, "sent": 1, "failed": 1, "total": 2}
        statuses = {r.phone_number: r.status for r in SMSRecipient.query.filter_by(campaign_id=campaign.id).all()}
        assert statuses["+15550000011"] == "sent"
        assert statuses["+15550000012"] == "failed"


def test_after_hours_custom_text_timezone_disabled_and_cooldown(app_client, monkeypatch):
    app, _, company, _ = app_client
    import twilio_sms

    with app.app_context():
        ta = TwilioAccount(
            company_id=company.id,
            automation_enabled=True,
            after_hours_sms_enabled=True,
            after_hours_text="Custom tenant closed text. Reply STOP to opt out.",
            after_hours_cooldown_minutes=60,
        )
        conv = TwilioConversation(company_id=company.id, from_number="+15551230000", to_number="+15557650000")
        rule = AutoReplyRule(company_id=company.id, name="After Hours", trigger_type="after_hours", response=None, is_active=True, action="reply")
        bh = BusinessHours(company_id=company.id, day_of_week=0, is_open=True, open_time="09:00", close_time="17:00", timezone="America/New_York")
        db.session.add_all([ta, conv, rule, bh])
        db.session.commit()

        sent = []
        monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda company_id: False)
        monkeypatch.setattr(twilio_sms, "_send_sms", lambda ta, to, body, **kw: sent.append(body) or {"success": True, "sid": "SM-AH"})

        assert twilio_sms._apply_auto_reply_rules(conv, "hello", ta) is True
        assert sent == ["Custom tenant closed text. Reply STOP to opt out."]

        db.session.add(TwilioMessage(
            conversation_id=conv.id,
            company_id=company.id,
            direction="outbound",
            from_number="+15557650000",
            to_number="+15551230000",
            body=sent[0],
            is_auto_reply=True,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        ))
        db.session.commit()
        assert twilio_sms._apply_auto_reply_rules(conv, "again", ta) is False

        ta.after_hours_sms_enabled = False
        db.session.commit()
        assert twilio_sms._apply_auto_reply_rules(conv, "disabled", ta) is False


def test_marketing_pages_forbid_viewer_role(app_client):
    app, _, company, _ = app_client
    with app.app_context():
        viewer = User(
            username="sms-viewer",
            email="sms-viewer@example.com",
            password_hash=generate_password_hash("password"),
            is_admin=False,
            default_company_id=company.id,
        )
        db.session.add(viewer)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=viewer.id, company_id=company.id, role="viewer", is_default=True))
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(viewer.id)
            sess["_fresh"] = True
        for url in ("/sms/campaigns", "/campaigns", "/social-media"):
            response = client.get(url)
            assert response.status_code == 403
            assert b"Internal Server Error" not in response.data


def test_marketing_pages_allow_management_roles(app_client):
    app, _, company, _ = app_client
    for role in ("owner", "admin", "manager", "editor"):
        with app.app_context():
            user = User(
                username=f"sms-role-{role}",
                email=f"sms-role-{role}@example.com",
                password_hash=generate_password_hash("password"),
                is_admin=False,
                default_company_id=company.id,
            )
            db.session.add(user)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role=role, is_default=True))
            db.session.commit()
            client = app.test_client()
            with client.session_transaction() as sess:
                sess["_user_id"] = str(user.id)
                sess["_fresh"] = True
            for url in ("/sms/campaigns", "/campaigns", "/social-media"):
                assert client.get(url).status_code == 200


def test_marketing_pages_redirect_unauthenticated(app_client):
    app, _, _, _ = app_client
    client = app.test_client()
    for url in ("/sms/campaigns", "/campaigns", "/social-media"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code in (302, 401)
        assert b"Internal Server Error" not in response.data


def test_tenant_isolation_for_campaign_sms_and_social_pages(app_client):
    app, client_a, company_a, _ = app_client
    with app.app_context():
        company_b = Company(name="SMS QA Other Tenant")
        db.session.add(company_b)
        db.session.flush()
        db.session.add(Campaign(company_id=company_a.id, name="Tenant A Email", subject="A", status="draft"))
        db.session.add(Campaign(company_id=company_b.id, name="Tenant B Email", subject="B", status="draft"))
        db.session.add(SMSCampaign(company_id=company_a.id, name="Tenant A SMS", message="A", status="draft"))
        db.session.add(SMSCampaign(company_id=company_b.id, name="Tenant B SMS", message="B", status="draft"))
        db.session.add(SocialPost(company_id=company_a.id, platform="facebook", content="Tenant A Social", status="draft"))
        db.session.add(SocialPost(company_id=company_b.id, platform="facebook", content="Tenant B Social", status="draft"))
        db.session.commit()

    checks = (
        ("/campaigns", "Tenant A Email", "Tenant B Email"),
        ("/sms/campaigns", "Tenant A SMS", "Tenant B SMS"),
        ("/social-media", "Tenant A Social", "Tenant B Social"),
    )
    for url, visible, hidden in checks:
        body = client_a.get(url).get_data(as_text=True)
        assert visible in body
        assert hidden not in body


def test_sms_recipient_add_rejects_cross_tenant_contacts(app_client):
    app, _, company_a, _ = app_client
    with app.app_context():
        company_b = Company(name="Recipient Other Tenant")
        db.session.add(company_b)
        db.session.flush()
        campaign = SMSService.create_campaign("Tenant A Campaign", "Hello", company_id=company_a.id)
        contact_a = Contact(company_id=company_a.id, phone="+15550000101", sms_marketing_opt_in=True, sms_consent_status="opted_in")
        contact_b = Contact(company_id=company_b.id, phone="+15550000102", sms_marketing_opt_in=True, sms_consent_status="opted_in")
        db.session.add_all([contact_a, contact_b])
        db.session.flush()
        SMSService.add_recipients(campaign.id, [contact_a.id, contact_b.id])
        recipients = SMSRecipient.query.filter_by(campaign_id=campaign.id).all()
        assert [r.phone_number for r in recipients] == ["+15550000101"]


def test_bulk_sms_prefers_tenant_twilio_config(app_client, monkeypatch):
    app, _, company, _ = app_client
    created = {}

    class FakeMessages:
        def create(self, **kwargs):
            created.update(kwargs)
            return type("Msg", (), {"sid": "SMTENANT", "status": "queued"})()

    class FakeClient:
        def __init__(self, sid, token):
            self.sid = sid
            self.token = token
            self.messages = FakeMessages()

    with app.app_context():
        ta = TwilioAccount(company_id=company.id, messaging_service_sid="MG_TENANT", from_phone="+15559990000", is_active=True)
        ta.set_account_sid("AC_TENANT")
        ta.set_auth_token("token")
        db.session.add(ta)
        db.session.commit()
        monkeypatch.setattr("services.sms_service.Client", FakeClient)
        result = SMSService.send_sms("+15550000123", "Tenant send", company_id=company.id)
        assert result["success"] is True
        assert created["messaging_service_sid"] == "MG_TENANT"
        assert "from_" not in created
        assert created["body"].endswith("Reply STOP to opt out.")
