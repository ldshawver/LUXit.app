"""Inbound call routing precedence after the native + forwarding merge.

resolve_inbound_voice_route() decides the route, inbound_forward_gate() decides
whether a "forward" route may really forward. The voice webhook and the LUX
Connect "where do my calls go" status (number_call_routing_status) both use
them, so every case checks that the two agree.

Per-number routes have model defaults (business hours "ring_pwa", after hours
"voicemail") and no "explicitly set" flag, so a stored default is treated as
explicit except in business hours, where "ring_pwa" does not shadow a company
route. A NULL route is the only "not configured" value after hours.
"""
import re

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, PhoneSettings, TwilioAccount, TwilioPhoneNumber, User, UserCompanyAccess

BUSINESS = "+19165989519"
OTHER_OWN = "+18302591310"
CELL = "+12792860000"
COMPANY_CELL = "+12792861111"
COMPANY_FALLBACK = "+12792862222"
CALLER = "+14155551212"


@pytest.fixture
def routing(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company = Company(name="Routing Tenant")
        db.session.add(company); db.session.flush()
        account = TwilioAccount(company_id=company.id, from_phone=BUSINESS, is_active=True)
        db.session.add(account); db.session.flush()
        line = TwilioPhoneNumber(company_id=company.id, twilio_account_id=account.id, phone_number=BUSINESS,
                                 friendly_name="Main", voice_enabled=True, is_active=True)
        other = TwilioPhoneNumber(company_id=company.id, twilio_account_id=account.id, phone_number=OTHER_OWN,
                                  friendly_name="Other", voice_enabled=True, is_active=True)
        agent = User(username="route-agent", email="route-agent@example.com", password_hash=generate_password_hash("pw"),
                     default_company_id=company.id)
        db.session.add_all([line, other, agent]); db.session.flush()
        db.session.add(UserCompanyAccess(user_id=agent.id, company_id=company.id, role="agent", is_default=True,
                                         can_access_mobile_inbox=True))
        db.session.commit()
        # One reachable app user, so "app" really rings instead of failing closed to voicemail.
        monkeypatch.setattr("services.receive_calls.receive_calls_user_ids", lambda cid: {agent.id})
        monkeypatch.setattr("services.phone_availability.available_user_ids", lambda cid: {agent.id})
        yield app, app.test_client(), {"company": company.id, "line": line.id}
        db.session.remove(); db.drop_all()


def _hours(monkeypatch, in_hours):
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **kw: in_hours)


def _line(ids, **fields):
    line = db.session.get(TwilioPhoneNumber, ids["line"])
    for k, v in fields.items():
        setattr(line, k, v)
    db.session.commit()


def _company(ids, **fields):
    db.session.add(PhoneSettings(company_id=ids["company"], **fields))
    db.session.commit()


def _call(client, sid):
    body = client.post("/twilio/voice/inbound", data={"To": BUSINESS, "From": CALLER, "CallSid": sid,
                                                      "Direction": "inbound"}).get_data(as_text=True)
    numbers = re.findall(r'method="POST">(\+\d+)</Number>', body)
    if numbers:
        return "forward", numbers
    if "<Client>" in body:
        return "app", []
    assert "<Record" in body, body
    return "voicemail", []


def _status(in_hours):
    from twilio_sms import number_call_routing_status
    return number_call_routing_status(BUSINESS)["business_hours" if in_hours else "after_hours"]


def _assert_route(client, monkeypatch, in_hours, sid, destination, numbers=()):
    _hours(monkeypatch, in_hours)
    got, dialed = _call(client, sid)
    assert (got, dialed) == (destination, list(numbers))
    status = _status(in_hours)
    assert status["destination"] == destination
    assert status["forward_to"] == (numbers[0] if numbers else None)


# ── business hours ──────────────────────────────────────────────────────── #

def test_company_forward_applies_to_a_line_left_on_its_default(routing, monkeypatch):
    app, client, ids = routing
    _company(ids, during_hours_route="forward", forward_number=COMPANY_CELL)
    _assert_route(client, monkeypatch, True, "CA_P1", "forward", [COMPANY_CELL])


def test_line_forward_beats_company_ring(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, during_hours_route="forward", call_forward_to=CELL)
    _company(ids, during_hours_route="ring_pwa")
    _assert_route(client, monkeypatch, True, "CA_P2", "forward", [CELL])


@pytest.mark.parametrize("company_route", ["ring_pwa", "forward"])
def test_line_voicemail_in_hours_is_not_overridden_by_ringing_or_company_forward(routing, monkeypatch, company_route):
    app, client, ids = routing
    _line(ids, during_hours_route="voicemail")
    _company(ids, during_hours_route=company_route, forward_number=COMPANY_CELL)
    _assert_route(client, monkeypatch, True, "CA_P3" + company_route, "voicemail")


