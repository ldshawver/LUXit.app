"""Workstream A: My Order Customer segment backfill (A1) + campaign audience
preview / execution parity (A4/A5).

Membership = customer/business classification. SMS eligibility = membership +
affirmative consent + STOP/suppression/phone/dedupe. The two must never be
conflated, and the backfill must never touch consent state.
"""
import os

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")

from app import create_app
from extensions import db
from models import Company, Contact, Segment, SegmentMember, User, UserCompanyAccess
from services.crm_automation import backfill_apply, ensure_my_order_automation


@pytest.fixture
def app_ctx():
    app = create_app()
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        a = Company(name="Tenant A")
        b = Company(name="Tenant B")
        db.session.add_all([a, b])
        db.session.commit()
        yield app, a, b
        db.session.remove()
        db.drop_all()


def _c(company, tags=None, **kw):
    kw.setdefault("is_active", True)
    row = Contact(company_id=company.id, tags=tags, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _dynamic_my_order_segment(company):
    """The prod shape of segment #10."""
    s = Segment(
        company_id=company.id, name="My Order Customer", segment_type="custom",
        is_dynamic=True, is_active=True, match_mode="any",
        conditions={"schema_version": 1, "match_mode": "any", "rules": [
            {"field": "tag", "op": "in", "value": ["My Order Customer", "MyOrder Customer", "My Order"]}
        ]},
        triggers=["tag_added"],
    )
    db.session.add(s)
    db.session.flush()
    return s


def _members(segment_id):
    return {m.contact_id for m in SegmentMember.query.filter_by(segment_id=segment_id).all()
            if not m.removed_at and not m.is_excluded}


# --------------------------------------------------------------------------- #
# A1 — backfill                                                               #
# --------------------------------------------------------------------------- #

def test_backfill_adds_historically_tagged_customer_to_segment(app_ctx):
    _, a, _ = app_ctx
    seg = _dynamic_my_order_segment(a)
    tagged = _c(a, tags="MyOrder Customer, new-lead", phone="+19165551212", normalized_phone="+19165551212")
    _c(a, tags="new-lead")  # not a customer
    db.session.commit()

    r = backfill_apply(a.id)
    db.session.commit()

    assert r["segment_id"] == seg.id
    assert r["additions"] == 1
    assert _members(seg.id) == {tagged.id}


@pytest.mark.parametrize("variant", ["My Order Customer", "MyOrder Customer", "My Order",
                                     "my order customer", "  MyOrder   Customer "])
def test_backfill_recognizes_legacy_tag_variants(app_ctx, variant):
    _, a, _ = app_ctx
    seg = _dynamic_my_order_segment(a)
    c = _c(a, tags=f"{variant}, x", phone="+19165550000")
    db.session.commit()
    backfill_apply(a.id)
    db.session.commit()
    assert c.id in _members(seg.id)
    # original tag text is preserved (provenance intact)
    assert variant.strip() in c.tags or "MyOrder" in c.tags or "My Order" in c.tags


def test_backfill_is_idempotent_second_run_zero_changes(app_ctx):
    _, a, _ = app_ctx
    _dynamic_my_order_segment(a)
    _c(a, tags="My Order Customer", phone="+19165551212")
    _c(a, tags="MyOrder Customer", phone="+19165559999")
    db.session.commit()

    first = backfill_apply(a.id); db.session.commit()
    second = backfill_apply(a.id); db.session.commit()

    assert first["additions"] == 2
    assert second["additions"] == 0 and second["removals"] == 0


def test_backfill_no_duplicate_membership_on_repeat(app_ctx):
    _, a, _ = app_ctx
    seg = _dynamic_my_order_segment(a)
    c = _c(a, tags="My Order Customer", phone="+19165551212")
    db.session.commit()
    for _ in range(3):
        backfill_apply(a.id); db.session.commit()
    assert SegmentMember.query.filter_by(segment_id=seg.id, contact_id=c.id).count() == 1


def test_backfill_excludes_merged_and_inactive_contacts(app_ctx):
    _, a, _ = app_ctx
    seg = _dynamic_my_order_segment(a)
    primary = _c(a, tags="My Order Customer", phone="+19165551212")
    _c(a, tags="My Order Customer", phone="+19165551212", is_active=False)
    _c(a, tags="My Order Customer", phone="+19165551212", merged_into_contact_id=primary.id)
    db.session.commit()

    backfill_apply(a.id); db.session.commit()
    assert _members(seg.id) == {primary.id}


def test_backfill_is_tenant_scoped(app_ctx):
    _, a, b = app_ctx
    seg_a = _dynamic_my_order_segment(a)
    _dynamic_my_order_segment(b)
    _c(b, tags="My Order Customer", phone="+19165551212")  # wrong tenant
    db.session.commit()

    backfill_apply(a.id); db.session.commit()
    assert _members(seg_a.id) == set()


def test_backfill_never_mutates_consent_or_stop_state(app_ctx):
    _, a, _ = app_ctx
    _dynamic_my_order_segment(a)
    c = _c(a, tags="My Order Customer", phone="+19165551212",
           sms_consent_status="unknown", sms_marketing_opt_in=False,
           sms_opted_out=True, do_not_sms=True, do_not_market=True)
    db.session.commit()
    before = (c.sms_consent_status, c.sms_marketing_opt_in, c.sms_opted_out,
              c.do_not_sms, c.do_not_market)

    backfill_apply(a.id); db.session.commit()

    assert (c.sms_consent_status, c.sms_marketing_opt_in, c.sms_opted_out,
            c.do_not_sms, c.do_not_market) == before


def test_backfill_handles_nondynamic_canonical_anchor(app_ctx):
    """When only the ensure_my_order_automation()-created behavioral anchor
    exists (no custom dynamic segment), backfill still adds qualifying
    contacts idempotently."""
    _, a, _ = app_ctx
    c = _c(a, tags="My Order Customer", phone="+19165551212")
    db.session.commit()
    seg = ensure_my_order_automation(a.id)["segment"]

    r1 = backfill_apply(a.id); db.session.commit()
    r2 = backfill_apply(a.id); db.session.commit()

    assert c.id in _members(seg.id)
    assert r1["additions"] == 1 and r2["additions"] == 0


# --------------------------------------------------------------------------- #
# A4 / A5 — campaign audience preview + preview==execution parity             #
# --------------------------------------------------------------------------- #

@pytest.fixture
def logged_client(app_ctx):
    app, a, b = app_ctx
    user = User(username="seg-admin", email="seg@example.com",
                password_hash=generate_password_hash("pw"), is_admin=True, default_company_id=a.id)
    db.session.add(user); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=user.id, company_id=a.id, role="admin", is_default=True))
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(user.id); s["_fresh"] = True
    return app, a, b, client


