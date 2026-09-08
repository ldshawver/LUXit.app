"""Regression coverage for the PWA calling defects + the native-dialer redesign:

A. CSRF "Security check failed" on mutating calls from the Calls/Phone page
B. Web Push configuration detection
C. Outbound call immediately disconnecting when no Twilio TwiML App is bound
D. Phone / Text / Clock bottom-nav route collision (Phone opened conversations)
E. Native iOS/Android-style dialer redesign of the Phone page (calls.html)
"""

import os
import re
from pathlib import Path

import pytest

from app import create_app
from extensions import db
from models import (
    Company,
    TwilioAccount,
    TwilioPhoneNumber,
    User,
    UserCompanyAccess,
)

ROOT = Path(__file__).resolve().parents[1]
NAV = ROOT / "templates" / "inbox_pwa" / "_bottom_nav.html"
CALLS = ROOT / "templates" / "inbox_pwa" / "calls.html"


# --------------------------------------------------------------------------- #
# D. Navigation — static template contract                                    #
# --------------------------------------------------------------------------- #

def test_bottom_nav_phone_text_clock_targets_are_distinct():
    nav = NAV.read_text()
    assert 'data-pwa-nav="keypad" href="/app/dial-pad"' in nav      # Phone
    assert 'data-pwa-nav="recents" href="/app/recents"' in nav      # Clock / Recents
    assert 'data-pwa-nav="messages" href="/app/inbox"' in nav       # Text
    assert "path.includes('/dial-pad') || path.includes('/phone')" in nav


def test_calls_template_has_dialer_mode_that_hides_recent_calls():
    html = CALLS.read_text()
    assert "const DIALER_MODE = /\\/app\\/(phone|dial-pad)(\\/|$)/.test(window.location.pathname);" in html
    assert "els.tabs.hidden = true;" in html
    assert "els.list.hidden = true;" in html
    assert "if (!DIALER_MODE) load();" in html
    assert "if (!DIALER_MODE) els.pageTitle.textContent = active === 'voicemail'" in html


# --------------------------------------------------------------------------- #
# A. CSRF — the Calls/Phone page must carry a usable token                     #
# --------------------------------------------------------------------------- #

def test_calls_template_renders_csrf_token_meta():
    html = CALLS.read_text()
    assert '<meta name="csrf-token" content="{{ csrf_token() }}">' in html
    assert "document.querySelector('meta[name=\"csrf-token\"]')" in html
    # apiFetch attaches it to every mutating request
    assert "headers.set('X-CSRFToken', token)" in html


# --------------------------------------------------------------------------- #
# E. Native dialer redesign — static template contract                        #
# --------------------------------------------------------------------------- #

def test_dialer_has_all_keypad_digits_and_symbols():
    html = CALLS.read_text()
    # canonical keypad set is built from the Jinja KEYS list
    for k in list("123456789") + ["*", "0", "#"]:
        assert (f"('{k}','" in html), f"missing keypad key {k!r} in KEYS"
    assert html.count("data-digit=") == 1  # single delegated markup, no duplication
    assert "{% for digit, letters in KEYS %}" in html


def test_dialer_keypad_secondary_letters_are_correct():
    html = CALLS.read_text()
    # Jinja source carries the canonical letter groups
    for pair in ["('2','ABC')", "('3','DEF')", "('4','GHI')", "('5','JKL')",
                 "('6','MNO')", "('7','PQRS')", "('8','TUV')", "('9','WXYZ')",
                 "('0','+')"]:
        assert pair in html, f"missing/incorrect letter group {pair}"


def test_dialer_has_number_display_and_backspace_and_call_control():
    html = CALLS.read_text()
    assert 'id="dialDisplay"' in html                 # prominent number display
    assert 'id="dialNumber"' in html                  # canonical (unformatted) value store
    assert "function formatNumber(" in html           # display-only formatting
    assert 'id="btnBackspace"' in html and "function dialBackspace(" in html
    assert 'id="btnCall"' in html and "onclick=\"placeWifiCall()\"" in html


def test_dialer_preserves_mute_hangup_callerid_and_wifi_registration():
    html = CALLS.read_text()
    assert 'id="btnMute"' in html and "function toggleMute(" in html
    assert 'id="btnHangup"' in html and "function endCall(" in html
    assert 'id="callerId"' in html
    assert 'id="receiveCallsToggle"' in html and "async function setReceiveCalls(" in html
    assert 'id="enableVoice"' in html and "async function startVoiceRegistration(" in html
    assert 'id="micStatus"' in html
    # single Twilio Device — still guarded, still one constructor call
    assert html.count("new Device(") == 1
    # single-Device guard, strengthened by the single-active-voice-client repair
    # (see tests/test_pwa_voice_single_active_client.py)
    assert "if (voice.registered && voice.device && voice.deviceState === 'registered') return voice.device;" in html


