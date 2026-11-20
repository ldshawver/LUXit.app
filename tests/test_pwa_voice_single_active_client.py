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
SW_JS = ROOT / "static" / "sw.js"


@pytest.fixture(scope="module")
def html():
    return CALLS.read_text()


ENGINE = ROOT / "static" / "js" / "lux-voice.js"


@pytest.fixture(scope="module")
def engine():
    """Phone P0 (2026-09-28): the coordinator moved out of calls.html into the
    persistent Voice service booted by every PWA page. Same authority, same
    lock name / bus key — these tests now lock the contract there."""
    return ENGINE.read_text()


def _fn(src, header):
    i = src.index(header)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[j:k + 1]
    raise AssertionError(header)


def _grant_cb(engine):
    i = engine.index("nav.locks.request(LOCK_NAME")
    return _fn(engine[i:], "() => {")


# ------------------------------------------------------------------------- #
# 1 + 7. ONE authority — the exclusive Web Lock. Obsolete paths removed.    #
# ------------------------------------------------------------------------- #

def test_web_lock_is_the_sole_ownership_authority(engine):
    assert "const LOCK_NAME = 'luxit-voice-owner-v1';" in engine
    assert engine.count("nav.locks.request(LOCK_NAME") == 1
    assert engine.count("st.isOwner = true;") == 1
    assert "st.isOwner = true;" in _grant_cb(engine)


def test_obsolete_ownership_mechanisms_are_gone(engine, html):
    for src in (engine, html):
        for banned in (
            "LEASE_STALE_MS", "HEARTBEAT_MS", "stampLease(", "leaseFresh(", "readLease(",
            "dropLeaseIfMine", "claimVotes", "CLAIM_SETTLE_MS", "becomeVoiceOwner",
            "becomePassiveOwner", "_acquireVoiceOwnership", "ifAvailable",
        ):
            assert banned not in src, f"obsolete ownership mechanism still present: {banned}"
    assert "setInterval" not in engine.split("function mountOverlay")[0]


def test_no_race_prone_fallback_when_locks_unavailable(engine):
    assert "lockSupported: !!(nav.locks && nav.locks.request)" in engine
    assert "return st.receiveCalls && st.availability !== 'away' && st.lockSupported;" in engine
    conv = _fn(engine, "function converge(o)")
    assert "if (!st.lockSupported)" in conv and "'unsupported'" in conv


# ------------------------------------------------------------------------- #
# 2. Hold the lock for the whole ownership lifetime.                        #
# ------------------------------------------------------------------------- #

def test_lock_is_held_until_deliberate_release(engine):
    grant = _grant_cb(engine)
    assert "const holding = new Promise((release) => { st.releaseLock = release; });" in grant
    assert "return holding;" in grant
    rel = _fn(engine, "function releaseOwnership()")
    assert "st.isOwner = false;" in rel
    assert "st.releaseLock" in rel and "r();" in rel
    assert "st.lockAbort" in rel and "abort()" in rel


def test_release_only_on_transfer_disable_teardown_or_context_loss(engine):
    transfer = _fn(engine, "} else if (msg.type === 'request-ownership')")
    assert "teardownDevice('handoff');" in transfer and "releaseOwnership();" in transfer
    assert "releaseOwnership();" in _fn(engine, "function onPageHide()")
    assert "releaseOwnership();" in _fn(engine, "function goQuiet(status, detail)")
    assert "releaseOwnership();" in _fn(engine, "function signOut()")


# ------------------------------------------------------------------------- #
# 3 + 9. Second tab queues, never steals.                                  #
# ------------------------------------------------------------------------- #

def test_ordering_invariant_lock_before_token_before_device_before_register(engine):
    body = _fn(engine, "function ensureRegistered(reason, o)")
    i_owner = body.index("if (!st.isOwner) { acquireOwnership(); return Promise.resolve(); }")
    i_token = body.index("await getToken(")
    i_recheck = body.index("if (!shouldRegister() || !st.isOwner) return;")
    i_device = body.index("buildDevice(tok.token)")
    i_reg = body.index("await nd.register()")
    assert i_owner < i_token < i_recheck < i_device < i_reg
    # outbound waits only briefly for a FREE lock — never steals
    assert "Promise.race([acquireOwnership(), new Promise((r) => env.setTimeout(() => r(false), 400))])" in engine


def test_passive_tab_shows_active_elsewhere_and_use_calling_here(engine, html):
    assert "Calling is active in another LUX Connect window." in engine
    assert "setStatus('passive'" in engine
    assert "function requestCallingHere()" in engine
    assert "ensureUseHereButton(s.phase === 'passive');" in html
    assert "els.enableVoice.hidden = s.phase !== 'error'" in html


def test_status_channels_cannot_grant_ownership(engine):
    handler = _fn(engine, "function handleCoordMessage(msg)")
    assert "st.isOwner = true" not in handler
    oc = handler.split("msg.type === 'owner-changed'")[1].split("} else if")[0]
    assert "st.ownerRegistered" in oc and "st.isOwner =" not in oc