def _seed_audience(company):
    seg = _dynamic_my_order_segment(company)
    # eligible: opted in
    _c(company, tags="My Order Customer", phone="+19165550001", normalized_phone="+19165550001",
       sms_marketing_opt_in=True, sms_consent_status="opted_in")
    # segment member but NO affirmative consent
    _c(company, tags="My Order Customer", phone="+19165550002", normalized_phone="+19165550002",
       sms_consent_status="unknown")
    # segment member but STOP
    _c(company, tags="My Order Customer", phone="+19165550003", normalized_phone="+19165550003",
       sms_marketing_opt_in=True, sms_consent_status="opted_in", sms_opted_out=True)
    # same phone as #1 in a different format -> dedupe to one recipient
    _c(company, tags="My Order Customer", phone="(916) 555-0001", normalized_phone="+19165550001",
       sms_marketing_opt_in=True, sms_consent_status="opted_in")
    db.session.commit()
    return seg


def test_audience_preview_separates_membership_from_eligibility(logged_client):
    app, a, _, client = logged_client
    seg = _seed_audience(a)
    backfill_apply(a.id); db.session.commit()

    r = client.get(f"/api/segments/{seg.id}/audience-preview")
    assert r.status_code == 200
    body = r.get_json()
    assert body["membership_is_not_sms_consent"] is True
    assert body["segment_members"] == 4
    assert body["no_affirmative_consent"] >= 1
    assert body["stop_suppressed"] >= 1
    assert body["duplicate_phone_exclusions"] == 1
    assert body["final_sms_eligible"] == 1          # only the one opted-in unique number
    assert body["final_sms_eligible"] < body["segment_members"]


def test_audience_preview_matches_campaign_execution_population(logged_client):
    app, a, _, client = logged_client
    seg = _seed_audience(a)
    backfill_apply(a.id); db.session.commit()

    preview = client.get(f"/api/segments/{seg.id}/audience-preview").get_json()

    from types import SimpleNamespace
    from services.contact_audience import resolve_sms_campaign_recipients
    probe = SimpleNamespace(company_id=a.id, segment=None, id=None,
                            audience_filter={"selected_tag_ids": [seg.id]})
    exec_counts = resolve_sms_campaign_recipients(probe, materialize=False)["counts"]

    assert preview["final_sms_eligible"] == exec_counts["eligible_recipients"]
    assert preview["segment_members"] == exec_counts["matching_contacts"]
    assert preview["unique_valid_phones"] == exec_counts["unique_phone_numbers"]


def test_audience_preview_fails_closed_for_a_contact_tag_anchor_segment(logged_client):
    app, a, _, client = logged_client
    anchor = Segment(company_id=a.id, name="My Order Customer", segment_type="contact_tag", is_active=True)
    db.session.add(anchor); db.session.commit()

    r = client.get(f"/api/segments/{anchor.id}/audience-preview")
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert r.get_json()["final_sms_eligible"] == 0

    r2 = client.get(f"/api/segments/{anchor.id}/audience-preview")
    assert r2.status_code == r.status_code


def test_audience_preview_is_tenant_scoped(logged_client):
    app, a, b, client = logged_client
    other_seg = _dynamic_my_order_segment(b)
    db.session.commit()
    r = client.get(f"/api/segments/{other_seg.id}/audience-preview")
    assert r.status_code == 404
