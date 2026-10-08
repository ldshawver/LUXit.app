/* Behavioral tests for static/js/lux-voice.js (the persistent Voice service).
 * Run: node --test tests/js/   (also invoked by tests/test_lux_voice_engine.py)
 *
 * Harness: a fake Twilio.Device with the SDK 2.x state/event surface, a real
 * FIFO exclusive Web-Lock manager, a manual clock, fetch routes, and a
 * per-tab sessionStorage shared between "documents" so a full-page
 * navigation (pagehide on page A, boot on page B) is simulated faithfully.
 */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const LuxVoice = require(path.join(__dirname, '..', '..', 'static', 'js', 'lux-voice.js'));

/* ── tiny EventEmitter ─────────────────────────────────────────────────── */
class Emitter {
  constructor() { this._h = new Map(); }
  on(ev, fn) { if (!this._h.has(ev)) this._h.set(ev, []); this._h.get(ev).push(fn); return this; }
  emit(ev, ...a) { (this._h.get(ev) || []).slice().forEach((fn) => fn(...a)); }
  removeAllListeners() { this._h.clear(); }
  listenerCount(ev) { return (this._h.get(ev) || []).length; }
}

/* ── manual clock ──────────────────────────────────────────────────────── */
function makeClock() {
  let now = 1_700_000_000_000; let seq = 0; const timers = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms) { const id = ++seq; timers.set(id, { at: now + (ms || 0), fn }); return id; },
    clearTimeout(id) { timers.delete(id); },
    async advance(ms) {
      const target = now + ms;
      for (;;) {
        await flush();                                   // let in-flight async work schedule its timers
        let next = null;
        for (const [id, t] of timers) if (t.at <= target && (!next || t.at < next[1].at)) next = [id, t];
        if (!next) break;
        timers.delete(next[0]); now = next[1].at; next[1].fn();
        await flush();
      }
      now = target; await flush();
    },
    pending: () => timers.size,
  };
}
const flush = () => new Promise((r) => setImmediate(r));   // drains every pending microtask

/* ── FIFO exclusive Web Locks ──────────────────────────────────────────── */
function makeLocks() {
  const queues = new Map();
  function pump(name) {
    const q = queues.get(name);
    if (!q || q.held || !q.waiting.length) return;
    const w = q.waiting.shift();
    q.held = w;
    Promise.resolve().then(() => w.cb()).then(() => { q.held = null; w.resolve(); pump(name); }, (e) => { q.held = null; w.reject(e); pump(name); });
  }
  return {
    request(name, opts, cb) {
      if (!queues.has(name)) queues.set(name, { held: null, waiting: [] });
      const q = queues.get(name);
      return new Promise((resolve, reject) => {
        const w = { cb, resolve, reject };
        if (opts && opts.signal) opts.signal.addEventListener('abort', () => {
          const i = q.waiting.indexOf(w); if (i >= 0) { q.waiting.splice(i, 1); reject(Object.assign(new Error('aborted'), { name: 'AbortError' })); }
        });
        q.waiting.push(w); pump(name);
      });
    },
    holder: (name) => queues.get(name) && queues.get(name).held,
  };
}

/* ── fake Twilio SDK ───────────────────────────────────────────────────── */
function makeSdk() {
  const created = [];
  const audioContext = { state: 'suspended', resumes: 0, resume() { this.resumes++; this.state = 'running'; return Promise.resolve(); } };
  class FakeCall extends Emitter {
    constructor(sid, params) {
      super(); this.parameters = { CallSid: sid, From: params.from_number || '' };
      this.customParameters = new Map(Object.entries(params));
      this.accepted = 0; this.rejected = 0; this.disconnected = 0; this.muted = false;
    }
    status() { return this._status || 'pending'; }
    accept() { this.accepted++; this._status = 'open'; this.emit('accept', this); }
    reject() { this.rejected++; this._status = 'closed'; this.emit('reject'); }
    disconnect() { this.disconnected++; this._status = 'closed'; this.emit('disconnect', this); }
    mute(m) { this.muted = m; }
  }
  class FakeDevice extends Emitter {
    constructor(token, options) {
      super(); this.token = token; this.options = options; this.state = 'unregistered';
      this.registerCalls = 0; this.updateTokenCalls = 0; this.destroyed = false;
      this.autoRegister = FakeDevice.autoRegister; this.nextRegisterError = FakeDevice.nextRegisterError; FakeDevice.nextRegisterError = null;
      this.ringtone = { playing: false, stops: 0, stop() { this.playing = false; this.stops++; } };
      this._soundcache = new Map([['incoming', this.ringtone]]);
      created.push(this);
    }
    async register() {
      this.registerCalls++;
      this.state = 'registering'; this.emit('registering');
      if (this.nextRegisterError) { const e = this.nextRegisterError; this.nextRegisterError = null; this.state = 'unregistered'; throw e; }
      if (this.autoRegister) { this.state = 'registered'; this.emit('registered'); }
    }
    updateToken(t) { this.updateTokenCalls++; this.token = t; }
    destroy() { this.destroyed = true; this.state = 'destroyed'; }
    disconnectAll() {}
    async connect({ params }) { const c = new FakeCall('CAout', {}); c.to = params.To; return c; }
    /* test drivers */
    dropSignaling() { this.state = 'unregistered'; this.emit('unregistered'); }
    sdkReconnects() { this.state = 'registered'; this.emit('registered'); }
    invite(sid, params) {
      const call = new FakeCall(sid, Object.assign({ call_log_id: '77', from_number: '+15005550006', caller_name: 'Test Caller' }, params || {}));
      this.ringtone.playing = true;                       // SDK starts its own ringtone first
      const stopSdkRing = () => { this.ringtone.playing = false; };
      ['accept', 'reject', 'cancel', 'disconnect'].forEach((ev) => call.on(ev, stopSdkRing));
      this.emit('incoming', call);
      return call;
    }
  }
  FakeDevice.autoRegister = true;
  FakeDevice.nextRegisterError = null;
  FakeDevice.audioContext = audioContext;
  return { FakeDevice, FakeCall, created, audioContext };
}

