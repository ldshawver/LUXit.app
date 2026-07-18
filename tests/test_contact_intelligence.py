import os
from datetime import datetime, timedelta

import pytest

from app import create_app
from extensions import db
from models import Company, Contact, ContactSourceEvent, ContactTask, Opportunity, User, user_company
from services.contact_audience import upsert_contact_from_source
from services.contact_dedupe import find_duplicate_contacts, merge_contacts
from services.contact_intelligence import apply_google_name_match, cleanup_audit, create_follow_up_task, create_opportunity, resolve_contact
from services.phone_normalization import normalize_phone, normalize_phone_e164


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
def tenant(app):
    company = Company(name="Intelligence Co")
    other = Company(name="Other Co")
    user = User(username="intel", email="intel@example.com", default_company_id=None, is_admin=True)
    user.password_hash = "testpass"
    db.session.add_all([company, other, user]); db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))
    db.session.commit()
    return user, company, other


def test_phone_normalization_common_us_and_extension():
    assert normalize_phone_e164("(916) 555-1212") == "+19165551212"
    assert normalize_phone_e164("916-555-1212") == "+19165551212"
    assert normalize_phone_e164("+1 916 555 1212") == "+19165551212"
    result = normalize_phone("(916) 555-1212 x44")
    assert result.normalized == "+19165551212"
    assert result.extension == "44"
    assert result.is_valid is True


def test_phone_normalization_international_invalid_and_blank():
    assert normalize_phone_e164("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("not a phone").is_valid is False
    assert normalize_phone("").normalized is None


def test_resolve_contact_records_source_and_reuses_safe_match(tenant):
    user, company, _ = tenant
    first = resolve_contact(company.id, phone="916-555-1212", proposed_name="Caller", source="twilio_inbound_sms", detail="Inbound SMS to +19165551212", user_id=user.id)
    second = resolve_contact(company.id, phone="+1 916 555 1212", email="person@example.com", source="website_form", detail="Website consultation form")
    db.session.commit()
    assert first.id == second.id
    assert first.original_source == "twilio_inbound_sms"
    assert first.latest_source == "website_form"
    assert ContactSourceEvent.query.filter_by(contact_id=first.id).count() == 2


def test_duplicate_detection_exact_phone_email_only_and_tenant_scoped(tenant):
    _, company, other = tenant
    a = Contact(company_id=company.id, phone="9165551212", normalized_phone="+19165551212", name="Same Name", is_active=True)
    b = Contact(company_id=company.id, phone="+1 916 555 1212", normalized_phone="+19165551212", name="Different", is_active=True)
    name_only = Contact(company_id=company.id, name="Same Name", is_active=True)
    other_tenant = Contact(company_id=other.id, phone="+19165551212", normalized_phone="+19165551212", is_active=True)
    db.session.add_all([a, b, name_only, other_tenant]); db.session.commit()
    groups = [set(g["contact_ids"]) for g in find_duplicate_contacts(company.id)]
    assert {a.id, b.id} in groups
    assert all(name_only.id not in g for g in groups)
    assert all(other_tenant.id not in g for g in groups)


def test_merge_archives_not_deletes_and_preserves_tasks_opportunities(tenant):
    user, company, _ = tenant
    master = Contact(company_id=company.id, email="merge@example.com", is_active=True, original_source="manual_entry", first_touch_at=datetime.utcnow() - timedelta(days=2))
    dupe = Contact(company_id=company.id, email="MERGE@example.com", normalized_email="merge@example.com", tags="VIP", is_active=True, latest_source="campaign", last_touch_at=datetime.utcnow())
    db.session.add_all([master, dupe]); db.session.flush()
    task = create_follow_up_task(company.id, dupe.id, "Call back", assigned_user_id=user.id)
    opp = create_opportunity(company.id, dupe.id, "Big Deal", owner_user_id=user.id, estimated_value=1000)
    db.session.commit()
    result = merge_contacts(master.id, [dupe.id], actor_user_id=user.id, company_id=company.id)
    db.session.refresh(dupe)
    assert dupe.is_active is False
    assert dupe.merged_into_contact_id == master.id
    assert ContactTask.query.get(task.id).contact_id == master.id
    assert Opportunity.query.get(opp.id).contact_id == master.id
    assert result["references_reassigned"]["contact_task"] == 1
    assert result["references_reassigned"]["opportunity"] == 1


def test_google_match_fills_missing_name_but_not_reliable_existing(tenant):
    _, company, _ = tenant
    missing = Contact(company_id=company.id, phone="+19165551212", normalized_phone="+19165551212", is_active=True)
    named = Contact(company_id=company.id, phone="+19165550000", normalized_phone="+19165550000", name="Customer Confirmed", name_source="user", is_active=True)
    db.session.add_all([missing, named]); db.session.commit()
    assert apply_google_name_match(missing, [{"normalized_phone":"+19165551212", "name":"Jane Google", "resource_id":"people/1", "etag":"a"}]) == "matched"
    assert missing.name == "Jane Google"
    assert missing.name_source == "google_contacts"
    assert apply_google_name_match(named, [{"normalized_phone":"+19165550000", "name":"Other Google", "resource_id":"people/2"}]) == "matched"
    assert named.name == "Customer Confirmed"


def test_google_ambiguous_and_cleanup_audit(tenant):
    _, company, _ = tenant
    contact = Contact(company_id=company.id, phone="+19165551212", normalized_phone="+19165551212", is_active=True)
    db.session.add(contact); db.session.commit()
    status = apply_google_name_match(contact, [
        {"normalized_phone":"+19165551212", "name":"A", "resource_id":"people/a"},
        {"normalized_phone":"+19165551212", "name":"B", "resource_id":"people/b"},
    ])
    assert status == "ambiguous"
    report = cleanup_audit(company.id)
    assert report["total_contacts_scanned"] >= 1
    assert "proposed_merges" in report


def test_upsert_contact_from_source_uses_intelligence_source(tenant):
    _, company, _ = tenant
    contact = upsert_contact_from_source(company.id, phone="9165551212", source_channel="csv_import", source_provider="test", source_context="Imported from customers.csv")
    db.session.commit()
    assert contact.original_source == "csv_import"
    assert contact.latest_source == "csv_import"
    assert contact.phone_numbers.count() == 1
