"""Hosted promotional web opt-in (customer_web_optin):

  - operator generates a signed per-contact consent link (no SMS sent)
  - the customer affirms the disclosure on the hosted page
  - promotional consent is granted through the SAME service path as an inbound
    YES, with immutable evidence (disclosure text + version + request context)
  - idempotent, STOP-authoritative, tamper/cross-tenant safe
  - the existing contextual-YES / STOP / send-flow behaviour is unchanged
"""
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
    os.environ.pop("PROMO_OPTIN_WEB_CONFIRMATION_SMS", None)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret-key")
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


def _member(company, email="member@example.com"):
    u = User(username=email, email=email, is_admin=False)
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


def _contact(company, phone="+14155550101", **kw):
    c = Contact(company_id=company.id, phone=phone, normalized_phone=phone, is_active=True, **kw)
    db.session.add(c)
    db.session.flush()
    return c


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

def test_generate_link_is_idempotent_and_sends_nothing(app):
    from services.promotional_optin import generate_web_optin_link, WEB_OPTIN_DISCLOSURE_VERSION
    co = _company()
    c = _contact(co)
    a = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()
    assert a["ok"] and a["created"] and a["token"]
    assert a["disclosure_version"] == WEB_OPTIN_DISCLOSURE_VERSION

    row = PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert row.status == "pending"
    assert row.source == "web_optin"
    assert row.web_token_jti and row.web_link_disclosure_version == WEB_OPTIN_DISCLOSURE_VERSION
    assert row.solicitation_message_sid is None and row.delivery_status is None  # no SMS

    b = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()
    assert b["created"] is False and b["token"] == a["token"]
    assert PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).count() == 1

    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False  # generating a link grants nothing


def test_generate_link_refuses_suppressed_and_already_promotional(app):
    from services.promotional_optin import generate_web_optin_link
    co = _company()
    stopped = _contact(co, "+14155550110", sms_opted_out=True, sms_opt_out_at=datetime.utcnow())
    promo = _contact(co, "+14155550111", sms_marketing_opt_in=True, sms_consent_status="opted_in")
    nophone = _contact(co, "")

    assert generate_web_optin_link(co.id, stopped.id, actor_user_id=None)["error"] == "contact_suppressed"
    assert generate_web_optin_link(co.id, promo.id, actor_user_id=None)["error"] == "already_promotional"
    assert generate_web_optin_link(co.id, nophone.id, actor_user_id=None)["error"] == "no_canonical_phone"
    assert PromotionalOptInSolicitation.query.count() == 0


def test_web_optin_grants_once_with_immutable_evidence(app):
    from services.promotional_optin import (
        generate_web_optin_link, record_web_optin,
        WEB_OPTIN_DISCLOSURE_VERSION, WEB_OPTIN_DISCLOSURE_TEXT,
    )
    from services.contact_audience import resolve_sms_campaign_recipients
    co = _company()
    c = _contact(co, "+14155550120")
    seg = _marketing_segment(co, [c.id])
    link = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()

    before = resolve_sms_campaign_recipients(_promo_campaign(co, seg))["counts"]
    assert before["eligible_recipients"] == 0

    out = record_web_optin(link["token"], disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION,
                           consent_ip="203.0.113.7", user_agent="pytest-UA", page_url="https://x/promo-optin/t")
    db.session.commit()
    assert out["ok"] and out["granted"] and out["duplicate"] is False
    assert out["confirmation_sent"] is False  # flag off by default

    ev = PromotionalConsentEvent.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert ev.consent_source == "customer_web_optin"
    assert ev.consent_purpose == "promotional"
    assert ev.inbound_message_sid is None
    assert ev.web_token_jti
    assert ev.disclosure_version == WEB_OPTIN_DISCLOSURE_VERSION
    assert ev.disclosure_text == WEB_OPTIN_DISCLOSURE_TEXT
    assert ev.consent_context["ip"] == "203.0.113.7"
    assert ev.consent_context["user_agent"] == "pytest-UA"

    sol = PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert sol.status == "consented" and sol.web_consent_at is not None

    db.session.refresh(c)
    assert c.sms_marketing_opt_in is True
    assert c.sms_consent_status == "opted_in"
    assert c.sms_marketing_opt_in_source == "promotional_web_optin"

    after = resolve_sms_campaign_recipients(_promo_campaign(co, seg))["counts"]
    assert after["eligible_recipients"] == 1


