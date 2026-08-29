"""Single-active-voice-client repair — regression coverage.

Root cause proven on staging: opening /app/phone (Twilio Device #1) and then
/app/dial-pad (Twilio Device #2) registered two Twilio Voice Devices under the
*same* logical Client identity, so an inbound <Dial><Client> fork could deliver
duplicate incoming-call events and there were duplicate registrations/WebSockets.

The repair is client-side coordination in templates/inbox_pwa/calls.html:
BroadcastChannel (localStorage `storage`-event fallback) + a heartbeat lease +
Web Locks leader election guarantee exactly one owning browser context.

These tests assert the coordination contract in the shipped template plus the
server-side invariants that must NOT have regressed (Twilio identity symmetry,
CSRF token, outbound safe-fail, no secrets, route/UI contract). The repo has no
headless-browser harness, so the tab-coordination behaviour (items C, D, F, G,
H, I) is asserted structurally against the template it runs from.
"""

import os
import re
from pathlib import Path

import pytest

from app import create_app
from extensions import db
from models import (
    Company,
    PWADevice,
    TwilioAccount,
    TwilioPhoneNumber,
    User,
    UserCompanyAccess,
)

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "templates" / "inbox_pwa" / "calls.html"
INBOX_PWA = ROOT / "inbox_pwa.py"
TWILIO_SMS = ROOT / "twilio_sms.py"
PHONE_IDENTITY = ROOT / "services" / "phone_identity.py"


@pytest.fixture(scope="module")
def html():
    return CALLS.read_text()


# ------------------------------------------------------------------------- #
# A. one dialer page -> one token request -> one Device -> one register()   #
# ------------------------------------------------------------------------- #

def test_single_device_and_single_register_call(html):
    assert html.count("new Device(") == 1
    assert html.count("voice.device.register()") == 1
    # early-return guard requires a *registered* Device, not merely a truthy one
    assert "voice.deviceState === 'registered') return voice.device;" in html


def test_token_is_requested_only_from_the_init_and_refresh_paths(html):
    # readVoiceToken() is the only site that hits /api/phone/voice-token
    assert html.count("/api/phone/voice-token") == 1
    assert "async function readVoiceToken()" in html
    # it is called from initVoice (registration) and tokenWillExpire (refresh) only
    callers = re.findall(r"readVoiceToken\(\)", html)
    assert len(callers) == 3  # 1 definition + initVoice + tokenWillExpire


def test_opening_the_page_does_not_register_or_mint_a_token(html):
    # the load handler only claims *ownership* (cheap, no token, no Device)
    assert "if (DIALER_MODE) acquireVoiceOwnership('load');" in html
    load_block = re.search(r"window\.addEventListener\('load', \(\) => \{(.*?)\}\);", html, re.S).group(1)
    assert "initVoice" not in load_block
    assert "readVoiceToken" not in load_block
    assert "new Device" not in load_block


# ------------------------------------------------------------------------- #
# B / E. initVoice() is idempotent and ownership-gated                      #
# ------------------------------------------------------------------------- #

def test_initvoice_is_single_flight_and_ownership_gated(html):
    body = re.search(r"async function initVoice\(\) \{(.*?)\n\}\n", html, re.S).group(1)
    assert "if (voice.initPromise) return voice.initPromise;" in body       # single-flight
    assert "if (coord.role !== 'owner') await acquireVoiceOwnership('init');" in body
    assert "voiceCode: 'VOICE_ACTIVE_ELSEWHERE'" in body                    # refuses when not owner
    assert "teardownDevice();" in body                                      # retire any prior Device first


def test_teardown_removes_listeners_so_no_zombie_incoming(html):
    fn = re.search(r"function teardownDevice\(\) \{(.*?)\n\}", html, re.S).group(1)
    assert "removeAllListeners" in fn
    assert "unregister" in fn
    assert "destroy" in fn
    assert "voice.device = null;" in fn


def test_passive_tab_cannot_place_a_call(html):
    body = re.search(r"async function placeWifiCall\(\) \{(.*?)\n\}\n", html, re.S).group(1)
    assert "if (coord.role !== 'owner')" in body
    assert "Use Calling Here" in body  # tells the user how to move ownership


# ------------------------------------------------------------------------- #
# C / D / I. leader election — deterministic single winner                  #
# ------------------------------------------------------------------------- #

