"""Phone P0 — persistent inbound reachability + audible ringing.

Prod evidence (2026-09-28): with the PWA open and active on Conversations
(`/app/inbox`), no voice token was minted and no Twilio.Device existed — the
Device lived only inside calls.html — so an inbound call's <Dial><Client>
failed within ~2 s (call logs 238/239) and went to voicemail. Separately,
iOS rejected getUserMedia outside a user gesture (MICROPHONE_BLOCKED x9),
which blocked *registration* because the old initVoice() required the mic
first, and an expired token after iOS suspension left the Device stuck
(TransportError 31009 -> AccessTokenExpired 20104, no recovery).

Fix: static/js/lux-voice.js is the single persistent Voice service booted by
EVERY authenticated PWA page; calls.html and index.html are views of it. The
runtime state machine is proven by tests/js/lux_voice.test.cjs (Node, fake
SDK + real FIFO Web-Lock queue + manual clock); this module runs that suite
and locks the page-integration and server-routing contracts.
"""
import os
import re
import shutil
import subprocess
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
from models import (Company, PhoneNumberUserPermission, TwilioAccount,
                    TwilioPhoneNumber, User, UserCompanyAccess)
from services.phone_availability import set_availability
from services.receive_calls import set_receive_calls

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "static" / "js" / "lux-voice.js"
ENGINE_JS = ENGINE.read_text()
CALLS_HTML = (ROOT / "templates" / "inbox_pwa" / "calls.html").read_text()
INDEX_HTML = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()
RINGTONE = ROOT / "static" / "vendor" / "twilio-voice-sdk" / "2.18.3" / "sounds" / "incoming.mp3"
NUMBER = "+19165550123"


# ─────────────────────────── engine runtime suite ─────────────────────────── #

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_engine_behavior_suite_passes():
    """Registration, navigation, token refresh, reconnect/backoff, bfcache,
    Web Lock, incoming call, ringtone start/stop, duplicates, sign-out,
    Receive Calls OFF, Away — see tests/js/lux_voice.test.cjs."""
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "js" / "lux_voice.test.cjs")],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert re.search(r"# fail 0|ℹ fail 0", r.stdout), r.stdout[-2000:]


# ───────────────────────────── static contracts ───────────────────────────── #

def test_only_the_engine_constructs_a_twilio_device():
    assert ENGINE_JS.count("new Device(") == 1
    for name, html in (("calls.html", CALLS_HTML), ("index.html", INDEX_HTML)):
        assert "new Device(" not in html, f"{name} must not own a Device"
        assert ".register()" not in html, f"{name} must not register a Device"


def _fn_body(src, header):
    i = src.index(header)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[j:k + 1]
    raise AssertionError(header)


def test_registration_never_requests_the_microphone():
    assert "getUserMedia" not in _fn_body(ENGINE_JS, "function ensureRegistered(")
    assert "ensureMic" not in _fn_body(ENGINE_JS, "function ensureRegistered(")
    assert "ensureMic" not in _fn_body(ENGINE_JS, "function buildDevice(")
    # Only the three user-gesture entry points may touch the microphone:
    # Answer, Call, and the explicit "Allow / Try again" microphone button.
    callers = {m.start() for m in re.finditer(r"await ensureMic\(\)", ENGINE_JS)}
    assert len(callers) == 3
    assert "await ensureMic()" in _fn_body(ENGINE_JS, "async function accept(")
    assert "await ensureMic()" in _fn_body(ENGINE_JS, "async function placeCall(")
    assert "await ensureMic()" in _fn_body(ENGINE_JS, "async function requestMicrophone(")
    for path in ("async function boot(", "function converge(", "function onVisible(", "function armSlowRetry("):
        assert "ensureMic" not in _fn_body(ENGINE_JS, path) and "getUserMedia" not in _fn_body(ENGINE_JS, path)


def test_sdk_reconnect_is_not_fought_on_unregistered():
    body = _fn_body(ENGINE_JS, "d.on('unregistered', () =>")
    assert "teardownDevice" not in body and "ensureRegistered" not in body
    assert "armWatchdog(RECONNECT_GRACE_MS)" in body


def test_token_errors_recover_with_a_fresh_token_in_place():
    for code in ("20101", "20104"):
        assert code in ENGINE_JS
    assert "d.updateToken(tok.token)" in ENGINE_JS
    body = _fn_body(ENGINE_JS, "async function refreshToken(")
    assert "st.device.updateToken(tok.token)" in body and "buildDevice" not in body


def test_ready_is_derived_from_sdk_registered_state_only():
    # setStatus('ready') appears only behind a live 'registered' check.
    for m in re.finditer(r"setStatus\('ready'", ENGINE_JS):
        window = ENGINE_JS[max(0, m.start() - 400):m.start()]
        assert ("isRegistered()" in window or "d.on('registered'" in window
                or "deviceState() === 'registered'" in window), window[-200:]


def test_web_lock_is_still_the_only_ownership_authority():
    assert "const LOCK_NAME = 'luxit-voice-owner-v1'" in ENGINE_JS
    assert "nav.locks.request(LOCK_NAME" in ENGINE_JS
    assert "ifAvailable" not in ENGINE_JS and "steal" not in ENGINE_JS.replace("never steals", "")
    assert "lockSupported: !!(nav.locks && nav.locks.request)" in ENGINE_JS


def test_backoff_is_bounded():
    assert re.search(r"const MAX_AUTO_ATTEMPTS = \d+;", ENGINE_JS)
    assert "st.attempts >= MAX_AUTO_ATTEMPTS" in ENGINE_JS
    assert "BACKOFF_CAP_MS" in _fn_body(ENGINE_JS, "function backoffDelay(")


