"""PWA Voice: token policy and inbound-routing policy must AGREE.

Real-phone acceptance defect (prod 7b34eef): an eligible user's PWA could not
register — /api/phone/voice-token returned 403 DEVICE_NOT_REGISTERED because a
stale `luxitPwaDeviceKey` in localStorage was treated as an approval requirement
even though the company has `require_approved_pwa_devices = false`. And even a
token-only fix would leave inbound `ring_pwa` targeting an identity nobody holds
-> "PWA Ready, incoming call = immediate voicemail".

Canonical device-approval policy:

  require_approved_pwa_devices = TRUE
    device approval mandatory; missing/stale/pending/unapproved fails closed;
    token + inbound <Client> both device-scoped and identical.

  require_approved_pwa_devices = FALSE
    device approval is NOT an authorization requirement; a missing / stale /
    unresolved / pending device_key must not block an otherwise-eligible user;
    token falls back to the per-user non-device identity; inbound routing rings
    that same per-user identity. TOKEN identity == inbound <Client> identity.

Non-negotiable regardless of the flag: authentication, tenant isolation, active
membership, user/company active, can_access_pwa, can_call, receive_calls,
Available/Away.
"""
import os
import re
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_API_KEY", "SKtest")
os.environ.setdefault("TWILIO_API_SECRET", "secretsecretsecret")

from app import create_app
from extensions import db
from models import (Company, PhoneNumberUserPermission, PWADevice, TwilioAccount,
                    TwilioPhoneNumber, User, UserCompanyAccess)
from services.phone_identity import pwa_voice_identity
from services.receive_calls import set_receive_calls
from services.phone_availability import set_availability

ROOT = Path(__file__).resolve().parents[1]
CALLS_HTML = (ROOT / "templates/inbox_pwa/calls.html").read_text()


@pytest.fixture
def app():
    a = create_app()
    a.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                    WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


NUMBER = "+19165550123"


def _tenant(require_approval=False):
    co = Company(name="T", is_active=True, require_approved_pwa_devices=require_approval)
    db.session.add(co); db.session.flush()
    ta = TwilioAccount(company_id=co.id, from_phone=NUMBER, is_active=True)
    db.session.add(ta); db.session.flush()
    pn = TwilioPhoneNumber(company_id=co.id, twilio_account_id=ta.id, phone_number=NUMBER,
                           friendly_name="Main", is_active=True, voice_enabled=True,
                           during_hours_route="ring_pwa", after_hours_route="voicemail")
    db.session.add(pn); db.session.flush()
    return co, ta, pn


def _user(co, pn, name="rep", *, active=True, can_call=True, can_access_pwa=True):
    u = User(username=name, email=f"{name}@t.co", password_hash=generate_password_hash("x"),
             active=active, default_company_id=co.id)
    db.session.add(u); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role="staff",
                                     is_active=active, pwa_access_enabled=True,
                                     can_access_full_app=True))
    db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=u.id, phone_number_id=pn.id,
                                             can_access_pwa=can_access_pwa, can_call=can_call,
                                             can_view_calls=True))
    db.session.flush()
    return u


def _device(co, u, key, *, approved="approved", lifecycle="active"):
    d = PWADevice(company_id=co.id, user_id=u.id, device_key=key,
                  approved_status=approved, lifecycle_status=lifecycle)
    db.session.add(d); db.session.flush()
    return d


def _login(client, u):
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id); s["_fresh"] = True


def _token(client, device_key=None):
    url = "/api/phone/voice-token"
    if device_key is not None:
        url += f"?device_key={device_key}"
    return client.get(url)


def _inbound_twiml(client, monkeypatch):
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **k: True)
    r = client.post("/twilio/voice/inbound", data={
        "To": NUMBER, "From": "+14155559999", "CallSid": "CAmatch", "Direction": "inbound",
    })
    return r.get_data(as_text=True)


def _identities_in(twiml):
    return set(re.findall(r"<Identity>([^<]+)</Identity>", twiml))