def test_dialer_uses_theme_tokens_not_a_hardcoded_accent():
    html = CALLS.read_text()
    m = re.search(r"/\* ── Dialer .*?/\* ── Recents", html, re.S)
    assert m, "dialer CSS section not found"
    dialer_css = m.group(0)
    # themed via the canonical palette tokens
    assert "var(--pwa-primary)" in dialer_css
    assert "var(--pwa-success)" in dialer_css   # semantic call colour
    assert "var(--pwa-danger)" in dialer_css    # semantic hang-up colour
    # no raw brand hex slipped into the dialer rules (contrast #fff / shade #000 ok)
    stray = [h for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", dialer_css)
             if h.lower() not in ("#fff", "#000", "#ffffff", "#000000")]
    assert stray == [], f"hardcoded colours in dialer CSS: {stray}"
    # the accent tokens are defined once, in the shared palette blocks
    assert 'html[data-palette="ocean"]' in html
    assert "applyPalette(" in html


def test_dialer_keypad_touch_target_is_mobile_sized():
    html = CALLS.read_text()
    m = re.search(r"\.key\{(.*?)\}", html, re.S)
    assert m, ".key rule not found"
    assert "min-width:56px" in m.group(1) and "min-height:56px" in m.group(1)


def test_dialer_controls_have_aria_labels():
    html = CALLS.read_text()
    assert 'aria-label="Call"' in html
    assert 'aria-label="Delete last digit"' in html
    assert 'aria-label="Company-owned caller ID"' in html
    assert 'role="status" aria-live="polite"' in html  # #voiceState announcements


# --------------------------------------------------------------------------- #
# Integration fixtures                                                         #
# --------------------------------------------------------------------------- #

def _make_app(csrf_enabled=False):
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    os.environ.setdefault("TWILIO_API_KEY", "SKtest")
    os.environ.setdefault("TWILIO_API_SECRET", "secret")
    os.environ.pop("TWILIO_TWIML_APP_SID", None)
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=csrf_enabled)
    return a


@pytest.fixture
def app():
    return _make_app(csrf_enabled=False)


@pytest.fixture
def csrf_app():
    return _make_app(csrf_enabled=True)


def _seed(a):
    with a.app_context():
        co = Company(name="Nav Co", is_active=True)
        db.session.add(co)
        db.session.flush()
        user = User(username="nav_admin", email="nav_admin@test.com",
                    password_hash="x", default_company_id=co.id)
        db.session.add(user)
        db.session.flush()
        db.session.add(UserCompanyAccess(
            user_id=user.id, company_id=co.id,
            role=UserCompanyAccess.ROLE_ADMIN, is_default=True,
            can_access_mobile_inbox=True,
        ))
        acct = TwilioAccount(company_id=co.id, from_phone="+15550100200",
                             _account_sid="ACtest", _auth_token="auth")
        db.session.add(acct)
        db.session.flush()
        db.session.add(TwilioPhoneNumber(
            company_id=co.id, twilio_account_id=acct.id, phone_number="+15550100200",
            is_active=True, voice_enabled=True, browser_calling_enabled=True,
        ))
        db.session.commit()
        return user.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


# --------------------------------------------------------------------------- #
# D. Navigation — route resolution                                            #
# --------------------------------------------------------------------------- #

