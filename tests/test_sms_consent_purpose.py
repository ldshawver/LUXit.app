"""Minimum-viable purpose-specific SMS eligibility.

customer_relationship / conversational_sms_evidence / transactional /
promotional / suppressed are distinct. STOP overrides all. The backfill and
this module never mutate consent state. Promotional stays the strict default
so existing campaigns are unchanged.
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
from models import (Company, Contact, Segment, SegmentMember, TwilioConversation,
                    TwilioMessage, User, UserCompanyAccess)
from services.sms_consent_purpose import (
    DEFAULT_CAMPAIGN_PURPOSE, classify, is_eligible_for_purpose,
    is_suppressed, normalize_campaign_purpose)


@pytest.fixture
def app_ctx():
    app = create_app()
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        a = Company(name="Tenant A")
        b = Company(name="Tenant B")
        db.session.add_all([a, b]); db.session.commit()
        yield app, a, b
        db.session.remove(); db.drop_all()


def _contact(company, phone, **kw):
    kw.setdefault("is_active", True)
    c = Contact(company_id=company.id, phone=phone, normalized_phone=phone, **kw)
    db.session.add(c); db.session.flush()
    return c


_SID_SEQ = [0]


def _inbound(company, from_number, sid=None):
    _SID_SEQ[0] += 1
    sid = sid or f"SM{_SID_SEQ[0]:032d}"
    conv = TwilioConversation(company_id=company.id, from_number=from_number, to_number="+18005550000")
    db.session.add(conv); db.session.flush()
    db.session.add(TwilioMessage(conversation_id=conv.id, company_id=company.id, twilio_sid=sid,
                                 direction="inbound", from_number=from_number, to_number="+18005550000"))
    db.session.flush()


def _seg(company):
    s = Segment(company_id=company.id, name="My Order Customer", segment_type="custom",
                is_dynamic=True, is_active=True, match_mode="any",
                conditions={"schema_version": 1, "match_mode": "any",
                            "rules": [{"field": "tag", "op": "in", "value": ["My Order Customer"]}]})
    db.session.add(s); db.session.flush()
    return s


def test_normalize_campaign_purpose_defaults_to_promotional():
    assert normalize_campaign_purpose(None) == "promotional"
    assert normalize_campaign_purpose("garbage") == "promotional"
    assert normalize_campaign_purpose("Conversational/Follow-up") == "conversational"
    assert normalize_campaign_purpose("informational") == "transactional"
    assert normalize_campaign_purpose("MARKETING") == "promotional"
    assert DEFAULT_CAMPAIGN_PURPOSE == "promotional"


def test_conversational_evidence_from_inbound_metadata_only(app_ctx):
    _, a, _ = app_ctx
    texted = _contact(a, "+19165550001")
    never = _contact(a, "+19165550002")
    _inbound(a, "(916) 555-0001")           # format differs; digit-match still holds
    db.session.commit()

    ct = classify(a.id, texted)
    cn = classify(a.id, never)
    assert ct["conversational_sms_evidence"] is True
    assert ct["transactional_eligibility"] is True
    assert cn["conversational_sms_evidence"] is False


def test_promotional_requires_affirmative_optin(app_ctx):
    _, a, _ = app_ctx
    optin = _contact(a, "+19165550001", sms_marketing_opt_in=True, sms_consent_status="opted_in")
    unknown = _contact(a, "+19165550002", sms_consent_status="unknown")
    _inbound(a, "+19165550001"); _inbound(a, "+19165550002")
    db.session.commit()

    assert classify(a.id, optin)["promotional_eligibility"] is True
    # prior inbound SMS does NOT confer promotional eligibility
    assert classify(a.id, unknown)["promotional_eligibility"] is False
    assert classify(a.id, unknown)["conversational_sms_evidence"] is True


def test_stop_overrides_every_purpose(app_ctx):
    _, a, _ = app_ctx
    stopped = _contact(a, "+19165550001", sms_marketing_opt_in=True, sms_consent_status="opted_in",
                       sms_opted_out=True)
    _inbound(a, "+19165550001")
    db.session.commit()

    cls = classify(a.id, stopped)
    assert cls["suppressed"] is True
    for p in ("conversational", "transactional", "promotional"):
        assert is_eligible_for_purpose(p, cls) is False


def test_classify_never_mutates_consent_fields(app_ctx):
    _, a, _ = app_ctx
    c = _contact(a, "+19165550001", sms_consent_status="unknown", sms_marketing_opt_in=False)
    _inbound(a, "+19165550001")
    db.session.commit()
    before = (c.sms_consent_status, c.sms_marketing_opt_in, c.sms_opted_out, c.do_not_market)
    classify(a.id, c)
    assert (c.sms_consent_status, c.sms_marketing_opt_in, c.sms_opted_out, c.do_not_market) == before


def test_conversational_evidence_is_tenant_scoped(app_ctx):
    _, a, b = app_ctx
    ca = _contact(a, "+19165550001")
    _inbound(b, "+19165550001")   # inbound belongs to the OTHER tenant
    db.session.commit()
    assert classify(a.id, ca)["conversational_sms_evidence"] is False


# --- resolver / four-bucket preview ---------------------------------------- #

@pytest.fixture
def client_ctx(app_ctx):
    app, a, b = app_ctx
    u = User(username="p-admin", email="p@example.com", password_hash=generate_password_hash("pw"),
             is_admin=True, default_company_id=a.id)
    db.session.add(u); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=a.id, role="admin", is_default=True))
    db.session.commit()
    cl = app.test_client()
    with cl.session_transaction() as s:
        s["_user_id"] = str(u.id); s["_fresh"] = True
    return app, a, b, cl


def _seed(a):
    seg = _seg(a)
    # opted-in + texted  -> eligible for all purposes
    _contact(a, "+19165550001", tags="My Order Customer", sms_marketing_opt_in=True, sms_consent_status="opted_in")
    _inbound(a, "+19165550001")
    # texted, no opt-in   -> conversational/transactional only
    _contact(a, "+19165550002", tags="My Order Customer", sms_consent_status="unknown")
    _inbound(a, "+19165550002")
    # no evidence at all   -> none
    _contact(a, "+19165550003", tags="My Order Customer", sms_consent_status="unknown")
    # STOP                 -> none, even though texted + opted-in
    _contact(a, "+19165550004", tags="My Order Customer", sms_marketing_opt_in=True,
             sms_consent_status="opted_in", sms_opted_out=True)
    _inbound(a, "+19165550004")
    db.session.commit()
    return seg


def test_four_bucket_preview_and_purpose_selects_the_rule(client_ctx):
    _, a, _, cl = client_ctx
    seg = _seed(a)
    from services.crm_automation import backfill_apply
    backfill_apply(a.id); db.session.commit()

    promo = cl.get(f"/api/segments/{seg.id}/audience-preview").get_json()
    assert promo["selected_campaign_purpose"] == "promotional"
    assert promo["total_customer_segment"] == 4
    assert promo["stop_suppressed"] == 1
    assert promo["conversational_followup_eligible"] == 2   # #1 and #2 (not #4 STOP)
    assert promo["transactional_informational_eligible"] == 2
    assert promo["promotional_marketing_eligible"] == 1     # only #1
    assert promo["final_sms_eligible"] == 1                 # promotional rule

    conv = cl.get(f"/api/segments/{seg.id}/audience-preview?purpose=conversational").get_json()
    assert conv["selected_campaign_purpose"] == "conversational"
    assert conv["final_sms_eligible"] == 2                  # conversational rule
    assert conv["promotional_marketing_eligible"] == 1      # bucket counts unchanged

    trans = cl.get(f"/api/segments/{seg.id}/audience-preview?purpose=transactional/informational").get_json()
    assert trans["final_sms_eligible"] == 2


def test_default_promotional_behaviour_is_unchanged_without_a_purpose(client_ctx):
    """A campaign with no campaign_purpose must resolve exactly as the strict
    opted-in rule (regression guard for existing campaigns)."""
    _, a, _, _ = client_ctx
    seg = _seed(a)
    from types import SimpleNamespace
    from services.contact_audience import resolve_sms_campaign_recipients
    no_purpose = SimpleNamespace(company_id=a.id, segment=None, id=None,
                                 audience_filter={"selected_tag_ids": [seg.id]})
    counts = resolve_sms_campaign_recipients(no_purpose)["counts"]
    assert counts["campaign_purpose"] == "promotional"
    assert counts["eligible_recipients"] == 1