/* ── fake storage + DOM-ish targets ────────────────────────────────────── */
function makeStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), _m: m };
}
function makeTarget() {
  const h = new Map();
  return {
    addEventListener(t, fn) { if (!h.has(t)) h.set(t, []); h.get(t).push(fn); },
    dispatch(t, ev) { (h.get(t) || []).forEach((fn) => fn(ev || {})); },
    count: (t) => (h.get(t) || []).length,
  };
}

let tokenSeq = 0;
function jwt(expSec) {
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `${b64({ alg: 'HS256' })}.${b64({ exp: expSec, n: ++tokenSeq })}.sig`;
}

/* A browser TAB: shared clock/locks/session across its documents. */
function makeTab(o) {
  o = o || {};
  const clock = o.clock || makeClock();
  const locks = o.locks || makeLocks();
  const sdk = o.sdk || makeSdk();
  const session = makeStorage();
  const local = o.local || makeStorage();
  const server = Object.assign({ receive_calls: true, availability: 'available', tokenStatus: 200, tokenCode: null, tokenTtlSec: 3600 }, o.server || {});
  const calls = { token: 0, posts: [] };
  const fetch = async (url, init) => {
    const method = (init && init.method) || 'GET';
    const json = (status, body) => ({ ok: status < 400, status, headers: { get: () => 'application/json' }, json: async () => body });
    if (url.startsWith('/api/phone/voice-token')) {
      calls.token++;
      if (server.tokenStatus !== 200) return json(server.tokenStatus, { success: false, code: server.tokenCode, error: 'nope' });
      return json(200, { success: true, token: jwt(Math.floor(clock.now() / 1000) + server.tokenTtlSec), identity: 'luxit_c1_u1_x', calling_number: '+19165550100', outbound_enabled: true });
    }
    if (url === '/api/phone/receive-calls') {
      if (method === 'PUT') server.receive_calls = JSON.parse(init.body).receive_calls;
      return json(200, { receive_calls: server.receive_calls });
    }
    if (url === '/api/phone/availability') {
      if (method === 'PUT') server.availability = JSON.parse(init.body).state;
      return json(200, { state: server.availability });
    }
    if (method === 'POST') { calls.posts.push(url); return json(200, { success: true }); }
    return json(200, {});
  };
  function openPage(extra, page) {
    page = page || {};
    const win = makeTarget(); const doc = makeTarget(); doc.visibilityState = 'visible';
    const nav = Object.assign({ locks, onLine: true, mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } }, page.nav || {});
    const env = Object.assign({
      window: win, document: doc, navigator: nav, fetch,
      setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, now: clock.now, random: () => 0.5,
      session, local, BroadcastChannel: null, tabId: `doc-${Math.random()}`,
      deviceCtor: () => sdk.FakeDevice, sdkReady: async () => true, confirm: () => true,
    }, page.env || {});
    const engine = LuxVoice.create(Object.assign({ userId: 1, companyId: 1, receiveCalls: true, csrf: 't', env }, extra || {}));
    return { engine, win, doc, nav, env };
  }
  return { clock, locks, sdk, session, local, server, calls, openPage };
}

async function bootReady(page, clock) {
  await page.engine.boot();
  await clock.advance(10);
  assert.equal(page.engine.snapshot().status, 'ready');
}

/* ─────────────────────────────── tests ─────────────────────────────────── */

test('initial registration: one token, one Device, Ready only when SDK is registered', async () => {
  const tab = makeTab();
  tab.sdk.FakeDevice.autoRegister = false;
  const p = tab.openPage();
  await p.engine.boot(); await tab.clock.advance(10);
  assert.equal(tab.sdk.created.length, 1);
  assert.equal(tab.calls.token, 1);
  assert.notEqual(p.engine.snapshot().status, 'ready', 'must not claim Ready before SDK registered');
  assert.equal(p.engine.snapshot().status, 'connecting');
  tab.sdk.created[0].sdkReconnects();
  assert.equal(p.engine.snapshot().status, 'ready');
  tab.sdk.FakeDevice.autoRegister = true;
});