def test_web_optin_duplicate_submission_is_idempotent(app):
    from services.promotional_optin import generate_web_optin_link, record_web_optin, WEB_OPTIN_DISCLOSURE_VERSION
    co = _company()
    c = _contact(co, "+14155550130")
    link = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()

    r1 = record_web_optin(link["token"], disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION)
    db.session.commit()
    r2 = record_web_optin(link["token"], disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION)
    db.session.commit()
    assert r1["granted"] is True
    assert r2["granted"] is False and r2["duplicate"] is True
    assert PromotionalConsentEvent.query.filter_by(company_id=co.id).count() == 1


def test_stop_precedence_web_optin_grants_nothing_and_closes_link(app):
    from services.promotional_optin import generate_web_optin_link, record_web_optin, WEB_OPTIN_DISCLOSURE_VERSION
    co = _company()
    c = _contact(co, "+14155550140")
    link = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()

    c.sms_opted_out = True
    c.sms_opt_out_at = datetime.utcnow()
    c.sms_consent_status = "opted_out"
    db.session.commit()

    out = record_web_optin(link["token"], disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION)
    db.session.commit()
    assert out["ok"] and out.get("granted") is not True and out.get("suppressed") is True
    assert PromotionalConsentEvent.query.count() == 0
    sol = PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert sol.status == "stopped" and sol.closed_reason == "suppressed_at_web_optin"
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False


def test_tampered_and_cross_tenant_tokens_are_rejected(app):
    from services.promotional_optin import (
        generate_web_optin_link, record_web_optin, get_web_optin_context,
        _encode_web_token, WEB_OPTIN_DISCLOSURE_VERSION,
    )
    co_a = _company("A")
    co_b = _company("B")
    ca = _contact(co_a, "+14155550150")
    cb = _contact(co_b, "+14155550151")
    link = generate_web_optin_link(co_a.id, ca.id, actor_user_id=None)
    db.session.commit()
    row = PromotionalOptInSolicitation.query.filter_by(company_id=co_a.id).one()

    # garbage / truncated token
    assert get_web_optin_context("not-a-real-token")["error"] == "invalid_token"
    assert record_web_optin(link["token"][:-4] + "aaaa",
                            disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION)["error"] in (
        "invalid_token", "unknown_link", "token_mismatch")

    # a validly-signed token whose body points at another tenant/contact
    forged = _encode_web_token(co_b.id, cb.id, row.web_token_jti)
    assert record_web_optin(forged, disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION)["error"] == "token_mismatch"
    assert PromotionalConsentEvent.query.count() == 0


def test_stale_disclosure_version_is_rejected(app):
    from services.promotional_optin import generate_web_optin_link, record_web_optin
    co = _company()
    c = _contact(co, "+14155550160")
    link = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()
    out = record_web_optin(link["token"], disclosure_version="1999-01-01.v0")
    db.session.commit()
    assert out["ok"] is False and out["error"] == "stale_disclosure"
    assert PromotionalConsentEvent.query.count() == 0


def test_safe_first_name_only_when_verified(app):
    from services.promotional_optin import generate_web_optin_link, get_web_optin_context
    co = _company()
    unverified = _contact(co, "+14155550170", first_name="Sam")
    verified = _contact(co, "+14155550171", first_name="Dana", name_verification_level="verified")
    lu = generate_web_optin_link(co.id, unverified.id, actor_user_id=None)
    lv = generate_web_optin_link(co.id, verified.id, actor_user_id=None)
    db.session.commit()
    assert get_web_optin_context(lu["token"])["first_name"] is None
    assert get_web_optin_context(lv["token"])["first_name"] == "Dana"


# --- HTTP surface ----------------------------------------------------------

def test_public_get_page_renders_unchecked_disclosure(app, client):
    from services.promotional_optin import generate_web_optin_link
    co = _company()
    c = _contact(co, "+14155550180")
    link = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()
    r = client.get(link["path"])
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "MyOrder.fun Text Specials" in html
    assert "recurring promotional text messages from MyOrder.fun" in html
    assert "Consent is not a condition of purchase." in html
    # the disclosure checkbox is present and starts unchecked
    seg = html.split('name="agree"', 1)[1][:60]
    assert 'name="agree"' in html and "checked" not in seg


