"""Promotional opt-in workflow: audience derivation, operator-approved
solicitation, contextual YES -> consent (idempotent), STOP precedence, and
campaign-resolver integration."""
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import create_app
from extensions import db
from models import (
    Company, Contact, PromotionalConsentEvent, PromotionalOptInSolicitation,
    Segment, SegmentMember, TwilioAccount, TwilioConversation, TwilioMessage,
    User, user_company,
)


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


def _company(name="Promo Co"):
    c = Company(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def _admin(company, email="promo-admin@example.com"):
    u = User(username=email, email=email, is_admin=True)
    u.password_hash = "x"
    db.session.add(u)
    db.session.flush()
    u.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=u.id, company_id=company.id, is_default=True))
    db.session.flush()
    return u


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _twilio_account(company, from_phone="+15559999999"):
    account = TwilioAccount(company_id=company.id, from_phone=from_phone, is_active=True)
    account.set_account_sid("ACtest")
    account.set_auth_token("token")
    db.session.add(account)
    db.session.flush()
    return account


def _contact(company, phone, **kw):
    c = Contact(company_id=company.id, phone=phone, normalized_phone=phone,
                is_active=True, **kw)
    db.session.add(c)
    db.session.flush()
    return c


def _inbound_evidence(company, phone, sid="SMEVID"):
    conv = TwilioConversation(company_id=company.id, from_number=phone, to_number="+15559999999")
    db.session.add(conv)
    db.session.flush()
    db.session.add(TwilioMessage(company_id=company.id, conversation_id=conv.id, direction="inbound",
                                 twilio_sid=sid, from_number=phone, body="hi"))
    db.session.flush()


def _marketing_segment(company, contact_ids):
    seg = Segment(company_id=company.id, name="My Order Customer", segment_type="custom",
                  match_mode="any", is_active=True)
    db.session.add(seg)
    db.session.flush()
    for cid in contact_ids:
        db.session.add(SegmentMember(segment_id=seg.id, contact_id=cid, source="manual", is_excluded=False))
    db.session.flush()
    return seg


def _promo_campaign(company, seg):
    return SimpleNamespace(company_id=company.id, id=None, segment=None,
                           audience_filter={"selected_tag_ids": [seg.id], "campaign_purpose": "promotional"})


# ---------------------------------------------------------------------------

def test_audience_buckets(app):
    from services.promotional_optin import classify_audience
    co = _company()
    # needs opt-in: customer + inbound evidence, no marketing opt-in, not STOP
    needs = _contact(co, "+14155550001")
    _inbound_evidence(co, "+14155550001", "SM1")
    # already promotional
    already = _contact(co, "+14155550002", sms_marketing_opt_in=True, sms_consent_status="opted_in")
    _inbound_evidence(co, "+14155550002", "SM2")
    # STOP / suppressed
    stopped = _contact(co, "+14155550003", sms_opted_out=True, sms_opt_out_at=datetime.utcnow())
    _inbound_evidence(co, "+14155550003", "SM3")
    # no conversational evidence
    noconv = _contact(co, "+14155550004")
    seg = _marketing_segment(co, [needs.id, already.id, stopped.id, noconv.id])

    counts = classify_audience(co.id, seg.id)["counts"]
    assert counts["my_order_customers"] == 4
    assert counts["needs_promotional_optin"] == 1
    assert counts["already_promotional"] == 1
    assert counts["stop_suppressed"] == 1
    assert counts["no_conversational_evidence"] == 1


def test_create_solicitation_is_idempotent(app):
    from services.promotional_optin import create_solicitation, get_pending_solicitation
    co = _company()
    c = _contact(co, "+14155550010")
    _inbound_evidence(co, "+14155550010", "SM10")
    r1 = create_solicitation(co.id, c.id, business_phone_number="+15559999999", actor_user_id=None)
    assert r1["ok"] and r1["created"] is True
    r2 = create_solicitation(co.id, c.id, business_phone_number="+15559999999", actor_user_id=None)
    assert r2["ok"] and r2["created"] is False
    assert PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).count() == 1
    # creating a solicitation never mutates consent
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False
    assert get_pending_solicitation(co.id, c.id) is not None