def test_coordination_bus_and_fallback_present(html):
    assert "new BroadcastChannel(VOICE_BUS_KEY)" in html
    assert "('BroadcastChannel' in self)" in html
    # localStorage storage-event fallback bus
    assert "window.addEventListener('storage', (e) =>" in html
    assert "localStorage.setItem(VOICE_BUS_KEY" in html


def test_acquire_ownership_is_single_flight(html):
    wrap = re.search(r"async function acquireVoiceOwnership\(reason\) \{(.*?)\n\}\n", html, re.S).group(1)
    # a second concurrent call returns the in-flight promise instead of racing itself
    assert "if (coord.acquiring) return coord.acquiring;" in wrap
    assert "coord.acquiring = _acquireVoiceOwnership(reason).finally(() => { coord.acquiring = null; });" in wrap


def test_race_free_election_via_web_locks_with_deterministic_fallback(html):
    fn = re.search(r"async function _acquireVoiceOwnership\(reason\) \{(.*?)\n\}\n", html, re.S).group(1)
    assert "navigator.locks?.request" in fn
    assert "ifAvailable: true" in fn
    # the request callback is NOT awaited to completion — we learn the result via a
    # side promise while the callback keeps holding the lock (fix: winner must not hang)
    assert "resolve(true);" in fn and "new Promise((release) => { coord.lockRelease = release; });" in fn
    # fallback: announce a claim, settle, lowest tab id wins
    assert "coord.claimVotes.slice().sort()[0]" in fn
    # split-brain guard: two owners converge, lowest tab id keeps it
    assert "if (String(msg.tabId) < String(TAB_ID)) becomePassiveOwner();" in html


def test_second_tab_renders_a_passive_state(html):
    fn = re.search(r"function renderVoiceRole\(\) \{(.*?)\n\}\n", html, re.S).group(1)
    assert "Wi-Fi Calling is active in another LUXit window." in fn
    assert "ensureUseHereButton(true);" in fn
    assert "setEnableState({disabled: true" in fn


# ------------------------------------------------------------------------- #
# F. explicit "Use Calling Here" ownership transfer                         #
# ------------------------------------------------------------------------- #

def test_use_calling_here_transfer_never_leaves_two_owners(html):
    assert "function requestCallingHere()" in html
    assert "coordPost('request-ownership');" in html
    # current owner tears its Device down and releases *before* acking
    handler = re.search(r"case 'request-ownership':(.*?)break;", html, re.S).group(1)
    assert "teardownDevice();" in handler
    assert "becomePassiveOwner();" in handler
    assert "coordPost('ownership-released', {to: msg.tabId});" in handler
    # the requester only registers after it becomes owner
    fin = re.search(r"async function completeTakeover\(\) \{(.*?)\n\}\n", html, re.S).group(1)
    assert "await acquireVoiceOwnership('takeover');" in fin
    assert "if (coord.role === 'owner') { renderVoiceRole(); enableWifiCalling(); }" in fin


# ------------------------------------------------------------------------- #
# G / H. owner disappears — clean and crash paths recover                   #
# ------------------------------------------------------------------------- #

def test_clean_unload_releases_ownership(html):
    assert "window.addEventListener('pagehide', cleanupVoice);" in html
    assert "window.addEventListener('beforeunload', cleanupVoice);" in html
    fn = re.search(r"function cleanupVoice\(\) \{(.*?)\n\}", html, re.S).group(1)
    assert "teardownDevice();" in fn
    assert "becomePassiveOwner();" in fn  # drops the lease / releases the Web Lock


def test_crashed_owner_is_recovered_via_heartbeat_lease(html):
    assert "const LEASE_STALE_MS  = 9000;" in html
    assert "const HEARTBEAT_MS    = 3000;" in html
    # a live dialer tab reclaims a stale lease
    tick = re.search(r"setInterval\(\(\) => \{(.*?)\}, HEARTBEAT_MS\);", html, re.S).group(1)
    assert "stampLease(); coordPost('heartbeat'" in tick
    assert "acquireVoiceOwnership('stale')" in tick
    assert "!leaseFresh(l)" in tick


# ------------------------------------------------------------------------- #
# J. token refresh must not create another Device                          #
# ------------------------------------------------------------------------- #

def test_token_refresh_updates_in_place(html):
    handler = re.search(r"on\('tokenWillExpire', async \(\) => \{(.*?)\}\);", html, re.S).group(1)
    assert "voice.device.updateToken(fresh.token)" in handler
    assert "new Device" not in handler


