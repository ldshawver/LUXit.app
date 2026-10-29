"""Cell forwarding safety: loop guard, off switch, carrier-voicemail screening,
caller ID, and who may change company routing (services/call_forwarding.py)."""
import re

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    CallEvent, Company, PhoneSettings, TwilioAccount, TwilioCallLog, TwilioPhoneNumber, User, UserCompanyAccess,
)

BUSINESS = "+19165989519"
OTHER_OWN = "+18302591310"
CELL = "+12792860000"
CALLER = "+14155551212"


@pytest.fixture
def fwd(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **kw: True)
    with app.app_context():
        db.create_all()
        company = Company(name="Forward Tenant")
        db.session.add(company); db.session.flush()
        account = TwilioAccount(company_id=company.id, from_phone=BUSINESS, is_active=True)
        db.session.add(account); db.session.flush()
        line = TwilioPhoneNumber(
            company_id=company.id, twilio_account_id=account.id, phone_number=BUSINESS, friendly_name="Main Line",
            voice_enabled=True, is_active=True, during_hours_route="forward", after_hours_route="voicemail",
            voice_forwarding_enabled=True, call_forward_to=CELL, missed_call_text="Sorry we missed you",
        )
        other = TwilioPhoneNumber(
            company_id=company.id, twilio_account_id=account.id, phone_number=OTHER_OWN, friendly_name="Other",
            voice_enabled=True, is_active=True, during_hours_route="ring_pwa", after_hours_route="voicemail",
        )
        admin = User(username="fwd-admin", email="fwd-admin@example.com", password_hash=generate_password_hash("pw"), is_admin=False, default_company_id=company.id)
        agent = User(username="fwd-agent", email="fwd-agent@example.com", password_hash=generate_password_hash("pw"), is_admin=False, default_company_id=company.id)
        db.session.add_all([line, other, admin, agent]); db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=admin.id, company_id=company.id, role=UserCompanyAccess.ROLE_ADMIN, is_default=True, can_access_mobile_inbox=True),
            UserCompanyAccess(user_id=agent.id, company_id=company.id, role="agent", is_default=True, can_access_mobile_inbox=True),
        ])
        db.session.commit()
        ids = {"company": company.id, "line": line.id, "other": other.id, "admin": admin.id, "agent": agent.id}
        yield app, app.test_client(), ids
        db.session.remove(); db.drop_all()


def _set_line(app, line_id, **fields):
    # The fixture's app context is still active; the test client reuses it.
    line = db.session.get(TwilioPhoneNumber, line_id)
    for k, v in fields.items():
        setattr(line, k, v)
    db.session.commit()


def _inbound(client, sid, caller=CALLER):
    return client.post("/twilio/voice/inbound", data={"To": BUSINESS, "From": caller, "CallSid": sid, "Direction": "inbound"}).get_data(as_text=True)


def _screen_query(body):
    return re.search(r'/twilio/voice/forward-screen\?([^"]+)"', body).group(1).replace("&amp;", "&")


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def test_forward_is_screened_and_shows_the_real_caller(fwd):
    app, client, ids = fwd
    body = _inbound(client, "CA_FWD_1")
    assert f'method="POST">{CELL}</Number>' in body
    assert 'url="/twilio/voice/forward-screen?to=%2B19165989519&amp;log=' in body
    assert f'callerId="{CALLER}"' in body
    assert "screened=1" in body
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CA_FWD_1").one()
        assert (log.status, log.forwarded_to_number) == ("forwarded", CELL)


def test_anonymous_caller_shows_the_business_number(fwd):
    _app, client, _ids = fwd
    body = _inbound(client, "CA_FWD_ANON", caller="anonymous")
    assert f'callerId="{BUSINESS}"' in body


@pytest.mark.parametrize("target", [OTHER_OWN, BUSINESS, "(830) 259-1310"])
def test_forward_to_our_own_number_never_dials_it(fwd, target):
    app, client, ids = fwd
    _set_line(app, ids["line"], call_forward_to=target, call_forwarding_number=target)
    body = _inbound(client, "CA_FWD_LOOP")
    assert "<Number" not in body
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CA_FWD_LOOP").one()
        assert not log.forwarded


def test_switching_forwarding_off_stops_it_on_the_next_call(fwd):
    app, client, ids = fwd
    _set_line(app, ids["line"], voice_forwarding_enabled=False, call_forwarding_enabled=False)
    body = _inbound(client, "CA_FWD_OFF")
    assert "<Number" not in body
    assert CELL not in body


def test_own_number_fallback_is_dropped_but_the_cell_still_rings(fwd):
    app, client, ids = fwd
    with app.app_context():
        db.session.add(PhoneSettings(company_id=ids["company"], during_hours_route="forward", fallback_forward_number=OTHER_OWN))
        db.session.commit()
    body = _inbound(client, "CA_FWD_FB")
    assert body.count("<Number") == 1
    assert f">{CELL}</Number>" in body


