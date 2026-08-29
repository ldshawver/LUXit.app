"""Single-active-voice-client — Web-Lock-sole-authority coordinator.

Phase-10 staging failure (2026-08-29): the first coordinator gated ownership on
a localStorage heartbeat lease with a 9 s staleness window. When Tab #1 was
backgrounded, Chrome throttled its heartbeat timer, the lease went stale, and
Tab #2 (foreground) elected itself owner → a second voice token was minted and
a second Twilio.Device registered.

Repaired design: an exclusive same-origin Web Lock (`luxit-voice-owner-v1`) is
the ONLY authority. A context owns Voice for exactly as long as its lock
callback runs; `coord.isOwner` is set true only inside that callback. No
heartbeat, no lease, no elapsed-time staleness, no claim/vote, no `ifAvailable`
race. Background timer throttling cannot move ownership. If Web Locks is
unavailable the page FAILS CLOSED (Voice not enabled) rather than racing.

The repo has no headless-browser harness; the runtime state machine is proven
by scripts/coord_harness.js (a Node/VM simulation with a real FIFO Web-Lock
queue — not committed). These tests lock the coordinator's *contract* in the
shipped template plus the server-side invariants that must not regress.
"""

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


def _coordinator(html):
    """The coordinator block: from its banner to the end of teardownDevice()."""
    m = re.search(r"/\* ── Single-active-voice-client coordination.*?\nfunction teardownDevice\(\) \{.*?\n\}\n", html, re.S)
    assert m, "coordinator block not found"
    return m.group(0)


# ------------------------------------------------------------------------- #
# 1 + 7. ONE authority — the exclusive Web Lock. Obsolete paths removed.    #
# ------------------------------------------------------------------------- #

def test_web_lock_is_the_sole_ownership_authority(html):
    coord = _coordinator(html)
    assert "const VOICE_LOCK_NAME = 'luxit-voice-owner-v1';" in html
    # exactly one lock request, and isOwner is set true only inside its callback
    assert html.count("navigator.locks.request(VOICE_LOCK_NAME") == 1
    assert coord.count("coord.isOwner = true;") == 1
    grant_cb = re.search(r"navigator\.locks\.request\(VOICE_LOCK_NAME,[^\n]*\n(.*?)\n  \}\)\.catch", coord, re.S).group(1)
    assert "coord.isOwner = true;" in grant_cb
    assert "coord.lockHeld = true;" in grant_cb


def test_obsolete_ownership_mechanisms_are_gone(html):
    for banned in (
        "LEASE_STALE_MS", "HEARTBEAT_MS", "stampLease(", "leaseFresh(", "readLease(",
        "dropLeaseIfMine", "claimVotes", "CLAIM_SETTLE_MS", "becomeVoiceOwner",
        "becomePassiveOwner", "_acquireVoiceOwnership", "ifAvailable",
        "coord.role", "coord.acquiring", "coord.lastOwnerBeat",
    ):
        assert banned not in html, f"obsolete ownership mechanism still present: {banned}"
    # no polling timer anywhere in the coordinator
    assert "setInterval" not in _coordinator(html)


def test_no_race_prone_fallback_when_locks_unavailable(html):
    coord = _coordinator(html)
    assert "supported: !!(navigator.locks && navigator.locks.request)" in coord
    # fail closed — never a weaker election
    assert "if (!coord.supported)" in coord
    body = re.search(r"async function initVoice\(\) \{(.*?)\n  voice\.initPromise = ", html, re.S).group(1)
    assert "voiceCode: 'VOICE_UNSUPPORTED'" in body
    assert "throw err;" in body


# ------------------------------------------------------------------------- #
# 2. Hold the lock for the whole ownership lifetime.                        #
# ------------------------------------------------------------------------- #

def test_lock_is_held_until_deliberate_release(html):
    coord = _coordinator(html)
    # the request callback returns a promise that stays pending until releaseLock()
    assert "return new Promise((release) => { coord.releaseLock = release; });" in coord
    rel = re.search(r"function releaseVoiceOwnership\(\) \{(.*?)\n\}", coord, re.S).group(1)
    assert "coord.isOwner = false;" in rel
    assert "coord.releaseLock" in rel and "r();" in rel          # resolves the hold promise → releases the lock
    assert "coord.lockAbort" in rel and "abort()" in rel          # or cancels a still-queued request


def test_release_only_on_transfer_disable_teardown_or_context_loss(html):
    coord = _coordinator(html)
    # releaseVoiceOwnership is called from: transfer handler, and cleanupVoice (unload/teardown)
    callers = re.findall(r"releaseVoiceOwnership\(\)", html)
    assert len(callers) >= 3           # definition + request-ownership handler + cleanupVoice
    transfer = re.search(r"case 'request-ownership':(.*?)break;", coord, re.S).group(1)
    assert "teardownDevice();" in transfer and "releaseVoiceOwnership();" in transfer


