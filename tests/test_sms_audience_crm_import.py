import os
import pytest

from app import create_app
from extensions import db
from models import Company, Contact, SegmentMember, SMSCampaign, SMSRecipient, User, user_company
from services.contact_audience import (
    build_sms_recipient_snapshot,
    import_contacts,
    preview_contact_import,
    resolve_segment_contacts,
    upsert_contact_from_source,
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
def tenant_user(app):
    company = Company(name="SMS Audience CRM Test Co")
    user = User(username="sms-audience-user", email="sms-audience@example.com", default_company_id=None, is_admin=True)
    user.password_hash = "testpass"
    db.session.add_all([company, user])
    db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))
    db.session.commit()
    return user, company


def test_sms_source_phone_and_myorder_tag_for_inbound_sources(app, tenant_user):
    _, company = tenant_user
    contact = upsert_contact_from_source(
        company.id,
        phone="5550001000",
        source_channel="sms",
        source_phone_number="9165989519",
        source_provider="twilio",
        source_context="auto_reply",
    )
    db.session.commit()

    assert contact.normalized_phone == "+15550001000"
    assert contact.source_phone_number == "+19165989519"
    assert "MyOrder Customer" in (contact.tags or "")


def test_segment_by_source_phone_and_tag_returns_tenant_contacts(app, tenant_user):
    _, company = tenant_user
    other_company_id = company.id + 999
    mine = upsert_contact_from_source(company.id, phone="5550001000", source_phone_number="9165989519", tags=["sms_consent"])
    upsert_contact_from_source(other_company_id, phone="5550001000", source_phone_number="9165989519", tags=["sms_consent"])
    db.session.commit()

    by_source = resolve_segment_contacts(company.id, audience_filter={"conditions": [{"field": "source_phone_number", "value": "+19165989519"}]})
    by_tag = resolve_segment_contacts(company.id, audience_filter={"conditions": [{"field": "tag", "value": "MyOrder Customer"}]})

    assert [c.id for c in by_source] == [mine.id]
    assert [c.id for c in by_tag] == [mine.id]


def test_campaign_snapshot_dedupes_and_excludes_optouts(app, tenant_user):
    _, company = tenant_user
    c1 = Contact(company_id=company.id, phone="+15550001000", normalized_phone="+15550001000", tags="vip,sms_consent", is_active=True, is_subscribed=True, segment="vip")
    c2 = Contact(company_id=company.id, phone="(555) 000-1000", normalized_phone="+15550001000", tags="vip,sms_consent", is_active=True, is_subscribed=True, segment="vip")
    c3 = Contact(company_id=company.id, phone="+19165989519", tags="vip,sms_opt_out", is_active=True, is_subscribed=True, segment="vip")
    campaign = SMSCampaign(company_id=company.id, name="VIP", message="Hi", segment="vip")
    db.session.add_all([c1, c2, c3, campaign]); db.session.commit()

    counts = build_sms_recipient_snapshot(campaign)
    db.session.commit()

    assert counts["total_matched"] == 3
    assert counts["duplicates_removed"] == 1
    assert counts["opt_outs_removed"] == 1
    assert counts["final_recipients"] == 1
    assert SMSRecipient.query.filter_by(campaign_id=campaign.id).count() == 1


def test_import_preview_and_apply_tags_list_preserve_stop(app, tenant_user):
    _, company = tenant_user
    existing = Contact(company_id=company.id, phone="+15550001000", normalized_phone="+15550001000", sms_opted_out=True, do_not_sms=True, is_active=True)
    db.session.add(existing); db.session.commit()
    csv_bytes = b"First Name,Email Address,Mobile Phone,Tags\nLuke,luke@example.com,5550001000,ios\nJane,jane@example.com,9165989519,mailchimp\n"

    preview = preview_contact_import(csv_bytes, "contacts.csv")
    assert preview["detected_mapping"]["first_name"] == "First Name"
    assert preview["detected_mapping"]["email"] == "Email Address"
    assert preview["detected_mapping"]["phone"] == "Mobile Phone"

    result = import_contacts(
        company.id,
        csv_bytes,
        "contacts.csv",
        source_provider="ios_contacts",
        imported_list="Summer Leads",
        apply_tags=["Imported"],
        sms_subscribed=True,
    )
    db.session.commit()

    assert result["success_count"] == 2
    db.session.refresh(existing)
    assert existing.sms_opted_out is True
    assert existing.sms_marketing_opt_in is not True
    assert SegmentMember.query.count() == 2
    assert Contact.query.filter(Contact.tags.ilike("%Imported%")).count() == 2