test('registration never requests the microphone', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  let mic = 0; p.nav.mediaDevices.getUserMedia = async () => { mic++; throw Object.assign(new Error('x'), { name: 'NotAllowedError' }); };
  await bootReady(p, tab.clock);
  assert.equal(mic, 0);
});

test('registers on a non-Phone page (fresh launch on Conversations)', async () => {
  const tab = makeTab();
  const inbox = tab.openPage();            // index.html boots the same engine
  await bootReady(inbox, tab.clock);
  assert.ok(tab.locks.holder(LuxVoice.constants.LOCK_NAME), 'owns the Voice lock');
});

test('navigation Phone -> Conversations -> Phone: no new token mint, exactly one live Device per document', async () => {
  const tab = makeTab();
  const phone = tab.openPage();
  await bootReady(phone, tab.clock);
  phone.engine._internal.onPageHide();                         // full-document navigation
  assert.equal(tab.sdk.created[0].destroyed, true);
  const inbox = tab.openPage();
  await bootReady(inbox, tab.clock);
  inbox.engine._internal.onPageHide();
  const phone2 = tab.openPage();
  await bootReady(phone2, tab.clock);
  assert.equal(tab.calls.token, 1, 'token minted once for the whole tab session');
  assert.equal(tab.sdk.created.length, 3, 'one Device per document');
  assert.equal(tab.sdk.created.filter((d) => !d.destroyed).length, 1, 'exactly one live Device');
});

test('token cache is keyed by user+company (tenant/user change mints a new identity)', async () => {
  const tab = makeTab();
  const a = tab.openPage();
  await bootReady(a, tab.clock);
  a.engine._internal.onPageHide();
  const b = tab.openPage({ companyId: 2 });
  await bootReady(b, tab.clock);
  assert.equal(tab.calls.token, 2);
});

test('token refresh: tokenWillExpire updates the token in place, no second Device', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  d.emit('tokenWillExpire');
  await tab.clock.advance(10);
  assert.equal(tab.calls.token, 2);
  assert.equal(d.updateTokenCalls, 1);
  assert.equal(tab.sdk.created.length, 1);
  assert.equal(p.engine.snapshot().status, 'ready');
});

test('transient signaling drop: SDK reconnect is not fought (no rebuild, no mint)', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  d.dropSignaling();
  assert.equal(p.engine.snapshot().status, 'reconnecting');
  await tab.clock.advance(3000);
  d.sdkReconnects();
  await tab.clock.advance(30000);
  assert.equal(p.engine.snapshot().status, 'ready');
  assert.equal(tab.sdk.created.length, 1);
  assert.equal(tab.calls.token, 1);
});

test('stuck reconnect: watchdog rebuilds once after the grace window', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  tab.sdk.created[0].dropSignaling();
  await tab.clock.advance(LuxVoice.constants.RECONNECT_GRACE_MS + 2500);
  assert.equal(tab.sdk.created.length, 2);
  assert.equal(tab.sdk.created[0].destroyed, true);
  assert.equal(p.engine.snapshot().status, 'ready');
  assert.equal(tab.calls.token, 1, 'rebuild reuses the still-valid cached token');
});

test('offline -> online recovery: waits while offline, recovers once online', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  p.nav.onLine = false; p.engine._internal.onOffline();
  d.dropSignaling();
  assert.equal(p.engine.snapshot().status, 'reconnecting');
  assert.equal(p.engine.snapshot().offline, true);
  await tab.clock.advance(120000);                    // long outage: no rebuild storm
  assert.ok(tab.sdk.created.length <= 2);
  p.nav.onLine = true; p.engine._internal.onOnline();
  await tab.clock.advance(10000);
  assert.equal(p.engine.snapshot().status, 'ready');
});

test('expired token after iOS suspension: 20104 -> fresh token -> recovered', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  await tab.clock.advance(2 * 3600 * 1000);           // page frozen past token expiry
  d.dropSignaling();
  d.emit('error', { code: 20104, message: 'AccessTokenExpired' });
  await tab.clock.advance(10);
  assert.equal(tab.calls.token, 2, 'fresh token minted');
  assert.equal(d.updateTokenCalls, 1, 'token swapped in place');
  assert.equal(p.engine.snapshot().status, 'ready');
  assert.equal(tab.sdk.created.length, 1);
});

test('resume (visibilitychange) with a live registration does nothing', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  p.engine._internal.onVisible();
  await tab.clock.advance(10);
  assert.equal(tab.sdk.created.length, 1);
  assert.equal(tab.calls.token, 1);
});