def test_screen_prompts_then_accept_connects_and_is_recorded(fwd):
    app, client, ids = fwd
    q = _screen_query(_inbound(client, "CA_FWD_2"))
    leg = {"CallSid": "CA_LEG_2", "ParentCallSid": "CA_FWD_2", "To": CELL}
    screen = client.post(f"/twilio/voice/forward-screen?{q}", data=leg).get_data(as_text=True)
    assert "<Gather" in screen and "Press 1" in screen and "Main Line" in screen
    assert "/twilio/voice/forward-accept?" in screen
    assert screen.rstrip().endswith("<Hangup/>\n</Response>")  # no key press -> leg hangs up
    for _ in range(2):  # Twilio retries are idempotent
        accepted = client.post(f"/twilio/voice/forward-accept?{q}", data={**leg, "Digits": "1"}).get_data(as_text=True)
        assert "<Hangup" not in accepted
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CA_FWD_2").one()
        assert CallEvent.query.filter_by(call_log_id=log.id, event_type="forward_accepted").count() == 1
        assert log.answered_at is not None


def test_wrong_key_or_foreign_call_does_not_accept(fwd):
    app, client, ids = fwd
    q = _screen_query(_inbound(client, "CA_FWD_3"))
    leg = {"CallSid": "CA_LEG_3", "ParentCallSid": "CA_FWD_3", "To": CELL}
    assert "<Hangup" in client.post(f"/twilio/voice/forward-accept?{q}", data={**leg, "Digits": "2"}).get_data(as_text=True)
    other_parent = {**leg, "ParentCallSid": "CA_SOMEONE_ELSE", "Digits": "1"}
    assert "<Hangup" in client.post(f"/twilio/voice/forward-accept?{q}", data=other_parent).get_data(as_text=True)
    assert "<Hangup" in client.post(f"/twilio/voice/forward-screen?{q}", data=other_parent).get_data(as_text=True)
    with app.app_context():
        assert CallEvent.query.filter_by(event_type="forward_accepted").count() == 0


def test_screen_rejects_unsigned_requests_in_strict_mode(fwd, monkeypatch):
    _app, client, _ids = fwd
    q = _screen_query(_inbound(client, "CA_FWD_4"))
    monkeypatch.setenv("TWILIO_STRICT_SIGNATURE", "1")
    leg = {"CallSid": "CA_LEG_4", "ParentCallSid": "CA_FWD_4", "To": CELL, "Digits": "1"}
    assert "<Hangup" in client.post(f"/twilio/voice/forward-screen?{q}", data=leg).get_data(as_text=True)
    assert "<Hangup" in client.post(f"/twilio/voice/forward-accept?{q}", data=leg).get_data(as_text=True)


def test_carrier_voicemail_pickup_still_reaches_luxit_voicemail_and_missed_text(fwd, monkeypatch):
    app, client, ids = fwd
    sent = []
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_can_send_call_auto_sms", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(twilio_sms, "sendConversationSms", lambda conv_id, body, **kw: sent.append(body) or {"success": True})
    _inbound(client, "CA_FWD_5")
    # The cell's voicemail answered (Twilio: completed) but nobody pressed 1.
    resp = client.post(f"/twilio/voice/no-answer?to={BUSINESS}&screened=1",
                       data={"To": BUSINESS, "From": CALLER, "CallSid": "CA_FWD_5", "DialCallStatus": "completed"})
    body = resp.get_data(as_text=True)
    assert "<Record" in body and "<Hangup" not in body
    assert sent == ["Sorry we missed you"]
    with app.app_context():
        log = TwilioCallLog.query.filter_by(twilio_sid="CA_FWD_5").one()
        assert log.status == "no-answer" and log.missed_text_sent


def test_accepted_forward_that_ends_hangs_up_without_voicemail(fwd):
    app, client, ids = fwd
    q = _screen_query(_inbound(client, "CA_FWD_6"))
    client.post(f"/twilio/voice/forward-accept?{q}", data={"CallSid": "CA_LEG_6", "ParentCallSid": "CA_FWD_6", "To": CELL, "Digits": "1"})
    body = client.post(f"/twilio/voice/no-answer?to={BUSINESS}&screened=1",
                       data={"To": BUSINESS, "From": CALLER, "CallSid": "CA_FWD_6", "DialCallStatus": "completed"}).get_data(as_text=True)
    assert "<Hangup/>" in body and "<Record" not in body


def test_unscreened_answered_call_still_hangs_up(fwd):
    """App (<Client>) dials carry no screened flag: unchanged behavior."""
    app, client, ids = fwd
    _inbound(client, "CA_FWD_7")
    body = client.post(f"/twilio/voice/no-answer?to={BUSINESS}",
                       data={"To": BUSINESS, "From": CALLER, "CallSid": "CA_FWD_7", "DialCallStatus": "completed"}).get_data(as_text=True)
    assert "<Hangup/>" in body


