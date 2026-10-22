import os
from datetime import datetime, timedelta

import pytest

os.environ["FLASK_ENV"] = "testing"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import create_app
from extensions import db
from models import (
    Company,
    Contact,
    ContactEmailAddress,
    ContactPhoneNumber,
    ContactSourceEvent,
    GoogleContactLookup,
    TwilioConversation,
    User,
)
from services.contact_identity import (
    APPROVED_CONTACT_TAG,
    begin_identity_collection,
    confirm_pending_identity,
    process_identity_message,
    should_request_identity,
    validate_name_candidate,
)
from services.contact_resolver import (
    ConfirmationResolverOutcome,
    resolve_confirmation_identity,
    resolve_contact_identity,
    safe_name,
)
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
    unsafe_confirmation = kw.pop("unsafe_confirmation", False)
    row = Contact(company_id=company.id, tenant_id=company.id, phone=phone, normalized_phone=phone, **kw)
    db.session.add(row); db.session.flush()
    if row.identity_status == "awaiting_confirmation" and not unsafe_confirmation:
        requested_at = row.identity_fields_requested_at or datetime.utcnow()
        request_sid = row.identity_last_request_sid or f"SM-pending-{row.id}"
        row.identity_fields_requested_at = requested_at
        row.identity_last_request_sid = request_sid
        row.identity_request_state = {
            "phase": "awaiting_confirmation",
            "company_id": row.company_id,
            "contact_id": row.id,
            "confirmation_nonce": request_sid,
            "requested_at": requested_at.isoformat(),
        }
    return row


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


@pytest.mark.parametrize("keyword", ["NO", "N", "INCORRECT", "CHANGE"])
def test_no_restarts_without_duplicate_or_consent_change(repair_app, keyword):
    with repair_app.app_context():
        company=Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact=make_contact(company, identity_status="awaiting_confirmation", pending_first_name="A", pending_last_name="B", pending_email="a@example.com", sms_consent_status="opted_in", sms_marketing_opt_in=True)
        conv=TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone); db.session.add(conv); db.session.flush()
        result=process_identity_message(contact, conv, keyword, f"SM-no-{keyword}")
        assert contact.identity_status == "awaiting_name" and contact.pending_email is None
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


def test_expired_pending_confirmation_restarts_collection(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(
            company,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
            identity_fields_requested_at=datetime.utcnow() - timedelta(days=2),
        )
        conv = TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone)
        db.session.add(conv); db.session.flush()

        result = confirm_pending_identity(contact, conv, "SM-expired")
        assert result["reason"] == "identity_confirm_missing_pending_state"
        assert contact.identity_status == "awaiting_name"
        assert contact.pending_first_name is contact.pending_last_name is contact.pending_email is None


def test_google_and_ios_names_resolve_without_overwriting_confirmed(repair_app):
    with repair_app.app_context():
        company=Company(name="Tenant"); db.session.add(company); db.session.flush()
        user = User(username="identity-google", email="identity-google@example.com", default_company_id=company.id)
        db.session.add(user); db.session.flush()
        google=make_contact(company)
        db.session.add(GoogleContactLookup(company_id=company.id, user_id=user.id, normalized_phone=google.phone, display_name="Grace Hopper", resource_id="people/1")); db.session.flush()
        assert resolve_contact_identity(company.id, contact_id=google.id, allow_enrichment=True).safe_display_name == "Grace Hopper"
        assert google.name_source == "google_contacts"
        ios=make_contact(company, phone="+12025550124", source_provider="ios_contacts", display_name="Katherine Johnson")
        assert resolve_contact_identity(company.id, contact_id=ios.id, allow_enrichment=True).safe_display_name == "Name needed"
        confirmed=make_contact(company, phone="+12025550125", display_name="Customer Name", name_source="customer_confirmed", identity_status="confirmed", name_verification_level="verified", name_provenance={"source": "customer_confirmed_sms"})
        db.session.add(GoogleContactLookup(company_id=company.id, user_id=user.id, normalized_phone=confirmed.phone, display_name="Google Name", resource_id="people/2")); db.session.flush()
        assert resolve_contact_identity(company.id, contact_id=confirmed.id, allow_enrichment=True).safe_display_name == "Customer Name"