# ------------------------------------------------------------------------- #
# K. incoming event rendered exactly once                                  #
# ------------------------------------------------------------------------- #

def test_incoming_dedup_guard_present(html):
    assert "if (key && voice.lastIncomingKey === key && !call) return;" in html
    assert html.count("on('incoming'") == 1


# ------------------------------------------------------------------------- #
# L / M / N. CSRF, outbound safe-fail and no-secrets must not regress      #
# ------------------------------------------------------------------------- #

def test_csrf_repair_intact(html):
    assert '<meta name="csrf-token" content="{{ csrf_token() }}">' in html
    assert "headers.set('X-CSRFToken', token)" in html


def test_outbound_disabled_safe_fail_intact(html):
    assert "voice.outboundEnabled = body.outbound_enabled;" in html
    assert "if (voice.outboundEnabled === false) {" in html
    assert '"outbound_enabled": bool(twiml_app_sid)' in INBOX_PWA.read_text()


def test_no_twilio_or_vapid_secret_names_in_template(html):
    for needle in ("TWILIO_AUTH_TOKEN", "TWILIO_API_SECRET", "VAPID_PRIVATE_KEY",
                   "auth_token", "api_secret"):
        assert needle not in html


# ------------------------------------------------------------------------- #
# server-side invariants — identity symmetry must NOT have moved            #
# ------------------------------------------------------------------------- #

def test_voice_token_and_inbound_use_the_same_identity_helper():
    pid = PHONE_IDENTITY.read_text()
    assert "def pwa_voice_identity(company_id: int, user_id: int | None = None, device_key: str | None = None)" in pid

    tok = INBOX_PWA.read_text()
    assert "identity = pwa_voice_identity(company.id, user.id, device_key) if device else pwa_voice_identity(company.id)" in tok

    inbound = TWILIO_SMS.read_text()
    assert "pwa_voice_identity(ta.company_id, device.user_id, device.device_key)" in inbound


# ------------------------------------------------------------------------- #
# O / P. route + UI contract                                               #
# ------------------------------------------------------------------------- #

def _make_app():
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    os.environ.setdefault("TWILIO_API_KEY", "SKtest")
    os.environ.setdefault("TWILIO_API_SECRET", "secret")
    os.environ.pop("TWILIO_TWIML_APP_SID", None)
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    return a


@pytest.fixture
def app():
    return _make_app()


def _seed(a, *, with_device=False):
    with a.app_context():
        co = Company(name="Voice Co", is_active=True)
        db.session.add(co)
        db.session.flush()
        user = User(username="voice_admin", email="voice_admin@test.com",
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
        if with_device:
            db.session.add(PWADevice(
                company_id=co.id, user_id=user.id, device_key="dev-key-1",
                approved_status="approved", lifecycle_status="active",
            ))
        db.session.commit()
        return user.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


@pytest.mark.parametrize("path", ["/app/phone", "/app/dial-pad"])
def test_both_dialer_aliases_render_the_native_dialer(app, path):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    body = client.get(path).get_data(as_text=True)
    assert 'id="btnCall"' in body and 'id="btnMute"' in body and 'id="btnHangup"' in body
    assert 'id="callerId"' in body and 'id="enableVoice"' in body
    assert 'data-digit=' in body                       # keypad
    assert "acquireVoiceOwnership('load')" in body     # coordination is wired in
    assert "new BroadcastChannel(VOICE_BUS_KEY)" in body


def test_text_and_clock_routes_are_unchanged(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)

    inbox = client.get("/app/inbox").get_data(as_text=True)
    assert "luxitPwaDeviceKey" in inbox          # SMS shell
    assert "new Device(" not in inbox            # SMS shell never builds a Voice Device

    recents = client.get("/app/recents").get_data(as_text=True)
    assert 'id="list"' in recents and "hidden" not in recents.split('id="list"')[1][:20]


def test_dialer_page_does_not_leak_secrets(app):
    uid = _seed(app, with_device=True)
    client = app.test_client()
    _login(client, uid)
    body = client.get("/app/phone").get_data(as_text=True)
    for needle in ("ACtest", "SKtest", "secret", "auth"):
        # allow the substring 'auth' only inside URLs/words like "authorized"/"/auth/"
        if needle == "auth":
            assert "auth_token" not in body and "authToken" not in body
            continue
        assert needle not in body
