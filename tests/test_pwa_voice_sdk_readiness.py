"""Voice SDK readiness — deterministic init, no defer/fetch race.

P0 (2026-09-11): the vendor Twilio Voice SDK <script> is `defer`, which only
runs once the whole document has parsed — strictly AFTER the page's own
inline bootstrap script (parser-synchronous, no defer/async) has already
started running. That bootstrap chain (loadReceiveCalls() ->
applyReceiveCalls() -> startVoiceRegistration() -> initVoice()) used to check
`voiceSdkCtor()` once, synchronously, with nothing forcing it to wait for the
deferred script. A fast /api/phone/receive-calls round-trip could beat a slow
SDK parse+eval (loaded mobile CPU, cold cache) and permanently fail
registration with SDK_MISSING (a terminal code — no auto-retry), even though
the script itself loaded fine a moment later. Reproduced against production
logs: twilio.min.js served 200, SDK_MISSING logged in the same second.

Repaired design: one shared readiness Promise, settled exactly once by the
SDK <script> element's real `load`/`error` event (never polled, never a
fixed sleep). initVoice() awaits it — inside the existing single-flighted
voice.initPromise, so still exactly one token fetch / one Device / one
registration per attempt regardless of how many callers ask.

The repo has no headless-browser harness (see test_pwa_voice_single_active_
client.py); the async ordering is proven by a Node/VM simulation
(scripts/coord_harness_sdk_ready.js-equivalent, run ad hoc, not committed)
that extracts this exact source and replays both orderings of the real race.
These tests lock the resulting *contract* in the shipped template.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "templates" / "inbox_pwa" / "calls.html"
ENGINE = ROOT / "static" / "js" / "lux-voice.js"


@pytest.fixture(scope="module")
def html():
    return CALLS.read_text()


@pytest.fixture(scope="module")
def engine():
    """Phone P0 (2026-09-28): readiness now lives in the persistent Voice
    service (browserEnv().sdkReady), shared by every PWA page."""
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


def test_sdk_script_tag_is_identifiable_and_still_deferred(html):
    assert '<script src="/static/vendor/twilio-voice-sdk/' in html
    tag = re.search(r'<script src="/static/vendor/twilio-voice-sdk/[^>]*>', html).group(0)
    assert "defer" in tag  # must not become blocking


def test_readiness_promise_settles_from_real_script_events_only(engine):
    body = _fn(engine, "sdkReady() {")
    assert "querySelector('script[src*=\"/twilio-voice-sdk/\"]')" in engine
    assert "el.addEventListener('load', done, { once: true });" in body
    assert "el.addEventListener('error', done, { once: true });" in body
    assert "setInterval" not in body


def test_waitforvoicesdk_is_bounded_not_a_blind_poll(engine):
    body = _fn(engine, "sdkReady() {")
    assert "SDK_LOAD_TIMEOUT_MS" in body and "voiceCode: 'SDK_MISSING'" in body
    assert "setInterval" not in body


def test_already_loaded_sdk_is_a_zero_cost_fast_path(engine):
    first_stmt = _fn(engine, "sdkReady() {").strip("{} \n").splitlines()[0].strip()
    assert first_stmt == "if (this.deviceCtor()) return Promise.resolve(true);"


def test_initvoice_awaits_readiness_before_touching_the_constructor(engine):
    body = _fn(engine, "function ensureRegistered(reason, o)")
    assert body.index("await env.sdkReady();") < body.index("await getToken(") < body.index("buildDevice(tok.token)")


def test_sdk_missing_retries_are_bounded_not_a_loop(engine):
    """Policy change (Phone P0): SDK_MISSING was terminal (explicit Retry only).
    A deferred script that failed on flaky cellular should not permanently
    disable Phone on that page, so it is now transient — but automatic retries
    are capped (MAX_AUTO_ATTEMPTS) with exponential backoff + jitter."""
    terminal = re.search(r"const TERMINAL_CODES = new Set\(\[(.*?)\]\);", engine, re.S).group(1)
    assert "'SDK_MISSING'" not in terminal
    assert "st.attempts >= MAX_AUTO_ATTEMPTS" in _fn(engine, "function scheduleRecovery(reason, o)")


def test_single_flight_and_lifecycle_hooks_unchanged(engine):
    assert engine.count("st.inflight = (async () => {") == 1
    assert "if (st.inflight) return st.inflight;" in engine
    attach = _fn(engine, "function attach()")
    assert "w.addEventListener('pagehide', onPageHide);" in attach
    assert "w.addEventListener('pageshow', onPageShow);" in attach
    assert "d.on('tokenWillExpire'" in engine


def test_mic_permission_is_not_part_of_registration(engine):
    """Policy change (Phone P0): iOS rejects getUserMedia outside a user
    gesture (prod MICROPHONE_BLOCKED x9), and receiving calls doesn't need the
    mic until answer. The mic is requested only in accept()/placeCall(); a
    blocked mic surfaces a distinct message there and never blocks
    registration. SDK_MISSING keeps its own distinct copy."""
    assert "ensureMic" not in _fn(engine, "function ensureRegistered(reason, o)")
    assert "MICROPHONE_BLOCKED" not in re.search(r"const TERMINAL_CODES = new Set\(\[(.*?)\]\);", engine, re.S).group(1)
    assert "Microphone access is blocked" in _fn(engine, "async function accept()")
    assert "SDK_MISSING: 'Calling SDK failed to load." in engine