def test_generic_yes_without_solicitation_grants_nothing(app):
    from services.promotional_optin import record_contextual_yes
    co = _company()
    c = _contact(co, "+14155550020")
    out = record_contextual_yes(co.id, "+14155550020", "+15559999999", "SMYESNOCTX", keyword="yes")
    assert out["matched"] is False
    assert PromotionalConsentEvent.query.count() == 0
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False


def test_contextual_yes_grants_once_and_is_idempotent(app):
    from services.promotional_optin import create_solicitation, record_contextual_yes
    co = _company()
    c = _contact(co, "+14155550030")
    _inbound_evidence(co, "+14155550030", "SM30")
    create_solicitation(co.id, c.id, business_phone_number="+15559999999")

    seg = _marketing_segment(co, [c.id])
    from services.contact_audience import resolve_sms_campaign_recipients
    before = resolve_sms_campaign_recipients(_promo_campaign(co, seg))["counts"]
    assert before["eligible_recipients"] == 0

    out1 = record_contextual_yes(co.id, "+14155550030", "+15559999999", "SMYES1", keyword="yes")
    assert out1["matched"] and out1["granted"] is True
    db.session.commit()

    db.session.refresh(c)
    assert c.sms_marketing_opt_in is True
    assert c.sms_consent_status == "opted_in"
    assert c.sms_marketing_opt_in_source == "promotional_optin_sms_yes"

    ev = PromotionalConsentEvent.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert ev.inbound_message_sid == "SMYES1"
    assert ev.canonical_phone == "+14155550030"
    assert ev.business_phone_number == "+15559999999"
    assert ev.consent_purpose == "promotional"
    assert ev.consent_source == "sms_reply_yes"
    assert ev.solicited_at is not None

    sol = PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert sol.status == "consented"
    assert sol.consent_message_sid == "SMYES1"

    # duplicate webhook delivery: same SID -> no second event, still eligible once
    out2 = record_contextual_yes(co.id, "+14155550030", "+15559999999", "SMYES1", keyword="yes")
    assert out2["matched"] and out2.get("duplicate") is True
    assert PromotionalConsentEvent.query.count() == 1

    after = resolve_sms_campaign_recipients(_promo_campaign(co, seg))["counts"]
    assert after["eligible_recipients"] == 1

    # STOP -> excluded again
    from twilio_sms import _update_contact_sms_consent
    _update_contact_sms_consent(co.id, "+14155550030", False, "keyword:stop")
    db.session.commit()
    excluded = resolve_sms_campaign_recipients(_promo_campaign(co, seg))["counts"]
    assert excluded["eligible_recipients"] == 0


def test_stop_precedence_cancels_pending_and_a_stale_yes_never_revives(app):
    from services.promotional_optin import (
        cancel_pending_for_phone, create_solicitation, record_contextual_yes,
    )
    co = _company()
    c = _contact(co, "+14155550040")
    _inbound_evidence(co, "+14155550040", "SM40")
    create_solicitation(co.id, c.id, business_phone_number="+15559999999")

    # STOP arrives first
    c.sms_opted_out = True
    c.sms_opt_out_at = datetime.utcnow()
    c.sms_consent_status = "opted_out"
    closed = cancel_pending_for_phone(co.id, "+14155550040", reason="stop")
    assert closed == 1
    sol = PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert sol.status == "stopped"

    # a later YES must not grant consent
    out = record_contextual_yes(co.id, "+14155550040", "+15559999999", "SMSTALEYES", keyword="yes")
    assert out.get("granted") is not True
    assert PromotionalConsentEvent.query.count() == 0
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False


def _fresh_contact(co_id):
    db.session.expire_all()
    return Contact.query.filter_by(company_id=co_id).order_by(Contact.id.asc()).first()


def test_webhook_contextual_yes_grants_and_is_idempotent(client, app):
    from services.promotional_optin import create_solicitation
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155550050", identity_status="confirmed")
    _inbound_evidence(co, "+14155550050", "SM50")
    create_solicitation(co.id, c.id, business_phone_number="+15559999999")
    db.session.commit()

    payload = {"From": "+14155550050", "To": "+15559999999", "Body": "YES", "MessageSid": "WBYES1"}
    r1 = client.post("/twilio/sms/inbound", data=payload)
    r2 = client.post("/twilio/sms/inbound", data=payload)  # duplicate delivery
    assert r1.status_code == r2.status_code == 200

    c = _fresh_contact(co.id)
    assert c.sms_marketing_opt_in is True
    assert c.sms_consent_status == "opted_in"
    assert PromotionalConsentEvent.query.filter_by(company_id=co.id).count() == 1


