/* LUX Connect — persistent Voice service.
 *
 * ONE owner of the Twilio Voice Device lifecycle for every authenticated PWA
 * page (Conversations / Settings / Contacts in inbox_pwa/index.html AND
 * Phone / Dial Pad / Recents / Voicemail in inbox_pwa/calls.html). Pages are
 * VIEWS: they subscribe to engine state and call engine actions; none of them
 * creates, registers or destroys a Device itself.
 *
 * Why (Phone P0, 2026-09-28): the Device used to live only inside calls.html.
 * The PWA is a multi-document app, so opening Conversations/Settings (or a
 * fresh launch, start_url=/app/inbox) meant NO registered Device — Twilio's
 * <Dial><Client> failed within ~2 s and the caller went to voicemail while the
 * PWA sat open in the foreground (prod call logs 238/239, 2026-09-28).
 *
 * Invariants
 *  - Exclusive same-origin Web Lock (LOCK_NAME) is the ONLY ownership
 *    authority (unchanged from the single-active-client design). Fail closed
 *    without Web Locks.
 *  - Registration never needs the microphone. getUserMedia runs only inside
 *    an accept/place-call gesture (iOS rejects it outside user activation —
 *    prod MICROPHONE_BLOCKED x9 blocked registration entirely).
 *  - One Device per document, one registration lifecycle. The SDK's own
 *    signaling reconnect is allowed to work (it re-registers by itself); we
 *    only intervene after a grace window, with bounded exponential backoff +
 *    jitter and a hard cap on consecutive automatic attempts.
 *  - Access tokens are cached per tab (sessionStorage) keyed by user+company,
 *    so page navigation does not mint a new token; tokenWillExpire and resume
 *    refresh via device.updateToken() — never a second Device.
 *  - 'ready' is reported only while the SDK Device state is 'registered'.
 *  - Receive Calls OFF / Away / sign-out tear the Device down and release the
 *    lock before any token fetch can happen.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LuxVoice = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const LOCK_NAME = 'luxit-voice-owner-v1';
  const BUS_KEY = 'luxit-voice-coord-v1';
  const STATUS_KEY = 'luxit-voice-status-v1';
  const TOKEN_KEY = 'luxit-voice-token-v1';
  const RINGTONE_URL = '/static/vendor/twilio-voice-sdk/2.18.3/sounds/incoming.mp3';

  const TOKEN_MIN_REMAINING_MS = 5 * 60 * 1000;   // reuse a cached token only with >=5 min left
  const RECONNECT_GRACE_MS = 15000;               // let the SDK's own reconnect finish first
  const ONLINE_GRACE_MS = 5000;
  const PASSIVE_AFTER_MS = 1500;                  // lock still queued -> another window owns Voice
  const BACKOFF_BASE_MS = 2000;
  const BACKOFF_CAP_MS = 60000;
  const MAX_AUTO_ATTEMPTS = 8;
  const SDK_LOAD_TIMEOUT_MS = 15000;
  const TAKEOVER_TIMEOUT_MS = 6000;

  // Twilio access-token / auth failures — recover with a FRESH token.
  const TOKEN_ERROR_CODES = new Set([20101, 20102, 20103, 20104, 20105, 20106, 20107, 20157, 31202, 31204, 31205, 31206, 31207]);
  // Token-endpoint / environment failures that a blind retry cannot fix.
  const TERMINAL_CODES = new Set([
    'VOICE_UNSUPPORTED', 'HTTPS_REQUIRED', 'SESSION_EXPIRED', 'NO_ASSIGNED_NUMBER',
    'DEVICE_NOT_REGISTERED', 'DEVICE_KEY_REQUIRED', 'DEVICE_NOT_FOUND', 'DEVICE_NOT_APPROVED',
    'DEVICE_DISABLED', 'DEVICE_USER_MISMATCH', 'DEVICE_TENANT_MISMATCH', 'CALLING_DISABLED', 'PHONE_AWAY',
  ]);
  const FAILURE_MESSAGES = {
    VOICE_UNSUPPORTED: 'Calling cannot be safely enabled in this browser.',
    SDK_MISSING: 'Calling SDK failed to load. Check your connection.',
    HTTPS_REQUIRED: 'Browser calling requires HTTPS.',
    SESSION_EXPIRED: 'Your session expired. Sign in again.',
    DEVICE_KEY_REQUIRED: 'Register this device to receive calls.',
    DEVICE_NOT_REGISTERED: 'Register and approve this device to receive calls.',
    DEVICE_NOT_FOUND: 'This browser is not registered as an approved device.',
    DEVICE_NOT_APPROVED: 'This device is awaiting administrator approval.',
    DEVICE_DISABLED: 'This device has been disabled.',
    DEVICE_USER_MISMATCH: 'This device belongs to a different user.',
    DEVICE_TENANT_MISMATCH: 'This device belongs to a different company.',
    NO_ASSIGNED_NUMBER: 'No authorized calling number is assigned.',
    TOKEN_ENDPOINT_FAILED: 'Could not reach the voice token service.',
    RETRY_LIMIT: 'Phone could not reconnect. Tap Retry.',
  };

  function jwtExpMs(token) {
    try {
      const part = String(token).split('.')[1];
      const json = JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')));
      return json && json.exp ? json.exp * 1000 : 0;
    } catch (_) { return 0; }
  }

  function safeStorage(getter) {
    try { const s = getter(); s && s.getItem('__lux_probe__'); return s || null; } catch (_) { return null; }
  }

  function browserEnv() {
    const w = self;
    const sdkScript = () => w.document && w.document.querySelector('script[src*="/twilio-voice-sdk/"]');
    return {
      window: w,
      document: w.document,
      navigator: w.navigator,
      fetch: (...a) => w.fetch(...a),
      setTimeout: (...a) => w.setTimeout(...a),
      clearTimeout: (...a) => w.clearTimeout(...a),
      now: () => Date.now(),
      random: () => Math.random(),
      session: safeStorage(() => w.sessionStorage),
      local: safeStorage(() => w.localStorage),
      BroadcastChannel: w.BroadcastChannel,
      tabId: (w.crypto && w.crypto.randomUUID && w.crypto.randomUUID()) || `tab-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      deviceCtor: () => (w.Twilio && (w.Twilio.Device || (w.Twilio.Voice && w.Twilio.Voice.Device))) || null,
      sdkReady() {
        if (this.deviceCtor()) return Promise.resolve(true);
        const el = sdkScript();
        return new Promise((resolve, reject) => {
          const t = w.setTimeout(() => reject(Object.assign(new Error('Voice SDK load timed out.'), { voiceCode: 'SDK_MISSING' })), SDK_LOAD_TIMEOUT_MS);
          const done = () => { w.clearTimeout(t); this.deviceCtor() ? resolve(true) : reject(Object.assign(new Error('Voice SDK missing.'), { voiceCode: 'SDK_MISSING' })); };
          if (!el) { w.clearTimeout(t); done(); return; }
          el.addEventListener('load', done, { once: true });
          el.addEventListener('error', done, { once: true });
        });
      },
      confirm: (msg) => w.confirm(msg),
    };
  }

  function create(options) {
    const opts = Object.assign({ userId: 0, companyId: 0, receiveCalls: true, csrf: '' }, options || {});
    const env = opts.env || browserEnv();
    const nav = env.navigator || {};
    const listeners = new Set();

    const st = {
      booted: false,
      status: 'loading',          // loading|off|away|unsupported|passive|connecting|reconnecting|ready|unavailable
      detail: '',
      failure: null,              // {code, terminal}
      receiveCalls: !!opts.receiveCalls,
      availability: 'available',
      // ownership
      lockSupported: !!(nav.locks && nav.locks.request),
      isOwner: false,
      lockQueued: false,
      releaseLock: null,
      lockAbort: null,
      ownershipPromise: null,
      wantsOwnership: false,
      ownerRegistered: false,
      passiveTimer: null,
      // device
      device: null,
      deviceToken: null,
      intentionalTeardown: false,
      everRegistered: false,
      inflight: null,
      attempts: 0,
      recoveryTimer: null,
      watchdog: null,
      offline: nav.onLine === false,
      tokenInfo: null,            // {calling_number, outbound_enabled, identity}
      // calls
      pendingCall: null,
      activeCall: null,
      callPhase: 'idle',          // idle|incoming|connecting|active
      callInfo: null,             // {callLogId, callSid, from, name, direction}
      muted: false,
      ringing: false,
      seenCallSids: [],
      actionPending: false,
      // audio
      audioLocked: false,
      // diagnostics (tests + support)
      diag: { tokenRequests: 0, tokenCacheHits: 0, devicesCreated: 0, registerCalls: 0, updateTokenCalls: 0, recoveries: 0, ringStarts: 0, ringStops: 0 },
    };

    const bus = env.BroadcastChannel ? new env.BroadcastChannel(BUS_KEY) : null;

    /* ── state + subscribers ─────────────────────────────────────────────── */
    function snapshot() {
      return {
        status: st.status, detail: st.detail, failure: st.failure,
        receiveCalls: st.receiveCalls, availability: st.availability,
        isOwner: st.isOwner, ownerRegistered: st.ownerRegistered, lockSupported: st.lockSupported,
        callPhase: st.callPhase, call: st.callInfo, muted: st.muted, ringing: st.ringing,
        audioLocked: st.audioLocked, offline: st.offline,
        callingNumber: st.tokenInfo && st.tokenInfo.calling_number,
        outboundEnabled: st.tokenInfo ? st.tokenInfo.outbound_enabled : null,
        deviceState: deviceState(),
      };
    }
    function emit() {
      const snap = snapshot();
      listeners.forEach((fn) => { try { fn(snap); } catch (e) { /* a view must never break the engine */ } });
    }
    function setStatus(status, detail) {
      st.status = status;
      st.detail = detail || '';
      emit();
    }
    function deviceState() {
      const d = st.device;
      if (!d) return 'none';
      return d.state || 'unknown';
    }
    function isRegistered() { return !!st.device && deviceState() === 'registered'; }
    function hasCall() { return !!(st.pendingCall || st.activeCall); }
    function shouldRegister() { return st.receiveCalls && st.availability !== 'away' && st.lockSupported; }

    function diagReport(code, message) {
      if (!opts.reportErrors) return;
      try {
        env.fetch('/api/phone/voice-client-error', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': opts.csrf || '' },
          body: JSON.stringify({ code: String(code).slice(0, 80), message: String(message || '').slice(0, 500), detail: '' }),
        }).catch(() => {});
      } catch (_) {}
    }

    /* ── token cache ─────────────────────────────────────────────────────── */
    function readCachedToken() {
      if (!env.session) return null;
      try {
        const c = JSON.parse(env.session.getItem(TOKEN_KEY) || 'null');
        if (!c || c.userId !== opts.userId || c.companyId !== opts.companyId) { env.session.removeItem(TOKEN_KEY); return null; }
        if (!c.exp || c.exp - env.now() < TOKEN_MIN_REMAINING_MS) return null;
        return c;
      } catch (_) { return null; }
    }
    function clearCachedToken() { try { env.session && env.session.removeItem(TOKEN_KEY); } catch (_) {} }

    async function getToken(forceFresh) {
      if (!forceFresh) {
        const c = readCachedToken();
        if (c) { st.diag.tokenCacheHits++; return c; }
      }
      st.diag.tokenRequests++;
      const key = (env.local && env.local.getItem('luxitPwaDeviceKey')) || '';
      const res = await env.fetch(`/api/phone/voice-token${key ? `?device_key=${encodeURIComponent(key)}` : ''}`, {
        cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'application/json', 'X-PWA-Device-Key': key },
      });
      const type = (res.headers && res.headers.get && res.headers.get('content-type')) || '';
      if (!type.includes('application/json')) throw Object.assign(new Error(`Voice token endpoint returned ${res.status}.`), { voiceCode: res.status === 401 ? 'SESSION_EXPIRED' : 'TOKEN_ENDPOINT_FAILED' });
      const body = await res.json();
      if (!res.ok || !body.success) {
        if (res.status === 401) clearCachedToken();
        throw Object.assign(new Error(body.error || `Voice token endpoint failed (${res.status}).`), { voiceCode: body.code || (res.status === 401 ? 'SESSION_EXPIRED' : 'TOKEN_ENDPOINT_FAILED') });
      }
      if (!body.calling_number) throw Object.assign(new Error('No calling number assigned.'), { voiceCode: 'NO_ASSIGNED_NUMBER' });
      const entry = {
        token: body.token, exp: jwtExpMs(body.token) || (env.now() + 55 * 60 * 1000),
        identity: body.identity, calling_number: body.calling_number, outbound_enabled: body.outbound_enabled,
        userId: opts.userId, companyId: opts.companyId,
      };
      try { env.session && env.session.setItem(TOKEN_KEY, JSON.stringify(entry)); } catch (_) {}
      return entry;
    }

    /* ── ownership (exclusive Web Lock — unchanged authority) ───────────── */
    function coordPost(type, extra) {
      const msg = Object.assign({ type, tabId: env.tabId, ts: env.now() }, extra || {});
      try { bus ? bus.postMessage(msg) : (env.local && env.local.setItem(BUS_KEY, JSON.stringify(msg))); } catch (_) {}
      if (type === 'owner-changed') { try { env.local && env.local.setItem(STATUS_KEY, JSON.stringify(msg)); } catch (_) {} }
    }

    function acquireOwnership() {
      if (!shouldRegister()) return Promise.resolve(false);
      if (st.isOwner) return Promise.resolve(true);
      if (st.ownershipPromise) return st.ownershipPromise;
      const ac = typeof AbortController !== 'undefined' ? new AbortController() : null;
      st.lockAbort = ac;
      st.lockQueued = true;
      let settle;
      st.ownershipPromise = new Promise((res) => { settle = res; });
      env.clearTimeout(st.passiveTimer);
      st.passiveTimer = env.setTimeout(() => {
        st.passiveTimer = null;
        if (!st.isOwner && st.lockQueued && shouldRegister()) setStatus('passive', st.ownerRegistered ? 'Calling is active in another LUX Connect window.' : 'Another LUX Connect window controls calling.');
      }, PASSIVE_AFTER_MS);
      nav.locks.request(LOCK_NAME, ac ? { signal: ac.signal } : {}, () => {
        // Create the holding promise FIRST so releaseOwnership() below can
        // actually release it (otherwise the lock would be held forever).
        const holding = new Promise((release) => { st.releaseLock = release; });
        st.lockAbort = null; st.lockQueued = false; st.isOwner = true;
        env.clearTimeout(st.passiveTimer); st.passiveTimer = null;
        settle(true);
        coordPost('owner-changed', { registered: isRegistered() });
        st.wantsOwnership = false;
        if (shouldRegister()) ensureRegistered('lock-granted');
        else releaseOwnership();                  // prefs changed while queued
        return holding;
      }).catch(() => {
        st.lockAbort = null; st.lockQueued = false;
        settle(false);
      });
      return st.ownershipPromise;
    }

    function releaseOwnership() {
      const wasOwner = st.isOwner;
      st.isOwner = false;
      st.ownerRegistered = false;
      st.ownershipPromise = null;
      st.lockQueued = false;
      env.clearTimeout(st.passiveTimer); st.passiveTimer = null;
      if (st.lockAbort) { try { st.lockAbort.abort(); } catch (_) {} st.lockAbort = null; }
      if (st.releaseLock) { const r = st.releaseLock; st.releaseLock = null; try { r(); } catch (_) {} }
      if (wasOwner) coordPost('owner-changed', { registered: false });
    }

    function handleCoordMessage(msg) {
      if (!msg || !msg.type || msg.tabId === env.tabId) return;
      if (msg.type === 'owner-query') {
        if (st.isOwner) coordPost('owner-changed', { registered: isRegistered() });
      } else if (msg.type === 'owner-changed') {
        if (!st.isOwner) {
          st.ownerRegistered = !!msg.registered;
          if (st.status === 'passive') setStatus('passive', st.ownerRegistered ? 'Calling is active in another LUX Connect window.' : 'Another LUX Connect window controls calling.');
        }
      } else if (msg.type === 'request-ownership') {
        // "Use Calling Here" from another window. Never hand off mid-call.
        if (st.isOwner && !hasCall()) {
          teardownDevice('handoff');
          releaseOwnership();
          // Re-queue behind the requester (FIFO) so this window resumes
          // ownership automatically if the other one goes away.
          if (shouldRegister()) acquireOwnership();
        }
      }
    }
    if (bus) bus.addEventListener('message', (e) => handleCoordMessage(e.data));

    /* ── Device lifecycle ────────────────────────────────────────────────── */
    function clearTimers() {
      env.clearTimeout(st.recoveryTimer); st.recoveryTimer = null;
      env.clearTimeout(st.watchdog); st.watchdog = null;
    }

    function teardownDevice(reason) {
      clearTimers();
      stopRinging('teardown');
      const d = st.device;
      st.device = null;
      st.deviceToken = null;
      if (d) {
        st.intentionalTeardown = true;
        try { d.removeAllListeners && d.removeAllListeners(); } catch (_) {}
        try { d.disconnectAll && d.disconnectAll(); } catch (_) {}
        try { d.destroy && d.destroy(); } catch (_) {}
        st.intentionalTeardown = false;
      }
      if (st.pendingCall || st.activeCall) clearCall('teardown', { reconverge: false });
    }

    function buildDevice(token) {
      const Device = env.deviceCtor();
      if (!Device) throw Object.assign(new Error('Voice SDK missing.'), { voiceCode: 'SDK_MISSING' });
      teardownDevice('rebuild');
      st.diag.devicesCreated++;
      // enableIceRestart: a live call survives a Wi-Fi<->cellular handover.
      // sounds.incoming: same-origin ringtone (SW/HTTP cached) instead of the
      // SDK's sdk.twilio.com default, fetched at ring time with a 2 s budget.
      const d = new Device(token, {
        logLevel: 1, codecPreferences: ['opus', 'pcmu'], enableIceRestart: true,
        sounds: { incoming: RINGTONE_URL },
      });
      st.device = d;
      st.deviceToken = token;
      d.on('registering', () => { if (st.device === d && !hasCall()) setStatus(st.everRegistered ? 'reconnecting' : 'connecting'); });
      d.on('registered', () => {
        if (st.device !== d) return;
        st.everRegistered = true;
        st.attempts = 0;
        st.failure = null;
        clearTimers();
        coordPost('owner-changed', { registered: true });
        refreshAudioLock();
        setStatus('ready', st.tokenInfo ? st.tokenInfo.calling_number : '');
      });
      d.on('unregistered', () => {
        if (st.device !== d || st.intentionalTeardown) return;
        coordPost('owner-changed', { registered: false });
        if (!shouldRegister()) return;
        // Usually the signaling socket dropped; the SDK re-registers on its
        // own when the stream reconnects. Show it truthfully and give it a
        // grace window before intervening — never rebuild immediately.
        if (!hasCall()) setStatus(st.offline ? 'reconnecting' : (st.everRegistered ? 'reconnecting' : 'connecting'), st.offline ? 'Offline — waiting for network' : '');
        armWatchdog(RECONNECT_GRACE_MS);
      });
      d.on('error', (error) => onDeviceError(d, error));
      d.on('incoming', (call) => onIncoming(d, call));
      d.on('tokenWillExpire', () => { if (st.device === d) refreshToken('tokenWillExpire'); });
      return d;
    }

    function onDeviceError(d, error) {
      if (st.device !== d) return;
      const code = Number(error && error.code) || 0;
      diagReport('TWILIO_DEVICE_ERROR', `${code} ${(error && error.message) || ''}`);
      if (hasCall()) return;                       // call-scoped problems surface on the call
      if (TOKEN_ERROR_CODES.has(code)) {
        scheduleRecovery('token-error', { forceToken: true, immediate: st.attempts === 0 });
        return;
      }
      if (!isRegistered()) {
        setStatus(st.everRegistered ? 'reconnecting' : 'connecting', st.offline ? 'Offline — waiting for network' : '');
        armWatchdog(RECONNECT_GRACE_MS);
      }
    }

    function armWatchdog(ms) {
      if (st.watchdog) return;
      st.watchdog = env.setTimeout(() => {
        st.watchdog = null;
        if (!isRegistered() && shouldRegister()) scheduleRecovery('watchdog', { rebuild: true });
      }, ms);
    }

    function backoffDelay(attempt) {
      const base = Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_CAP_MS);
      return Math.round(base / 2 + env.random() * base / 2);   // "equal jitter"
    }

    function scheduleRecovery(reason, o) {
      o = o || {};
      if (!shouldRegister() || !st.isOwner || st.recoveryTimer) return;
      if (st.failure && st.failure.terminal) return;
      if (st.offline) { setStatus('reconnecting', 'Offline — waiting for network'); return; }
      if (st.attempts >= MAX_AUTO_ATTEMPTS) {
        st.failure = { code: 'RETRY_LIMIT', terminal: false };
        setStatus('unavailable', FAILURE_MESSAGES.RETRY_LIMIT);
        return;
      }
      const delay = o.immediate ? 0 : backoffDelay(st.attempts);
      st.attempts++;
      st.diag.recoveries++;
      st.recoveryTimer = env.setTimeout(() => {
        st.recoveryTimer = null;
        if (!shouldRegister() || !st.isOwner || hasCall()) return;   // never touch a live call
        if (isRegistered()) { setStatus('ready', st.tokenInfo ? st.tokenInfo.calling_number : ''); return; }
        if (o.rebuild) teardownDevice('recover');
        ensureRegistered(reason, { forceToken: !!o.forceToken });
      }, delay);
    }

    function registrationFailed(err) {
      const twilioCode = Number(err && err.code) || 0;
      if (TOKEN_ERROR_CODES.has(twilioCode)) { scheduleRecovery('token-error', { forceToken: true }); return; }
      const code = (err && err.voiceCode) || 'TWILIO_REGISTRATION_FAILED';
      const terminal = TERMINAL_CODES.has(code);
      st.failure = { code, terminal };
      diagReport(code, (err && err.message) || code);
      if (terminal) {
        if (code === 'SESSION_EXPIRED') clearCachedToken();
        setStatus('unavailable', FAILURE_MESSAGES[code] || `Phone unavailable — ${(err && err.message) || code}`);
        return;
      }
      setStatus(st.everRegistered ? 'reconnecting' : 'connecting', FAILURE_MESSAGES[code] || '');
      scheduleRecovery('failure');
    }

    /* Single-flight: converge this document onto one registered Device. */
    function ensureRegistered(reason, o) {
      o = o || {};
      if (!shouldRegister()) return Promise.resolve();
      if (!st.isOwner) { acquireOwnership(); return Promise.resolve(); }
      if (st.inflight) return st.inflight;
      if (isRegistered() && !o.forceToken) { setStatus('ready', st.tokenInfo ? st.tokenInfo.calling_number : ''); return Promise.resolve(); }
      if (!hasCall()) setStatus(st.everRegistered ? 'reconnecting' : 'connecting');
      st.inflight = (async () => {
        try {
          if (typeof location !== 'undefined' && location.protocol && location.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(location.hostname)) {
            throw Object.assign(new Error('HTTPS required'), { voiceCode: 'HTTPS_REQUIRED' });
          }
          await env.sdkReady();
          const tok = await getToken(!!o.forceToken);
          if (!shouldRegister() || !st.isOwner) return;          // state changed while awaiting
          st.tokenInfo = { calling_number: tok.calling_number, outbound_enabled: tok.outbound_enabled, identity: tok.identity };
          st.failure = null;
          const d = st.device;
          if (d && deviceState() !== 'destroyed') {
            if (st.deviceToken !== tok.token) { st.diag.updateTokenCalls++; d.updateToken(tok.token); st.deviceToken = tok.token; }
            if (deviceState() === 'unregistered') { st.diag.registerCalls++; await d.register(); }
            else if (deviceState() === 'registered') setStatus('ready', tok.calling_number);
          } else {
            const nd = buildDevice(tok.token);
            st.diag.registerCalls++;
            await nd.register();
          }
        } catch (err) {
          registrationFailed(err);
        } finally {
          st.inflight = null;
        }
      })();
      return st.inflight;
    }

    async function refreshToken(reason) {
      if (!st.device || !shouldRegister()) return;
      try {
        const tok = await getToken(true);
        if (!st.device) return;
        st.tokenInfo = { calling_number: tok.calling_number, outbound_enabled: tok.outbound_enabled, identity: tok.identity };
        st.diag.updateTokenCalls++;
        st.device.updateToken(tok.token);
        st.deviceToken = tok.token;
      } catch (err) {
        registrationFailed(err);
      }
    }

    /* ── authoritative preferences ───────────────────────────────────────── */
    function goQuiet(status, detail) {
      teardownDevice(status);
      releaseOwnership();
      setStatus(status, detail);
    }

    function converge(o) {
      o = o || {};
      if (!st.lockSupported) { teardownDevice('unsupported'); setStatus('unsupported', FAILURE_MESSAGES.VOICE_UNSUPPORTED); return; }
      if (!st.receiveCalls) { goQuiet('off', 'Receive Calls is off'); return; }
      if (st.availability === 'away') { goQuiet('away', "You're Away. Phone calls are paused."); return; }
      if (o.force) { st.failure = null; st.attempts = 0; clearTimers(); }
      if (st.failure && st.failure.terminal) { setStatus('unavailable', FAILURE_MESSAGES[st.failure.code] || st.detail); return; }
      if (!st.isOwner) {
        if (st.status !== 'passive') setStatus(st.everRegistered ? 'reconnecting' : 'connecting');
        acquireOwnership();
        coordPost('owner-query');
        return;
      }
      ensureRegistered('converge');
    }

    function applyReceiveCalls(enabled, o) {
      const next = !!enabled;
      const changed = next !== st.receiveCalls;
      st.receiveCalls = next;
      converge({ force: (o && o.force) || changed });
    }
    function applyAvailability(state, o) {
      const next = state === 'away' ? 'away' : 'available';
      const changed = next !== st.availability;
      st.availability = next;
      converge({ force: (o && o.force) || changed });
    }

    async function apiJson(url, init) {
      const headers = Object.assign({ Accept: 'application/json' }, (init && init.headers) || {});
      if (init && init.method && init.method !== 'GET') headers['X-CSRFToken'] = opts.csrf || '';
      const res = await env.fetch(url, Object.assign({ credentials: 'same-origin' }, init || {}, { headers }));
      let body = null;
      try { body = await res.json(); } catch (_) {}
      if (!res.ok) throw Object.assign(new Error((body && (body.error || body.message)) || `Request failed (${res.status}).`), { status: res.status });
      return body;
    }

    async function setReceiveCalls(next) {
      const body = await apiJson('/api/phone/receive-calls', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ receive_calls: !!next }) });
      applyReceiveCalls(body && typeof body.receive_calls === 'boolean' ? body.receive_calls : !!next, { force: true });
      return st.receiveCalls;
    }
    async function setAvailability(next) {
      const body = await apiJson('/api/phone/availability', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state: next }) });
      applyAvailability((body && body.state) || next, { force: true });
      return st.availability;
    }

    /* ── incoming calls + ringtone ───────────────────────────────────────── */
    function sdkAudioContext() {
      const D = env.deviceCtor && env.deviceCtor();
      try { return (D && D.audioContext) || null; } catch (_) { return null; }
    }
    function refreshAudioLock() {
      const ctx = sdkAudioContext();
      const locked = !!ctx && ctx.state !== 'running';
      if (locked !== st.audioLocked) { st.audioLocked = locked; emit(); }
    }
    /* iOS/Safari: the SDK rings through its shared static AudioContext, which
       starts 'suspended' in every new document and after an audio
       interruption (backgrounding, another app's audio). Only resume() inside
       a real user gesture un-suspends it. Listeners stay installed for the
       document lifetime (cheap) so a later interruption is re-unlocked by the
       next tap — the eea5b84 candidate removed them after the first unlock. */
    function unlockAudio() {
      const ctx = sdkAudioContext();
      if (ctx && ctx.state !== 'running' && ctx.resume) {
        try { const p = ctx.resume(); p && p.then && p.then(refreshAudioLock, () => {}); } catch (_) {}
      }
      refreshAudioLock();
    }

    function startRinging() {
      if (st.ringing) return;
      st.ringing = true;
      st.diag.ringStarts++;
      refreshAudioLock();
      if (st.audioLocked) diagReport('RINGTONE_AUDIO_LOCKED', 'Incoming call while ringtone audio is locked (no tap yet in this page).');
    }
    function stopRinging(reason) {
      if (!st.ringing) return;
      st.ringing = false;
      st.diag.ringStops++;
      // The SDK stops its own 'incoming' sound on accept/reject/cancel/
      // disconnect; this is a defensive stop for teardown/navigation paths.
      try { const s = st.device && st.device._soundcache && st.device._soundcache.get && st.device._soundcache.get('incoming'); s && s.stop && s.stop(); } catch (_) {}
    }

    function callSid(call) {
      try { return (call.parameters && call.parameters.CallSid) || ''; } catch (_) { return ''; }
    }
    function callParam(call, name) {
      try { return (call.customParameters && call.customParameters.get && call.customParameters.get(name)) || ''; } catch (_) { return ''; }
    }

    function onIncoming(d, call) {
      if (st.device !== d) return;
      const sid = callSid(call);
      if (sid && st.seenCallSids.includes(sid)) return;          // duplicate invite for the same call
      if (sid) { st.seenCallSids.push(sid); if (st.seenCallSids.length > 20) st.seenCallSids.shift(); }
      if (hasCall()) {
        // Never replace a ringing/active call. Reject the extra invite so that
        // caller reaches voicemail promptly instead of ringing out silently.
        try { call.reject(); } catch (_) {}
        return;
      }
      st.pendingCall = call;
      st.callPhase = 'incoming';
      st.callInfo = {
        callLogId: callParam(call, 'call_log_id') || null, callSid: sid,
        from: callParam(call, 'from_number') || (call.parameters && call.parameters.From) || '',
        name: callParam(call, 'caller_name') || '', direction: 'inbound',
      };
      bindCall(call);
      startRinging();
      emit();
    }

    function bindCall(call) {
      if (!call || !call.on) return;
      const end = (why) => () => { if (st.pendingCall === call || st.activeCall === call) { stopRinging(why); clearCall(why); } };
      call.on('accept', () => {
        stopRinging('accept');
        if (st.pendingCall === call) st.pendingCall = null;
        st.activeCall = call; st.callPhase = 'active';
        emit();
      });
      call.on('cancel', end('cancel'));
      call.on('reject', end('reject'));
      call.on('disconnect', end('disconnect'));
      call.on('error', end('error'));
    }

    function clearCall(why, o) {
      st.pendingCall = null; st.activeCall = null; st.callPhase = 'idle';
      st.callInfo = null; st.muted = false; st.actionPending = false;
      st.lastCallEnd = why || '';
      if ((!o || o.reconverge !== false) && shouldRegister() && st.isOwner) {
        if (isRegistered()) setStatus('ready', st.tokenInfo ? st.tokenInfo.calling_number : '');
        else ensureRegistered('call-ended');
      }
      emit();
    }

    async function markCall(action, info) {
      const id = info && info.callLogId;
      if (!id) return;
      try { await apiJson(`/api/calls/${encodeURIComponent(id)}/${action}`, { method: 'POST' }); } catch (_) {}
    }

    async function ensureMic() {
      const md = nav.mediaDevices;
      if (!md || !md.getUserMedia) throw Object.assign(new Error('Microphone capture is not supported in this browser.'), { voiceCode: 'WEBRTC_UNSUPPORTED' });
      try { const s = await md.getUserMedia({ audio: true }); s.getTracks().forEach((t) => t.stop()); }
      catch (e) { throw Object.assign(e, { voiceCode: ['NotAllowedError', 'SecurityError'].includes(e.name) ? 'MICROPHONE_BLOCKED' : 'MICROPHONE_FAILED' }); }
    }

    async function accept() {
      const call = st.pendingCall;
      if (!call || st.actionPending) return false;
      st.actionPending = true;
      stopRinging('accept');                     // silence immediately on tap
      const info = st.callInfo;
      try {
        await ensureMic();                       // inside the tap's user activation
        if (st.pendingCall !== call) return false;   // caller hung up meanwhile
        st.callPhase = 'connecting'; emit();
        call.accept();
        markCall('accept', info);
        return true;
      } catch (e) {
        diagReport(e.voiceCode || 'CALL_FAILED', e.message);
        st.detail = e.voiceCode === 'MICROPHONE_BLOCKED' ? 'Microphone access is blocked — allow it in Settings to answer calls.' : `Could not answer: ${e.message}`;
        try { call.reject(); } catch (_) {}
        clearCall('accept-failed');
        return false;
      } finally { st.actionPending = false; }
    }
    function reject(action) {
      const call = st.pendingCall;
      if (!call) return false;
      const info = st.callInfo;
      stopRinging(action || 'decline');
      try { call.reject(); } catch (_) {}
      markCall(action || 'decline', info);
      clearCall(action || 'decline');
      return true;
    }
    function hangup() {
      const call = st.activeCall || st.pendingCall;
      if (!call) return false;
      const info = st.callInfo;
      stopRinging('hangup');
      try { st.activeCall ? call.disconnect() : call.reject(); } catch (_) {}
      markCall('end', info);
      clearCall('hangup');
      return true;
    }
    function toggleMute() {
      if (!st.activeCall || !st.activeCall.mute) return st.muted;
      st.muted = !st.muted;
      st.activeCall.mute(st.muted);
      emit();
      return st.muted;
    }
    function sendDigits(d) { try { st.activeCall && st.activeCall.sendDigits && st.activeCall.sendDigits(d); } catch (_) {} }

    async function placeCall(to, phoneNumberId) {
      if (!st.receiveCalls) throw Object.assign(new Error('Turn on Receive Calls to place calls from LUX Connect.'), { voiceCode: 'CALLING_DISABLED' });
      if (st.availability === 'away') throw Object.assign(new Error("You're Away. Set yourself Available to make calls."), { voiceCode: 'PHONE_AWAY' });
      if (hasCall()) throw Object.assign(new Error('A call is already active.'), { voiceCode: 'CALL_ACTIVE' });
      await ensureMic();                           // inside the Call button's user activation
      if (!st.isOwner) {
        await Promise.race([acquireOwnership(), new Promise((r) => env.setTimeout(() => r(false), 400))]);
      }
      if (!st.isOwner) throw Object.assign(new Error('Calling is active in another LUX Connect window — choose “Use Calling Here” first.'), { voiceCode: 'VOICE_ACTIVE_ELSEWHERE' });
      if (!st.device || deviceState() === 'destroyed') await ensureRegistered('outbound');
      if (!st.device) throw Object.assign(new Error(st.detail || 'Phone is not connected.'), { voiceCode: (st.failure && st.failure.code) || 'NOT_CONNECTED' });
      if (st.tokenInfo && st.tokenInfo.outbound_enabled === false) throw Object.assign(new Error('Outbound calling isn’t enabled in this environment yet. Incoming calls still work.'), { voiceCode: 'OUTBOUND_DISABLED' });
      st.callPhase = 'connecting';
      st.callInfo = { callLogId: null, callSid: '', from: String(to), name: '', direction: 'outbound' };
      emit();
      try {
        const call = await st.device.connect({ params: { To: to, phone_number_id: phoneNumberId } });
        st.activeCall = call;
        bindCall(call);
        emit();
        return call;
      } catch (e) {
        clearCall('outbound-failed');
        throw e;
      }
    }

    function requestCallingHere() {
      if (st.isOwner || !st.lockSupported) return;
      st.wantsOwnership = true;
      acquireOwnership();
      coordPost('request-ownership');
      env.setTimeout(() => {
        if (!st.isOwner && st.wantsOwnership) {
          st.wantsOwnership = false;
          setStatus('passive', 'Could not move calling here — the other window may be on a call.');
        }
      }, TAKEOVER_TIMEOUT_MS);
    }

    function retry() {
      st.failure = null; st.attempts = 0; clearTimers();
      converge({ force: true });
      if (st.isOwner) { teardownDevice('retry'); ensureRegistered('retry'); }
    }

    /* ── page lifecycle ──────────────────────────────────────────────────── */
    function onOnline() {
      st.offline = false;
      st.attempts = 0;
      if (st.failure && !st.failure.terminal) st.failure = null;
      if (!shouldRegister() || !st.isOwner) { emit(); return; }
      if (isRegistered()) {                      // registration survived the blip
        if (!hasCall()) setStatus('ready', st.tokenInfo ? st.tokenInfo.calling_number : '');
        else emit();
        return;
      }
      if (st.device) { setStatus('reconnecting'); armWatchdog(ONLINE_GRACE_MS); }
      else scheduleRecovery('online', { immediate: true });
    }
    function onOffline() {
      st.offline = true;
      if (shouldRegister() && !hasCall()) setStatus('reconnecting', 'Offline — waiting for network');
    }
    /* Foreground resume. iOS may have suspended the page past token expiry
       (tokenWillExpire cannot fire while frozen) and killed the socket. */
    function onVisible() {
      refreshAudioLock();
      if (!shouldRegister() || hasCall()) return;
      if (!st.isOwner) { converge(); return; }
      const cached = readCachedToken();
      if (isRegistered()) {
        if (!cached) refreshToken('resume');            // token near expiry — refresh in place
        return;
      }
      if (st.failure && st.failure.terminal) return;
      if (st.recoveryTimer || st.inflight) return;
      st.attempts = 0;
      if (st.device) { setStatus('reconnecting'); armWatchdog(ONLINE_GRACE_MS); if (!cached) refreshToken('resume'); }
      else ensureRegistered('resume');
    }
    function onPageHide() {
      // The document is leaving (navigation) or entering bfcache (frozen:
      // sockets die anyway). Unregister cleanly so Twilio stops routing to a
      // dead socket, and release the lock so the next page can own Voice.
      teardownDevice('pagehide');
      releaseOwnership();
    }
    function onPageShow(e) {
      if (e && e.persisted) { st.everRegistered = false; converge(); }
    }
    function signOut() {
      teardownDevice('signout');
      releaseOwnership();
      clearCachedToken();
      try { env.local && env.local.removeItem(STATUS_KEY); } catch (_) {}
      setStatus('off', 'Signed out');
    }

    function attach() {
      const w = env.window, doc = env.document;
      if (w && w.addEventListener) {
        w.addEventListener('online', onOnline);
        w.addEventListener('offline', onOffline);
        w.addEventListener('pagehide', onPageHide);
        w.addEventListener('pageshow', onPageShow);
      }
      if (doc && doc.addEventListener) {
        doc.addEventListener('visibilitychange', () => { if (doc.visibilityState === 'visible') onVisible(); });
        ['pointerdown', 'touchend', 'keydown', 'click'].forEach((t) => doc.addEventListener(t, unlockAudio, { passive: true, capture: true }));
        // Sign-out link: tear Voice down and drop the cached token first.
        doc.addEventListener('click', (e) => {
          const a = e.target && e.target.closest && e.target.closest('a[href]');
          if (!a) return;
          const href = a.getAttribute('href') || '';
          if (/^\/(logout|auth\/logout)(\?|$)/.test(href)) { signOut(); return; }
          // Leaving the document ends a live browser call — confirm first.
          if (st.activeCall && href && !href.startsWith('#') && !/^(tel|sms|mailto):/.test(href) && a.target !== '_blank') {
            if (!env.confirm('Leaving this screen will end your call. Leave anyway?')) { e.preventDefault(); e.stopPropagation(); }
          }
        }, true);
      }
      if (w && w.addEventListener) {
        w.addEventListener('beforeunload', (e) => { if (st.activeCall) { e.preventDefault(); e.returnValue = ''; } });
      }
    }

    /* Load server-authoritative preferences, then converge. */
    async function boot() {
      if (st.booted) return snapshot();
      st.booted = true;
      if (opts.attach !== false) attach();
      // Warm the ringtone into the HTTP/SW cache so the first ring is local.
      try { env.fetch(RINGTONE_URL, { credentials: 'same-origin' }).catch(() => {}); } catch (_) {}
      try {
        const rc = await apiJson('/api/phone/receive-calls');
        if (rc && typeof rc.receive_calls === 'boolean') st.receiveCalls = rc.receive_calls;
      } catch (_) {}
      try {
        const av = await apiJson('/api/phone/availability');
        if (av && av.state) st.availability = av.state === 'away' ? 'away' : 'available';
      } catch (_) {}
      converge();
      return snapshot();
    }

    /* Server-sent events forwarded by each page's existing SSE handler. */
    function handleServerEvent(data) {
      if (!data || !data.type) return false;
      if (data.type === 'phone_availability' && Number(data.user_id) === opts.userId) { applyAvailability(data.state, { force: true }); return true; }
      if (data.type === 'receive_calls' && Number(data.user_id) === opts.userId && typeof data.receive_calls === 'boolean') {
        if (data.receive_calls !== st.receiveCalls) applyReceiveCalls(data.receive_calls, { force: true });
        return true;
      }
      return false;
    }

    function subscribe(fn) { listeners.add(fn); try { fn(snapshot()); } catch (_) {} return () => listeners.delete(fn); }

    return {
      boot, subscribe, snapshot, handleServerEvent,
      setReceiveCalls, setAvailability, applyReceiveCalls, applyAvailability,
      accept, decline: () => reject('decline'), sendToVoicemail: () => reject('voicemail'), hangup, toggleMute, sendDigits, placeCall,
      retry, requestCallingHere, signOut, unlockAudio,
      get device() { return st.device; },
      /* test/support hooks */
      _internal: { st, onOnline, onOffline, onVisible, onPageHide, onPageShow, handleCoordMessage, TOKEN_KEY, LOCK_NAME },
    };
  }

  /* ── Shared call overlay (incoming sheet + in-call bar) ─────────────────
     Mounted on every PWA page so an incoming call is presented wherever the
     user is. Pages with their own in-call controls (the dialer) pass
     {inCallBar:false}. Pure view: reads engine snapshots, calls engine. */
  function mountOverlay(engine, o) {
    o = Object.assign({ inCallBar: true, statusChip: true }, o || {});
    const doc = document;
    if (doc.getElementById('luxVoiceOverlay')) return;
    const style = doc.createElement('style');
    style.textContent = `
#luxVoiceOverlay{position:fixed;left:12px;right:12px;bottom:calc(84px + env(safe-area-inset-bottom));max-width:420px;margin:0 auto;z-index:120;font:inherit;color:var(--pwa-text,#f8fafc)}
#luxVoiceOverlay .lv-sheet{background:var(--pwa-surface,#0b0d14);border:1px solid var(--pwa-border,#273044);border-radius:24px;padding:18px;box-shadow:0 24px 80px rgba(0,0,0,.55)}
#luxVoiceOverlay .lv-mode{font-size:.84rem;color:var(--pwa-muted,#94a3b8)}
#luxVoiceOverlay h2{margin:.2em 0;font-size:1.3rem}
#luxVoiceOverlay .lv-num{color:var(--pwa-muted,#94a3b8);font-size:.9rem}
#luxVoiceOverlay .lv-controls{display:flex;justify-content:space-around;margin-top:16px;gap:8px}
#luxVoiceOverlay .lv-btn{min-width:58px;min-height:58px;border-radius:999px;border:0;color:#fff;cursor:pointer;font-weight:600;padding:0 14px}
#luxVoiceOverlay .lv-accept{background:var(--pwa-success,#22c55e)}
#luxVoiceOverlay .lv-decline{background:var(--pwa-danger,#ef4444)}
#luxVoiceOverlay .lv-vm,#luxVoiceOverlay .lv-mute{background:var(--pwa-primary,#2563eb)}
#luxVoiceOverlay .lv-mute[aria-pressed="true"]{background:var(--pwa-warning,#f59e0b)}
#luxVoiceOverlay .lv-hint{margin-top:10px;font-size:.8rem;color:var(--pwa-warning,#f59e0b)}
#luxVoiceChip{position:fixed;top:calc(8px + env(safe-area-inset-top));left:50%;transform:translateX(-50%);z-index:119;background:var(--pwa-surface,#0b0d14);border:1px solid var(--pwa-border,#273044);color:var(--pwa-muted,#94a3b8);border-radius:999px;padding:4px 12px;font-size:.75rem;box-shadow:0 6px 18px rgba(0,0,0,.35)}
`;
    doc.head.appendChild(style);
    const root = doc.createElement('div');
    root.id = 'luxVoiceOverlay';
    root.hidden = true;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-live', 'assertive');
    root.innerHTML = `<div class="lv-sheet"><div class="lv-mode" data-lv="mode"></div><h2 data-lv="name"></h2><div class="lv-num" data-lv="num"></div>
<div class="lv-controls" data-lv="incoming"><button type="button" class="lv-btn lv-accept" data-lv-act="accept" aria-label="Accept call">Accept</button><button type="button" class="lv-btn lv-vm" data-lv-act="voicemail" aria-label="Send to voicemail">VM</button><button type="button" class="lv-btn lv-decline" data-lv-act="decline" aria-label="Decline call">Decline</button></div>
<div class="lv-controls" data-lv="incall" hidden><button type="button" class="lv-btn lv-mute" data-lv-act="mute" aria-pressed="false">Mute</button><button type="button" class="lv-btn lv-decline" data-lv-act="hangup" aria-label="Hang up">End</button></div>
<div class="lv-hint" data-lv="hint" hidden></div></div>`;
    doc.body.appendChild(root);
    const chip = doc.createElement('div');
    chip.id = 'luxVoiceChip';
    chip.hidden = true;
    chip.setAttribute('role', 'status');
    doc.body.appendChild(chip);
    const q = (k) => root.querySelector(`[data-lv="${k}"]`);
    root.addEventListener('click', async (e) => {
      const act = e.target.closest && e.target.closest('[data-lv-act]');
      if (!act) return;
      const a = act.dataset.lvAct;
      if (a === 'accept') await engine.accept();
      else if (a === 'decline') engine.decline();
      else if (a === 'voicemail') engine.sendToVoicemail();
      else if (a === 'hangup') engine.hangup();
      else if (a === 'mute') engine.toggleMute();
    });
    let timer = null, startedAt = 0;
    engine.subscribe((s) => {
      const incoming = s.callPhase === 'incoming';
      const inCall = (s.callPhase === 'active' || s.callPhase === 'connecting') && o.inCallBar;
      root.hidden = !(incoming || inCall);
      q('incoming').hidden = !incoming;
      q('incall').hidden = !inCall;
      if (s.call) {
        q('name').textContent = s.call.name || s.call.from || 'Unknown caller';
        q('num').textContent = s.call.name ? (s.call.from || '') : '';
      }
      if (incoming) q('mode').textContent = 'Incoming call';
      if (inCall) {
        if (s.callPhase === 'active' && !timer) {
          startedAt = Date.now();
          timer = setInterval(() => { const sec = Math.floor((Date.now() - startedAt) / 1000); q('mode').textContent = `On call · ${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`; }, 1000);
        }
        if (s.callPhase === 'connecting') q('mode').textContent = 'Connecting…';
      } else if (timer) { clearInterval(timer); timer = null; }
      const mute = root.querySelector('[data-lv-act="mute"]');
      mute.setAttribute('aria-pressed', String(!!s.muted));
      mute.textContent = s.muted ? 'Unmute' : 'Mute';
      const hint = q('hint');
      hint.hidden = !(incoming && s.audioLocked);
      hint.textContent = 'Ringtone is muted by the browser until you tap the screen once on this page.';
      if (o.statusChip) {
        let text = '';
        if (s.callPhase === 'idle' && s.receiveCalls && s.availability !== 'away') {
          if (s.status === 'reconnecting') text = s.offline ? 'Phone offline — waiting for network' : 'Phone reconnecting…';
          else if (s.status === 'unavailable') text = 'Phone unavailable — open Phone to retry';
          else if (s.status === 'ready' && s.audioLocked) text = 'Tap anywhere to enable the ringtone';
        }
        chip.textContent = text;
        chip.hidden = !text;
      }
    });
  }

  return { create, mountOverlay, jwtExpMs, constants: { LOCK_NAME, BUS_KEY, STATUS_KEY, TOKEN_KEY, RINGTONE_URL, MAX_AUTO_ATTEMPTS, RECONNECT_GRACE_MS, TOKEN_ERROR_CODES } };
});