test('bounded backoff: fast retries stop at the cap, then ONE quiet attempt per SLOW_RETRY_MS, self-heals without a tap', async () => {
  const tab = makeTab({ server: { tokenStatus: 503, tokenCode: 'TOKEN_ENDPOINT_FAILED' } });
  const p = tab.openPage();
  await p.engine.boot();
  await tab.clock.advance(5 * 60 * 1000);              // fast backoff fully spent (2+4+8+…+60 s ≈ 4 min)
  assert.equal(p.engine.snapshot().status, 'unavailable');
  assert.equal(p.engine.snapshot().phase, 'error');
  assert.ok(tab.calls.token <= LuxVoice.constants.MAX_AUTO_ATTEMPTS + 1, `token attempts ${tab.calls.token}`);
  const before = tab.calls.token;
  await tab.clock.advance(60 * 60 * 1000);             // one hour of a dead token service
  const slow = tab.calls.token - before;
  assert.ok(slow <= Math.ceil(3600000 / LuxVoice.constants.SLOW_RETRY_MS), `slow attempts ${slow} — bounded, no storm`);
  assert.ok(slow >= 1, 'keeps trying quietly instead of stranding the user');
  tab.server.tokenStatus = 200;                        // service comes back; NO user action
  await tab.clock.advance(LuxVoice.constants.SLOW_RETRY_MS + 10);
  assert.equal(p.engine.snapshot().phase, 'ready');
  assert.equal(tab.sdk.created.filter((d) => !d.destroyed).length, 1, 'exactly one live Device after recovery');
});

test('slow retry pauses while the page is hidden; resume retries immediately', async () => {
  const tab = makeTab({ server: { tokenStatus: 503, tokenCode: 'TOKEN_ENDPOINT_FAILED' } });
  const p = tab.openPage();
  await p.engine.boot();
  await tab.clock.advance(5 * 60 * 1000);
  p.doc.visibilityState = 'hidden';
  const before = tab.calls.token;
  await tab.clock.advance(30 * 60 * 1000);
  assert.equal(tab.calls.token, before, 'no token requests from a background page');
  tab.server.tokenStatus = 200;
  p.doc.visibilityState = 'visible'; p.doc.dispatch('visibilitychange');
  await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().phase, 'ready');
});

test('explicit Reconnect (diagnostic) still recovers at once', async () => {
  const tab = makeTab({ server: { tokenStatus: 503, tokenCode: 'TOKEN_ENDPOINT_FAILED' } });
  const p = tab.openPage();
  await p.engine.boot();
  await tab.clock.advance(5 * 60 * 1000);
  tab.server.tokenStatus = 200;
  p.engine.retry();
  await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().status, 'ready');
});

test('terminal token-endpoint failure: no automatic retry', async () => {
  const tab = makeTab({ server: { tokenStatus: 403, tokenCode: 'NO_ASSIGNED_NUMBER' } });
  const p = tab.openPage();
  await p.engine.boot();
  await tab.clock.advance(10 * 60 * 1000);
  assert.equal(tab.calls.token, 1);
  assert.equal(p.engine.snapshot().status, 'unavailable');
});

test('bfcache: pagehide tears down + releases; pageshow(persisted) re-registers from cached token', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  p.engine._internal.onPageHide();
  await flush();
  assert.equal(tab.locks.holder(LuxVoice.constants.LOCK_NAME), null);
  p.engine._internal.onPageShow({ persisted: true });
  await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().status, 'ready');
  assert.equal(tab.calls.token, 1);
  assert.equal(tab.sdk.created.filter((d) => !d.destroyed).length, 1, 'no duplicate Device after restore');
});

test('Web Lock: second document in another window is passive, never registers, takes over on handoff', async () => {
  const tab = makeTab();
  const other = makeTab({ clock: tab.clock, locks: tab.locks, sdk: tab.sdk });   // separate window, same origin
  const a = tab.openPage();
  await bootReady(a, tab.clock);
  const b = other.openPage();
  await b.engine.boot();
  await tab.clock.advance(2000);
  assert.equal(b.engine.snapshot().status, 'passive');
  assert.equal(tab.sdk.created.length, 1);
  assert.equal(other.calls.token, 0, 'passive window never mints a token');
  a.engine._internal.handleCoordMessage({ type: 'request-ownership', tabId: 'b' });
  await tab.clock.advance(10);
  assert.equal(b.engine.snapshot().status, 'ready');
  assert.equal(tab.sdk.created.filter((d) => !d.destroyed).length, 1);
});

test('Web Lock: handoff is refused while the owner is on a call', async () => {
  const tab = makeTab();
  const a = tab.openPage();
  await bootReady(a, tab.clock);
  tab.sdk.created[0].invite('CA1');
  a.engine._internal.handleCoordMessage({ type: 'request-ownership', tabId: 'b' });
  assert.equal(a.engine.snapshot().isOwner, true);
  assert.equal(tab.sdk.created[0].destroyed, false);
});

test('incoming call: incoming UI state + ringtone start', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  tab.sdk.created[0].invite('CA1');
  const s = p.engine.snapshot();
  assert.equal(s.callPhase, 'incoming');
  assert.equal(s.ringing, true);
  assert.equal(s.call.name, 'Test Caller');
  assert.equal(tab.sdk.created[0].options.sounds.incoming, LuxVoice.constants.RINGTONE_URL, 'same-origin ringtone');
});

test('ringtone stops on answer (immediately on tap), call becomes active', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  const call = d.invite('CA1');
  const accepted = p.engine.accept();
  assert.equal(p.engine.snapshot().ringing, false, 'ringing flag cleared synchronously on tap');
  assert.ok(d.ringtone.stops >= 1, 'SDK ringtone stopped defensively');
  assert.equal(await accepted, true);
  assert.equal(call.accepted, 1);
  assert.equal(p.engine.snapshot().callPhase, 'active');
  assert.ok(tab.calls.posts.includes('/api/calls/77/accept'));
});