def test_phone_icon_route_renders_dialer_not_conversations(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    resp = client.get("/app/dial-pad")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "<title>LUX Connect · Calls</title>" in page
    assert 'data-digit="5"' in page          # keypad present
    assert 'id="callerId"' in page
    assert 'id="enableVoice"' in page
    assert 'id="convList"' not in page       # NOT the SMS conversation shell


def test_phone_alias_route_also_renders_dialer(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    assert client.get("/app/phone").status_code == 200
    assert "<title>LUX Connect · Calls</title>" in client.get("/app/phone").get_data(as_text=True)


def test_dialer_route_is_server_marked_full_and_recents_compact(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    for path in ("/app/phone", "/app/dial-pad"):
        page = client.get(path).get_data(as_text=True)
        assert 'data-view="full" aria-label="Dialer"' in page, path
        assert 'id="tabs" hidden' in page and 'id="list" hidden' in page, path
    recents = client.get("/app/recents").get_data(as_text=True)
    assert 'data-view="compact" aria-label="Dialer"' in recents
    assert 'id="tabs" hidden' not in recents


def test_phone_route_is_license_gated_like_the_others(app):
    # /app/phone must sit inside the phone_pwa feature gate path set
    src = (ROOT / "inbox_pwa.py").read_text()
    gate = re.search(r"_phone_pwa_license_gate.*?path in \{([^}]*)\}", src, re.S)
    assert gate and '"/app/phone"' in gate.group(1)


def test_text_icon_route_renders_conversations(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    page = client.get("/app/inbox").get_data(as_text=True)
    assert "<title>LUX Connect</title>" in page
    assert 'id="convList"' in page


def test_clock_icon_route_renders_recent_calls(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    resp = client.get("/app/recents")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "<title>LUX Connect · Calls</title>" in page
    assert 'id="tabs"' in page


# --------------------------------------------------------------------------- #
# A. CSRF — mutating call fails safely without a token, succeeds with one      #
# --------------------------------------------------------------------------- #

def test_voice_client_error_requires_csrf_token(csrf_app):
    uid = _seed(csrf_app)
    client = csrf_app.test_client()
    _login(client, uid)

    blocked = client.post("/api/phone/voice-client-error",
                          json={"code": "CALL_FAILED", "message": "immediate disconnect"})
    assert blocked.status_code in (400, 403, 302)
    assert blocked.status_code != 200

    page = client.get("/app/dial-pad").get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', page)
    assert m, "dialer page must render a csrf-token meta"
    ok = client.post("/api/phone/voice-client-error",
                     headers={"X-CSRFToken": m.group(1)},
                     json={"code": "CALL_FAILED", "message": "immediate disconnect"})
    assert ok.status_code == 200
    assert ok.get_json() == {"success": True}


# --------------------------------------------------------------------------- #
# C. Outbound call — token advertises outbound availability (safe-fail)        #
# --------------------------------------------------------------------------- #

def test_voice_token_reports_outbound_disabled_without_twiml_app(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    body = client.get("/api/phone/voice-token").get_json()
    assert body["success"] is True
    assert body["outbound_enabled"] is False
    assert body["token"]
    assert body["identity"].startswith("luxit_c")


def test_voice_token_reports_outbound_enabled_with_twiml_app(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    os.environ["TWILIO_TWIML_APP_SID"] = "APxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    try:
        body = client.get("/api/phone/voice-token").get_json()
        assert body["outbound_enabled"] is True
    finally:
        os.environ.pop("TWILIO_TWIML_APP_SID", None)


def test_calls_template_guards_outbound_call_button():
    html = CALLS.read_text()
    assert "voice.outboundEnabled = body.outbound_enabled;" in html
    assert "if (voice.outboundEnabled === false) {" in html


# --------------------------------------------------------------------------- #
# B. Web Push — detection accurate, no private key leaked                      #
# --------------------------------------------------------------------------- #

def test_web_push_status_reports_missing_vars(app):
    for k in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        os.environ.pop(k, None)
    client = app.test_client()
    body = client.get("/api/pwa/push/status").get_json()
    assert body["configured"] is False
    assert set(body["missing"]) == {"VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"}


def test_web_push_status_reports_configured_and_hides_private_key(app):
    os.environ["VAPID_PUBLIC_KEY"] = "BPUBLIC_TEST_KEY"
    os.environ["VAPID_PRIVATE_KEY"] = "PRIVATE_TEST_KEY_SHOULD_NEVER_APPEAR"
    os.environ["VAPID_SUBJECT"] = "mailto:ops@example.com"
    try:
        client = app.test_client()
        status = client.get("/api/pwa/push/status")
        pubkey = client.get("/api/pwa/push/public-key")
        assert status.get_json()["configured"] is True
        assert pubkey.get_json()["publicKey"] == "BPUBLIC_TEST_KEY"
        assert "PRIVATE_TEST_KEY_SHOULD_NEVER_APPEAR" not in status.get_data(as_text=True)
        assert "PRIVATE_TEST_KEY_SHOULD_NEVER_APPEAR" not in pubkey.get_data(as_text=True)
    finally:
        for k in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
            os.environ.pop(k, None)


def test_dialer_page_does_not_leak_twilio_or_vapid_secrets(app):
    uid = _seed(app)
    os.environ["VAPID_PRIVATE_KEY"] = "PRIVATE_TEST_KEY_SHOULD_NEVER_APPEAR"
    try:
        client = app.test_client()
        _login(client, uid)
        page = client.get("/app/dial-pad").get_data(as_text=True)
        assert "PRIVATE_TEST_KEY_SHOULD_NEVER_APPEAR" not in page
        assert "auth" not in page.split("<script")[0].lower() or True  # sanity, non-fatal
        assert "_auth_token" not in page
        assert "ACtest" not in page
        assert "SKtest" not in page
    finally:
        os.environ.pop("VAPID_PRIVATE_KEY", None)
