from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import Company, Contact, IntegrationAuditLog, SMSCampaign, SMSRecipient
from services.sms_service import SMSService
import twilio_sms


@pytest.fixture
def app_ctx():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company_a = Company(name="Keyword Tenant A")
        company_b = Company(name="Keyword Tenant B")
        db.session.add_all([company_a, company_b])
        db.session.flush()
        contact_a = Contact(
            company_id=company_a.id,
            phone="+12025550101",
            normalized_phone="+12025550101",
            sms_marketing_opt_in=True,
            sms_consent_status="opted_in",
            sms_marketing_opt_in_at=datetime.utcnow(),
        )
        contact_b = Contact(
            company_id=company_b.id,
            phone="+12025550101",
            normalized_phone="+12025550101",
            sms_marketing_opt_in=True,
            sms_consent_status="opted_in",
            sms_marketing_opt_in_at=datetime.utcnow(),
        )
        db.session.add_all([contact_a, contact_b])
        db.session.commit()
        yield app, company_a, company_b, contact_a, contact_b
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("keyword", ["STOP", " stopall ", "Unsubscribe", "CANCEL", "end", "Quit"])
def test_stop_variants_opt_out_only_current_tenant(app_ctx, keyword):
    _, company_a, company_b, contact_a, contact_b = app_ctx
    normalized = twilio_sms._normalized_keyword(keyword)
    assert normalized in twilio_sms._STOP_KEYWORDS

    changed = twilio_sms._update_contact_sms_consent(company_a.id, "+12025550101", False, f"keyword:{normalized}")
    twilio_sms._write_sms_compliance_audit(company_a.id, "opt_out", "+12025550101", normalized, changed.id, 123)
    db.session.commit()

    db.session.refresh(contact_a)
    db.session.refresh(contact_b)
    assert contact_a.sms_marketing_opt_in is False
    assert contact_a.sms_consent_status == "opted_out"
    assert contact_a.sms_opt_out_at is not None
    assert contact_b.sms_marketing_opt_in is True
    assert contact_b.sms_consent_status == "opted_in"
    audit = IntegrationAuditLog.query.filter_by(company_id=company_a.id, service_slug="sms_compliance", action="opt_out").one()
    assert audit.changes["keyword"] == normalized
    assert "auth_token" not in str(audit.changes).lower()


@pytest.mark.parametrize("keyword", ["START", " unstop ", "YES"])
def test_start_variants_restore_opt_in_only_current_tenant(app_ctx, keyword):
    _, company_a, company_b, contact_a, contact_b = app_ctx
    contact_a.sms_marketing_opt_in = False
    contact_a.sms_consent_status = "opted_out"
    contact_a.sms_opt_out_at = datetime.utcnow()
    db.session.commit()

    normalized = twilio_sms._normalized_keyword(keyword)
    assert normalized in twilio_sms._START_KEYWORDS
    changed = twilio_sms._update_contact_sms_consent(company_a.id, "+12025550101", True, f"keyword:{normalized}")
    twilio_sms._write_sms_compliance_audit(company_a.id, "opt_in", "+12025550101", normalized, changed.id, 123)
    db.session.commit()

    db.session.refresh(contact_a)
    db.session.refresh(contact_b)
    assert contact_a.sms_marketing_opt_in is True
    assert contact_a.sms_consent_status == "opted_in"
    assert contact_a.sms_opt_out_at is None
    assert contact_b.sms_marketing_opt_in is True


@pytest.mark.parametrize("keyword", ["HELP", " info "])
def test_help_info_audits_without_changing_consent(app_ctx, keyword):
    _, company_a, _, contact_a, _ = app_ctx
    before = (contact_a.sms_marketing_opt_in, contact_a.sms_consent_status, contact_a.sms_opt_out_at)
    normalized = twilio_sms._normalized_keyword(keyword)
    assert normalized in {"help", "info"}
    twilio_sms._write_sms_compliance_audit(company_a.id, "help", "+12025550101", normalized, contact_a.id, 456)
    db.session.commit()
    db.session.refresh(contact_a)
    after = (contact_a.sms_marketing_opt_in, contact_a.sms_consent_status, contact_a.sms_opt_out_at)
    assert after == before
    assert IntegrationAuditLog.query.filter_by(company_id=company_a.id, action="help").count() == 1


def test_future_campaign_excludes_contact_after_stop(app_ctx):
    _, company_a, _, contact_a, _ = app_ctx
    twilio_sms._update_contact_sms_consent(company_a.id, "+12025550101", False, "keyword:stop")
    campaign = SMSService.create_campaign("After STOP", "Do not send", company_id=company_a.id)
    db.session.flush()
    SMSService.add_recipients(campaign.id, [contact_a.id])
    assert SMSRecipient.query.filter_by(campaign_id=campaign.id).count() == 0