test('ringtone stops on reject (decline)', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  const call = d.invite('CA1');
  p.engine.decline();
  assert.equal(call.rejected, 1);
  assert.equal(p.engine.snapshot().ringing, false);
  assert.equal(d.ringtone.playing, false);
  assert.equal(p.engine.snapshot().callPhase, 'idle');
  assert.ok(tab.calls.posts.includes('/api/calls/77/decline'));
});

test('ringtone stops on caller cancel (also covers Dial timeout -> voicemail)', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  const call = d.invite('CA1');
  call.emit('cancel');
  assert.equal(p.engine.snapshot().ringing, false);
  assert.equal(d.ringtone.playing, false);
  assert.equal(p.engine.snapshot().callPhase, 'idle');
  assert.equal(p.engine.snapshot().status, 'ready');
});

test('ringtone stops on disconnect', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA1');
  call.emit('disconnect', call);
  assert.equal(p.engine.snapshot().ringing, false);
  assert.equal(p.engine.snapshot().callPhase, 'idle');
});

test('ringtone stops on navigation away while ringing (pagehide)', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  d.invite('CA1');
  p.engine._internal.onPageHide();
  assert.equal(p.engine.snapshot().ringing, false);
  assert.ok(d.ringtone.stops >= 1);
});

test('duplicate incoming events do not create overlapping ringers or replace the call', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  const first = d.invite('CA1');
  const dup = d.invite('CA1');
  const second = d.invite('CA2');                    // a second call while one is ringing
  assert.equal(p.engine._internal.st.diag.ringStarts, 1);
  assert.equal(p.engine._internal.st.pendingCall, first);
  assert.equal(dup.rejected, 0, 'a duplicate invite for the same call is ignored, not rejected');
  assert.equal(second.rejected, 1, 'a different second call is rejected -> its caller reaches voicemail');
  assert.equal(first.rejected, 0);
});

test('audio unlock: first gesture resumes the SDK AudioContext; re-unlocks after interruption', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  assert.equal(p.engine.snapshot().audioLocked, true);
  p.doc.dispatch('pointerdown');
  await flush();
  assert.equal(tab.sdk.audioContext.state, 'running');
  assert.equal(p.engine.snapshot().audioLocked, false);
  tab.sdk.audioContext.state = 'interrupted';         // iOS backgrounding
  p.doc.dispatch('touchend');
  await flush();
  assert.equal(tab.sdk.audioContext.state, 'running');
  assert.equal(tab.sdk.audioContext.resumes, 2);
  assert.ok(p.doc.count('pointerdown') >= 1, 'unlock listeners stay installed');
});

test('sign-out tears Voice down, releases the lock and clears the cached token', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  p.engine.signOut();
  assert.equal(tab.sdk.created[0].destroyed, true);
  await flush();
  assert.equal(tab.locks.holder(LuxVoice.constants.LOCK_NAME), null);
  assert.equal(tab.session.getItem(LuxVoice.constants.TOKEN_KEY), null);
});

test('Receive Calls OFF: no token, no Device; turning it off tears down', async () => {
  const tab = makeTab({ server: { receive_calls: false } });
  const p = tab.openPage();
  await p.engine.boot(); await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().status, 'off');
  assert.equal(tab.calls.token, 0);
  assert.equal(tab.sdk.created.length, 0);
  await p.engine.setReceiveCalls(true);
  await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().status, 'ready');
  p.engine.handleServerEvent({ type: 'receive_calls', user_id: 1, receive_calls: false });
  assert.equal(p.engine.snapshot().status, 'off');
  assert.equal(tab.sdk.created[0].destroyed, true);
  await flush();
  assert.equal(tab.locks.holder(LuxVoice.constants.LOCK_NAME), null);
});

test('Away: never mints/registers; Available converges back to Ready', async () => {
  const tab = makeTab({ server: { availability: 'away' } });
  const p = tab.openPage();
  await p.engine.boot(); await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().status, 'away');
  assert.equal(tab.calls.token, 0);
  p.engine._internal.onVisible(); p.engine._internal.onPageShow({ persisted: true });
  await tab.clock.advance(10);
  assert.equal(tab.calls.token, 0, 'resume/bfcache cannot bypass Away');
  p.engine.handleServerEvent({ type: 'phone_availability', user_id: 1, state: 'available' });
  await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().status, 'ready');
  p.engine.handleServerEvent({ type: 'phone_availability', user_id: 2, state: 'away' });
  assert.equal(p.engine.snapshot().status, 'ready', 'other users’ events ignored');
});

test('outbound call asks for the microphone only inside placeCall', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  let mic = 0; p.nav.mediaDevices.getUserMedia = async () => { mic++; return { getTracks: () => [] }; };
  await bootReady(p, tab.clock);
  assert.equal(mic, 0);
  const call = await p.engine.placeCall('+15005550006', '3');
  assert.equal(mic, 1);
  assert.equal(call.to, '+15005550006');
  call.emit('accept', call);
  assert.equal(p.engine.snapshot().callPhase, 'active');
  p.engine.hangup();
  assert.equal(call.disconnected, 1);
  assert.equal(p.engine.snapshot().callPhase, 'idle');
});

