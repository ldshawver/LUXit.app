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


@pytest.fixture(scope="module")
def html():
    return CALLS.read_text()


def test_sdk_script_tag_is_identifiable_and_still_deferred(html):
    assert '<script src="/static/vendor/twilio-voice-sdk/' in html
    tag = re.search(r'<script src="/static/vendor/twilio-voice-sdk/[^>]*>', html).group(0)
    assert "defer" in tag  # must not become blocking


def test_readiness_promise_settles_from_real_script_events_only(html):
    block = re.search(
        r"const voiceSdkScriptEl = document\.querySelector\('script\[src\^=\"/static/vendor/twilio-voice-sdk/\"\]'\);\n"
        r"const voiceSdkReady = new Promise\(\(resolve, reject\) => \{(.*?)\n\}\);\n",
        html, re.S,
    )
    assert block, "voiceSdkReady promise not found"
    body = block.group(1)
    assert "voiceSdkScriptEl.addEventListener('load'" in body
    assert "voiceSdkScriptEl.addEventListener('error'" in body
    assert "{once: true}" in body
    # no polling anywhere in the readiness contract
    assert "setInterval" not in block.group(0)


def test_waitforvoicesdk_is_bounded_not_a_blind_poll(html):
    fn = re.search(r"async function waitForVoiceSdk\(\) \{(.*?)\n\}\n", html, re.S)
    assert fn, "waitForVoiceSdk() not found"
    body = fn.group(1)
    assert "voiceSdkReady" in body               # awaits the shared, single-settlement promise
    assert "setInterval" not in body              # never a polling loop
    assert "setTimeout(() => reject" in body, "must be bounded (timeout), not an unbounded wait"
    assert "voiceCode: 'SDK_MISSING'" in body      # uniform failure code, unchanged from before the fix


def test_already_loaded_sdk_is_a_zero_cost_fast_path(html):
    fn = re.search(r"async function waitForVoiceSdk\(\) \{(.*?)\n\}\n", html, re.S).group(1)
    # first statement is a synchronous short-circuit — no awaited work when the
    # SDK is already present (the overwhelmingly common case, and the exact
    # path a bfcache restore takes since the script never re-executes there)
    first_stmt = fn.strip().splitlines()[0].strip()
    assert first_stmt == "if (voiceSdkCtor()) return true;"


def test_initvoice_awaits_readiness_before_touching_the_constructor(html):
    iife = re.search(r"voice\.initPromise = \(async \(\) => \{\n    try \{(.*?)\n    \} catch", html, re.S).group(1)
    i_wait = iife.index("await waitForVoiceSdk();")
    i_ctor = iife.index("voiceSdkCtor()")
    i_token = iife.index("readVoiceToken()")
    assert i_wait < i_ctor < i_token, "must wait for the SDK before reading the constructor or minting a token"
    # the old one-shot throw-if-missing check is gone from this call site —
    # waitForVoiceSdk() is now the single source of the SDK_MISSING error here
    assert "if (!Device) throw" not in iife


def test_sdk_missing_is_still_terminal_no_regression_to_retry_policy(html):
    # unchanged from before the fix: one failed wait still requires explicit
    # Retry / reload, it must not auto-hammer the endpoint in a loop
    terminal = re.search(r"const VOICE_TERMINAL_CODES = new Set\(\[(.*?)\]\);", html, re.S).group(1)
    assert "'SDK_MISSING'" in terminal


def test_single_flight_and_lifecycle_hooks_unchanged(html):
    # the fix lives entirely inside the existing single-flighted initPromise —
    # confirms we did not introduce a second init path or duplicate registration
    assert html.count("voice.initPromise = (async () => {") == 1
    assert "if (voice.initPromise) return voice.initPromise;" in html
    assert "window.addEventListener('pagehide', cleanupVoice);" in html
    assert "window.addEventListener('pageshow', (e) => { if (e.persisted) resyncVoiceUi(true); });" in html
    assert "voice.device.on('tokenWillExpire'" in html


def test_mic_permission_states_remain_distinct_from_sdk_missing(html):
    # Phase 4 guard: this fix must not blur SDK_MISSING into MICROPHONE_BLOCKED
    # or vice versa — both already existed pre-fix and must still be separate
    # terminal codes with separate user-facing copy.
    terminal = re.search(r"const VOICE_TERMINAL_CODES = new Set\(\[(.*?)\]\);", html, re.S).group(1)
    assert "'MICROPHONE_BLOCKED'" in terminal
    messages = re.search(r"const messages = \{(.*?)\n      \};", html, re.S).group(1)
    assert "SDK_MISSING:" in messages and "MICROPHONE_BLOCKED:" in messages
    assert messages.split("SDK_MISSING:")[1].split("\n")[0] != messages.split("MICROPHONE_BLOCKED:")[1].split("\n")[0]
