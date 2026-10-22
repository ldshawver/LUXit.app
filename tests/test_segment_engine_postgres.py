"""Regression suite for the canonical segment engine (services/segment_engine.py).

Requires a real PostgreSQL database via TEST_POSTGRES_URL -- the engine's
correctness (JSON condition storage, tenant scoping, delta semantics) needs
real Postgres, not SQLite. Run with:

    TEST_POSTGRES_URL=postgresql://user:pass@host:port/dbname \
        pytest tests/test_segment_engine_postgres.py -v
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def pg_app():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL segment engine tests")
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


def _make_company(db, Company, name="TestCo"):
    c = Company(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def _make_contact(db, Contact, company_id, **kw):
    defaults = dict(company_id=company_id, is_active=True, status="active")
    defaults.update(kw)
    c = Contact(**defaults)
    db.session.add(c)
    db.session.flush()
    return c


def _make_segment(db, Segment, company_id, **kw):
    defaults = dict(company_id=company_id, name="Test Segment", segment_type="custom",
                     match_mode="all", is_dynamic=True, is_active=True)
    defaults.update(kw)
    s = Segment(**defaults)
    db.session.add(s)
    db.session.flush()
    return s


# ---------------------------------------------------------------------------
# Schema validation / fail-closed behavior
# ---------------------------------------------------------------------------

class TestValidation:
    def test_malformed_json_fails_closed(self, pg_app):
        from services.segment_engine import validate_conditions, SegmentConditionError
        with pytest.raises(SegmentConditionError):
            validate_conditions({"nonsense": 1}, "all")

    def test_unsupported_field_rejected(self, pg_app):
        from services.segment_engine import validate_conditions, SegmentConditionError
        with pytest.raises(SegmentConditionError):
            validate_conditions([{"field": "ssn", "op": "equals", "value": "x"}], "all")

    def test_unsupported_operator_rejected(self, pg_app):
        from services.segment_engine import validate_conditions, SegmentConditionError
        with pytest.raises(SegmentConditionError):
            validate_conditions([{"field": "tag", "op": "regex_match", "value": "x"}], "all")

    def test_empty_conditions_valid_and_matches_nothing(self, pg_app):
        from services.segment_engine import validate_conditions
        result = validate_conditions({}, "all")
        assert result["rules"] == []

    def test_in_operator_requires_list(self, pg_app):
        from services.segment_engine import validate_conditions, SegmentConditionError
        with pytest.raises(SegmentConditionError):
            validate_conditions([{"field": "tag", "op": "in", "value": "not-a-list"}], "all")

    def test_legacy_format_a_tag_dict_list_value(self, pg_app):
        from services.segment_engine import validate_conditions
        result = validate_conditions({"tag": ["My Order Customer"]}, "any")
        assert result["rules"] == [{"field": "tag", "op": "in", "value": ["My Order Customer"]}]

    def test_legacy_format_a_tag_dict_string_value(self, pg_app):
        """Segment #11's actual stored shape: {"tag": "My Order Customer"} (bare string)."""
        from services.segment_engine import validate_conditions
        result = validate_conditions({"tag": "My Order Customer"}, "all")
        assert result["rules"] == [{"field": "tag", "op": "in", "value": ["My Order Customer"]}]

    def test_legacy_format_b_field_value_list(self, pg_app):
        from services.segment_engine import validate_conditions
        result = validate_conditions([{"field": "source", "value": "sms_inbound"}], "all")
        assert result["rules"] == [{"field": "source", "op": "equals", "value": "sms_inbound"}]

    def test_ambiguous_legacy_shape_rejected_not_guessed(self, pg_app):
        from services.segment_engine import validate_conditions, SegmentConditionError
        with pytest.raises(SegmentConditionError):
            validate_conditions("just a string", "all")

    def test_segment_10_stored_condition_is_canonical(self, pg_app):
        """Exact fixture reproducing Production segment #10's stored conditions."""
        from services.segment_engine import validate_conditions
        result = validate_conditions({"tag": ["My Order Customer"]}, "any")
        assert result["match_mode"] == "any"
        assert result["rules"][0]["field"] == "tag"
        assert result["rules"][0]["op"] == "in"


# ---------------------------------------------------------------------------
# Per-contact evaluation: tag matching, normalization, ANY/ALL
# ---------------------------------------------------------------------------