def test_switched_off_line_forward_ignores_a_company_forward_number(routing, monkeypatch):
    # Regression: any company forward_number used to turn a switched-off
    # line's own forward back on (forwarding_on = settings_forward_to or ...).
    app, client, ids = routing
    _line(ids, during_hours_route="forward", call_forward_to=CELL,
          voice_forwarding_enabled=False, call_forwarding_enabled=False)
    _company(ids, during_hours_route="ring_pwa", forward_number=COMPANY_CELL)
    _assert_route(client, monkeypatch, True, "CA_P4", "app")


def test_company_forward_without_a_company_number_uses_the_line_target(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, call_forward_to=CELL)
    _company(ids, during_hours_route="forward")
    _assert_route(client, monkeypatch, True, "CA_P5", "forward", [CELL])


def test_unconfigured_line_rings_the_app_in_hours(routing, monkeypatch):
    app, client, ids = routing
    _assert_route(client, monkeypatch, True, "CA_P6", "app")


def test_forward_with_unsafe_primary_dials_the_safe_fallback(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, during_hours_route="forward", call_forward_to=OTHER_OWN)
    _company(ids, during_hours_route="ring_pwa", fallback_forward_number=COMPANY_FALLBACK)
    _assert_route(client, monkeypatch, True, "CA_P7", "forward", [COMPANY_FALLBACK])


def test_forward_with_no_safe_target_uses_the_period_default(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, during_hours_route="forward", after_hours_route="forward", call_forward_to=OTHER_OWN)
    _assert_route(client, monkeypatch, True, "CA_P8a", "app")
    _assert_route(client, monkeypatch, False, "CA_P8b", "voicemail")


# ── after hours ─────────────────────────────────────────────────────────── #

def test_line_ring_after_hours_beats_company_voicemail(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, after_hours_route="ring_pwa")
    _company(ids, after_hours_route="voicemail")
    _assert_route(client, monkeypatch, False, "CA_A1", "app")


def test_line_voicemail_after_hours_beats_company_forward(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, after_hours_route="voicemail")
    _company(ids, after_hours_route="forward", after_hours_forward_number=COMPANY_CELL)
    _assert_route(client, monkeypatch, False, "CA_A2", "voicemail")


def test_line_without_an_after_hours_route_takes_the_company_forward(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, after_hours_route=None)
    _company(ids, after_hours_route="forward", after_hours_forward_number=COMPANY_CELL)
    _assert_route(client, monkeypatch, False, "CA_A3", "forward", [COMPANY_CELL])


def test_switched_off_after_hours_forward_goes_to_voicemail_despite_company_number(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, after_hours_route="forward", call_forward_to=CELL,
          voice_forwarding_enabled=False, call_forwarding_enabled=False)
    _company(ids, after_hours_route="voicemail", after_hours_forward_number=COMPANY_CELL)
    _assert_route(client, monkeypatch, False, "CA_A4", "voicemail")


def test_nothing_configured_after_hours_is_voicemail_not_the_app(routing, monkeypatch):
    app, client, ids = routing
    _line(ids, after_hours_route=None)
    _assert_route(client, monkeypatch, False, "CA_A5", "voicemail")


# ── who may change company routing ──────────────────────────────────────── #

def test_company_routing_admin_edits_only_their_own_tenant(routing):
    app, client, ids = routing
    other_co = Company(name="Elsewhere")
    db.session.add(other_co); db.session.flush()
    db.session.add(PhoneSettings(company_id=other_co.id, during_hours_route="ring_pwa"))
    admin = User(username="route-admin", email="route-admin@example.com", password_hash=generate_password_hash("pw"),
                 default_company_id=ids["company"])
    db.session.add(admin); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=admin.id, company_id=ids["company"], role=UserCompanyAccess.ROLE_ADMIN,
                                     is_default=True, can_access_mobile_inbox=True))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id); sess["_fresh"] = True
    r = client.put("/api/phone/settings", json={"during_hours_route": "forward", "forward_number": COMPANY_CELL,
                                                "company_id": other_co.id})
    assert r.status_code == 200
    mine = PhoneSettings.query.filter_by(company_id=ids["company"]).one()
    theirs = PhoneSettings.query.filter_by(company_id=other_co.id).one()
    assert (mine.during_hours_route, mine.forward_number) == ("forward", COMPANY_CELL)
    assert (theirs.during_hours_route, theirs.forward_number) == ("ring_pwa", None)