def test_ringtone_is_same_origin_and_unlock_listeners_persist():
    assert RINGTONE.exists() and RINGTONE.stat().st_size > 10000
    assert "sounds: { incoming: RINGTONE_URL }" in ENGINE_JS
    assert "const RINGTONE_URL = '/static/vendor/twilio-voice-sdk/2.18.3/sounds/incoming.mp3'" in ENGINE_JS
    # eea5b84 removed its gesture listeners after the first unlock; iOS
    # re-suspends the context after an interruption, so they must persist.
    assert "removeEventListener" not in _fn_body(ENGINE_JS, "function unlockAudio(")


# ───────────────────────────── page integration ───────────────────────────── #

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


def _tenant():
    co = Company(name="T", is_active=True, require_approved_pwa_devices=False)
    db.session.add(co); db.session.flush()
    ta = TwilioAccount(company_id=co.id, from_phone=NUMBER, is_active=True)
    db.session.add(ta); db.session.flush()
    pn = TwilioPhoneNumber(company_id=co.id, twilio_account_id=ta.id, phone_number=NUMBER,
                           friendly_name="Main", is_active=True, voice_enabled=True,
                           during_hours_route="ring_pwa", after_hours_route="voicemail")
    db.session.add(pn); db.session.flush()
    return co, ta, pn


def _user(co, pn, name="rep"):
    u = User(username=name, email=f"{name}@t.co", password_hash=generate_password_hash("x"),
             active=True, default_company_id=co.id)
    db.session.add(u); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role="staff", is_active=True,
                                     pwa_access_enabled=True, can_access_full_app=True))
    db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=u.id, phone_number_id=pn.id,
                                             can_access_pwa=True, can_call=True, can_view_calls=True))
    db.session.flush()
    return u


def _login(client, u):
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id); s["_fresh"] = True


@pytest.mark.parametrize("path", ["/app/inbox", "/app/settings", "/app/contacts", "/app/phone", "/app/dial-pad", "/app/recents", "/app/voicemail"])
def test_every_pwa_page_boots_the_persistent_voice_service(app, path):
    co, ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    c = app.test_client(); _login(c, u)
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    html = r.get_data(as_text=True)
    assert '/static/vendor/twilio-voice-sdk/2.18.3/twilio.min.js' in html
    assert '/static/js/lux-voice.js' in html
    assert "LuxVoice.create(" in html and ".boot()" in html
    assert "LuxVoice.mountOverlay(" in html
    assert f"companyId: Number('{co.id}')" in html
    assert "receiveCalls: true" in html


def test_inbox_shell_renders_server_receive_calls_off(app):
    co, ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    set_receive_calls(u.id, co.id, False, actor_user_id=u.id, source="user"); db.session.commit()
    c = app.test_client(); _login(c, u)
    html = c.get("/app/inbox").get_data(as_text=True)
    assert "receiveCalls: false" in html


def test_inbox_sse_forwards_voice_preferences_and_avoids_double_ring():
    body = _fn_body(INDEX_HTML, "function handleSSEEvent(data)")
    assert "window.luxVoice?.handleServerEvent(data)" in body
    assert "deviceRings" in body


# ─────────────────── server: unreachable -> voicemail/fallback ────────────── #

def _inbound(client, monkeypatch, sid="CAp0"):
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **k: True)
    return client.post("/twilio/voice/inbound", data={
        "To": NUMBER, "From": "+14155559999", "CallSid": sid, "Direction": "inbound",
    }).get_data(as_text=True)


def _no_answer(client, sid, status):
    return client.post(f"/twilio/voice/no-answer?to={NUMBER}", data={
        "CallSid": sid, "DialCallStatus": status, "From": "+14155559999", "To": NUMBER,
    }).get_data(as_text=True)


@pytest.fixture
def open_signature(monkeypatch):
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_validate_twilio_signature", lambda *a, **k: True)


def test_reachable_user_gets_client_dial_with_no_answer_fallback(app, monkeypatch, open_signature):
    co, ta, pn = _tenant(); _user(co, pn); db.session.commit()
    twiml = _inbound(app.test_client(), monkeypatch)
    assert "<Client>" in twiml and "<Identity>" in twiml
    assert 'action="/twilio/voice/no-answer' in twiml
    assert re.search(r'timeout="\d+"', twiml)


@pytest.mark.parametrize("dial_status", ["no-answer", "failed", "busy", "canceled"])
def test_client_not_reached_goes_to_voicemail(app, monkeypatch, open_signature, dial_status):
    """Twilio fails a <Dial><Client> to an unregistered identity (PWA
    suspended/terminated/not registered) almost immediately; any
    unconnected outcome must land in voicemail, never dead air."""
    co, ta, pn = _tenant(); _user(co, pn); db.session.commit()
    twiml = _no_answer(app.test_client(), "CAp0", dial_status)
    assert "<Record" in twiml, twiml
    assert "<Hangup/>" not in twiml.split("<Record")[0]


@pytest.mark.parametrize("excluded", ["receive_calls_off", "away"])
def test_no_eligible_client_goes_straight_to_voicemail(app, monkeypatch, open_signature, excluded):
    co, ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    if excluded == "receive_calls_off":
        set_receive_calls(u.id, co.id, False, actor_user_id=u.id, source="user")
    else:
        set_availability(u.id, co.id, "away", actor_user_id=u.id, source="user")
    db.session.commit()
    twiml = _inbound(app.test_client(), monkeypatch)
    assert "<Client>" not in twiml
    assert "<Record" in twiml or "<Play" in twiml or "<Say" in twiml