def test_public_post_requires_affirmative_check(app, client):
    from services.promotional_optin import generate_web_optin_link
    co = _company()
    c = _contact(co, "+14155550181")
    link = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()
    r = client.post(link["path"], data={"disclosure_version": link["disclosure_version"]})
    assert r.status_code == 400
    assert PromotionalConsentEvent.query.count() == 0

    r2 = client.post(link["path"], data={"agree": "on", "disclosure_version": link["disclosure_version"]})
    assert r2.status_code == 200
    assert "opted in" in r2.get_data(as_text=True).lower()
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is True
    assert PromotionalConsentEvent.query.filter_by(company_id=co.id).count() == 1


def test_public_invalid_token_is_404_without_leaking_reason(app, client):
    r = client.get("/promo-optin/garbage-token")
    assert r.status_code == 404
    assert "isn" in r.get_data(as_text=True)  # "isn’t valid"


def test_operator_create_link_endpoint(app, client):
    from services.promotional_optin import generate_web_optin_link  # noqa: F401
    co = _company()
    admin = _admin(co)
    c = _contact(co, "+14155550190")
    _marketing_segment(co, [c.id])
    db.session.commit()
    _login(client, admin)

    r = client.post("/api/promotional-optin/consent-link", json={"contact_id": c.id})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] and body["path"].startswith("/promo-optin/")
    assert body["consent_status"]["state"] in ("link", "not_sent")

    # status endpoint returns the same link
    s = client.get(f"/api/promotional-optin/consent-link/{c.id}")
    assert s.status_code == 200 and s.get_json()["token"] == body["token"]


def test_operator_create_link_rejects_non_admin(app, client):
    co = _company()
    member = _member(co)
    _contact(co, "+14155550200")
    db.session.commit()
    _login(client, member)
    r = client.post("/api/promotional-optin/consent-link",
                    json={"contact_id": Contact.query.first().id})
    assert r.status_code == 403


def test_operator_create_link_never_reaches_cross_tenant_contact(app, client):
    co_a = _company("A")
    co_b = _company("B")
    admin_a = _admin(co_a, "admin-a@example.com")
    _contact(co_b, "+14155550201")
    db.session.commit()
    cb_id = Contact.query.filter_by(company_id=co_b.id).one().id

    _login(client, admin_a)
    r = client.post("/api/promotional-optin/consent-link", json={"contact_id": cb_id})
    # create_solicitation scopes by (id, company_id=tenant) -> not found for A
    assert r.status_code == 422
    assert PromotionalOptInSolicitation.query.count() == 0


def test_qr_endpoint_returns_svg_or_503(app, client):
    from services.promotional_optin import generate_web_optin_link
    co = _company()
    admin = _admin(co)
    c = _contact(co, "+14155550210")
    generate_web_optin_link(co.id, c.id, actor_user_id=admin.id)
    db.session.commit()
    _login(client, admin)
    r = client.get(f"/api/promotional-optin/consent-link/{c.id}/qr.svg")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert r.mimetype == "image/svg+xml"
        assert b"<svg" in r.data


def test_confirmation_sms_sent_only_when_flag_and_messaging_service(app, monkeypatch):
    from services.promotional_optin import generate_web_optin_link, record_web_optin, WEB_OPTIN_DISCLOSURE_VERSION
    co = _company()
    ta = TwilioAccount(company_id=co.id, from_phone="+15559999999", is_active=True,
                       messaging_service_sid="MGtest")
    ta.set_account_sid("ACtest")
    ta.set_auth_token("token")
    db.session.add(ta)
    c = _contact(co, "+14155550220")
    db.session.flush()
    link = generate_web_optin_link(co.id, c.id, actor_user_id=None)
    db.session.commit()

    sent = {"n": 0}

    def fake_send(conv_id, body, **kw):
        sent["n"] += 1
        assert kw.get("effect_type") == "promotional_optin_web_confirmation"
        return {"success": True, "sid": "SMsent"}

    monkeypatch.setattr("twilio_sms.sendConversationSms", fake_send)
    monkeypatch.setenv("PROMO_OPTIN_WEB_CONFIRMATION_SMS", "true")

    out = record_web_optin(link["token"], disclosure_version=WEB_OPTIN_DISCLOSURE_VERSION)
    db.session.commit()
    assert out["granted"] and out["confirmation_sent"] is True
    assert sent["n"] == 1