class TestEvaluate:
    def test_tag_equals_case_and_whitespace_normalized(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="  MY ORDER CUSTOMER  , other")
        segment = _make_segment(db, Segment, company.id,
                                 conditions={"tag": ["my order customer"]}, match_mode="any")
        assert evaluate(segment, contact) is True

    @pytest.mark.parametrize("tag_value", ["My Order", "MyOrder Customer", "My Order Customer"])
    def test_my_order_variants_each_match_their_own_condition(self, pg_app, tag_value):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags=tag_value)
        segment = _make_segment(db, Segment, company.id,
                                 conditions={"tag": [tag_value]}, match_mode="any")
        assert evaluate(segment, contact) is True

    @pytest.mark.parametrize("tag_value", ["My Order Customer", "MyOrder Customer", "My Order",
                                            "  myorder   customer  "])
    def test_my_order_alias_variants_all_match_segment_10_actual_condition(self, pg_app, tag_value):
        """Segment #10's real stored condition names only the canonical spelling
        ({"tag": ["My Order Customer"]}), but production contacts carry a mix of
        historical alias spellings. The evaluator must expand through the shared
        MY_ORDER_CUSTOMER_ALIASES registry (services/crm_automation.py) so every
        alias-tagged contact still qualifies for the narrowly-worded condition --
        without hardcoding the alias list a second time in this module."""
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags=tag_value)
        segment = _make_segment(db, Segment, company.id,
                                 conditions={"tag": ["My Order Customer"]}, match_mode="any")
        assert evaluate(segment, contact) is True

    @pytest.mark.parametrize("tag_value", ["VIP, Newsletter", "MyOrder", "MYORDER"])
    def test_unrelated_tag_does_not_qualify_for_my_order_customer(self, pg_app, tag_value):
        """Bare "MyOrder"/"myorder" (no "Customer"/"Order" qualifier) is
        deliberately excluded from MY_ORDER_CUSTOMER_ALIASES -- see
        tests/test_my_order_crm_automation.py::test_bare_myorder_is_not_a_customer_tag_alias,
        a considered product decision from the original CRM automation feature
        (commit 5c78657), not something this engine should silently widen."""
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags=tag_value)
        segment = _make_segment(db, Segment, company.id,
                                 conditions={"tag": ["My Order Customer"]}, match_mode="any")
        assert evaluate(segment, contact) is False

    def test_duplicate_alias_tags_on_one_contact_produce_single_membership(self, pg_app):
        """A contact carrying more than one alias spelling at once (e.g. from a
        messy import) must still produce exactly one SegmentMember row, never
        one per matching tag."""
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="MyOrder Customer, My Order")
        segment = _make_segment(db, Segment, company.id,
                                 conditions={"tag": ["My Order Customer"]}, match_mode="any")
        db.session.commit()

        result = refresh(segment)
        db.session.commit()

        assert result.additions == {contact.id}
        assert SegmentMember.query.filter_by(segment_id=segment.id, contact_id=contact.id).count() == 1

    def test_multiple_tag_values_any_match(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="MyOrder Customer")
        segment = _make_segment(db, Segment, company.id,
                                 conditions={"tag": ["My Order", "MyOrder Customer"]}, match_mode="any")
        assert evaluate(segment, contact) is True

    def test_any_or_semantics(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="B", approval_status="review_required")
        segment = _make_segment(db, Segment, company.id, match_mode="any", conditions=[
            {"field": "tag", "op": "equals", "value": "A"},
            {"field": "tag", "op": "equals", "value": "B"},
        ])
        assert evaluate(segment, contact) is True

    def test_all_and_semantics(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        match = _make_contact(db, Contact, company.id, tags="A", approval_status="approved")
        nomatch = _make_contact(db, Contact, company.id, tags="A", approval_status="review_required")
        segment = _make_segment(db, Segment, company.id, match_mode="all", conditions=[
            {"field": "tag", "op": "equals", "value": "A"},
            {"field": "approval_status", "op": "equals", "value": "approved"},
        ])
        assert evaluate(segment, match) is True
        assert evaluate(segment, nomatch) is False

    def test_not_equals(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, source="web")
        segment = _make_segment(db, Segment, company.id, match_mode="all",
                                 conditions=[{"field": "source", "op": "not_equals", "value": "sms_inbound"}])
        assert evaluate(segment, contact) is True

    def test_contains(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, email="person@example.com")
        segment = _make_segment(db, Segment, company.id, match_mode="all",
                                 conditions=[{"field": "email", "op": "contains", "value": "@example.com"}])
        assert evaluate(segment, contact) is True

    def test_exists(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        has_email = _make_contact(db, Contact, company.id, email="x@y.com")
        no_email = _make_contact(db, Contact, company.id, email=None)
        segment = _make_segment(db, Segment, company.id, match_mode="all",
                                 conditions=[{"field": "email", "op": "exists"}])
        assert evaluate(segment, has_email) is True
        assert evaluate(segment, no_email) is False

    def test_missing_field_value_is_falsy_not_crash(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import evaluate
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, source=None)
        segment = _make_segment(db, Segment, company.id, match_mode="all",
                                 conditions=[{"field": "source", "op": "equals", "value": "sms_inbound"}])
        assert evaluate(segment, contact) is False


# ---------------------------------------------------------------------------
# Membership computation / refresh: delta semantics, atomicity, fail-closed
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_malformed_condition_zero_adds_zero_removes(self, pg_app):
        """The mandated regression: malformed condition -> error, zero mutation."""
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="A")
        segment = _make_segment(db, Segment, company.id, conditions={"totally": "unrecognized"})
        db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id, source="manual"))
        db.session.commit()
        before = SegmentMember.query.filter_by(segment_id=segment.id).count()

        result = refresh(segment)

        assert result.error is not None
        assert result.additions == set()
        assert result.removals == set()
        after = SegmentMember.query.filter_by(segment_id=segment.id).count()
        assert after == before, "malformed condition must never mutate membership"

    def test_unsupported_segment_type_never_destructive(self, pg_app):
        """contact_tag / automation_rule segments must be skipped, not wiped."""
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id)
        segment = _make_segment(db, Segment, company.id, segment_type="contact_tag", conditions=None)
        db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id, source="manual"))
        db.session.commit()

        result = refresh(segment)

        assert result.skipped_reason is not None
        assert SegmentMember.query.filter_by(segment_id=segment.id).count() == 1

    def test_static_segment_protected_from_dynamic_evaluator(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="unrelated")
        segment = _make_segment(db, Segment, company.id, is_dynamic=False,
                                 conditions={"tag": ["something-else"]})
        db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id, source="manual"))
        db.session.commit()

        result = refresh(segment)

        assert result.skipped_reason is not None
        assert SegmentMember.query.filter_by(segment_id=segment.id).count() == 1

    def test_refresh_additions(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        match = _make_contact(db, Contact, company.id, tags="Qualifies")
        nomatch = _make_contact(db, Contact, company.id, tags="Nope")
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.commit()

        result = refresh(segment)
        db.session.commit()

        assert result.additions == {match.id}
        assert match.id not in result.removals
        members = {m.contact_id for m in SegmentMember.query.filter_by(segment_id=segment.id).all()
                   if not m.removed_at}
        assert members == {match.id}

    def test_refresh_removals_when_qualification_lost(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="no-longer-qualifies")
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id, source="dynamic_rule"))
        db.session.commit()

        result = refresh(segment)
        db.session.commit()

        assert result.removals == {contact.id}
        member = SegmentMember.query.filter_by(segment_id=segment.id, contact_id=contact.id).first()
        assert member is not None, "removal must preserve the audit row (removed_at), not delete it"
        assert member.removed_at is not None

    def test_refresh_idempotent_second_run_zero_changes(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        _make_contact(db, Contact, company.id, tags="Qualifies")
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.commit()

        refresh(segment)
        db.session.commit()
        second = refresh(segment)
        db.session.commit()

        assert second.additions == set()
        assert second.removals == set()

    def test_segment_member_uniqueness_no_duplicates_on_repeated_refresh(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="Qualifies")
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.commit()

        for _ in range(3):
            refresh(segment)
            db.session.commit()

        count = SegmentMember.query.filter_by(segment_id=segment.id, contact_id=contact.id).count()
        assert count == 1

    def test_tenant_isolation_foreign_company_never_matches(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import refresh
        company_a = _make_company(db, Company, "A")
        company_b = _make_company(db, Company, "B")
        _make_contact(db, Contact, company_b.id, tags="Qualifies")  # wrong tenant
        segment = _make_segment(db, Segment, company_a.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.commit()

        result = refresh(segment)

        assert result.desired_member_ids == set()

    def test_merged_contact_excluded_from_membership(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        primary = _make_contact(db, Contact, company.id, tags="Qualifies")
        merged = _make_contact(db, Contact, company.id, tags="Qualifies", merged_into_contact_id=primary.id)
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.commit()

        result = refresh(segment)

        assert primary.id in result.desired_member_ids
        assert merged.id not in result.desired_member_ids

    def test_inactive_contact_excluded_from_membership(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        _make_contact(db, Contact, company.id, tags="Qualifies", is_active=False)
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.commit()

        result = refresh(segment)

        assert result.desired_member_ids == set()

    def test_excluded_member_never_re_added_by_refresh(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        contact = _make_contact(db, Contact, company.id, tags="Qualifies")
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id,
                                      source="manual", is_excluded=True, exclusion_reason="test"))
        db.session.commit()

        result = refresh(segment)

        assert contact.id not in result.additions
        assert contact.id not in result.desired_member_ids

    def test_preview_never_mutates(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import compute_membership_delta
        company = _make_company(db, Company)
        _make_contact(db, Contact, company.id, tags="Qualifies")
        segment = _make_segment(db, Segment, company.id, conditions={"tag": ["Qualifies"]}, match_mode="any")
        db.session.commit()

        compute_membership_delta(segment)
        compute_membership_delta(segment)

        assert SegmentMember.query.filter_by(segment_id=segment.id).count() == 0

    def test_segment_type_all_matches_every_tenant_contact_by_design(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        _make_contact(db, Contact, company.id, tags="")
        _make_contact(db, Contact, company.id, tags="")
        segment = _make_segment(db, Segment, company.id, segment_type="all", conditions=None)
        db.session.commit()

        result = refresh(segment)

        assert len(result.desired_member_ids) == 2


class TestSegment10RealWorldScenario:
    """End-to-end reproduction of Production Segment #10 ("My Order Customer"):
    stored condition {"tag": ["My Order Customer"]}, match_mode=any, populated
    from a mixed-tenant population carrying every historical spelling plus
    contacts that must be excluded on other grounds."""

    def test_full_alias_population_refresh_matches_expected_set_only(self, pg_app):
        from extensions import db
        from models import Company, Contact, Segment, SegmentMember
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        other_company = _make_company(db, Company, "Other")

        canonical = _make_contact(db, Contact, company.id, tags="My Order Customer")
        variant_a = _make_contact(db, Contact, company.id, tags="MyOrder Customer")
        variant_b = _make_contact(db, Contact, company.id, tags="My Order")
        bare_myorder = _make_contact(db, Contact, company.id, tags="MyOrder")
        unrelated = _make_contact(db, Contact, company.id, tags="VIP")
        inactive = _make_contact(db, Contact, company.id, tags="My Order Customer", is_active=False)
        merged = _make_contact(db, Contact, company.id, tags="My Order Customer",
                                merged_into_contact_id=canonical.id)
        wrong_tenant = _make_contact(db, Contact, other_company.id, tags="My Order Customer")

        segment = _make_segment(db, Segment, company.id, name="My Order Customer",
                                 conditions={"tag": ["My Order Customer"]}, match_mode="any")
        db.session.commit()

        result = refresh(segment)
        db.session.commit()

        expected = {canonical.id, variant_a.id, variant_b.id}
        assert result.desired_member_ids == expected
        # Bare "MyOrder" (no "Customer"/"Order" qualifier) is deliberately NOT
        # an alias -- see test_bare_myorder_is_not_a_customer_tag_alias.
        assert bare_myorder.id not in result.desired_member_ids
        assert unrelated.id not in result.desired_member_ids
        assert inactive.id not in result.desired_member_ids
        assert merged.id not in result.desired_member_ids
        assert wrong_tenant.id not in result.desired_member_ids

        members = {m.contact_id for m in SegmentMember.query.filter_by(segment_id=segment.id).all()
                   if not m.removed_at}
        assert members == expected

        second = refresh(segment)
        assert second.additions == set() and second.removals == set()

    def test_opted_out_contact_remains_segment_member_but_is_suppressed_at_send_time(self, pg_app):
        """Segment membership is an eligibility concept; SMS suppression is a
        send-time concern (see services/segment_engine.py module docstring and
        the campaign recipient resolver). An opted-out contact must remain a
        member so headcount/reporting stay accurate, while the send path is
        responsible for excluding them."""
        from extensions import db
        from models import Company, Contact, Segment
        from services.segment_engine import refresh
        company = _make_company(db, Company)
        opted_out = _make_contact(db, Contact, company.id, tags="My Order Customer", sms_opted_out=True)
        segment = _make_segment(db, Segment, company.id,
                                 conditions={"tag": ["My Order Customer"]}, match_mode="any")
        db.session.commit()

        result = refresh(segment)

        assert opted_out.id in result.desired_member_ids