test('answer with blocked microphone rejects cleanly instead of hanging', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  p.nav.mediaDevices.getUserMedia = async () => { throw Object.assign(new Error('denied'), { name: 'NotAllowedError' }); };
  const call = tab.sdk.created[0].invite('CA1');
  assert.equal(await p.engine.accept(), false);
  assert.equal(call.rejected, 1);
  assert.equal(p.engine.snapshot().callPhase, 'idle');
  assert.equal(p.engine.snapshot().ringing, false);
});

test('lock granted after Receive Calls went OFF is released (no leaked lock)', async () => {
  const tab = makeTab();
  const other = makeTab({ clock: tab.clock, locks: tab.locks, sdk: tab.sdk });
  const a = tab.openPage();
  await bootReady(a, tab.clock);
  const b = other.openPage();
  await b.engine.boot(); await tab.clock.advance(2000);          // b queued, passive
  b.engine._internal.st.receiveCalls = false;                    // pref flips while queued (no abort path)
  a.engine._internal.onPageHide();                               // a leaves -> b's queued request granted
  await tab.clock.advance(10);
  assert.equal(tab.locks.holder(LuxVoice.constants.LOCK_NAME), null, 'b released the lock it no longer wants');
  assert.equal(other.calls.token, 0);
});

test('pagehide during a call tears down without starting a new registration', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA1');
  await p.engine.accept();
  p.engine._internal.onPageHide();
  await tab.clock.advance(60000);
  assert.equal(tab.sdk.created.length, 1, 'no Device built for a document that is leaving');
  assert.equal(tab.calls.token, 1);
  assert.equal(call.accepted, 1);
});

test('brief offline blip where the SDK stayed registered returns to Ready on online', async () => {
  const tab = makeTab();
  const p = tab.openPage();
  await bootReady(p, tab.clock);
  p.nav.onLine = false; p.engine._internal.onOffline();
  assert.equal(p.engine.snapshot().status, 'reconnecting', 'offline is shown truthfully');
  p.nav.onLine = true; p.engine._internal.onOnline();
  assert.equal(p.engine.snapshot().status, 'ready');
  assert.equal(tab.sdk.created.length, 1);
  assert.equal(tab.calls.token, 1);
});


/* ───────────── Phone reliability + native-like UX (2026-10-08) ───────────── */

test('state machine: initializing → registering → ready → incoming → connecting → connected → ended → ready', async () => {
  const tab = makeTab();
  tab.sdk.FakeDevice.autoRegister = false;
  const p = tab.openPage();
  assert.equal(p.engine.snapshot().phase, 'initializing');
  await p.engine.boot();
  await tab.clock.advance(10);
  assert.equal(p.engine.snapshot().phase, 'registering');
  tab.sdk.created[0].sdkReconnects();                  // SDK 'registered'
  assert.equal(p.engine.snapshot().phase, 'ready');
  const call = tab.sdk.created[0].invite('CA1');
  assert.equal(p.engine.snapshot().phase, 'incoming');
  const accepting = p.engine.accept();
  assert.equal(p.engine.snapshot().phase, 'connecting', 'tap gives instant Connecting feedback');
  await accepting;
  assert.equal(p.engine.snapshot().phase, 'connected');
  await tab.clock.advance(65000);
  call.emit('disconnect', call);                       // remote party hangs up
  const s = p.engine.snapshot();
  assert.equal(s.phase, 'ended');
  assert.equal(s.ended.durationSec, 65);
  assert.equal(s.ended.why, 'disconnect');
  await tab.clock.advance(LuxVoice.constants.ENDED_HOLD_MS + 1);
  assert.equal(p.engine.snapshot().phase, 'ready');
  const t = p.engine._internal.st.transitions.join(' ');
  assert.match(t, /initializing>registering.*registering>ready.*ready>incoming.*incoming>connecting.*connecting>connected.*connected>ended.*ended>ready/);
  for (const ph of p.engine._internal.st.transitions.flatMap((x) => x.split('>'))) assert.ok(LuxVoice.constants.PHASES.includes(ph), ph);
});

test('Ready is never shown when offline even if the SDK still reports registered', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  p.engine._internal.st.offline = true;                 // network dropped, socket not yet noticed
  assert.notEqual(p.engine.snapshot().phase, 'ready');
  p.engine._internal.st.offline = false;
  tab.sdk.created[0].state = 'unregistered';            // status word says ready, SDK disagrees
  assert.notEqual(p.engine.snapshot().phase, 'ready');
});

test('local hang-up: End disconnects the call once, shows ended, returns to Ready, posts /end', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA2');
  await p.engine.accept();
  assert.equal(p.engine.hangup(), true);
  assert.equal(call.disconnected, 1);
  assert.equal(call.rejected, 0);
  assert.equal(p.engine.snapshot().phase, 'ended');
  assert.equal(p.engine.hangup(), false, 'second End is a no-op');
  assert.ok(tab.calls.posts.some((u) => u.endsWith('/77/end')));
  await tab.clock.advance(LuxVoice.constants.ENDED_HOLD_MS + 1);
  assert.equal(p.engine.snapshot().phase, 'ready');
});