# ------------------------------------------------------------------------- #
# 4 + 10. Controlled transfer — old Device dies before new Device.         #
# ------------------------------------------------------------------------- #

def test_transfer_tears_down_old_device_before_releasing_lock(engine):
    transfer = _fn(engine, "} else if (msg.type === 'request-ownership')")
    assert transfer.index("teardownDevice('handoff');") < transfer.index("releaseOwnership();")
    assert "!hasCall()" in transfer, "never hand off mid-call"
    # the requester registers only from inside its own lock grant
    assert "if (shouldRegister()) ensureRegistered('lock-granted');" in _grant_cb(engine)


def test_no_intentional_overlap_window_in_transfer(engine):
    rch = _fn(engine, "function requestCallingHere()")
    assert "st.wantsOwnership = false;" in rch          # timeout only gives up asking
    assert "acquireOwnership();" in rch and "coordPost('request-ownership');" in rch


# ------------------------------------------------------------------------- #
# 5 + 11. Crash / abnormal loss — browser releases the lock, no heartbeat. #
# ------------------------------------------------------------------------- #

def test_crash_recovery_relies_on_browser_lock_release_not_a_heartbeat(engine):
    assert "heartbeat" not in engine.lower().replace("no heartbeat", "")
    assert "new AbortController()" in engine and "{ signal: ac.signal }" in engine
    ph = _fn(engine, "function onPageHide()")
    assert "teardownDevice('pagehide');" in ph and "releaseOwnership();" in ph


# ------------------------------------------------------------------------- #
# 8. Single-flight within the owner.                                       #
# ------------------------------------------------------------------------- #

def test_initvoice_single_flight_and_device_teardown(engine, html):
    body = _fn(engine, "function ensureRegistered(reason, o)")
    assert "if (st.inflight) return st.inflight;" in body
    assert "if (isRegistered() && !o.forceToken)" in body
    bd = _fn(engine, "function buildDevice(token)")
    assert bd.index("teardownDevice('rebuild');") < bd.index("new Device(token")
    td = _fn(engine, "function teardownDevice(reason)")
    assert "removeAllListeners" in td and "destroy" in td
    assert engine.count("new Device(") == 1 and "new Device(" not in html


# ------------------------------------------------------------------------- #
# 11b. Ready is derived only from the LIVE Twilio Device lifecycle.        #
# ------------------------------------------------------------------------- #

def test_ready_string_only_emitted_from_the_registered_event(engine, html):
    assert "case 'ready': return 'Ready to receive calls';" in html
    assert html.count("Ready to receive calls") == 1, "the view prints Ready only for engine phase 'ready'"
    # the phase itself demands a live registration + network, not just the status word
    derive = _fn(engine, "function derivePhase()")
    assert "if (isRegistered() && !st.offline) return 'ready';" in derive
    reg = _fn(engine, "d.on('registered', () =>")
    assert "setStatus('ready'" in reg
    assert "setItem('luxit-voice-ready" not in engine + html
    assert 'data-phase="ready"' not in re.search(r"<body[^>]*>", html).group(0)


def test_stale_restored_page_cannot_show_ready_before_device_registered(engine):
    ps = _fn(engine, "function onPageShow(e)")
    assert "if (e && e.persisted)" in ps and "converge()" in ps
    assert "teardownDevice('pagehide');" in _fn(engine, "function onPageHide()")
    assert "return !!st.device && deviceState() === 'registered';" in engine


def test_reload_requires_a_fresh_registration_before_ready(engine, html):
    assert re.search(r"DOMContentLoaded[^\n]*ensureRegistered", html) is None
    assert "voiceEngine.boot();" in html
    body = _fn(engine, "async function boot()")
    assert body.index("apiJson('/api/phone/receive-calls')") < body.index("converge();")


def test_passive_second_tab_cannot_display_ready(engine):
    assert "setStatus('ready'" not in _fn(engine, "function handleCoordMessage(msg)")


def test_one_lifecycle_mints_bounded_tokens_and_never_loops(engine):
    """Token mints: at most one per tab session (sessionStorage cache keyed by
    user+company, reused while >=5 min remain) plus Twilio-driven refreshes.
    Automatic recovery is capped at MAX_AUTO_ATTEMPTS."""
    gt = _fn(engine, "async function getToken(forceFresh)")
    assert "readCachedToken()" in gt and "cache: 'no-store'" in gt
    rc = _fn(engine, "function readCachedToken()")
    assert "c.userId !== opts.userId || c.companyId !== opts.companyId" in rc
    assert "TOKEN_MIN_REMAINING_MS" in rc
    assert "st.attempts >= MAX_AUTO_ATTEMPTS" in _fn(engine, "function scheduleRecovery(reason, o)")


def test_registration_failure_cannot_leave_ready_visible(engine):
    rf = _fn(engine, "function registrationFailed(err)")
    assert "setStatus('unavailable'" in rf and "setStatus('ready'" not in rf
    unreg = _fn(engine, "d.on('unregistered', () =>")
    assert "setStatus(" in unreg and "'ready'" not in unreg