def test_canonical_contact_with_first_name_and_phone_skips_identity_collection(repair_app):
    """Regression test: a known/canonical contact with only first_name+phone
    (no last name, no email, never Google/iOS-matched or SMS-confirmed) must
    not be asked for email or a confirmation, and must not sit in
    pending_identity forever. It reaches the new 'minimum_established' status
    instead of being folded into 'confirmed'.
    """
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(company, first_name="Luke", phone="+12025550199")
        assert contact.identity_status == "pending_identity"
        resolution = resolve_contact_identity(company.id, contact_id=contact.id, allow_enrichment=True)
        assert resolution.safe_display_name == "Luke"
        assert contact.identity_status == "minimum_established"
        assert not should_request_identity(contact)
        assert contact.last_name is None
        assert contact.email is None


def test_canonical_name_outranks_conflicting_google_match(repair_app):
    """Canonical/first-party contact data must take precedence over a Google
    Contacts match, not be silently overwritten by it (SOURCE_RANK ordering).
    """
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        user = User(username="identity-canonical", email="identity-canonical@example.com", default_company_id=company.id)
        db.session.add(user); db.session.flush()
        contact = make_contact(company, first_name="Luke", last_name="Shawver", phone="+12025550198")
        db.session.add(GoogleContactLookup(company_id=company.id, user_id=user.id, normalized_phone=contact.normalized_phone, display_name="Someone Else", resource_id="p/canon-1"))
        db.session.flush()
        resolution = resolve_contact_identity(company.id, contact_id=contact.id, allow_enrichment=True)
        assert resolution.safe_display_name == "Luke Shawver"
        assert contact.first_name == "Luke" and contact.last_name == "Shawver"


def test_conflicts_cross_tenant_placeholders_and_cooldown(repair_app):
    with repair_app.app_context():
        a=Company(name="A"); b=Company(name="B"); db.session.add_all([a,b]); db.session.flush()
        user = User(username="identity-conflict", email="identity-conflict@example.com", default_company_id=a.id)
        db.session.add(user); db.session.flush()
        target=make_contact(a); make_contact(b, display_name="Other Tenant")
        db.session.add_all([GoogleContactLookup(company_id=a.id, user_id=user.id, normalized_phone=target.phone, display_name="One Person", resource_id="p/1"), GoogleContactLookup(company_id=a.id, user_id=user.id, normalized_phone=target.phone, display_name="Two Person", resource_id="p/2")]); db.session.flush()
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


@pytest.mark.parametrize("body", ["YES", "Y", "CONFIRM", "CONFIRMED", "NO", "N", "INCORRECT", "CHANGE",
                                  "STOP", "START", "HELP", "+12025550123", "luke@example.com",
                                  "https://example.com", "", "...", "thanks", "Please send appointment details"])
def test_name_validator_rejects_non_names(body):
    assert validate_name_candidate(body) is None


@pytest.mark.parametrize("body", [
    "Will Smith", "May Lee", "Hope Davis", "Grace Park", "Prince", "Madonna",
    "Jean-Luc Picard", "D'Arcy Wretzky", "María-José Carreño Quiñones",
    "Shawver, Luke", "Martin Luther King Jr.", "McDonald", "李小龍",
])
def test_name_validator_accepts_legal_names_without_recapitalizing(body):
    parsed = validate_name_candidate(body)
    assert parsed is not None
    normalized = " ".join(part for part in parsed if part)
    assert body.replace(",", "").casefold() == normalized.casefold() or body == "Shawver, Luke"


@pytest.mark.parametrize("body", [
    "Can You Respond", "Need More Information", "Where Are You",
    "Please send appointment details", "I need help with my order",
])
def test_name_validator_rejects_structural_message_sentences(body):
    assert validate_name_candidate(body) is None


