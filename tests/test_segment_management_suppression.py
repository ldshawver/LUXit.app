import os
from datetime import datetime
import pytest

from app import create_app
from extensions import db
from models import Campaign, CampaignRecipient, Company, Contact, MarketingAuditLog, Segment, SegmentMember, SMSCampaign, SMSRecipient, User, user_company


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
def tenant_user(client):
    company = Company(name="Segments Co")
    user = User(username="admin", email="admin@example.com", default_company_id=None, is_admin=True)
    user.password_hash = "testpass"
    db.session.add_all([company, user]); db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id); sess["_fresh"] = True
    return user, company


def test_admin_can_edit_copy_delete_segment_without_deleting_contacts(client, tenant_user):
    _, company = tenant_user
    contact = Contact(company_id=company.id, email="a@example.com")
    seg = Segment(company_id=company.id, name="VIP", description="old", segment_type="behavioral", category="buyers", match_mode="all", triggers=[{"t": 1}], conditions=[{"field": "tag", "value": "vip"}], actions=[{"a": 1}], is_active=True)
    db.session.add_all([contact, seg]); db.session.flush()
    db.session.add(SegmentMember(segment_id=seg.id, contact_id=contact.id, source="manual")); db.session.commit()

    edit = client.patch(f"/api/segments/{seg.id}", json={"name": "VIP Renamed", "description": "new", "is_active": False})
    assert edit.status_code == 200
    assert edit.get_json()["segment"]["name"] == "VIP Renamed"

    copy = client.post(f"/api/segments/{seg.id}/copy")
    assert copy.status_code == 201
    copied = Segment.query.get(copy.get_json()["segment"]["id"])
    assert copied.name == "VIP Renamed Copy"
    assert copied.description == "new"
    assert copied.conditions == seg.conditions
    assert copied.is_active is False
    assert MarketingAuditLog.query.filter_by(entity_id=copied.id, action="segment_created").count() == 0

    delete = client.delete(f"/api/segments/{seg.id}")
    assert delete.status_code == 200
    assert Contact.query.get(contact.id) is not None
    assert SegmentMember.query.filter_by(segment_id=seg.id).count() == 0


def test_manual_add_remove_and_bulk_contacts(client, tenant_user):
    _, company = tenant_user
    seg = Segment(company_id=company.id, name="Manual")
    contacts = [Contact(company_id=company.id, email=f"c{i}@example.com") for i in range(3)]
    db.session.add(seg); db.session.add_all(contacts); db.session.commit()

    assert client.post(f"/api/segments/{seg.id}/contacts", json={"contact_id": contacts[0].id}).status_code == 201
    assert client.post(f"/api/segments/{seg.id}/contacts/bulk-add", json={"contact_ids": [contacts[1].id, contacts[2].id]}).get_json()["added"] == 2
    listed = client.get(f"/api/segments/{seg.id}/contacts?q=c1").get_json()["contacts"]
    assert len(listed) == 1 and listed[0]["membership"]["source"] == "manual"
    assert client.delete(f"/api/segments/{seg.id}/contacts/{contacts[1].id}").status_code == 200
    assert client.post(f"/api/segments/{seg.id}/contacts/bulk-remove", json={"contact_ids": [contacts[0].id, contacts[2].id]}).get_json()["removed"] == 2


def test_permanent_exclusion_prevents_dynamic_readd(client, tenant_user):
    _, company = tenant_user
    contact = Contact(company_id=company.id, email="vip@example.com", tags="vip", is_active=True)
    seg = Segment(company_id=company.id, name="Dynamic", is_dynamic=True, conditions=[{"field": "tag", "value": "vip"}])
    db.session.add_all([contact, seg]); db.session.commit()
    assert len(client.get(f"/api/segments/{seg.id}/contacts").get_json()["contacts"]) == 1
    assert client.post(f"/api/segments/{seg.id}/contacts/{contact.id}/exclude", json={"reason": "requested"}).status_code == 200
    assert len(client.get(f"/api/segments/{seg.id}/contacts").get_json()["contacts"]) == 0


