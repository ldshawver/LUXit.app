import os
import pytest

from app import create_app
from extensions import db
from models import (
    Company, Contact, MarketingAuditLog, SMSCampaign, SMSKeywordRule, SMSRecipient,
    SocialPost, TwilioAccount, TwilioConversation, User, user_company,
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
def tenant_user(app, client):
    company = Company(name="Marketing Test Co")
    user = User(username="marketing-user", email="marketing@example.com", default_company_id=None, is_admin=True)
    user.password_hash = "testpass"
    db.session.add_all([company, user])
    db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return user, company


def test_campaign_sms_and_social_pages_render_without_500(client, tenant_user):
    for path in ("/campaigns", "/sms/campaigns", "/social-media"):
        response = client.get(path)
        assert response.status_code == 200, path


def test_sms_campaign_create_save_and_preview_excludes_unsubscribed(client, tenant_user):
    _, company = tenant_user
    db.session.add_all([
        Contact(company_id=company.id, first_name="Opt", last_name="In", phone="+15550000001", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip"),
        Contact(company_id=company.id, first_name="Opt", last_name="Out", phone="+15550000002", tags="sms_opt_out", is_active=True, is_subscribed=False, segment="vip"),
    ])
    db.session.commit()

    create = client.post("/api/marketing/sms-campaigns", json={"name": "VIP Drop", "message": "Book your concierge appointment", "segment": "vip"})
    assert create.status_code == 201
    campaign = create.get_json()["campaign"]
    assert "STOP" in campaign["message"]

    update = client.put(f"/api/marketing/sms-campaigns/{campaign['id']}", json={"objective": "Drive bookings", "status": "draft"})
    assert update.status_code == 200

    preview = client.post(f"/api/marketing/sms-campaigns/{campaign['id']}/preview")
    payload = preview.get_json()
    assert preview.status_code == 200
    assert payload["recipients_selected"] == 1
    assert payload["excluded"] == 1


def test_sms_send_refuses_missing_twilio_config(client, tenant_user, monkeypatch):
    _, company = tenant_user
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(key, raising=False)
    db.session.add(Contact(company_id=company.id, phone="+15550000001", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip"))
    campaign = SMSCampaign(company_id=company.id, name="No Twilio", message="Hello Reply STOP to opt out.", segment="vip", status="draft")
    db.session.add(campaign)
    db.session.commit()

    response = client.post(f"/api/marketing/sms-campaigns/{campaign.id}/send")
    assert response.status_code == 409
    assert "TWILIO" in response.get_json()["error"]


def test_sms_send_creates_recipient_records_when_configured(client, tenant_user, monkeypatch):
    _, company = tenant_user
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559999999")
    db.session.add(Contact(company_id=company.id, phone="+15550000003", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip"))
    campaign = SMSCampaign(company_id=company.id, name="Configured", message="Hello Reply STOP to opt out.", segment="vip", status="draft")
    db.session.add(campaign)
    db.session.commit()

    response = client.post(f"/api/marketing/sms-campaigns/{campaign.id}/send")
    assert response.status_code == 200
    assert SMSRecipient.query.filter_by(campaign_id=campaign.id, status="sent").count() == 1


def test_tenant_a_cannot_see_tenant_b_sms_campaigns(client, tenant_user):
    _, company_a = tenant_user
    company_b = Company(name="Other Tenant")
    db.session.add(company_b)
    db.session.flush()
    db.session.add_all([
        SMSCampaign(company_id=company_a.id, name="Visible", message="A", status="draft"),
        SMSCampaign(company_id=company_b.id, name="Hidden", message="B", status="draft"),
    ])
    db.session.commit()

    response = client.get("/api/marketing/sms-campaigns")
    names = [c["name"] for c in response.get_json()["campaigns"]]
    assert names == ["Visible"]


def test_social_page_and_api_are_graceful_without_integrations(client, tenant_user, monkeypatch):
    for key in ("TWITTER_BEARER_TOKEN", "FACEBOOK_ACCESS_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "TIKTOK_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    assert client.get("/social-media").status_code == 200
    response = client.get("/api/marketing/social-campaigns")
    assert response.status_code == 200
    assert response.get_json()["connected"] is False


def test_ai_endpoints_return_structured_output(client, tenant_user):
    for path in ("generate-campaign", "rewrite-sms", "generate-keyword-flow", "suggest-segment", "compliance-check"):
        response = client.post(f"/api/marketing/ai/{path}", json={"objective": "bookings", "message": "VIP offer"})
        data = response.get_json()
        assert response.status_code == 200
        assert data["success"] is True
        assert data["variants"]
        assert data["compliance_warnings"]



def _twilio_account(company):
    account = TwilioAccount(company_id=company.id, from_phone="+15559999999", is_active=True)
    account.set_account_sid("ACtest")
    account.set_auth_token("token")
    db.session.add(account)
    db.session.commit()
    return account


def test_twilio_delivery_webhook_updates_sms_campaign_recipient(client, tenant_user):
    _, company = tenant_user
    _twilio_account(company)
    recipient = SMSRecipient(company_id=company.id, provider_message_sid="SM123", status="sent")
    db.session.add(recipient)
    db.session.commit()

    response = client.post("/twilio/sms/status", data={"MessageSid": "SM123", "MessageStatus": "delivered"})
    assert response.status_code == 204
    updated = db.session.get(SMSRecipient, recipient.id)
    assert updated.status == "delivered"
    assert updated.delivered_at is not None


def test_inbound_stop_marks_contact_and_blocks_future_preview(client, tenant_user):
    _, company = tenant_user
    _twilio_account(company)
    contact = Contact(company_id=company.id, phone="+15550000009", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
    campaign = SMSCampaign(company_id=company.id, name="STOP Test", message="Offer Reply STOP to opt out.", segment="vip", status="sent")
    db.session.add_all([contact, campaign])
    db.session.flush()
    db.session.add(SMSRecipient(company_id=company.id, campaign_id=campaign.id, contact_id=contact.id, status="sent"))
    db.session.commit()

    response = client.post("/twilio/sms/inbound", data={"From": contact.phone, "To": "+15559999999", "Body": "STOP", "MessageSid": "SMSTOP"})
    assert response.status_code == 200
    db.session.refresh(contact)
    assert contact.is_subscribed is False
    assert "sms_opt_out" in (contact.tags or "")

    preview = client.post(f"/api/marketing/sms-campaigns/{campaign.id}/preview").get_json()
    assert preview["recipients_selected"] == 0


def test_keyword_rule_triggers_reply_tags_contact_and_audits(client, tenant_user):
    _, company = tenant_user
    _twilio_account(company)
    contact = Contact(company_id=company.id, phone="+15550000010", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
    rule = SMSKeywordRule(
        company_id=company.id,
        keyword="VIP",
        match_type="exact",
        reply_message="You are on the VIP list.",
        priority=1,
        tag_to_add="vip_keyword",
        segment_to_add="vip_keyword_segment",
        is_active=True,
    )
    db.session.add_all([contact, rule])
    db.session.commit()

    response = client.post("/twilio/sms/inbound", data={"From": contact.phone, "To": "+15559999999", "Body": "VIP", "MessageSid": "SMVIP"})
    assert response.status_code == 200
    db.session.refresh(contact)
    assert "vip_keyword" in (contact.tags or "")
    assert contact.segment == "vip_keyword_segment"
    assert MarketingAuditLog.query.filter_by(company_id=company.id, action="sms_keyword_triggered").count() == 1


def test_inbound_reply_attaches_to_latest_campaign_recipient(client, tenant_user):
    _, company = tenant_user
    _twilio_account(company)
    contact = Contact(company_id=company.id, phone="+15550000011", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
    campaign = SMSCampaign(company_id=company.id, name="Reply Test", message="Reply BOOK", segment="vip", status="sent")
    db.session.add_all([contact, campaign])
    db.session.flush()
    recipient = SMSRecipient(company_id=company.id, campaign_id=campaign.id, contact_id=contact.id, status="sent")
    db.session.add(recipient)
    db.session.commit()

    response = client.post("/twilio/sms/inbound", data={"From": contact.phone, "To": "+15559999999", "Body": "BOOK", "MessageSid": "SMBOOK"})
    assert response.status_code == 200
    db.session.refresh(recipient)
    assert recipient.status == "replied"
    assert recipient.replied_at is not None
    assert TwilioConversation.query.filter_by(company_id=company.id, from_number=contact.phone).first() is not None


def test_tenant_a_cannot_see_tenant_b_rules_posts_or_audit_logs(client, tenant_user):
    _, company_a = tenant_user
    company_b = Company(name="Tenant B")
    db.session.add(company_b)
    db.session.flush()
    db.session.add_all([
        SMSKeywordRule(company_id=company_a.id, keyword="A", reply_message="A", is_active=True),
        SMSKeywordRule(company_id=company_b.id, keyword="B", reply_message="B", is_active=True),
        SocialPost(company_id=company_a.id, content="A post", status="draft"),
        SocialPost(company_id=company_b.id, content="B post", status="draft"),
        MarketingAuditLog(company_id=company_a.id, action="a", entity_type="test"),
        MarketingAuditLog(company_id=company_b.id, action="b", entity_type="test"),
    ])
    db.session.commit()

    rules = client.get("/api/marketing/sms-keywords").get_json()["rules"]
    assert [r["keyword"] for r in rules] == ["A"]

    posts = client.get("/api/marketing/social-campaigns").get_json()["posts"]
    assert [p["content"] for p in posts] == ["A post"]

    visible_audits = MarketingAuditLog.query.filter_by(company_id=company_a.id).all()
    assert [a.action for a in visible_audits] == ["a"]


def test_tenant_a_inbound_does_not_trigger_tenant_b_keyword_or_campaign(client, tenant_user):
    _, company_a = tenant_user
    company_b = Company(name="Tenant B")
    db.session.add(company_b)
    db.session.flush()
    account_a = _twilio_account(company_a)
    account_b = TwilioAccount(company_id=company_b.id, from_phone="+15558888888", is_active=True)
    account_b.set_account_sid("ACtenantb")
    account_b.set_auth_token("tokenb")
    contact_a = Contact(company_id=company_a.id, phone="+15550000021", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
    contact_b = Contact(company_id=company_b.id, phone="+15550000021", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
    campaign_b = SMSCampaign(company_id=company_b.id, name="B", message="B", segment="vip", status="sent")
    rule_b = SMSKeywordRule(company_id=company_b.id, keyword="VIP", reply_message="B only", tag_to_add="tenant_b", is_active=True)
    db.session.add_all([account_b, contact_a, contact_b, campaign_b, rule_b])
    db.session.flush()
    recipient_b = SMSRecipient(company_id=company_b.id, campaign_id=campaign_b.id, contact_id=contact_b.id, status="sent")
    db.session.add(recipient_b)
    db.session.commit()

    response = client.post("/twilio/sms/inbound", data={"From": contact_a.phone, "To": account_a.from_phone, "Body": "VIP", "MessageSid": "SMTENANTA"})
    assert response.status_code == 200
    db.session.refresh(contact_b)
    db.session.refresh(recipient_b)
    assert "tenant_b" not in (contact_b.tags or "")
    assert recipient_b.status == "sent"
    assert MarketingAuditLog.query.filter_by(company_id=company_b.id, action="sms_keyword_triggered").count() == 0


def _twilio_signature(path, data, token="token"):
    from twilio.request_validator import RequestValidator
    return RequestValidator(token).compute_signature(f"https://luxit.app{path}", data)


def test_twilio_signature_validation_for_inbound_and_status(client, tenant_user, monkeypatch):
    _, company = tenant_user
    _twilio_account(company)
    monkeypatch.setenv("TWILIO_STRICT_SIGNATURE", "1")
    inbound_data = {"From": "+15550000031", "To": "+15559999999", "Body": "HELP", "MessageSid": "SMSIGIN"}
    bad = client.post("/twilio/sms/inbound", data=inbound_data)
    assert bad.status_code == 403
    good = client.post(
        "/twilio/sms/inbound",
        data=inbound_data,
        headers={"X-Twilio-Signature": _twilio_signature("/twilio/sms/inbound", inbound_data)},
    )
    assert good.status_code == 200

    recipient = SMSRecipient(company_id=company.id, provider_message_sid="SMSIGSTATUS", status="sent")
    db.session.add(recipient)
    db.session.commit()
    status_data = {"MessageSid": "SMSIGSTATUS", "MessageStatus": "delivered"}
    bad_status = client.post("/twilio/sms/status", data=status_data)
    assert bad_status.status_code == 403
    good_status = client.post(
        "/twilio/sms/status",
        data=status_data,
        headers={"X-Twilio-Signature": _twilio_signature("/twilio/sms/status", status_data)},
    )
    assert good_status.status_code == 204


def test_sms_campaign_send_is_idempotent_and_batches(client, tenant_user, monkeypatch):
    _, company = tenant_user
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACenv")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559999999")
    contacts = [
        Contact(company_id=company.id, phone=f"+15550010{i:03d}", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
        for i in range(3)
    ]
    campaign = SMSCampaign(company_id=company.id, name="Batch", message="Batch Reply STOP to opt out.", segment="vip", status="draft")
    db.session.add_all([*contacts, campaign])
    db.session.commit()

    first = client.post(f"/api/marketing/sms-campaigns/{campaign.id}/send", json={"batch_size": 2})
    assert first.status_code == 200
    db.session.refresh(campaign)
    assert campaign.status == "sending"
    assert SMSRecipient.query.filter_by(campaign_id=campaign.id, status="sent").count() == 2
    assert SMSRecipient.query.filter_by(campaign_id=campaign.id, status="queued").count() == 1

    second = client.post(f"/api/marketing/sms-campaigns/{campaign.id}/send", json={"batch_size": 2})
    assert second.status_code == 200
    db.session.refresh(campaign)
    assert campaign.status == "sent"
    assert SMSRecipient.query.filter_by(campaign_id=campaign.id, status="sent").count() == 3

    duplicate = client.post(f"/api/marketing/sms-campaigns/{campaign.id}/send", json={"batch_size": 2})
    assert duplicate.status_code == 409
    assert "duplicate send prevented" in duplicate.get_json()["error"]


def test_stop_start_help_compliance_keywords(client, tenant_user):
    _, company = tenant_user
    _twilio_account(company)
    contact = Contact(company_id=company.id, phone="+15550000041", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
    db.session.add(contact)
    db.session.commit()

    for keyword in ("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"):
        contact.is_subscribed = True
        contact.tags = "sms_consent"
        db.session.commit()
        response = client.post(
            "/twilio/sms/inbound",
            data={"From": contact.phone, "To": "+15559999999", "Body": keyword, "MessageSid": f"SM{keyword}A"},
        )
        assert response.status_code == 200
        assert b"unsubscribed" in response.data
        db.session.refresh(contact)
        assert contact.is_subscribed is False
        assert "sms_opt_out" in (contact.tags or "")

    response = client.post(
        "/twilio/sms/inbound",
        data={"From": contact.phone, "To": "+15559999999", "Body": "START", "MessageSid": "SMSTARTA"},
    )
    assert response.status_code == 200
    assert b"subscribed" in response.data
    db.session.refresh(contact)
    assert contact.is_subscribed is True
    assert "sms_consent" in (contact.tags or "")
    assert "sms_opt_out" not in (contact.tags or "")

    help_response = client.post(
        "/twilio/sms/inbound",
        data={"From": contact.phone, "To": "+15559999999", "Body": "HELP", "MessageSid": "SMHELPA"},
    )
    assert help_response.status_code == 200
    assert b"Reply STOP" in help_response.data


def test_delivery_status_replay_is_idempotent(client, tenant_user):
    _, company = tenant_user
    recipient = SMSRecipient(company_id=company.id, provider_message_sid="SMREPLAY", status="sent")
    db.session.add(recipient)
    db.session.commit()

    from services.sms_keyword_engine import update_delivery_status

    update_delivery_status("SMREPLAY", "delivered")
    db.session.commit()
    first_audit_count = MarketingAuditLog.query.filter_by(
        company_id=company.id,
        action="sms_delivery_status",
    ).count()
    delivered_at = recipient.delivered_at

    update_delivery_status("SMREPLAY", "delivered")
    db.session.commit()
    db.session.refresh(recipient)
    assert recipient.status == "delivered"
    assert recipient.delivered_at == delivered_at
    assert MarketingAuditLog.query.filter_by(company_id=company.id, action="sms_delivery_status").count() == first_audit_count


def test_campaign_statistics_reconcile_mixed_outcomes(client, tenant_user):
    _, company = tenant_user
    campaign = SMSCampaign(company_id=company.id, name="Stats", message="Stats Reply STOP", status="sent")
    db.session.add(campaign)
    db.session.flush()
    db.session.add_all([
        SMSRecipient(company_id=company.id, campaign_id=campaign.id, status="delivered"),
        SMSRecipient(company_id=company.id, campaign_id=campaign.id, status="failed"),
        SMSRecipient(company_id=company.id, campaign_id=campaign.id, status="opted_out"),
        SMSRecipient(company_id=company.id, campaign_id=campaign.id, status="queued"),
        SMSRecipient(company_id=company.id, campaign_id=campaign.id, status="sent"),
    ])
    db.session.commit()

    response = client.get(f"/api/marketing/sms-campaigns/{campaign.id}/analytics")
    assert response.status_code == 200
    analytics = response.get_json()["analytics"]
    assert analytics["recipients_selected"] == 5
    assert analytics["delivered"] == 1
    assert analytics["failed"] == 1
    assert analytics["opted_out"] == 1
    assert analytics["queued"] == 1
    assert analytics["sent"] == 1


def test_tenant_a_cannot_add_tenant_b_recipients_or_read_audits(client, tenant_user):
    _, company_a = tenant_user
    company_b = Company(name="Tenant B Audit")
    db.session.add(company_b)
    db.session.flush()
    contact_b = Contact(company_id=company_b.id, phone="+15550000051", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
    campaign_a = SMSCampaign(company_id=company_a.id, name="A campaign", message="A Reply STOP", segment="vip", status="draft")
    audit_b = MarketingAuditLog(company_id=company_b.id, action="tenant_b_secret", entity_type="sms_campaign", entity_id=999, details={"hidden": True})
    db.session.add_all([contact_b, campaign_a, audit_b])
    db.session.commit()

    preview = client.post(f"/api/marketing/sms-campaigns/{campaign_a.id}/preview")
    assert preview.status_code == 200
    assert preview.get_json()["recipients_selected"] == 0
    assert SMSRecipient.query.filter_by(campaign_id=campaign_a.id, contact_id=contact_b.id).count() == 0

    audits = client.get("/api/marketing/audit-logs")
    assert audits.status_code == 200
    assert [row["action"] for row in audits.get_json()["audit_logs"]] == []


def test_status_callback_cannot_spoof_other_tenant_message_sid(client, tenant_user, monkeypatch):
    _, company_a = tenant_user
    _twilio_account(company_a)
    company_b = Company(name="Tenant B Sid")
    db.session.add(company_b)
    db.session.flush()
    account_b = TwilioAccount(company_id=company_b.id, from_phone="+15558888888", is_active=True)
    account_b.set_account_sid("ACtenantb")
    account_b.set_auth_token("tokenb")
    recipient_b = SMSRecipient(company_id=company_b.id, provider_message_sid="SMTENANTB", status="sent")
    db.session.add_all([account_b, recipient_b])
    db.session.commit()
    monkeypatch.setenv("TWILIO_STRICT_SIGNATURE", "1")
    status_data = {"MessageSid": "SMTENANTB", "MessageStatus": "delivered"}

    spoofed = client.post(
        "/twilio/sms/status",
        data=status_data,
        headers={"X-Twilio-Signature": _twilio_signature("/twilio/sms/status", status_data, token="token")},
    )
    assert spoofed.status_code == 403
    db.session.refresh(recipient_b)
    assert recipient_b.status == "sent"

    valid = client.post(
        "/twilio/sms/status",
        data=status_data,
        headers={"X-Twilio-Signature": _twilio_signature("/twilio/sms/status", status_data, token="tokenb")},
    )
    assert valid.status_code == 204
    db.session.refresh(recipient_b)
    assert recipient_b.status == "delivered"


def test_ten_simultaneous_send_requests_no_duplicate_recipient_sends(app, tenant_user, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        pytest.skip("Concurrent send proof requires a database with row-level locks; run with TEST_DATABASE_URL=postgresql://...")

    user, company = tenant_user
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACenv")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559999999")
    contacts = [
        Contact(company_id=company.id, phone=f"+15550020{i:03d}", tags="sms_consent", is_active=True, is_subscribed=True, segment="vip")
        for i in range(3)
    ]
    campaign = SMSCampaign(company_id=company.id, name="Concurrent", message="Concurrent Reply STOP", segment="vip", status="draft")
    db.session.add_all([*contacts, campaign])
    db.session.commit()
    campaign_id = campaign.id
    user_id = user.id

    def send_once():
        with app.test_client() as thread_client:
            with thread_client.session_transaction() as sess:
                sess["_user_id"] = str(user_id)
                sess["_fresh"] = True
            return thread_client.post(f"/api/marketing/sms-campaigns/{campaign_id}/send", json={"batch_size": 1}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(lambda _: send_once(), range(10)))

    assert statuses.count(200) == 3
    assert statuses.count(409) == 7
    recipients = SMSRecipient.query.filter_by(campaign_id=campaign_id).all()
    assert len(recipients) == 3
    assert len({r.contact_id for r in recipients}) == 3
    assert all(r.status == "sent" for r in recipients)