# ══════════════════════════════════════════════════════════════════════════ #
#  approval = FALSE                                                          #
# ══════════════════════════════════════════════════════════════════════════ #

def test_1_approval_false_no_device_key_token_200_and_inbound_matches(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=False)
    u = _user(co, pn)
    db.session.commit()
    c = app.test_client(); _login(c, u)

    body = _token(c).get_json()
    assert body["success"] is True, body
    want = pwa_voice_identity(co.id, u.id)
    assert body["identity"] == want

    twiml = _inbound_twiml(c, monkeypatch)
    assert "<Client>" in twiml
    assert _identities_in(twiml) == {want}, twiml            # inbound <Client> == token identity


def test_2_approval_false_stale_unresolved_device_key_same_result(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=False)
    u = _user(co, pn)
    db.session.commit()
    c = app.test_client(); _login(c, u)

    body = _token(c, device_key="stale-key-not-in-db").get_json()
    assert body["success"] is True
    want = pwa_voice_identity(co.id, u.id)
    assert body["identity"] == want
    assert _identities_in(_inbound_twiml(c, monkeypatch)) == {want}


def test_3_approval_false_pending_unapproved_device_same_result(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=False)
    u = _user(co, pn)
    _device(co, u, "pending-dev", approved="pending", lifecycle="pending")
    db.session.commit()
    c = app.test_client(); _login(c, u)

    body = _token(c, device_key="pending-dev").get_json()
    assert body["success"] is True
    want = pwa_voice_identity(co.id, u.id)
    assert body["identity"] == want
    assert _identities_in(_inbound_twiml(c, monkeypatch)) == {want}


def test_4_approval_false_zero_approved_devices_still_routes(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=False)
    u = _user(co, pn)
    db.session.commit()                                       # NO PWADevice row at all
    c = app.test_client(); _login(c, u)

    twiml = _inbound_twiml(c, monkeypatch)
    assert "<Client>" in twiml and "<Record" not in twiml     # not immediate voicemail
    assert _identities_in(twiml) == {pwa_voice_identity(co.id, u.id)}


# ══════════════════════════════════════════════════════════════════════════ #
#  approval = TRUE                                                           #
# ══════════════════════════════════════════════════════════════════════════ #

def test_5_approval_true_missing_or_pending_device_fails_closed(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=True)
    u = _user(co, pn)
    _device(co, u, "pending-dev", approved="pending", lifecycle="pending")
    db.session.commit()
    c = app.test_client(); _login(c, u)

    assert _token(c).status_code == 403                       # no device_key
    assert _token(c).get_json()["code"] == "DEVICE_NOT_REGISTERED"
    assert _token(c, device_key="stale").status_code == 403   # stale
    assert _token(c, device_key="pending-dev").status_code == 403  # pending

    # inbound: no approved+active device -> nobody rung via <Client>
    twiml = _inbound_twiml(c, monkeypatch)
    assert "<Client>" not in twiml


def test_6_approval_true_approved_active_device_token_and_routing_match(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=True)
    u = _user(co, pn)
    _device(co, u, "good-dev", approved="approved", lifecycle="active")
    db.session.commit()
    c = app.test_client(); _login(c, u)

    body = _token(c, device_key="good-dev").get_json()
    assert body["success"] is True
    want = pwa_voice_identity(co.id, u.id, "good-dev")
    assert body["identity"] == want

    twiml = _inbound_twiml(c, monkeypatch)
    assert _identities_in(twiml) == {want}                    # device-scoped, identical


# ══════════════════════════════════════════════════════════════════════════ #
#  eligibility invariants preserved (7 / 8 / 9)                              #
# ══════════════════════════════════════════════════════════════════════════ #

def test_7_receive_calls_off_no_token_no_routing(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=False)
    u = _user(co, pn)
    set_receive_calls(u.id, co.id, False, actor_user_id=u.id, source="user")
    db.session.commit()
    c = app.test_client(); _login(c, u)

    assert _token(c).status_code == 403
    assert _token(c).get_json()["code"] == "CALLING_DISABLED"
    assert "<Client>" not in _inbound_twiml(c, monkeypatch)