def test_marketing_preferences_precedence_email_and_sms(client, tenant_user, monkeypatch):
    _, company = tenant_user
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest"); monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token"); monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15559999999")
    email_ok_sms_block = Contact(company_id=company.id, email="emailok@example.com", phone="+15550000001", tags="sms_consent", is_active=True, is_subscribed=True, do_not_sms=True)
    sms_ok_email_block = Contact(company_id=company.id, email="smsok@example.com", phone="+15550000002", tags="sms_consent", is_active=True, is_subscribed=True, do_not_email=True)
    all_block = Contact(company_id=company.id, email="all@example.com", phone="+15550000003", tags="sms_consent", is_active=True, is_subscribed=True, do_not_market=True)
    sms_opted = Contact(company_id=company.id, email="optedsms@example.com", phone="+15550000004", tags="sms_consent", is_active=True, is_subscribed=True, sms_opted_out=True)
    email_unsub = Contact(company_id=company.id, email="unsub@example.com", phone="+15550000005", tags="sms_consent", is_active=True, is_subscribed=True, email_unsubscribed=True)
    db.session.add_all([email_ok_sms_block, sms_ok_email_block, all_block, sms_opted, email_unsub]); db.session.flush()
    email_campaign = Campaign(company_id=company.id, name="Email", status="draft")
    sms_campaign = SMSCampaign(company_id=company.id, name="SMS", message="Hi Reply STOP to opt out.", segment=None, status="draft")
    db.session.add_all([email_campaign, sms_campaign]); db.session.commit()

    e = client.post(f"/api/marketing/campaigns/{email_campaign.id}/send").get_json()
    assert {s["reason"] for s in e["skipped"]} >= {"do_not_market", "do_not_email", "email_unsubscribed"}
    assert CampaignRecipient.query.filter_by(campaign_id=email_campaign.id, contact_id=email_ok_sms_block.id).count() == 1

    sms_campaign.segment = None
    # Use legacy audience path by setting all contacts active and a segment value.
    for c in [email_ok_sms_block, sms_ok_email_block, all_block, sms_opted, email_unsub]:
        c.segment = "all"
    sms_campaign.segment = "all"; db.session.commit()
    s = client.post(f"/api/marketing/sms-campaigns/{sms_campaign.id}/send")
    assert s.status_code == 200
    assert SMSRecipient.query.filter_by(campaign_id=sms_campaign.id, contact_id=sms_ok_email_block.id).count() == 1
    reasons = {a.details["reason"] for a in MarketingAuditLog.query.filter_by(action="campaign_contact_skipped_suppression", entity_type="sms_campaign").all()}
    assert {"do_not_market", "do_not_sms", "sms_opted_out"} <= reasons


def test_tenant_isolation_and_non_admin_delete(client, tenant_user):
    user, company_a = tenant_user
    company_b = Company(name="Tenant B")
    seg_b = Segment(company_id=None, name="Hidden")
    db.session.add_all([company_b, seg_b]); db.session.flush(); seg_b.company_id = company_b.id
    seg_a = Segment(company_id=company_a.id, name="Visible")
    db.session.add(seg_a); db.session.commit()
    assert client.get(f"/api/segments/{seg_b.id}").status_code == 404
    user.is_admin = False
    db.session.commit()
    assert client.delete(f"/api/segments/{seg_a.id}").status_code == 403


def test_segment_detail_loads_html_tabs_contacts_exclusions_and_badges(client, tenant_user):
    _, company = tenant_user
    active = Contact(company_id=company.id, first_name="Active", last_name="Member", email="active@example.com", phone="+15550000101", tags="sms_consent", is_active=True, is_subscribed=True, source="import")
    suppressed = Contact(company_id=company.id, first_name="No", last_name="Market", email="no@example.com", phone="+15550000102", tags="sms_consent", is_active=True, is_subscribed=True, do_not_market=True, do_not_email=True, do_not_sms=True, email_unsubscribed=True, sms_opted_out=True)
    excluded = Contact(company_id=company.id, first_name="Excluded", last_name="User", email="excluded@example.com", tags="vip", is_active=True)
    seg = Segment(company_id=company.id, name="Detail VIP", description="Detail dashboard", is_dynamic=True, match_mode="any", triggers=[{"type": "sms_opt_in"}], conditions=[{"field": "tag", "value": "vip"}], actions=[{"type": "tag"}], is_active=True)
    db.session.add_all([active, suppressed, excluded, seg]); db.session.flush()
    db.session.add_all([
        SegmentMember(segment_id=seg.id, contact_id=active.id, source="manual", added_by_user_id=1),
        SegmentMember(segment_id=seg.id, contact_id=suppressed.id, source="sms_opt_in", added_by_user_id=1),
        SegmentMember(segment_id=seg.id, contact_id=excluded.id, source="dynamic_rule", is_excluded=True, exclusion_reason="requested", removed_by_user_id=1, removed_at=datetime.utcnow()),
    ])
    db.session.commit()

    list_page = client.get("/segments")
    assert list_page.status_code == 200
    assert f"/segments/{seg.id}" in list_page.get_data(as_text=True)
    assert f"/api/segments/{seg.id}/contacts" not in list_page.get_data(as_text=True)

    detail = client.get(f"/segments/{seg.id}")
    html = detail.get_data(as_text=True)
    assert detail.status_code == 200
    for tab in ("Overview", "Rules", "Contacts", "Excluded", "Suppression", "Campaigns", "Audit"):
        assert tab in html
    assert "Active Member" in html
    assert "Excluded User" in html
    assert "SMS Opt-In" in html
    assert "Suppressed" in html
    assert "Do Not Market" in html
    assert "Do Not Email" in html
    assert "Do Not SMS" in html
    assert "SMS Opted Out" in html
    assert "Email Unsubscribed" in html
    assert "Preview Segment" in html
    assert "Email Eligible" in html
    assert "SMS Eligible" in html
    assert "Deleting this segment will remove segment membership links and rules, but will not delete contacts or campaign history." in html
    assert "Optimize Segment with AI" in html and "Coming soon" in html


