"""P2 regression matrix — explicit per-number after-hours routing.

Prod defect 2026-09-08 (+19165989519): the operator set the number's
`after_hours_route = ring_pwa`, but

  1. route resolution treated an explicit "ring_pwa" as "unset" and let the
     tenant-wide PhoneSettings `after_hours_route = voicemail` win, and
  2. the ring_pwa dispatch branch was gated on `in_hours == True`,

so an after-hours call went to voicemail and the PWA never rang.

These tests lock the fixed semantics:
  * an explicit per-number route wins over PhoneSettings for that number
  * an explicit route == "ring_pwa" rings the PWA regardless of business hours
  * an *implicit* default (route is None) still falls through to voicemail
    after hours, so an unconfigured number is unchanged
  * Away / receive_calls=off users are still excluded from the ring set
  * the inbound <Client> identity equals the /api/phone/voice-token identity
"""
import re

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    Company, PhoneSettings, TwilioAccount, TwilioPhoneNumber, User,
    UserCompanyAccess,
)

NUMBER = "+18305550123"
CALLER = "+14155551212"


@pytest.fixture
def routing_app():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                      SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company = Company(name="Routing Tenant", require_approved_pwa_devices=False)
        db.session.add(company); db.session.flush()
        account = TwilioAccount(company_id=company.id, from_phone=NUMBER, is_active=True,
                                after_hours_voicemail_enabled=True)
        db.session.add(account); db.session.flush()
        pn = TwilioPhoneNumber(
            company_id=company.id, twilio_account_id=account.id, phone_number=NUMBER,
            friendly_name="Shared Line", voice_enabled=True, is_active=True,
            during_hours_route="ring_pwa", after_hours_route=None,
            after_hours_voicemail_enabled=True,
        )
        user = User(username="ring-user", email="ring-user@example.com",
                    password_hash=generate_password_hash("pw"), active=True,
                    default_company_id=company.id)
        db.session.add_all([pn, user]); db.session.flush()
        db.session.add(UserCompanyAccess(
            user_id=user.id, company_id=company.id, role="owner", is_default=True,
            is_active=True, receive_calls=True, phone_availability="available",
        ))
        db.session.commit()
        yield app, app.test_client(), company, pn, user
        db.session.remove(); db.drop_all()


def _set(app, *, pn_route="__keep__", settings_route=None, receive_calls=None,
         availability=None):
    with app.app_context():
        pn = TwilioPhoneNumber.query.filter_by(phone_number=NUMBER).one()
        if pn_route != "__keep__":
            pn.after_hours_route = pn_route
        if settings_route is not None:
            ps = PhoneSettings.query.filter_by(company_id=pn.company_id).first()
            if not ps:
                ps = PhoneSettings(company_id=pn.company_id)
                db.session.add(ps)
            ps.after_hours_route = settings_route
        if receive_calls is not None or availability is not None:
            uca = UserCompanyAccess.query.filter_by(company_id=pn.company_id).first()
            if receive_calls is not None:
                uca.receive_calls = receive_calls
            if availability is not None:
                uca.phone_availability = availability
        db.session.commit()


def _post(client, call_sid, *, business_hours):
    import twilio_sms
    # monkeypatch-free: patch the module attribute directly for the call
    orig = twilio_sms._is_business_hours
    twilio_sms._is_business_hours = lambda *a, **kw: business_hours
    try:
        return client.post("/twilio/voice/inbound", data={
            "To": NUMBER, "From": CALLER, "CallSid": call_sid, "Direction": "inbound",
            "CallStatus": "ringing",
        })
    finally:
        twilio_sms._is_business_hours = orig


def _body(resp):
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/xml")
    b = resp.get_data(as_text=True)
    assert "InFailedSqlTransaction" not in b and "Traceback" not in b
    return b


# ── A. after-hours + explicit per-number ring_pwa + eligible → <Client> ─────────
def test_A_after_hours_explicit_ring_pwa_rings_client(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route="ring_pwa", settings_route="voicemail")
    body = _body(_post(client, "AH-A", business_hours=False))
    assert "<Dial" in body and "<Client>" in body and "<Identity>" in body
    assert "<Record" not in body


# ── B. after-hours + explicit per-number voicemail → voicemail, no Client ───────
def test_B_after_hours_explicit_voicemail(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route="voicemail", settings_route="ring_pwa")
    body = _body(_post(client, "AH-B", business_hours=False))
    assert "<Record" in body
    assert "<Client>" not in body


# ── C. after-hours + no per-number override + company voicemail → voicemail ─────
def test_C_after_hours_no_override_company_voicemail(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route=None, settings_route="voicemail")
    body = _body(_post(client, "AH-C", business_hours=False))
    assert "<Record" in body
    assert "<Client>" not in body


# ── D. after-hours + no per-number override + company ring_pwa → <Client> ───────
def test_D_after_hours_no_override_company_ring_pwa(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route=None, settings_route="ring_pwa")
    body = _body(_post(client, "AH-D", business_hours=False))
    assert "<Client>" in body and "<Identity>" in body


# ── E. in-hours default routing unchanged (ring_pwa number rings) ──────────────
def test_E_in_hours_default_unchanged(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route=None, settings_route=None)
    body = _body(_post(client, "IH-E", business_hours=True))
    assert "<Client>" in body and "<Identity>" in body


# ── E2. after-hours + no override + no company route → voicemail (unchanged) ────
def test_E2_after_hours_fully_unconfigured_is_voicemail(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route=None, settings_route=None)
    body = _body(_post(client, "AH-E2", business_hours=False))
    assert "<Record" in body
    assert "<Client>" not in body


# ── F. Away / receive_calls=off excluded even with explicit ring_pwa ───────────
def test_F_away_user_excluded_from_after_hours_ring(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route="ring_pwa", availability="away")
    body = _body(_post(client, "AH-F1", business_hours=False))
    assert "<Client>" not in body and "<Record" in body


def test_F_receive_calls_off_excluded_from_after_hours_ring(routing_app):
    app, client, *_ = routing_app
    _set(app, pn_route="ring_pwa", receive_calls=False)
    body = _body(_post(client, "AH-F2", business_hours=False))
    assert "<Client>" not in body and "<Record" in body


# ── G. inbound <Client> identity == /api/phone/voice-token identity ────────────
def test_G_after_hours_client_identity_matches_token_identity(routing_app, monkeypatch):
    app, client, company, pn, user = routing_app
    # Pin the identity secret + Twilio Voice SDK creds up front so every path
    # (expected, inbound <Client>, token endpoint) hashes with the same key.
    monkeypatch.setenv("PWA_VOICE_IDENTITY_SECRET", "fixed-test-identity-secret")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000000")
    monkeypatch.setenv("TWILIO_API_KEY", "SKtest0000000000000000000000000000")
    monkeypatch.setenv("TWILIO_API_SECRET", "test-api-secret-value")
    _set(app, pn_route="ring_pwa")

    with app.app_context():
        from services.phone_identity import pwa_voice_identity
        expected = pwa_voice_identity(company.id, user.id)

    body = _body(_post(client, "AH-G", business_hours=False))
    ids = re.findall(r"<Identity>([^<]+)</Identity>", body)
    assert ids == [expected], f"{ids!r} != [{expected!r}]"

    # the token endpoint mints the SAME identity for this approval-disabled user
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    tok = client.get("/api/phone/voice-token?device_key=stale-unknown-key")
    assert tok.status_code == 200, tok.get_data(as_text=True)
    assert tok.get_json()["identity"] == expected
