from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import Company, Contact, ContactEmailAddress, ContactSourceEvent, GoogleContactLookup, TwilioConversation
from services.contact_identity import APPROVED_CONTACT_TAG, confirm_pending_identity, process_identity_message, should_request_identity
from services.contact_resolver import resolve_contact_identity, safe_name
from services.contact_audience import import_contacts


@pytest.fixture(scope="module")
def repair_app():
    app = create_app(); app.config.update(TESTING=True)
    return app


@pytest.fixture(autouse=True)
def clean(repair_app):
    with repair_app.app_context():
        db.drop_all(); db.create_all()
        yield
        db.session.remove(); db.drop_all()


def make_contact(company, phone="+12025550123", **kw):
    row = Contact(company_id=company.id, tenant_id=company.id, phone=phone, normalized_phone=phone, **kw)
    db.session.add(row); db.session.flush(); return row


@pytest.mark.parametrize("keyword", ["YES", " y ", "Confirm", " CONFIRMED "])
def test_confirmation_variants_are_one_step_and_preserve_consent(repair_app, keyword):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(company, identity_status="awaiting_confirmation", pending_first_name="Ada",
            pending_last_name="Lovelace", pending_email="ada@example.com", sms_marketing_opt_in=False,
            sms_consent_status="unknown", sms_opted_out=False)
        conv = TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone, to_number="+12025550199")
        db.session.add(conv); db.session.flush()
        before = (contact.sms_marketing_opt_in, contact.sms_consent_status, contact.sms_opted_out)
        result = process_identity_message(contact, conv, keyword, "SM-confirm")
        assert result["confirmed"] and contact.identity_status == "confirmed"
        assert (contact.first_name, contact.last_name, contact.display_name, contact.normalized_email) == ("Ada", "Lovelace", "Ada Lovelace", "ada@example.com")
        assert contact.pending_first_name is contact.pending_last_name is contact.pending_email is None
        assert contact.approval_status == "approved"
        assert (contact.tags or "").split(", ").count(APPROVED_CONTACT_TAG) == 1
        assert before == (contact.sms_marketing_opt_in, contact.sms_consent_status, contact.sms_opted_out)
        assert ContactEmailAddress.query.filter_by(contact_id=contact.id, normalized_value="ada@example.com").count() == 1


def test_no_restarts_without_duplicate_or_consent_change(repair_app):
    with repair_app.app_context():
        company=Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact=make_contact(company, identity_status="awaiting_confirmation", pending_first_name="A", pending_last_name="B", pending_email="a@example.com", sms_consent_status="opted_in", sms_marketing_opt_in=True)
        conv=TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone); db.session.add(conv); db.session.flush()
        result=process_identity_message(contact, conv, " incorrect ", "SM-no")
        assert contact.identity_status == "pending_identity" and contact.pending_email is None
        assert contact.sms_marketing_opt_in and contact.sms_consent_status == "opted_in"
        assert "first and last name" in result["reply"] and Contact.query.count() == 1


def test_repeated_confirmation_is_idempotent(repair_app):
    with repair_app.app_context():
        company=Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact=make_contact(company, identity_status="awaiting_confirmation", pending_first_name="Ada", pending_last_name="Lovelace", pending_email="ada@example.com")
        conv=TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone); db.session.add(conv); db.session.flush()
        assert confirm_pending_identity(contact, conv, "SM-one")["confirmed"]
        assert confirm_pending_identity(contact, conv, "SM-two")["idempotent"]
        assert ContactEmailAddress.query.filter_by(contact_id=contact.id).count() == 1
        assert (contact.tags or "").split(", ").count(APPROVED_CONTACT_TAG) == 1


def test_invalid_pending_identity_cannot_confirm(repair_app):
    with repair_app.app_context():
        company=Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact=make_contact(company, identity_status="awaiting_confirmation", pending_first_name="", pending_last_name="", pending_email="bad")
        conv=TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone)
        result=confirm_pending_identity(contact, conv, "SM-bad")
        assert result["review"] and contact.identity_status != "confirmed"