def test_segment_detail_refresh_honors_exclusions_delete_preserves_contacts_and_tenant_guard(client, tenant_user):
    user, company_a = tenant_user
    company_b = Company(name="Other Segment Tenant")
    keep = Contact(company_id=company_a.id, email="keep@example.com", tags="vip", is_active=True)
    excluded = Contact(company_id=company_a.id, email="excluded-refresh@example.com", tags="vip", is_active=True)
    seg = Segment(company_id=company_a.id, name="Refreshable", segment_type="newsletter", is_dynamic=True, conditions=[{"field": "tag", "value": "vip"}])
    other_seg = Segment(company_id=None, name="Other Hidden")
    db.session.add_all([company_b, keep, excluded, seg, other_seg]); db.session.flush(); other_seg.company_id = company_b.id
    db.session.add(SegmentMember(segment_id=seg.id, contact_id=excluded.id, source="dynamic_rule", is_excluded=True, exclusion_reason="manual", removed_at=datetime.utcnow(), removed_by_user_id=user.id))
    db.session.commit()

    assert client.get(f"/segments/{other_seg.id}").status_code == 404
    refresh = client.post(f"/segments/{seg.id}/refresh", follow_redirects=True)
    assert refresh.status_code == 200
    excluded_member = SegmentMember.query.filter_by(segment_id=seg.id, contact_id=excluded.id).one()
    assert excluded_member.is_excluded is True

    user.is_admin = False
    db.session.commit()
    assert client.delete(f"/api/segments/{seg.id}").status_code == 403
    user.is_admin = True
    db.session.commit()
    assert client.delete(f"/api/segments/{seg.id}").status_code == 200
    assert Contact.query.filter_by(id=keep.id).count() == 1
    assert Contact.query.filter_by(id=excluded.id).count() == 1


def test_segment_contact_picker_search_and_campaign_links_preselect_segment(client, tenant_user):
    _, company = tenant_user
    contact = Contact(company_id=company.id, first_name="Picker", last_name="Person", email="picker@example.com", phone="+15550001999", company="Picker Co", is_active=True)
    seg = Segment(company_id=company.id, name="Picker Segment")
    db.session.add_all([contact, seg]); db.session.commit()

    search_by_company = client.get("/api/contacts/search?q=Picker%20Co")
    assert search_by_company.status_code == 200
    payload = search_by_company.get_json()
    assert payload["contacts"][0]["id"] == contact.id

    detail = client.get(f"/segments/{seg.id}")
    html = detail.get_data(as_text=True)
    assert "/campaigns/create?segment=" in html and "Picker" in html
    assert "/sms/create?segment=" in html and "Picker" in html
    assert "Search by name, phone, email, or company" in html
    assert "Add Tag <span class=\"badge bg-secondary\">Coming soon</span>" in html
    assert "Start Campaign <span class=\"badge bg-secondary\">Coming soon</span>" in html

    email_create = client.get("/campaigns/create?segment=Picker+Segment")
    assert 'value="Picker Segment"' in email_create.get_data(as_text=True)
    sms_create = client.get("/sms/create?segment=Picker+Segment")
    assert 'value="Picker Segment"' in sms_create.get_data(as_text=True)


def test_segment_suppression_migration_is_idempotent_for_production_workflow():
    sql = open("migrations/20260618_segment_management_suppression.sql", encoding="utf-8").read()
    assert "ADD COLUMN IF NOT EXISTS do_not_market" in sql
    assert "ADD COLUMN IF NOT EXISTS source" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_segment_member_contact" in sql