def test_8_away_user_excluded_but_pref_intact(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=False)
    u = _user(co, pn)
    set_availability(u.id, co.id, "away", actor_user_id=u.id, source="user")
    db.session.commit()
    c = app.test_client(); _login(c, u)

    assert _token(c).get_json()["code"] == "PHONE_AWAY"
    assert "<Client>" not in _inbound_twiml(c, monkeypatch)
    from services.receive_calls import receive_calls_enabled
    assert receive_calls_enabled(u.id, co.id) is True         # Receive Calls preference untouched


def test_9_available_again_restores_routing_without_touching_receive_calls(app, monkeypatch):
    co, ta, pn = _tenant(require_approval=False)
    u = _user(co, pn)
    set_availability(u.id, co.id, "away", actor_user_id=u.id, source="user")
    set_availability(u.id, co.id, "available", actor_user_id=u.id, source="user")
    db.session.commit()
    c = app.test_client(); _login(c, u)

    want = pwa_voice_identity(co.id, u.id)
    assert _token(c).get_json()["identity"] == want
    assert _identities_in(_inbound_twiml(c, monkeypatch)) == {want}


def test_cross_tenant_device_key_never_bypasses(app, monkeypatch):
    co_a, _, pn_a = _tenant(require_approval=True)
    u_a = _user(co_a, pn_a, name="a")
    other = Company(name="B", is_active=True, require_approved_pwa_devices=True)
    db.session.add(other); db.session.flush()
    # an approved+active device that belongs to another tenant / user
    db.session.add(PWADevice(company_id=other.id, user_id=u_a.id, device_key="foreign",
                             approved_status="approved", lifecycle_status="active"))
    db.session.commit()
    c = app.test_client(); _login(c, u_a)
    assert _token(c, device_key="foreign").status_code == 403


# ══════════════════════════════════════════════════════════════════════════ #
#  client: one controlled attempt / no retry storm (10 / 11 / 12)           #
# ══════════════════════════════════════════════════════════════════════════ #

def test_10_11_12_client_retry_guard_contract():
    h = CALLS_HTML
    # single-flight preserved
    assert "if (voice.initPromise) return voice.initPromise;" in h
    # a recorded failure gates automatic retries; terminal => never auto-retry
    assert "voice.failure = { code, terminal, at: Date.now() };" in h
    assert "const VOICE_TERMINAL_CODES = new Set([" in h
    assert "if (voice.failure && voice.failure.terminal) { showRetryAffordance(); return; }" in h
    # bounded backoff for transient failures
    assert "Math.min(2000 * (2 ** (voice.backoffStep - 1)), 60000)" in h
    # visibilitychange / pageshow must NOT storm after a failure
    resync = h.split("function resyncVoiceUi(")[1].split("\nwindow.addEventListener('pageshow'")[0]
    assert "voice.failure && (voice.failure.terminal || (voice.nextAttemptAt && Date.now() < voice.nextAttemptAt))" in resync
    assert "showRetryAffordance();" in resync
    # explicit Retry force-bypasses the guard
    retry = h.split("function retryVoiceRegistration()")[1].split("\n}\n")[0]
    assert "clearVoiceFailure();" in retry
    assert "startVoiceRegistration({ force: true })" in retry
    # a material authoritative state change may force one controlled retry
    assert "applyReceiveCalls(data.receive_calls, { force: true })" in h
    assert "applyPhoneAvailability(data.state, { force: true })" in h
    # bootstrap still a single chained load (single-flight + guard collapse the double trigger)
    assert "loadReceiveCalls().then(loadPhoneAvailability)" in h
    # success clears the failure/backoff state
    assert h.split("voice.device.on('registered', () => {")[1].split("\n")[1].strip() == "clearVoiceFailure();"