def test_number_settings_refuse_our_own_number_as_forward_target(fwd):
    app, client, ids = fwd
    _login(client, ids["admin"])
    resp = client.put(f"/api/phone/numbers/{ids['line']}/settings", json={"call_forwarding_number": "(830) 259-1310"})
    assert resp.status_code == 400
    ok = client.put(f"/api/phone/numbers/{ids['line']}/settings", json={"call_forwarding_number": "+12795550123"})
    assert ok.status_code == 200


def test_company_routing_is_admin_only(fwd):
    app, client, ids = fwd
    _login(client, ids["agent"])
    assert client.put("/api/phone/settings", json={"forward_number": "+12795550123"}).status_code == 403
    assert client.get("/api/phone/settings").status_code == 200
    with app.app_context():
        assert PhoneSettings.query.filter_by(company_id=ids["company"]).one().forward_number is None


def test_company_routing_refuses_loops_and_normalizes(fwd):
    app, client, ids = fwd
    _login(client, ids["admin"])
    assert client.put("/api/phone/settings", json={"after_hours_forward_number": BUSINESS}).status_code == 400
    assert client.put("/api/phone/settings", json={"forward_number": "not a number"}).status_code == 400
    resp = client.put("/api/phone/settings", json={"forward_number": "279-555-0123", "fallback_forward_number": ""})
    assert resp.status_code == 200
    settings = resp.get_json()["settings"]
    assert settings["forward_number"] == "+12795550123" and settings["fallback_forward_number"] is None


def test_webhook_failure_fallback_still_screens_the_forward(fwd, monkeypatch):
    """If the webhook fails after routing, the safe fallback forwards with the
    same press-1 screen; it never hands the call to a bare <Number>."""
    app, client, ids = fwd
    import twilio_sms
    real = twilio_sms._forward_dial_twiml
    calls = []

    def flaky(*a, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("boom after routing")
        return real(*a, **kw)

    monkeypatch.setattr(twilio_sms, "_forward_dial_twiml", flaky)
    body = _inbound(client, "CA_FWD_FAIL")
    assert len(calls) == 2
    assert f'method="POST">{CELL}</Number>' in body and "/twilio/voice/forward-screen?" in body
    assert "screened=1" in body and f'callerId="{CALLER}"' in body
    assert "<Number>" not in body


@pytest.mark.parametrize("extra", [{"From": OTHER_OWN}, {"ForwardedFrom": "(279) 286-0000"}])
def test_call_that_was_already_forwarded_is_not_forwarded_again(fwd, extra):
    """A cell whose no-answer forwarding points back at the business line, or a
    call from one of our own numbers, must not start a forwarding loop."""
    app, client, ids = fwd
    data = {"To": BUSINESS, "From": CALLER, "CallSid": "CA_FWD_BACK", "Direction": "inbound", **extra}
    body = client.post("/twilio/voice/inbound", data=data).get_data(as_text=True)
    assert "<Number" not in body


def test_numbers_outside_this_database_can_be_declared_own(fwd, monkeypatch):
    app, client, ids = fwd
    monkeypatch.setenv("LUXIT_OWN_NUMBERS", f"+15550001111, {CELL}")
    assert "<Number" not in _inbound(client, "CA_FWD_ENV")


def _after_hours(monkeypatch):
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **kw: False)


def test_after_hours_forward_is_screened_too(fwd, monkeypatch):
    app, client, ids = fwd
    _after_hours(monkeypatch)
    _set_line(app, ids["line"], after_hours_route="forward")
    body = _inbound(client, "CA_FWD_AH")
    assert f'method="POST">{CELL}</Number>' in body and "screened=1" in body


def test_after_hours_forwarding_off_goes_to_luxit_voicemail_not_the_app(fwd, monkeypatch):
    app, client, ids = fwd
    _after_hours(monkeypatch)
    _set_line(app, ids["line"], after_hours_route="forward", voice_forwarding_enabled=False, call_forwarding_enabled=False)
    body = _inbound(client, "CA_FWD_AH_OFF")
    assert "<Number" not in body and "<Client" not in body
    assert "<Record" in body


def test_in_hours_forwarding_off_routes_like_an_unconfigured_line(fwd):
    app, client, ids = fwd
    _set_line(app, ids["line"], during_hours_route=None)
    unconfigured = _inbound(client, "CA_FWD_DEFAULT")
    _set_line(app, ids["line"], during_hours_route="forward", voice_forwarding_enabled=False, call_forwarding_enabled=False)
    switched_off = _inbound(client, "CA_FWD_IH_OFF")
    assert "<Number" not in switched_off
    call_ids = lambda body: re.sub(r'name="call_log_id" value="\d+"', 'name="call_log_id" value=""', body)
    assert call_ids(switched_off) == call_ids(unconfigured)