# ------------------------------------------------------------------------- #
# 3 + 9. Second tab queues, never steals — timer throttling is irrelevant. #
# ------------------------------------------------------------------------- #

def test_ordering_invariant_lock_before_token_before_device_before_register(html):
    body = re.search(r"async function initVoice\(\) \{(.*?)\n  return voice\.initPromise;\n\}", html, re.S).group(1)
    i_owner = body.index("if (!coord.isOwner)")
    i_block = body.index("voiceCode: 'VOICE_ACTIVE_ELSEWHERE'")
    i_token = body.index("readVoiceToken()")
    i_device = body.index("new Device(body.token")
    i_reg = body.index("voice.device.register()")
    assert i_owner < i_block < i_token < i_device < i_reg, "ownership gate must precede token/Device/register"
    # the gate never steals — it only waits briefly for a *free* lock to be granted
    assert "Promise.race([acquireVoiceOwnership(), new Promise((r) => setTimeout(() => r(false), 400))])" in body


def test_passive_tab_shows_active_elsewhere_and_use_calling_here(html):
    render = re.search(r"function renderVoiceRole\(\) \{(.*?)\n\}\n", html, re.S).group(1)
    assert "Wi-Fi Calling is active in another LUXit window." in render
    assert "ensureUseHereButton(true);" in render
    assert "setEnableState({ disabled: true" in render
    assert "function requestCallingHere()" in html


def test_status_channels_cannot_grant_ownership(html):
    coord = _coordinator(html)
    handler = re.search(r"function handleCoordMessage\(msg\) \{(.*?)\n\}\n", coord, re.S).group(1)
    # the only isOwner writes are in the lock callback (true) and releaseVoiceOwnership (false)
    assert "coord.isOwner = true" not in handler
    assert "coord.isOwner = true" not in re.search(r"window\.addEventListener\('storage'.*?\}\);", coord, re.S).group(0)
    # 'owner-changed' only updates passive UI state
    oc = re.search(r"case 'owner-changed':(.*?)break;", handler, re.S).group(1)
    assert "coord.ownerRegistered" in oc and "coord.isOwner" not in oc.replace("!coord.isOwner", "")


# ------------------------------------------------------------------------- #
# 4 + 10. Controlled transfer — old Device dies before new Device.         #
# ------------------------------------------------------------------------- #

def test_transfer_tears_down_old_device_before_releasing_lock(html):
    coord = _coordinator(html)
    transfer = re.search(r"case 'request-ownership':\s*\n(.*?)break;", coord, re.S).group(1)
    i_teardown = transfer.index("teardownDevice();")
    i_release = transfer.index("releaseVoiceOwnership();")
    assert i_teardown < i_release, "owner must destroy its Device before releasing the lock"
    # the requester only registers after it holds the lock (grant callback → enableWifiCalling)
    grant = re.search(r"navigator\.locks\.request\(VOICE_LOCK_NAME.*?\n(.*?)\n  \}\)\.catch", coord, re.S).group(1)
    assert "if (coord.wantsOwnership) { coord.wantsOwnership = false; enableWifiCalling(); }" in grant


def test_no_intentional_overlap_window_in_transfer(html):
    coord = _coordinator(html)
    # transfer has no setTimeout that would let the requester proceed before release
    rch = re.search(r"async function requestCallingHere\(\) \{(.*?)\n\}", coord, re.S).group(1)
    assert "coord.wantsOwnership = false;" in rch          # timeout only *gives up asking*
    assert "renderVoiceRole();" in rch
    assert "acquireVoiceOwnership()" in rch                # queue for the lock; browser FIFO does the rest


# ------------------------------------------------------------------------- #
# 5 + 11. Crash / abnormal loss — browser releases the lock, no heartbeat. #
# ------------------------------------------------------------------------- #

def test_crash_recovery_relies_on_browser_lock_release_not_a_heartbeat(html):
    coord = _coordinator(html)
    assert "heartbeat" in coord and "no lease" in coord            # explicitly rejects the old model
    # queued request uses an AbortController so a clean unload cancels it;
    # a crash needs no handler — the browser drops the lock on context loss
    assert "new AbortController()" in coord
    assert "{ signal: ac.signal }" in coord
    cv = re.search(r"function cleanupVoice\(\) \{(.*?)\n\}", html, re.S).group(1)
    assert "releaseVoiceOwnership();" in cv
    assert "auto-releases the Web Lock" in cv


# ------------------------------------------------------------------------- #
# 8. initVoice single-flight within the owner.                             #
# ------------------------------------------------------------------------- #