test('decline rejects the pending call cleanly and returns to Ready (no ended-duration)', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA3');
  assert.equal(p.engine.decline(), true);
  assert.equal(call.rejected, 1);
  const s = p.engine.snapshot();
  assert.equal(s.phase, 'ended'); assert.equal(s.ended.answered, false); assert.equal(s.ended.durationSec, 0);
  assert.ok(tab.calls.posts.some((u) => u.endsWith('/77/decline')));
  assert.equal(p.engine.decline(), false, 'no second reject');
});

test('double-tap Answer accepts exactly once', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA4');
  const a = p.engine.accept(); const b = p.engine.accept();
  assert.equal(await b, false);
  assert.equal(await a, true);
  assert.equal(call.accepted, 1);
  assert.equal(tab.calls.posts.filter((u) => u.endsWith('/accept')).length, 1);
});

test('End while the answer is still connecting disconnects (does not reject an accepted call)', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA5');
  call.accept = function () { this.accepted++; this._status = 'connecting'; };   // media not up yet
  await p.engine.accept();
  assert.equal(p.engine.snapshot().phase, 'connecting');
  p.engine.hangup();
  assert.equal(call.disconnected, 1); assert.equal(call.rejected, 0);
});

test('second incoming call while busy: rejected to voicemail, current call untouched, user notified', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const first = tab.sdk.created[0].invite('CA6');
  await p.engine.accept();
  const second = tab.sdk.created[0].invite('CA7', { from_number: '+15005550009', caller_name: 'Second' });
  assert.equal(second.rejected, 1);
  const s = p.engine.snapshot();
  assert.equal(s.phase, 'connected'); assert.equal(s.call.callSid, 'CA6');
  assert.equal(first.disconnected, 0);
  assert.deepEqual([s.notice.kind, s.notice.name], ['missed-busy', 'Second']);
  p.engine.dismissNotice();
  assert.equal(p.engine.snapshot().notice, null);
});

test('denied microphone on Answer: call released to voicemail, mic=denied, recovery notice shown', async () => {
  const tab = makeTab();
  const denied = Object.assign(new Error('denied'), { name: 'NotAllowedError' });
  const p = tab.openPage({}, { nav: { mediaDevices: { getUserMedia: async () => { throw denied; } } } });
  await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA8');
  assert.equal(await p.engine.accept(), false);
  assert.equal(call.rejected, 1);
  const s = p.engine.snapshot();
  assert.equal(s.mic, 'denied');
  assert.deepEqual([s.notice.kind, s.notice.context], ['mic-blocked', 'answer']);
  assert.equal(s.phase, 'ended');
  await tab.clock.advance(LuxVoice.constants.ENDED_HOLD_MS + 1);
  assert.equal(p.engine.snapshot().phase, 'ready', 'registration itself is unaffected by the microphone');
});

test('Allow microphone (gesture) grants and clears the blocked notice', async () => {
  const tab = makeTab(); let allow = false;
  const p = tab.openPage({}, { nav: { mediaDevices: { getUserMedia: async () => { if (!allow) throw Object.assign(new Error('x'), { name: 'NotAllowedError' }); return { getTracks: () => [] }; } } } });
  await bootReady(p, tab.clock);
  assert.equal(await p.engine.requestMicrophone(), false);
  assert.equal(p.engine.snapshot().notice.kind, 'mic-blocked');
  allow = true;
  assert.equal(await p.engine.requestMicrophone(), true);
  assert.equal(p.engine.snapshot().mic, 'granted');
  assert.equal(p.engine.snapshot().notice, null);
});

test('microphone permission state is read from the Permissions API without prompting', async () => {
  const tab = makeTab(); let prompted = 0;
  const perm = makeTarget(); perm.state = 'denied';
  const p = tab.openPage({}, { nav: {
    permissions: { query: async ({ name }) => { assert.equal(name, 'microphone'); return perm; } },
    mediaDevices: { getUserMedia: async () => { prompted++; return { getTracks: () => [] }; } },
  } });
  await bootReady(p, tab.clock);
  assert.equal(p.engine.snapshot().mic, 'denied');
  perm.state = 'granted'; perm.dispatch('change');
  assert.equal(p.engine.snapshot().mic, 'granted');
  assert.equal(prompted, 0);
});

test('End during outbound dialing cancels the call even before connect() resolves', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  let resolveConnect; const out = new tab.sdk.FakeCall('CAout', {});
  tab.sdk.created[0].connect = () => new Promise((r) => { resolveConnect = r; });
  const placing = p.engine.placeCall('+15005550006', '1');
  await tab.clock.advance(1);
  assert.equal(p.engine.snapshot().phase, 'connecting');
  assert.equal(p.engine.hangup(), true);
  assert.equal(p.engine.snapshot().phase, 'ended');
  resolveConnect(out);
  assert.equal(await placing, null);
  assert.equal(out.disconnected, 1, 'late call leg is torn down');
});