def test_service_worker_never_serves_a_cached_voice_page_or_token():
    sw = SW_JS.read_text()
    guard = re.search(r"if \(url\.pathname === '/app/phone'.*?\n    return;\n  \}", sw, re.S)
    assert guard, "sw.js voice-path network-only guard missing"
    g = guard.group(0)
    assert "'/app/dial-pad'" in g and "'/api/phone/voice-token'" in g
    assert "e.respondWith(fetch(e.request));" in g
    # and the guard runs before the generic /app/ + /api/ cache-fallback branch
    assert sw.index(g) < sw.index("caches.match(e.request).then(cached => cached || new Response('{\"error\":\"offline\"}'")
    # /app/phone is not pre-cached into the app shell
    shell = re.search(r"const APP_SHELL = \[(.*?)\];", sw, re.S).group(1)
    assert "/app/phone" not in shell and "/app/dial-pad" not in shell


# ------------------------------------------------------------------------- #
# 12. Route aliases share one ownership domain.                            #
# ------------------------------------------------------------------------- #

def test_route_aliases_use_one_coordinator(html, engine):
    # One engine, one lock name, one bus key for every page and alias.
    assert engine.count("const LOCK_NAME =") == 1 and engine.count("const BUS_KEY =") == 1
    assert "const BUS_KEY = 'luxit-voice-coord-v1';" in engine
    assert "VOICE_LOCK_NAME" not in html and "navigator.locks" not in html
    dm = re.search(r"const DIALER_MODE = (.*?);", html).group(1)
    assert "phone|dial-pad" in dm


# ------------------------------------------------------------------------- #
# 13. Server-side invariants — identity / CSRF / outbound / secrets.       #
# ------------------------------------------------------------------------- #

def test_twilio_identity_algorithm_is_unchanged():
    assert "def pwa_voice_identity(company_id: int, user_id: int | None = None, device_key: str | None = None)" in PHONE_IDENTITY.read_text()
    inbox = INBOX_PWA.read_text()
    # Device-approval policy branches the identity, not the Voice authorization:
    #   approval required  -> device-scoped identity (fail closed without one)
    #   approval disabled  -> per-user non-device identity (stale key can't block)
    assert "identity = pwa_voice_identity(company.id, user.id, device.device_key)" in inbox
    assert "identity = pwa_voice_identity(company.id, user.id)" in inbox
    assert 'require_approved_devices = bool(getattr(company, "require_approved_pwa_devices", False))' in inbox
    # inbound routing keeps the device-scoped identity for approval-required tenants
    assert "pwa_voice_identity(ta.company_id, device.user_id, device.device_key)" in TWILIO_SMS.read_text()
    # …and rings the matching per-user identity for approval-disabled tenants
    assert "client_identities.append(pwa_voice_identity(ta.company_id, uid))" in TWILIO_SMS.read_text()


def test_csrf_and_outbound_safe_fail_intact(html, engine):
    assert '<meta name="csrf-token" content="{{ csrf_token() }}">' in html
    assert "headers.set('X-CSRFToken', token)" in html
    assert "csrf: document.querySelector('meta[name=\"csrf-token\"]')?.content" in html
    assert "headers['X-CSRFToken'] = opts.csrf" in engine
    assert "outbound_enabled: tok.outbound_enabled" in engine
    assert "st.tokenInfo.outbound_enabled === false" in engine
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
    assert 'id="btnCall"' in body and "LuxVoice.mountOverlay(voiceEngine" in body
    assert 'id="callerId"' in body and 'id="enableVoice"' in body and 'data-digit=' in body
    assert "/static/js/lux-voice.js" in body and "LuxVoice.create(" in body
    assert "voiceEngine.boot();" in body


def test_text_and_clock_routes_unchanged(app):
    uid = _seed(app)
    client = app.test_client()
    _login(client, uid)
    inbox = client.get("/app/inbox").get_data(as_text=True)
    # Phone P0: the inbox shell boots the persistent Voice service but never
    # constructs a Device itself (only static/js/lux-voice.js does).
    assert "luxitPwaDeviceKey" in inbox and "new Device(" not in inbox
    assert "LuxVoice.create(" in inbox
    recents = client.get("/app/recents").get_data(as_text=True)
    assert 'id="list"' in recents


def test_dialer_page_leaks_no_secrets(app):
    uid = _seed(app, with_device=True)
    client = app.test_client()
    _login(client, uid)
    body = client.get("/app/phone").get_data(as_text=True)
    assert "ACtest" not in body and "SKtest" not in body
    assert "auth_token" not in body and "api_secret" not in body


def test_device_registration_enables_ice_restart_for_network_handover(engine):
    """A live call must be able to survive a real Wi-Fi<->cellular handover.
    Without enableIceRestart the SDK will not renegotiate ICE candidates when
    the network path changes mid-call, so an active call is likely to
    freeze/drop on handover rather than self-heal."""
    i_device = engine.index("new Device(token")
    snippet = engine[i_device:i_device + 200]
    assert "enableIceRestart: true" in snippet