def test_initvoice_single_flight_and_device_teardown(html):
    body = re.search(r"async function initVoice\(\) \{(.*?)\n  return voice\.initPromise;\n\}", html, re.S).group(1)
    assert "if (voice.initPromise) return voice.initPromise;" in body
    assert "if (voice.registered && voice.device && voice.deviceState === 'registered') return voice.device;" in body
    assert "teardownDevice();" in body and body.index("teardownDevice();") < body.index("new Device(body.token")
    td = re.search(r"function teardownDevice\(\) \{(.*?)\n\}", html, re.S).group(1)
    assert "removeAllListeners" in td and "destroy" in td and "voice.initPromise = null;" in td
    assert html.count("new Device(") == 1
    assert html.count("voice.device.register()") == 1


# ------------------------------------------------------------------------- #
# 12. Route aliases share one ownership domain.                            #
# ------------------------------------------------------------------------- #

def test_route_aliases_use_one_coordinator(html):
    assert html.count("const VOICE_LOCK_NAME =") == 1
    assert html.count("const VOICE_BUS_KEY   =") == 1
    dm = re.search(r"const DIALER_MODE = (.*?);", html).group(1)
    assert "phone|dial-pad" in dm            # both aliases → same DIALER_MODE branch
    assert "if (DIALER_MODE) { acquireVoiceOwnership(); coordPost('owner-query'); }" in html


# ------------------------------------------------------------------------- #
# 13. Server-side invariants — identity / CSRF / outbound / secrets.       #
# ------------------------------------------------------------------------- #

def test_twilio_identity_algorithm_is_unchanged():
    assert "def pwa_voice_identity(company_id: int, user_id: int | None = None, device_key: str | None = None)" in PHONE_IDENTITY.read_text()
    assert "identity = pwa_voice_identity(company.id, user.id, device_key) if device else pwa_voice_identity(company.id)" in INBOX_PWA.read_text()
    assert "pwa_voice_identity(ta.company_id, device.user_id, device.device_key)" in TWILIO_SMS.read_text()


def test_csrf_and_outbound_safe_fail_intact(html):
    assert '<meta name="csrf-token" content="{{ csrf_token() }}">' in html
    assert "headers.set('X-CSRFToken', token)" in html
    assert "voice.outboundEnabled = body.outbound_enabled;" in html
    assert "if (voice.outboundEnabled === false) {" in html
    assert '"outbound_enabled": bool(twiml_app_sid)' in INBOX_PWA.read_text()


def test_no_secret_names_in_template(html):
    for needle in ("TWILIO_AUTH_TOKEN", "TWILIO_API_SECRET", "VAPID_PRIVATE_KEY", "auth_token", "api_secret"):
        assert needle not in html


# ------------------------------------------------------------------------- #
# Route + UI contract (rendered).                                          #
# ------------------------------------------------------------------------- #

def _make_app():
    import os
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
        db.session.add(co); db.session.flush()
        user = User(username="voice_admin", email="voice_admin@test.com",
                    password_hash="x", default_company_id=co.id)
        db.session.add(user); db.session.flush()
        db.session.add(UserCompanyAccess(
            user_id=user.id, company_id=co.id, role=UserCompanyAccess.ROLE_ADMIN,
            is_default=True, can_access_mobile_inbox=True))
        acct = TwilioAccount(company_id=co.id, from_phone="+15550100200",
                             _account_sid="ACtest", _auth_token="auth")
        db.session.add(acct); db.session.flush()
        db.session.add(TwilioPhoneNumber(
            company_id=co.id, twilio_account_id=acct.id, phone_number="+15550100200",
            is_active=True, voice_enabled=True, browser_calling_enabled=True))
        if with_device:
            db.session.add(PWADevice(
                company_id=co.id, user_id=user.id, device_key="dev-key-1",
                approved_status="approved", lifecycle_status="active"))
        db.session.commit()
        return user.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


@pytest.mark.parametrize("path", ["/app/phone", "/app/dial-pad"])
def test_both_dialer_aliases_carry_the_lock_coordinator(app, path):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    body = client.get(path).get_data(as_text=True)
    assert 'id="btnCall"' in body and 'id="btnMute"' in body and 'id="btnHangup"' in body
    assert 'id="callerId"' in body and 'id="enableVoice"' in body and 'data-digit=' in body
    assert "navigator.locks.request(VOICE_LOCK_NAME" in body
    assert "coordPost('owner-query')" in body


def test_text_and_clock_routes_unchanged(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    inbox = client.get("/app/inbox").get_data(as_text=True)
    assert "luxitPwaDeviceKey" in inbox and "new Device(" not in inbox
    recents = client.get("/app/recents").get_data(as_text=True)
    assert 'id="list"' in recents


def test_dialer_page_leaks_no_secrets(app):
    uid = _seed(app, with_device=True)
    client = app.test_client()
    _login(client, uid)
    body = client.get("/app/phone").get_data(as_text=True)
    assert "ACtest" not in body and "SKtest" not in body
    assert "auth_token" not in body and "api_secret" not in body
