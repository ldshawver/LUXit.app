"""Regression suite for services/contact_resolver.py::resolve_or_create_contact(),
the canonical tenant-scoped find-or-create every Contact ingestion path
(inbound SMS, CSV/iCloud import, manual entry, newsletter forms, Zapier,
Google Contacts sync) now converges on.

Requires a real PostgreSQL database via TEST_POSTGRES_URL. Run with:

    TEST_POSTGRES_URL=postgresql://user:pass@host:port/dbname \
        pytest tests/test_contact_resolver_creation_postgres.py -v
"""
from __future__ import annotations

import os
import threading

import pytest


@pytest.fixture
def pg_app():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for these tests")
    os.environ["TEST_DATABASE_URL"] = url
    from app import create_app
    from extensions import db as _db

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        assert _db.engine.url.get_backend_name() == "postgresql"
        _db.drop_all()
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _company(db, Company, name="ResolverCo"):
    c = Company(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def _active_count(Contact, company_id):
    return Contact.query.filter_by(company_id=company_id, is_active=True).count()


# ---------------------------------------------------------------------------
# 1. same tenant + same phone
# ---------------------------------------------------------------------------
def test_same_tenant_same_phone_resolves_existing(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = upsert_contact_from_source(co.id, phone="+19165551000", source_channel="sms")
        db.session.commit()
        c2 = upsert_contact_from_source(co.id, phone="+19165551000", source_channel="sms")
        db.session.commit()
        assert c1.id == c2.id
        assert _active_count(Contact, co.id) == 1


# ---------------------------------------------------------------------------
# 2. same tenant + same email
# ---------------------------------------------------------------------------
def test_same_tenant_same_email_resolves_existing(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = resolve_contact(co.id, email="alice@realcorp.test", source="manual_entry")
        db.session.commit()
        c2 = resolve_contact(co.id, email="ALICE@RealCorp.test", source="manual_entry")
        db.session.commit()
        assert c1.id == c2.id
        assert _active_count(Contact, co.id) == 1


# ---------------------------------------------------------------------------
# 3. same tenant + same phone/email
# ---------------------------------------------------------------------------
def test_same_tenant_same_phone_and_email_resolves_existing(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_resolver import resolve_or_create_contact, ContactCreationOutcome
    from services.contact_intelligence import sync_contact_points

    with pg_app.app_context():
        co = _company(db, Company)
        r1 = resolve_or_create_contact(co.id, phone="+19165551001", email="bob@realcorp.test")
        sync_contact_points(r1.contact, "+19165551001", "bob@realcorp.test", "manual_entry")
        db.session.commit()

        r2 = resolve_or_create_contact(co.id, phone="+19165551001", email="bob@realcorp.test")
        db.session.commit()
        assert r2.outcome == ContactCreationOutcome.RESOLVED_PHONE_AND_EMAIL
        assert r2.contact.id == r1.contact.id
        assert _active_count(Contact, co.id) == 1


# ---------------------------------------------------------------------------
# 4. different tenants + same phone
# ---------------------------------------------------------------------------
def test_different_tenants_same_phone_stay_separate(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co_a = _company(db, Company, "TenantA")
        co_b = _company(db, Company, "TenantB")
        c_a = upsert_contact_from_source(co_a.id, phone="+19165551002", source_channel="sms")
        db.session.commit()
        c_b = upsert_contact_from_source(co_b.id, phone="+19165551002", source_channel="sms")
        db.session.commit()
        assert c_a.id != c_b.id
        assert c_a.company_id == co_a.id
        assert c_b.company_id == co_b.id


# ---------------------------------------------------------------------------
# 5. different tenants + same email
# ---------------------------------------------------------------------------
def test_different_tenants_same_email_stay_separate(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co_a = _company(db, Company, "TenantA")
        co_b = _company(db, Company, "TenantB")
        c_a = resolve_contact(co_a.id, email="shared@realcorp.test", source="manual_entry")
        db.session.commit()
        c_b = resolve_contact(co_b.id, email="shared@realcorp.test", source="manual_entry")
        db.session.commit()
        assert c_a.id != c_b.id
        assert c_a.company_id == co_a.id
        assert c_b.company_id == co_b.id


# ---------------------------------------------------------------------------
# 6. same name, different people
# ---------------------------------------------------------------------------
def test_same_name_different_people_never_auto_matched(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = resolve_contact(co.id, phone="+19165551003", first_name="John", last_name="Smith", source="manual_entry")
        db.session.commit()
        c2 = resolve_contact(co.id, phone="+19165551004", first_name="John", last_name="Smith", source="manual_entry")
        db.session.commit()
        assert c1.id != c2.id
        assert _active_count(Contact, co.id) == 2


# ---------------------------------------------------------------------------
# 7. provider-ID match
# ---------------------------------------------------------------------------
def test_provider_id_match_resolves_existing_without_phone(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = resolve_contact(co.id, phone="+19165551005", provider_id="people/cAAA111", source="google_contacts")
        db.session.commit()
        c2 = resolve_contact(co.id, provider_id="people/cAAA111", source="google_contacts")
        db.session.commit()
        assert c1.id == c2.id
        assert _active_count(Contact, co.id) == 1
        assert c1.external_google_contact_id == "people/cAAA111"


# ---------------------------------------------------------------------------
# 8. conflicting phone/email
# ---------------------------------------------------------------------------
def test_conflicting_phone_and_email_creates_flagged_ambiguous_contact(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        a = upsert_contact_from_source(co.id, phone="+19165551006", source_channel="sms")
        db.session.commit()
        b = resolve_contact(co.id, email="conflict@realcorp.test", source="manual_entry")
        db.session.commit()

        c = resolve_contact(co.id, phone="+19165551006", email="conflict@realcorp.test", source="manual_entry")
        db.session.commit()

        assert c.id not in (a.id, b.id)
        assert c.duplicate_status == "ambiguous"
        assert c.possible_duplicate_of_id in (a.id, b.id)
        # Neither pre-existing contact was silently merged or mutated into the other.
        assert Contact.query.get(a.id).is_active is True
        assert Contact.query.get(b.id).is_active is True
        assert Contact.query.get(a.id).merged_into_contact_id is None
        assert Contact.query.get(b.id).merged_into_contact_id is None


# ---------------------------------------------------------------------------
# 9. merged contact referenced by incoming identity
# ---------------------------------------------------------------------------
def test_merged_contact_referenced_by_incoming_identity_resolves_to_survivor(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source
    from services.contact_dedupe import merge_contacts

    with pg_app.app_context():
        co = _company(db, Company)
        survivor = upsert_contact_from_source(co.id, phone="+19165551007", email="merge1@realcorp.test", source_channel="sms")
        db.session.commit()
        duplicate = upsert_contact_from_source(co.id, phone="+19165551008", source_channel="sms")
        db.session.commit()

        merge_contacts(survivor.id, [duplicate.id], dry_run=False)
        db.session.commit()

        again = upsert_contact_from_source(co.id, phone="+19165551008", source_channel="sms")
        db.session.commit()
        assert again.id == survivor.id
        assert Contact.query.get(duplicate.id).is_active is False
        assert Contact.query.get(duplicate.id).merged_into_contact_id == survivor.id


# ---------------------------------------------------------------------------
# 10. concurrent same-phone creation
# ---------------------------------------------------------------------------
def test_concurrent_same_phone_creation_yields_one_contact(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co = _company(db, Company)
        company_id = co.id
        db.session.commit()

    results = []

    def worker():
        with pg_app.app_context():
            try:
                c = upsert_contact_from_source(company_id, phone="+19165559000", source_channel="sms")
                db.session.commit()
                results.append(("ok", c.id))
            except Exception as exc:  # pragma: no cover - failure path asserted below
                db.session.rollback()
                results.append(("error", repr(exc)))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert all(status == "ok" for status, _ in results), results
    assert len({cid for _, cid in results}) == 1

    with pg_app.app_context():
        n = Contact.query.filter_by(company_id=company_id, normalized_phone="+19165559000", is_active=True).count()
        assert n == 1


# ---------------------------------------------------------------------------
# 11. concurrent same-phone+email creation
# ---------------------------------------------------------------------------
def test_concurrent_same_phone_and_email_creation_yields_one_contact(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        company_id = co.id
        db.session.commit()

    results = []

    def worker():
        with pg_app.app_context():
            try:
                c = resolve_contact(company_id, phone="+19165559001", email="race@realcorp.test", source="manual_entry")
                db.session.commit()
                results.append(("ok", c.id))
            except Exception as exc:  # pragma: no cover
                db.session.rollback()
                results.append(("error", repr(exc)))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert all(status == "ok" for status, _ in results), results
    assert len({cid for _, cid in results}) == 1

    with pg_app.app_context():
        n = Contact.query.filter_by(company_id=company_id, normalized_phone="+19165559001", is_active=True).count()
        assert n == 1


# ---------------------------------------------------------------------------
# 12. Google sync replay + the exact production duplicate pattern:
#     original Google contact has phone+email; a later Google resource for
#     the same person shares only the email, has no phone, and carries a
#     *different* resource/provider ID (mirrors #1738/#2222 "mythplus" and
#     #1878/#2162 "seedlesstudent" in the live duplicate backlog).
# ---------------------------------------------------------------------------
def test_google_sync_replay_is_idempotent(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = resolve_contact(co.id, phone="+19165551009", email="replay@realcorp.test",
                              provider_id="people/cREPLAY", source="google_contacts")
        db.session.commit()
        c2 = resolve_contact(co.id, phone="+19165551009", email="replay@realcorp.test",
                              provider_id="people/cREPLAY", source="google_contacts")
        db.session.commit()
        assert c1.id == c2.id
        assert _active_count(Contact, co.id) == 1


def test_google_sync_phone_less_resource_resolves_via_email_not_duplicate(pg_app):
    """Regression for the live production bug: a phone-less Google resource
    for an already-known person, synced weeks later under a brand-new
    resource id, must resolve to the existing contact via email -- not spawn
    a second Contact row the way #2222/#2162 did before this fix."""
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        original = resolve_contact(
            co.id, phone="+19165551010", email="mythlike@realcorp.test",
            first_name="Keith", last_name="Davis Badlands",
            provider_id="people/cORIGINAL", source="google_contacts",
        )
        db.session.commit()

        later = resolve_contact(
            co.id, email="Mythlike@RealCorp.test",  # case-variant, as in the real backlog
            first_name="Keith Davis",
            provider_id="people/cDIFFERENT",  # a genuinely different Google resource
            source="google_contacts",
        )
        db.session.commit()

        assert later.id == original.id
        assert _active_count(Contact, co.id) == 1


# ---------------------------------------------------------------------------
# 13. inbound SMS replay
# ---------------------------------------------------------------------------
def test_inbound_sms_replay_does_not_duplicate_contact(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co = _company(db, Company)
        for _ in range(3):
            c = upsert_contact_from_source(co.id, phone="+19165551011", source_channel="sms",
                                           source_provider="twilio")
            db.session.commit()
        assert _active_count(Contact, co.id) == 1


# ---------------------------------------------------------------------------
# 14. source-event replay
# ---------------------------------------------------------------------------
def test_source_event_replay_does_not_duplicate_contact(pg_app):
    from extensions import db
    from models import Company, Contact, ContactSourceEvent
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        first = resolve_contact(co.id, phone="+19165551012", source="website_form", detail="landing-page")
        db.session.commit()
        second = resolve_contact(co.id, phone="+19165551012", source="website_form", detail="landing-page")
        db.session.commit()
        assert first.id == second.id
        assert _active_count(Contact, co.id) == 1
        # Replaying the same source event is allowed to log repeat touches --
        # it must never fan out into a second Contact.
        assert ContactSourceEvent.query.filter_by(contact_id=first.id).count() >= 1


# ---------------------------------------------------------------------------
# 15. consent preservation
# ---------------------------------------------------------------------------
def test_consent_not_weakened_on_resolve(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = upsert_contact_from_source(co.id, phone="+19165551013", source_channel="sms", sms_opt_in=False)
        db.session.commit()
        c1.sms_opted_out = True
        c1.do_not_sms = True
        db.session.commit()

        c2 = upsert_contact_from_source(co.id, phone="+19165551013", source_channel="sms", sms_opt_in=True)
        db.session.commit()

        assert c2.id == c1.id
        refreshed = Contact.query.get(c1.id)
        assert refreshed.sms_opted_out is True
        assert refreshed.do_not_sms is True
        assert refreshed.sms_marketing_opt_in is False


# ---------------------------------------------------------------------------
# 16. suppression preservation
# ---------------------------------------------------------------------------
def test_suppression_not_weakened_on_resolve(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = upsert_contact_from_source(co.id, email="suppressed@realcorp.test", source_channel="csv_import")
        db.session.commit()
        c1.email_unsubscribed = True
        c1.do_not_email = True
        db.session.commit()

        c2 = upsert_contact_from_source(co.id, email="suppressed@realcorp.test", source_channel="csv_import",
                                        email_opt_in=True)
        db.session.commit()

        assert c2.id == c1.id
        refreshed = Contact.query.get(c1.id)
        assert refreshed.email_unsubscribed is True
        assert refreshed.do_not_email is True
        assert refreshed.email_opt_in is False


# ---------------------------------------------------------------------------
# 17. archived/inactive contact behavior
# ---------------------------------------------------------------------------
def test_archived_contact_is_not_matched_new_contact_created(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co = _company(db, Company)
        old = upsert_contact_from_source(co.id, phone="+19165551014", source_channel="sms")
        db.session.commit()
        old.is_active = False
        old.status = "archived"
        db.session.commit()

        new = upsert_contact_from_source(co.id, phone="+19165551014", source_channel="sms")
        db.session.commit()

        assert new.id != old.id
        assert new.is_active is True


# ---------------------------------------------------------------------------
# 18. blank/invalid/placeholder identifiers
# ---------------------------------------------------------------------------
def test_blank_invalid_placeholder_identifiers_never_crash_or_falsely_match(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        # No phone, no email at all.
        blank1 = resolve_contact(co.id, first_name="No Info", source="manual_entry")
        db.session.commit()
        blank2 = resolve_contact(co.id, first_name="No Info", source="manual_entry")
        db.session.commit()
        assert blank1.id != blank2.id  # name-only never matches, per policy

        # Invalid email string must not raise.
        garbage = resolve_contact(co.id, email="not-an-email", source="manual_entry")
        db.session.commit()
        assert garbage.id is not None

        # A shared/role inbox must never resolve an existing contact by itself.
        role1 = resolve_contact(co.id, email="support@realcorp.test", source="manual_entry")
        db.session.commit()
        role2 = resolve_contact(co.id, email="support@realcorp.test", source="manual_entry")
        db.session.commit()
        assert role1.id != role2.id


# ---------------------------------------------------------------------------
# 19. phone/email formatting normalization variants
# ---------------------------------------------------------------------------
def test_formatting_variants_normalize_to_same_contact(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_audience import upsert_contact_from_source

    with pg_app.app_context():
        co = _company(db, Company)
        c1 = upsert_contact_from_source(co.id, phone="(916) 555-1015", source_channel="sms")
        db.session.commit()
        c2 = upsert_contact_from_source(co.id, phone="+1 916 555 1015", source_channel="sms")
        db.session.commit()
        c3 = upsert_contact_from_source(co.id, phone="9165551015", source_channel="sms")
        db.session.commit()
        assert c1.id == c2.id == c3.id
        assert _active_count(Contact, co.id) == 1


# ---------------------------------------------------------------------------
# 20. ambiguous email-only match (multiple existing contacts share an email)
# ---------------------------------------------------------------------------
def test_ambiguous_email_shared_by_two_existing_contacts_stays_separate(pg_app):
    from extensions import db
    from models import Company, Contact
    from services.contact_intelligence import resolve_contact

    with pg_app.app_context():
        co = _company(db, Company)
        e1 = resolve_contact(co.id, phone="+19165551016", first_name="First", source="manual_entry")
        db.session.commit()
        e2 = resolve_contact(co.id, phone="+19165551017", first_name="Second", source="manual_entry")
        db.session.commit()
        # Two genuinely different, already-established contacts share one email
        # (mirrors the carriebmac/jjzmail/pete.moldenhauer pattern in the live
        # backlog -- distinct phones, distinct names, one shared inbox).
        e1.email = "shared-inbox@realcorp.test"
        e1.normalized_email = "shared-inbox@realcorp.test"
        e2.email = "shared-inbox@realcorp.test"
        e2.normalized_email = "shared-inbox@realcorp.test"
        db.session.commit()

        third = resolve_contact(co.id, email="shared-inbox@realcorp.test", source="manual_entry")
        db.session.commit()

        assert third.id not in (e1.id, e2.id)
        assert third.duplicate_status == "ambiguous"
        # Neither existing contact was silently chosen, mutated into the
        # other, or merged.
        assert Contact.query.get(e1.id).merged_into_contact_id is None
        assert Contact.query.get(e2.id).merged_into_contact_id is None