def test_identity_collection_is_explicitly_state_gated(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        identified = make_contact(
            company,
            first_name="Actual",
            last_name="Customer",
            name="Actual Customer",
            display_name="Actual Customer",
            identity_status="confirmed",
            name_verification_level="verified",
        )
        unidentified = make_contact(company, phone="+12025550124", identity_status="pending_identity")
        identified_conv = TwilioConversation(company_id=company.id, contact_id=identified.id, from_number=identified.phone)
        unidentified_conv = TwilioConversation(company_id=company.id, contact_id=unidentified.id, from_number=unidentified.phone)
        db.session.add_all([identified_conv, unidentified_conv]); db.session.flush()

        assert process_identity_message(identified, identified_conv, "YES", "SM-normal-yes")["reply"] is None
        assert process_identity_message(identified, identified_conv, "Please send appointment details", "SM-normal")["reply"] is None
        outcome = process_identity_message(unidentified, unidentified_conv, "This should never be my name", "SM-unidentified")

        assert outcome["not_in_collection_state"]
        assert (identified.first_name, identified.last_name, identified.display_name) == ("Actual", "Customer", "Actual Customer")
        assert (unidentified.pending_first_name, unidentified.pending_last_name, unidentified.display_name) == (None, None, None)


def test_trusted_stored_name_skips_name_collection_and_cannot_be_overwritten(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(
            company,
            first_name="Actual",
            last_name="Customer",
            name="Actual Customer",
            display_name="Actual Customer",
            identity_status="confirmed",
            name_verification_level="verified",
            name_source="manual",
            name_provenance={"source": "manual"},
        )
        conv = TwilioConversation(
            company_id=company.id,
            contact_id=contact.id,
            from_number=contact.phone,
        )
        db.session.add(conv); db.session.flush()

        prompt = begin_identity_collection(contact, "SM-begin")
        assert contact.identity_status == "awaiting_email"
        assert (contact.pending_first_name, contact.pending_last_name) == ("Actual", "Customer")
        assert "email address" in prompt

        result = process_identity_message(
            contact,
            conv,
            "Please send appointment details",
            "SM-business-message",
        )
        assert "valid email address" in result["reply"]
        assert (contact.first_name, contact.last_name, contact.display_name) == (
            "Actual", "Customer", "Actual Customer"
        )
        assert (contact.pending_first_name, contact.pending_last_name) == ("Actual", "Customer")


def test_confirmation_requires_server_side_pending_values(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(
            company,
            first_name="Stored",
            last_name="Customer",
            email="stored@example.com",
            identity_status="awaiting_confirmation",
            unsafe_confirmation=True,
        )
        conv = TwilioConversation(
            company_id=company.id,
            contact_id=contact.id,
            from_number=contact.phone,
        )
        db.session.add(conv); db.session.flush()

        result = confirm_pending_identity(contact, conv, "SM-no-pending")

        assert result["review"]
        assert contact.identity_status == "awaiting_name"
        assert contact.display_name is None
        assert ContactSourceEvent.query.filter_by(
            contact_id=contact.id,
            event_type="identity_confirmed",
        ).count() == 0


def test_awaiting_name_and_email_collect_only_expected_fields(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(company, identity_status="awaiting_name")
        conv = TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone)
        db.session.add(conv); db.session.flush()

        result = process_identity_message(contact, conv, "Luke Shawver", "SM-name")
        assert result["reply"].endswith("email address.")
        assert contact.identity_status == "awaiting_email"
        assert (contact.pending_first_name, contact.pending_last_name) == ("Luke", "Shawver")
        assert contact.first_name is contact.last_name is contact.display_name is None

        result = process_identity_message(contact, conv, "luke@adiken.com", "SM-email")
        assert contact.identity_status == "awaiting_confirmation"
        assert "Luke Shawver, luke@adiken.com" in result["reply"]


def test_luke_self_match_confirms_instead_of_manual_review(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(
            company,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
        )
        conv = TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone)
        db.session.add(conv); db.session.flush()

        result = process_identity_message(contact, conv, "YES", "SM-luke-self")
        assert result["confirmed"]
        assert result["resolver_outcome"] == ConfirmationResolverOutcome.SELF_MATCH.value
        assert contact.display_name == "Luke Shawver"


def test_duplicate_self_contact_points_are_not_ambiguity(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(
            company,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
        )
        db.session.add_all([
            ContactPhoneNumber(
                company_id=company.id,
                contact_id=contact.id,
                normalized_value=contact.phone,
            ),
            ContactPhoneNumber(
                company_id=company.id,
                contact_id=contact.id,
                normalized_value=contact.phone,
            ),
            ContactEmailAddress(
                company_id=company.id,
                contact_id=contact.id,
                normalized_value="luke@adiken.com",
            ),
        ])
        conv = TwilioConversation(company_id=company.id, contact_id=contact.id, from_number=contact.phone)
        db.session.add(conv); db.session.flush()

        result = confirm_pending_identity(contact, conv, "SM-self-points")
        assert result["confirmed"]
        assert result["resolver_outcome"] == ConfirmationResolverOutcome.SELF_MATCH.value


def test_luke_single_compatible_match_merges_and_preserves_suppression(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        canonical = make_contact(
            company,
            email="luke@adiken.com",
            normalized_email="luke@adiken.com",
            identity_status="pending_identity",
            sms_opted_out=True,
            do_not_sms=True,
            do_not_market=True,
            sms_consent_status="opted_out",
            tags="Canonical",
        )
        pending = make_contact(
            company,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
            tags="Inbound",
            do_not_contact=True,
            sms_marketing_opt_in_source="keyword:start",
        )
        db.session.add_all([
            ContactPhoneNumber(
                company_id=company.id, contact_id=canonical.id,
                normalized_value=canonical.phone, verification_status="confirmed",
                source="customer_confirmed",
            ),
            ContactEmailAddress(
                company_id=company.id, contact_id=canonical.id,
                normalized_value="luke@adiken.com", verification_status="confirmed",
                source="customer_confirmed",
            ),
        ])
        conv = TwilioConversation(company_id=company.id, contact_id=pending.id, from_number=pending.phone)
        db.session.add(conv); db.session.flush()

        result = confirm_pending_identity(pending, conv, "SM-luke-merge")
        db.session.flush()
        db.session.refresh(canonical)
        db.session.refresh(pending)

        assert result["resolver_outcome"] == ConfirmationResolverOutcome.SINGLE_COMPATIBLE_MATCH.value
        assert result["canonical_contact_id"] == canonical.id
        assert pending.is_active is False and pending.merged_into_contact_id == canonical.id
        assert conv.contact_id == canonical.id
        assert canonical.display_name == "Luke Shawver"
        assert canonical.sms_opted_out and canonical.do_not_sms and canonical.do_not_market and canonical.do_not_contact
        assert canonical.sms_consent_status == "opted_out"
        assert canonical.sms_marketing_opt_in_source == "keyword:start"
        assert {"Canonical", "Inbound"}.issubset(set((canonical.tags or "").split(", ")))


def test_true_ambiguity_and_verified_conflict_require_review(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        pending = make_contact(
            company,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
        )
        make_contact(company, phone=pending.phone, email="luke@adiken.com")
        make_contact(company, phone=pending.phone, email="luke@adiken.com")
        conv = TwilioConversation(company_id=company.id, contact_id=pending.id, from_number=pending.phone)
        db.session.add(conv); db.session.flush()
        ambiguous = confirm_pending_identity(pending, conv, "SM-ambiguous")
        assert ambiguous["resolver_outcome"] == ConfirmationResolverOutcome.TRUE_AMBIGUITY.value
        assert ambiguous["review"]

        db.session.rollback()

    with repair_app.app_context():
        db.drop_all(); db.create_all()
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        pending = make_contact(
            company,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
        )
        make_contact(
            company,
            phone=pending.phone,
            email="different@example.com",
            normalized_email="different@example.com",
            identity_status="confirmed",
            identity_verification_source="customer_confirmed",
            first_name="Different",
            last_name="Person",
            name_verification_level="verified",
            name_source="customer_confirmed",
            name_provenance={"source": "customer_confirmed_sms"},
        )
        conv = TwilioConversation(company_id=company.id, contact_id=pending.id, from_number=pending.phone)
        db.session.add(conv); db.session.flush()
        conflict = confirm_pending_identity(pending, conv, "SM-conflict")
        assert conflict["resolver_outcome"] == ConfirmationResolverOutcome.IDENTITY_CONFLICT.value
        assert conflict["review"]


def test_cross_tenant_match_is_not_exposed_or_merged(repair_app):
    with repair_app.app_context():
        tenant = Company(name="Current"); other_tenant = Company(name="Other")
        db.session.add_all([tenant, other_tenant]); db.session.flush()
        pending = make_contact(
            tenant,
            phone=None,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
        )
        other = make_contact(
            other_tenant,
            phone="+12025550129",
            email="luke@adiken.com",
            normalized_email="luke@adiken.com",
            identity_status="confirmed",
        )
        resolution = resolve_confirmation_identity(
            tenant.id,
            current_contact_id=pending.id,
            phone=None,
            email=pending.pending_email,
            first_name="Luke",
            last_name="Shawver",
        )
        assert resolution.outcome == ConfirmationResolverOutcome.CROSS_TENANT_ONLY
        assert resolution.candidate_contact_ids == ()
        assert resolution.canonical_contact_id == pending.id
        assert other.is_active


def test_cross_tenant_contact_points_are_classified_without_exposure(repair_app):
    with repair_app.app_context():
        tenant = Company(name="Current"); other_tenant = Company(name="Other")
        db.session.add_all([tenant, other_tenant]); db.session.flush()
        pending = make_contact(
            tenant,
            phone=None,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
        )
        other = make_contact(other_tenant, phone="+12025550129")
        db.session.add(ContactEmailAddress(
            company_id=other_tenant.id,
            contact_id=other.id,
            normalized_value="luke@adiken.com",
            verification_status="confirmed",
        ))
        db.session.flush()

        resolution = resolve_confirmation_identity(
            tenant.id,
            current_contact_id=pending.id,
            phone=None,
            email=pending.pending_email,
            first_name="Luke",
            last_name="Shawver",
        )

        assert resolution.outcome == ConfirmationResolverOutcome.CROSS_TENANT_ONLY
        assert resolution.candidate_contact_ids == ()
        assert resolution.candidate_count == 0


def test_verified_contact_point_conflict_requires_review(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        pending = make_contact(
            company,
            identity_status="awaiting_confirmation",
            pending_first_name="Luke",
            pending_last_name="Shawver",
            pending_email="luke@adiken.com",
        )
        canonical = make_contact(
            company,
            phone=pending.phone,
            identity_status="confirmed",
            first_name="Luke",
            last_name="Shawver",
            name_verification_level="verified",
        )
        db.session.add(ContactEmailAddress(
            company_id=company.id,
            contact_id=canonical.id,
            normalized_value="different@example.com",
            verification_status="confirmed",
        ))
        conv = TwilioConversation(
            company_id=company.id,
            contact_id=pending.id,
            from_number=pending.phone,
        )
        db.session.add(conv); db.session.flush()

        result = confirm_pending_identity(pending, conv, "SM-point-conflict")

        assert result["resolver_outcome"] == ConfirmationResolverOutcome.IDENTITY_CONFLICT.value
        assert result["reason"] == "identity_confirm_verified_conflict"
        assert result["review"]


def test_message_like_stored_name_is_not_a_display_name(repair_app):
    with repair_app.app_context():
        company = Company(name="Tenant"); db.session.add(company); db.session.flush()
        contact = make_contact(
            company,
            first_name="Please",
            last_name="Send Appointment Details",
            name="Please send appointment details",
            display_name="Please send appointment details",
            identity_status="confirmed",
            name_verification_level="verified",
            name_source="customer_confirmed",
        )

        resolved = resolve_contact_identity(company.id, contact_id=contact.id)

        assert resolved.safe_display_name == "Name needed"
        assert resolved.masked_phone.endswith(contact.phone[-4:])


def test_migration_is_forward_only_and_contains_stale_repair():
    from pathlib import Path
    sql=Path("migrations/20260724_contact_identity_resolution_repair.sql").read_text().lower()
    assert "add column if not exists identity_requested_fields jsonb" in sql
    assert "add column if not exists approval_status" in sql
    assert "identity_status = 'confirmed'" in sql
    assert "not exists (select 1 from contact d where d.company_id = c.company_id" in sql
    assert "drop column" not in sql and "delete from contact" not in sql