def test_webhook_stop_cancels_pending_solicitation_and_stale_yes_creates_no_event(client, app):
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155550055", identity_status="confirmed")
    _inbound_evidence(co, "+14155550055", "SM55")
    db.session.add(PromotionalOptInSolicitation(
        company_id=co.id, contact_id=c.id, canonical_phone="+14155550055",
        business_phone_number="+15559999999", status="pending"))
    db.session.commit()
    cid = c.id

    client.post("/twilio/sms/inbound", data={"From": "+14155550055", "To": "+15559999999", "Body": "STOP", "MessageSid": "WBSTOP2"})
    db.session.expire_all()
    assert db.session.get(Contact, cid).sms_consent_status == "opted_out"
    assert PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=cid).one().status == "stopped"

    # A later stale YES: the solicitation is closed, so no promotional consent
    # event is created and the closed solicitation is not reopened.
    client.post("/twilio/sms/inbound", data={"From": "+14155550055", "To": "+15559999999", "Body": "YES", "MessageSid": "WBYES3"})
    db.session.expire_all()
    assert PromotionalConsentEvent.query.filter_by(company_id=co.id).count() == 0
    assert PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=cid).one().status == "stopped"


def test_webhook_generic_yes_no_context_does_not_opt_in_unknown_contact(client, app):
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155550060", identity_status="confirmed")
    db.session.commit()
    client.post("/twilio/sms/inbound", data={"From": "+14155550060", "To": "+15559999999", "Body": "YES", "MessageSid": "WBGEN1"})
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False
    assert c.sms_consent_status != "opted_in"
    assert PromotionalConsentEvent.query.count() == 0


def test_webhook_yes_still_resubscribes_an_opted_out_contact(client, app):
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155550070", identity_status="confirmed",
                 sms_opted_out=True, sms_opt_out_at=datetime.utcnow(), sms_consent_status="opted_out")
    db.session.commit()
    resp = client.post("/twilio/sms/inbound", data={"From": "+14155550070", "To": "+15559999999", "Body": "YES", "MessageSid": "WBRESUB1"})
    assert resp.status_code == 200
    db.session.refresh(c)
    assert c.sms_opted_out is False
    assert c.sms_consent_status == "opted_in"


def test_api_overview_and_audience_are_tenant_scoped(client, app):
    from services.promotional_optin import create_solicitation
    co_a = _company("Tenant A")
    co_b = _company("Tenant B")
    admin_a = _admin(co_a, "a-admin@example.com")
    ca = _contact(co_a, "+14155550080")
    _inbound_evidence(co_a, "+14155550080", "SMA80")
    cb = _contact(co_b, "+14155550081")
    _inbound_evidence(co_b, "+14155550081", "SMB81")
    _marketing_segment(co_a, [ca.id])
    _marketing_segment(co_b, [cb.id])
    db.session.commit()

    _login(client, admin_a)
    ov = client.get("/api/promotional-optin/overview")
    assert ov.status_code == 200
    assert ov.get_json()["counts"]["needs_promotional_optin"] == 1

    aud = client.get("/api/promotional-optin/audience")
    assert aud.status_code == 200
    body = aud.get_json()
    assert body["count"] == 1
    assert body["audience"][0]["contact_id"] == ca.id
    assert all(row["contact_id"] != cb.id for row in body["audience"])

    # cannot create a solicitation for another tenant's contact
    resp = client.post("/api/promotional-optin/solicitations", json={"contact_id": cb.id})
    assert resp.status_code == 422


def test_api_audience_requires_admin(client, app):
    co = _company()
    user = _admin(co, "plain@example.com")
    user.is_admin = False
    db.session.flush()
    _login(client, user)
    db.session.commit()
    resp = client.get("/api/promotional-optin/audience")
    assert resp.status_code == 403