test('outbound: remote ringing then answer — duration counts from answer, not from dialing', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const call = await p.engine.placeCall('+15005550006', '1');
  call.emit('ringing');
  assert.equal(p.engine.snapshot().remoteRinging, true);
  assert.equal(p.engine.snapshot().phase, 'connecting');
  await tab.clock.advance(12000);
  call.emit('accept', call);
  assert.equal(p.engine.snapshot().phase, 'connected');
  assert.equal(p.engine.snapshot().callStartedAt, tab.clock.now());
});

test('call network quality: reconnecting / poor / recovered are reflected live', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const call = tab.sdk.created[0].invite('CA9'); await p.engine.accept();
  call.emit('warning', 'high-rtt'); assert.equal(p.engine.snapshot().callNetwork, 'poor');
  call.emit('reconnecting'); assert.equal(p.engine.snapshot().callNetwork, 'reconnecting');
  call.emit('reconnected'); assert.equal(p.engine.snapshot().callNetwork, 'ok');
  assert.equal(p.engine.snapshot().phase, 'connected');
});

test('vibration while ringing (where supported) stops on answer', async () => {
  const tab = makeTab(); const buzz = [];
  const p = tab.openPage({}, { nav: { vibrate: (x) => { buzz.push(x); return true; } } });
  await bootReady(p, tab.clock);
  tab.sdk.created[0].invite('CA10');
  await tab.clock.advance(3000);
  const ringingBuzzes = buzz.filter((x) => x !== 0).length;
  assert.ok(ringingBuzzes >= 2, `buzzes ${ringingBuzzes}`);
  await p.engine.accept();
  assert.equal(buzz[buzz.length - 1], 0, 'vibrate(0) cancels');
  await tab.clock.advance(5000);
  assert.equal(buzz.filter((x) => x !== 0).length, ringingBuzzes, 'no buzz after answer');
});

test('background window: incoming call raises a system notification, cleared when ringing stops', async () => {
  const tab = makeTab(); const shown = []; let cleared = 0;
  const p = tab.openPage({}, { env: { notifyCall: (t, b) => shown.push([t, b]), clearCallNotification: () => { cleared++; } } });
  await bootReady(p, tab.clock);
  p.doc.visibilityState = 'hidden';
  const call = tab.sdk.created[0].invite('CA11');
  assert.equal(shown.length, 1); assert.match(shown[0][1], /Test Caller/);
  call.emit('cancel');
  assert.equal(cleared, 1);
  p.doc.visibilityState = 'visible';
  tab.sdk.created[0].invite('CA12');
  assert.equal(shown.length, 1, 'no notification while the app is in front');
});

test('screen wake lock is held only while connected', async () => {
  const tab = makeTab(); const locks = [];
  const wakeLock = { request: async () => { const l = makeTarget(); l.released = false; l.release = () => { l.released = true; }; locks.push(l); return l; } };
  const p = tab.openPage({}, { nav: { wakeLock } });
  await bootReady(p, tab.clock);
  tab.sdk.created[0].invite('CA13');
  assert.equal(locks.length, 0, 'not while merely ringing');
  await p.engine.accept(); await tab.clock.advance(1);
  assert.equal(locks.length, 1); assert.equal(locks[0].released, false);
  p.engine.hangup();
  assert.equal(locks[0].released, true);
});

test('caller name equal to the number is not shown twice', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  tab.sdk.created[0].invite('CA14', { from_number: '+15005550006', caller_name: '+15005550006' });
  assert.equal(p.engine.snapshot().call.name, '');
  assert.equal(p.engine.snapshot().call.from, '+15005550006');
});

test('repeat calls: 10 answer/hang-up cycles leave one Device, constant listeners, no stuck state', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  const d = tab.sdk.created[0];
  const before = ['incoming', 'registered', 'unregistered', 'error', 'tokenWillExpire'].map((e) => d.listenerCount(e));
  for (let i = 0; i < 10; i++) {
    const call = d.invite(`CArep${i}`);
    await p.engine.accept();
    if (i % 2) p.engine.hangup(); else call.emit('disconnect', call);
    await tab.clock.advance(LuxVoice.constants.ENDED_HOLD_MS + 1);
    assert.equal(p.engine.snapshot().phase, 'ready', `cycle ${i}`);
  }
  const after = ['incoming', 'registered', 'unregistered', 'error', 'tokenWillExpire'].map((e) => d.listenerCount(e));
  assert.deepEqual(after, before);
  assert.equal(tab.sdk.created.length, 1);
  assert.equal(tab.calls.token, 1);
  assert.equal(p.engine._internal.st.diag.accepts, 10);
});

test('a new incoming call during the "Call ended" screen is presented immediately', async () => {
  const tab = makeTab(); const p = tab.openPage(); await bootReady(p, tab.clock);
  tab.sdk.created[0].invite('CA15'); p.engine.decline();
  assert.equal(p.engine.snapshot().phase, 'ended');
  tab.sdk.created[0].invite('CA16');
  assert.equal(p.engine.snapshot().phase, 'incoming');
  await tab.clock.advance(LuxVoice.constants.ENDED_HOLD_MS + 1);
  assert.equal(p.engine.snapshot().phase, 'incoming', 'stale ended timer cannot clobber the new call');
});
