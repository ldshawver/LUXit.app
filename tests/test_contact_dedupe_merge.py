import os
import pytest

from app import create_app
from extensions import db
from models import (
    Campaign, CampaignRecipient, Company, Contact, IntegrationAuditLog,
    Segment, SegmentMember, SMSCampaign, SMSRecipient, TwilioConversation, User, user_company,
)
from services.contact_dedupe import find_duplicate_contacts, merge_contacts


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
    company = Company(name="Merge Test Co")
    user = User(username="merge-user", email="merge@example.com", default_company_id=None, is_admin=True)
    user.password_hash = "testpass"
    db.session.add_all([company, user]); db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return user, company


def test_find_and_merge_duplicates_by_phone(tenant):
    user, company = tenant
    primary = Contact(company_id=company.id, phone="+15550001000", normalized_phone="+15550001000", first_name="Primary", is_active=True)
    dupe = Contact(company_id=company.id, phone="(555) 000-1000", normalized_phone="+15550001000", last_name="Phone", is_active=True)
    db.session.add_all([primary, dupe]); db.session.commit()

    groups = find_duplicate_contacts(company.id)
    assert any(set(g["contact_ids"]) == {primary.id, dupe.id} for g in groups)
    result = merge_contacts(primary.id, [dupe.id], actor_user_id=user.id, company_id=company.id)

    db.session.refresh(primary); db.session.refresh(dupe)
    assert primary.last_name == "Phone"
    assert dupe.is_active is False
    assert result["surviving_contact_id"] == primary.id


def test_find_and_merge_duplicates_by_email_and_name_company(tenant):
    _, company = tenant
    by_email_1 = Contact(company_id=company.id, email="VIP@Example.com", is_active=True)
    by_email_2 = Contact(company_id=company.id, email="vip@example.com", first_name="Email", is_active=True)
    by_name_1 = Contact(company_id=company.id, name="Jane Buyer", company="Acme", is_active=True)
    by_name_2 = Contact(company_id=company.id, first_name="Jane", last_name="Buyer", company="Acme", tags="name-dupe", is_active=True)
    db.session.add_all([by_email_1, by_email_2, by_name_1, by_name_2]); db.session.commit()

    groups = [set(g["contact_ids"]) for g in find_duplicate_contacts(company.id)]
    assert {by_email_1.id, by_email_2.id} in groups
    assert {by_name_1.id, by_name_2.id} in groups

    merge_contacts(by_email_1.id, [by_email_2.id], company_id=company.id)
    merge_contacts(by_name_1.id, [by_name_2.id], company_id=company.id)
    db.session.refresh(by_email_1); db.session.refresh(by_name_1)
    assert by_email_1.first_name == "Email"
    assert "name-dupe" in by_name_1.tags


def test_merge_tags_lists_and_history_references(tenant):
    user, company = tenant
    primary = Contact(company_id=company.id, email="a@example.com", tags="Primary", is_active=True)
    dupe = Contact(company_id=company.id, email="a@example.com", tags="Dupe", imported_list="Mailchimp", is_active=True)
    seg = Segment(company_id=company.id, name="Mailchimp", segment_type="imported_list")
    sms_campaign = SMSCampaign(company_id=company.id, name="SMS", message="Hi")
    email_campaign = Campaign(company_id=company.id, name="Email")
    db.session.add_all([primary, dupe, seg, sms_campaign, email_campaign]); db.session.flush()
    db.session.add_all([
        SegmentMember(segment_id=seg.id, contact_id=dupe.id, source="import"),
        SMSRecipient(company_id=company.id, campaign_id=sms_campaign.id, contact_id=dupe.id, phone_number="+15550001000"),
        CampaignRecipient(campaign_id=email_campaign.id, contact_id=dupe.id),
        TwilioConversation(company_id=company.id, from_number="+15550001000", contact_id=dupe.id),
    ])
    db.session.commit()

    result = merge_contacts(primary.id, [dupe.id], actor_user_id=user.id, company_id=company.id)
    db.session.refresh(primary)

    assert "Primary" in primary.tags and "Dupe" in primary.tags
    assert result["references_reassigned"]["segment_member"] == 1
    assert SegmentMember.query.filter_by(contact_id=primary.id).count() == 1
    assert SMSRecipient.query.filter_by(contact_id=primary.id).count() == 1
    assert CampaignRecipient.query.filter_by(contact_id=primary.id).count() == 1
    assert TwilioConversation.query.filter_by(contact_id=primary.id).count() == 1
    audit = IntegrationAuditLog.query.filter_by(service_slug="contact_dedupe", action="merge").one()
    assert audit.changes["merged_contact_ids"] == [dupe.id]
    assert audit.changes["surviving_contact_id"] == primary.id


def test_merge_preserves_sms_and_email_optouts(tenant):
    _, company = tenant
    primary = Contact(company_id=company.id, email="safe@example.com", sms_marketing_opt_in=True, email_opt_in=True, email_subscribed=True, is_subscribed=True, is_active=True)
    dupe = Contact(company_id=company.id, email="safe@example.com", sms_opted_out=True, do_not_sms=True, email_unsubscribed=True, do_not_email=True, is_active=True)
    db.session.add_all([primary, dupe]); db.session.commit()

    merge_contacts(primary.id, [dupe.id], company_id=company.id)
    db.session.refresh(primary)

    assert primary.sms_opted_out is True
    assert primary.do_not_sms is True
    assert primary.sms_marketing_opt_in is False
    assert primary.email_unsubscribed is True
    assert primary.do_not_email is True
    assert primary.email_subscribed is False
    assert primary.email_opt_in is False


def test_merge_blocks_cross_tenant_contacts(tenant):
    _, company = tenant
    other = Company(name="Other Merge Co")
    c1 = Contact(company_id=company.id, email="same@example.com", is_active=True)
    c2 = Contact(company_id=None, email="same@example.com", is_active=True)
    db.session.add_all([other, c1, c2]); db.session.flush()
    c2.company_id = other.id
    db.session.commit()

    with pytest.raises(ValueError, match="cannot merge contacts across"):
        merge_contacts(c1.id, [c2.id], company_id=company.id)


def test_duplicate_review_and_manual_merge_api(client, tenant):
    user, company = tenant
    primary = Contact(company_id=company.id, email="api@example.com", tags="Primary", is_active=True)
    dupe = Contact(company_id=company.id, email="API@example.com", tags="Dupe", is_active=True)
    db.session.add_all([primary, dupe]); db.session.commit()

    review = client.get("/api/marketing/contacts/duplicates")
    assert review.status_code == 200
    body = review.get_json()
    assert body["count"] == 1
    assert set(body["duplicate_groups"][0]["contact_ids"]) == {primary.id, dupe.id}

    merge = client.post(
        "/api/marketing/contacts/duplicates/merge",
        json={"primary_contact_id": primary.id, "duplicate_contact_ids": [dupe.id]},
    )
    assert merge.status_code == 200
    db.session.refresh(primary); db.session.refresh(dupe)
    assert "Dupe" in primary.tags
    assert dupe.is_active is False
    assert merge.get_json()["merge"]["actor_user_id"] == user.id