def test_google_and_ios_names_resolve_without_overwriting_confirmed(repair_app):
    with repair_app.app_context():
        company=Company(name="Tenant"); db.session.add(company); db.session.flush()
        google=make_contact(company)
        db.session.add(GoogleContactLookup(company_id=company.id, user_id=1, normalized_phone=google.phone, display_name="Grace Hopper", resource_id="people/1")); db.session.flush()
        assert resolve_contact_identity(company.id, contact_id=google.id, allow_enrichment=True).safe_display_name == "Grace Hopper"
        assert google.name_source == "google_contacts"
        ios=make_contact(company, phone="+12025550124", source_provider="ios_contacts", display_name="Katherine Johnson")
        assert resolve_contact_identity(company.id, contact_id=ios.id, allow_enrichment=True).safe_display_name == "Katherine Johnson"
        confirmed=make_contact(company, phone="+12025550125", display_name="Customer Name", name_source="customer_confirmed", identity_status="confirmed", name_verification_level="verified")
        db.session.add(GoogleContactLookup(company_id=company.id, user_id=1, normalized_phone=confirmed.phone, display_name="Google Name", resource_id="people/2")); db.session.flush()
        assert resolve_contact_identity(company.id, contact_id=confirmed.id, allow_enrichment=True).safe_display_name == "Customer Name"


def test_conflicts_cross_tenant_placeholders_and_cooldown(repair_app):
    with repair_app.app_context():
        a=Company(name="A"); b=Company(name="B"); db.session.add_all([a,b]); db.session.flush()
        target=make_contact(a); make_contact(b, display_name="Other Tenant")
        db.session.add_all([GoogleContactLookup(company_id=a.id, user_id=1, normalized_phone=target.phone, display_name="One Person", resource_id="p/1"), GoogleContactLookup(company_id=a.id, user_id=1, normalized_phone=target.phone, display_name="Two Person", resource_id="p/2")]); db.session.flush()
        assert resolve_contact_identity(a.id, contact_id=target.id, allow_enrichment=True).conflict_state == "google_ambiguous"
        assert safe_name("Pending identity", target.phone) is None and safe_name(target.phone, target.phone) is None
        target.identity_status="pending_identity"; target.google_match_status="not_checked"; target.identity_request_count=1
        target.identity_fields_requested_at=datetime.utcnow(); target.sms_opted_out=target.do_not_sms=target.do_not_contact=False
        assert not should_request_identity(target)


def test_repeated_ios_vcard_import_is_idempotent(repair_app):
    with repair_app.app_context():
        company=Company(name="Tenant"); db.session.add(company); db.session.flush()
        payload=b"BEGIN:VCARD\nVERSION:3.0\nFN:Test Person\nN:Person;Test;;;\nTEL:+12025550126\nEMAIL:test@example.com\nEND:VCARD\n"
        first=import_contacts(company.id, payload, "iphone.vcf", source_provider="ios_contacts")
        second=import_contacts(company.id, payload, "iphone.vcf", source_provider="ios_contacts")
        assert first["success_count"] == second["success_count"] == 1
        assert Contact.query.filter_by(company_id=company.id, normalized_phone="+12025550126").count() == 1
        assert ContactEmailAddress.query.filter_by(company_id=company.id, normalized_value="test@example.com").count() == 1
        assert ContactSourceEvent.query.filter_by(company_id=company.id, source="ios_import").count() == 1


def test_migration_is_forward_only_and_contains_stale_repair():
    from pathlib import Path
    sql=Path("migrations/20260724_contact_identity_resolution_repair.sql").read_text().lower()
    assert "add column if not exists identity_requested_fields jsonb" in sql
    assert "add column if not exists approval_status" in sql
    assert "identity_status = 'confirmed'" in sql
    assert "not exists (select 1 from contact d where d.company_id = c.company_id" in sql
    assert "drop column" not in sql and "delete from contact" not in sql
