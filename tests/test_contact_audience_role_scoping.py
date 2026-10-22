"""Regression suite for services/contact_audience.py's role-scoped campaign
audience resolution (Segment #9 vs Segment #10 name collision fix).

Requires a real PostgreSQL database via TEST_POSTGRES_URL. Run with:

    TEST_POSTGRES_URL=postgresql://user:pass@host:port/dbname \
        pytest tests/test_contact_audience_role_scoping.py -v
"""
from __future__ import annotations

import os

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


def _company(db, Company, name="RoleScopeCo"):
    c = Company(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def _contact(db, Contact, company_id, **kw):
    defaults = dict(company_id=company_id, is_active=True, status="active")
    defaults.update(kw)
    c = Contact(**defaults)
    db.session.add(c)
    db.session.flush()
    return c


def _segment(db, Segment, company_id, **kw):
    defaults = dict(company_id=company_id, name="Test Segment", segment_type="custom",
                     match_mode="all", is_dynamic=True, is_active=True)
    defaults.update(kw)
    s = Segment(**defaults)
    db.session.add(s)
    db.session.flush()
    return s


def _member(db, SegmentMember, segment_id, contact_id, **kw):
    m = SegmentMember(segment_id=segment_id, contact_id=contact_id, source="dynamic_rule", **kw)
    db.session.add(m)
    db.session.flush()
    return m


def _campaign(db, SMSCampaign, company_id, **kw):
    defaults = dict(company_id=company_id, name="Test Campaign", message="hi")
    defaults.update(kw)
    c = SMSCampaign(**defaults)
    db.session.add(c)
    db.session.flush()
    return c


# ---------------------------------------------------------------------------
# A/B/C: the #9 vs #10 name-collision core fix
# ---------------------------------------------------------------------------

class TestNameCollisionResolution:
    def test_duplicate_display_name_across_roles_selects_marketing_segment_only(self, pg_app):
        """A: two segments share a display name but different roles -- the
        marketing resolver must ignore the non-marketing one entirely."""
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.contact_audience import resolve_segment_contacts

        company = _company(db, Company)
        tag_anchor = _segment(db, Segment, company.id, name="My Order Customer", segment_type="contact_tag")
        marketing = _segment(db, Segment, company.id, name="My Order Customer", segment_type="custom")
        member = _contact(db, Contact, company.id, tags="My Order Customer", normalized_phone="+12025550111")
        _member(db, SegmentMember, marketing.id, member.id)
        db.session.commit()

        result = resolve_segment_contacts(company.id, segment="My Order Customer")
        assert [c.id for c in result] == [member.id]

    def test_my_order_customer_marketing_resolution_selects_segment_10_role_never_9(self, pg_app):
        """B: reproduces the exact production shape (segment #9 =
        contact_tag/0 members, segment #10 = custom/71-style membership) and
        confirms only the marketing-role segment's members are returned."""
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.contact_audience import resolve_segment_contacts

        company = _company(db, Company)
        seg9 = _segment(db, Segment, company.id, name="My Order Customer", segment_type="contact_tag", is_dynamic=False)
        seg10 = _segment(db, Segment, company.id, name="My Order Customer", segment_type="custom")
        in_seg10 = _contact(db, Contact, company.id, tags="My Order Customer")
        _member(db, SegmentMember, seg10.id, in_seg10.id)
        # a contact only ever added to seg9 (never seg10) must NOT surface
        stray = _contact(db, Contact, company.id, tags="unrelated")
        _member(db, SegmentMember, seg9.id, stray.id)
        db.session.commit()

        result = resolve_segment_contacts(company.id, segment="My Order Customer")
        ids = {c.id for c in result}
        assert in_seg10.id in ids
        assert stray.id not in ids

    def test_segment_9_remains_usable_by_crm_automation(self, pg_app):
        """C: the contact_tag anchor itself (crm_automation.py's own
        role-scoped lookup) is unaffected by the contact_audience.py fix."""
        from extensions import db
        from models import Company, Segment
        from services.crm_automation import _canonical_candidates

        company = _company(db, Company)
        seg9 = _segment(db, Segment, company.id, name="My Order Customer", segment_type="contact_tag", is_dynamic=False)
        _segment(db, Segment, company.id, name="My Order Customer", segment_type="custom")
        db.session.commit()

        candidates = _canonical_candidates(company.id, "tag")
        assert [row.id for row in candidates] == [seg9.id]


# ---------------------------------------------------------------------------
# D: alias behavior preserved
# ---------------------------------------------------------------------------

class TestAliasBehaviorPreserved:
    @pytest.mark.parametrize("tag_value", ["myorder customer", "MyOrder Customer", "my order customer", "My Order"])
    def test_alias_variants_matched_via_raw_tag_fallback(self, pg_app, tag_value):
        from extensions import db
        from models import Company, Contact, Segment
        from services.contact_audience import resolve_segment_contacts

        company = _company(db, Company)
        _segment(db, Segment, company.id, name="My Order Customer", segment_type="custom")
        contact = _contact(db, Contact, company.id, tags=tag_value)
        db.session.commit()

        result = resolve_segment_contacts(company.id, segment="My Order Customer")
        assert [c.id for c in result] == [contact.id]

    def test_bare_myorder_not_matched(self, pg_app):
        """Bare 'myorder' stays out of MY_ORDER_CUSTOMER_ALIASES by design
        (see e023109f) -- the audience fix must not widen this."""
        from extensions import db
        from models import Company, Contact, Segment
        from services.contact_audience import resolve_segment_contacts

        company = _company(db, Company)
        _segment(db, Segment, company.id, name="My Order Customer", segment_type="custom")
        _contact(db, Contact, company.id, tags="MyOrder")
        db.session.commit()

        result = resolve_segment_contacts(company.id, segment="My Order Customer")
        assert result == []


# ---------------------------------------------------------------------------
# E/F: fail closed, never silently create
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_unknown_segment_name_fails_closed_without_creating_a_segment(self, pg_app):
        from extensions import db
        from models import Company, Segment
        from services.contact_audience import resolve_segment_contacts

        company = _company(db, Company)
        db.session.commit()
        before = Segment.query.filter_by(company_id=company.id).count()

        with pytest.raises(ValueError):
            resolve_segment_contacts(company.id, segment="my order")

        db.session.rollback()
        after = Segment.query.filter_by(company_id=company.id).count()
        assert before == after == 0

    def test_ambiguous_marketing_segment_name_fails_closed(self, pg_app):
        from extensions import db
        from models import Company, Segment
        from services.contact_audience import resolve_segment_contacts, AmbiguousAudienceSegment

        company = _company(db, Company)
        _segment(db, Segment, company.id, name="VIP", segment_type="custom")
        _segment(db, Segment, company.id, name="VIP", segment_type="imported_list")
        db.session.commit()

        with pytest.raises(AmbiguousAudienceSegment):
            resolve_segment_contacts(company.id, segment="VIP")


# ---------------------------------------------------------------------------
# G/H: preview non-mutating, preview == send population
# ---------------------------------------------------------------------------

class TestPreviewSendParity:
    def test_preview_does_not_mutate_db_state(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember, SMSCampaign
        from services.contact_audience import resolve_sms_campaign_recipients

        company = _company(db, Company)
        seg = _segment(db, Segment, company.id, name="Marketing Audience", segment_type="custom")
        contact = _contact(db, Contact, company.id, tags="Marketing Audience",
                            normalized_phone="+12025550222", sms_marketing_opt_in=True,
                            sms_consent_status="opted_in")
        _member(db, SegmentMember, seg.id, contact.id)
        campaign = _campaign(db, SMSCampaign, company.id, segment="Marketing Audience")
        db.session.commit()

        member_count_before = SegmentMember.query.filter_by(segment_id=seg.id).count()
        segment_count_before = Segment.query.filter_by(company_id=company.id).count()

        resolve_sms_campaign_recipients(campaign, materialize=False)

        assert db.session.new == set() or not db.session.new
        assert SegmentMember.query.filter_by(segment_id=seg.id).count() == member_count_before
        assert Segment.query.filter_by(company_id=company.id).count() == segment_count_before

    def test_send_and_preview_resolve_identical_population(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember, SMSCampaign
        from services.contact_audience import resolve_sms_campaign_recipients

        company = _company(db, Company)
        seg = _segment(db, Segment, company.id, name="Marketing Audience", segment_type="custom")
        contact = _contact(db, Contact, company.id, tags="Marketing Audience",
                            normalized_phone="+12025550333", sms_marketing_opt_in=True,
                            sms_consent_status="opted_in")
        _member(db, SegmentMember, seg.id, contact.id)
        campaign = _campaign(db, SMSCampaign, company.id, segment="Marketing Audience")
        db.session.commit()

        preview = resolve_sms_campaign_recipients(campaign, materialize=False)
        send = resolve_sms_campaign_recipients(campaign, materialize=True)
        db.session.commit()

        preview_ids = sorted(c.id for c, _phone in preview["recipients"])
        send_ids = sorted(c.id for c, _phone in send["recipients"])
        assert preview_ids == send_ids == [contact.id]


# ---------------------------------------------------------------------------
# I/J: tenant isolation
# ---------------------------------------------------------------------------

class TestTenantIsolation:
    def test_campaign_cannot_resolve_another_companys_segment(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.contact_audience import resolve_segment_contacts

        company_a = _company(db, Company, name="CoA")
        company_b = _company(db, Company, name="CoB")
        seg_b = _segment(db, Segment, company_b.id, name="Shared Name", segment_type="custom")
        contact_b = _contact(db, Contact, company_b.id, tags="Shared Name")
        _member(db, SegmentMember, seg_b.id, contact_b.id)
        db.session.commit()

        # company_a has no segment named "Shared Name" of its own -- must fail
        # closed, never reach across into company_b's segment/members.
        with pytest.raises(ValueError):
            resolve_segment_contacts(company_a.id, segment="Shared Name")

    def test_tag_ids_scoped_to_owning_company(self, pg_app):
        from extensions import db
        from models import Company, Segment
        from services.contact_audience import canonical_tag_ids

        company_a = _company(db, Company, name="CoA2")
        company_b = _company(db, Company, name="CoB2")
        seg_b = _segment(db, Segment, company_b.id, name="Other Co Segment", segment_type="custom")
        db.session.commit()

        with pytest.raises(ValueError):
            canonical_tag_ids(company_a.id, tag_ids=[seg_b.id])


# ---------------------------------------------------------------------------
# K/L: Segment #10-shaped population stays correct, Segment #9-shaped anchor
# is left alone by the marketing resolver
# ---------------------------------------------------------------------------

class TestSegment9And10Shapes:
    def test_segment_10_shaped_population_unaffected(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.contact_audience import resolve_segment_contacts

        company = _company(db, Company)
        _segment(db, Segment, company.id, name="My Order Customer", segment_type="contact_tag", is_dynamic=False)
        seg10 = _segment(db, Segment, company.id, name="My Order Customer", segment_type="custom")
        contacts = [_contact(db, Contact, company.id, tags="My Order Customer") for _ in range(5)]
        for c in contacts:
            _member(db, SegmentMember, seg10.id, c.id)
        db.session.commit()

        result = resolve_segment_contacts(company.id, segment="My Order Customer")
        assert {c.id for c in result} == {c.id for c in contacts}
        assert SegmentMember.query.filter_by(segment_id=seg10.id).count() == 5

    def test_segment_9_shaped_anchor_never_appears_in_marketing_resolution(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.contact_audience import resolve_segment_contacts

        company = _company(db, Company)
        seg9 = _segment(db, Segment, company.id, name="My Order Customer", segment_type="contact_tag", is_dynamic=False)
        seg10 = _segment(db, Segment, company.id, name="My Order Customer", segment_type="custom")
        only_on_9 = _contact(db, Contact, company.id, tags="unrelated-tag")
        _member(db, SegmentMember, seg9.id, only_on_9.id)
        db.session.commit()

        result = resolve_segment_contacts(company.id, segment="My Order Customer")
        assert only_on_9.id not in {c.id for c in result}
        # segment #9 itself is untouched by this read-only resolution
        assert SegmentMember.query.filter_by(segment_id=seg9.id).count() == 1
