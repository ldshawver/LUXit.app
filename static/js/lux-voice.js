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
  const SLOW_RETRY_MS = 120000;                   // after the backoff cap: one quiet attempt per 2 min while visible
  const ENDED_HOLD_MS = 1800;                     // "Call ended" screen before returning to Ready
  const RING_VIBRATE = [700, 500];                // Android only; iOS has no Vibration API

  /* Phone lifecycle (the ONLY values a view should branch on):
       initializing → registering → ready → incoming → connecting → connected → ended → ready
     plus reconnecting / error and the quiet states off / away / passive.
     The phase is DERIVED on every emit from live SDK Device + Call state —
     never stored or persisted — so it cannot drift from the truth ('ready'
     requires device.state === 'registered' and a network). */
  const PHASES = ['initializing', 'registering', 'ready', 'incoming', 'connecting', 'connected', 'ended', 'reconnecting', 'error', 'off', 'away', 'passive'];

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
    RETRY_LIMIT: 'Phone could not reconnect. Retrying automatically every 2 minutes.',
  };

  function detectPlatform(nav, w) {
    const ua = String((nav && nav.userAgent) || '');
    const touchMac = /Macintosh/.test(ua) && Number(nav && nav.maxTouchPoints) > 1;   // iPadOS reports as Mac
    const os = /iPhone|iPad|iPod/.test(ua) || touchMac ? 'ios' : /Android/.test(ua) ? 'android' : 'desktop';
    let standalone = false;
    try { standalone = !!(nav && nav.standalone) || !!(w && w.matchMedia && w.matchMedia('(display-mode: standalone)').matches); } catch (_) {}
    return { os, standalone };
  }

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
      /* System notification for a call that rings while this window is in the
         background (desktop tab / installed app not focused). Shown only if
         notification permission was already granted — never prompts. */
      notifyCall(title, body) {
        try {
          if (!w.Notification || w.Notification.permission !== 'granted' || !w.navigator.serviceWorker) return;
          w.navigator.serviceWorker.ready.then((reg) => reg.showNotification(title, {
            body, tag: 'lux-incoming-call', renotify: true, requireInteraction: true, silent: true,
            icon: '/static/pwa/lux-connect-192.png', data: { url: '/app/phone', focusOnly: true },
          })).catch(() => {});
        } catch (_) {}
      },
      clearCallNotification() {
        try {
          if (!w.navigator.serviceWorker) return;
          w.navigator.serviceWorker.ready.then((reg) => reg.getNotifications({ tag: 'lux-incoming-call' }))
            .then((list) => (list || []).forEach((n) => n.close())).catch(() => {});
        } catch (_) {}
      },
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
      callStartedAt: 0,           // engine clock ms when media connected (duration source of truth)
      callNetwork: 'ok',          // ok|reconnecting|poor — live call media health
      remoteRinging: false,       // outbound: far end is ringing (answerOnBridge)
      ended: null,                // {why, name, from, direction, durationSec} while the "Call ended" screen shows
      endedTimer: null,
      notice: null,               // {kind:'mic-blocked'|'missed-busy'|'call-failed', ...} — dismissible
      // platform
      mic: 'unknown',             // unknown|prompt|granted|denied (Permissions API where supported)
      platform: detectPlatform(nav, env.window),
      wakeLock: null,
      vibrateTimer: null,
      slowRetryTimer: null,
      // audio
      audioLocked: false,
      // diagnostics (tests + support)
      phase: 'initializing',
      transitions: [],
      diag: { tokenRequests: 0, tokenCacheHits: 0, devicesCreated: 0, registerCalls: 0, updateTokenCalls: 0, recoveries: 0, slowRetries: 0, ringStarts: 0, ringStops: 0, accepts: 0, wakeLocks: 0 },
    };

    const bus = env.BroadcastChannel ? new env.BroadcastChannel(BUS_KEY) : null;

    /* ── state + subscribers ─────────────────────────────────────────────── */
    function derivePhase() {
      if (st.callPhase === 'incoming') return 'incoming';
      if (st.callPhase === 'connecting') return 'connecting';
      if (st.callPhase === 'active') return 'connected';
      if (st.ended) return 'ended';
      switch (st.status) {
        case 'loading': return 'initializing';
        case 'connecting': return 'registering';
        case 'reconnecting': return 'reconnecting';
        case 'ready':
          // Never trust the status word alone: Ready needs a live registration.
          if (isRegistered() && !st.offline) return 'ready';
          return st.offline || st.everRegistered ? 'reconnecting' : 'registering';
        case 'unavailable': case 'unsupported': return 'error';
        case 'off': case 'away': case 'passive': return st.status;
        default: return 'initializing';
      }
    }
    function snapshot() {
      return {
        phase: derivePhase(),
        status: st.status, detail: st.detail, failure: st.failure,
        receiveCalls: st.receiveCalls, availability: st.availability,
        isOwner: st.isOwner, ownerRegistered: st.ownerRegistered, lockSupported: st.lockSupported,
        callPhase: st.callPhase, call: st.callInfo, muted: st.muted, ringing: st.ringing,
        callStartedAt: st.callStartedAt, callNetwork: st.callNetwork, remoteRinging: st.remoteRinging,
        ended: st.ended, notice: st.notice, mic: st.mic, platform: st.platform,
        audioLocked: st.audioLocked, offline: st.offline,
        callingNumber: st.tokenInfo && st.tokenInfo.calling_number,
        outboundEnabled: st.tokenInfo ? st.tokenInfo.outbound_enabled : null,
        outputSelectable: outputSelectable(),
        deviceState: deviceState(),
        now: env.now(),
      };
    }
    function emit() {
      const snap = snapshot();
      if (snap.phase !== st.phase) {
        st.transitions.push(`${st.phase}>${snap.phase}`);
        if (st.transitions.length > 60) st.transitions.shift();
        st.phase = snap.phase;
      }
      listeners.forEach((fn) => { try { fn(snap); } catch (e) { /* a view must never break the engine */ } });
    }
    function outputSelectable() {
      try { return !!(st.device && st.device.audio && st.device.audio.isOutputSelectionSupported); } catch (_) { return false; }
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
      env.clearTimeout(st.slowRetryTimer); st.slowRetryTimer = null;
    }
    function pageVisible() { return !env.document || env.document.visibilityState !== 'hidden'; }

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
        // Fast backoff exhausted. Do not strand the user behind a Retry
        // button: keep ONE quiet attempt every SLOW_RETRY_MS while the page
        // is visible (resume/online also retry immediately). Still bounded —
        // a single timer, a single in-flight attempt, never a loop.
        st.failure = { code: 'RETRY_LIMIT', terminal: false };
        setStatus('unavailable', FAILURE_MESSAGES.RETRY_LIMIT);
        armSlowRetry();
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

    function armSlowRetry() {
      if (st.slowRetryTimer) return;
      st.slowRetryTimer = env.setTimeout(() => {
        st.slowRetryTimer = null;
        if (!shouldRegister() || !st.isOwner || hasCall() || isRegistered()) return;
        if (st.failure && st.failure.terminal) return;
        if (st.offline || !pageVisible()) { armSlowRetry(); return; }   // onOnline / onVisible take over
        st.diag.slowRetries++;
        teardownDevice('slow-retry');
        ensureRegistered('slow-retry');
      }, SLOW_RETRY_MS);
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

    /* Ringing = SDK ringtone (same-origin, warmed) + Android vibration + a
       system notification when this window is in the background. */
    function startRinging() {
      if (st.ringing) return;
      st.ringing = true;
      st.diag.ringStarts++;
      refreshAudioLock();
      if (st.audioLocked) diagReport('RINGTONE_AUDIO_LOCKED', 'Incoming call while ringtone audio is locked (no tap yet in this page).');
      if (nav.vibrate) {
        const buzz = () => {
          if (!st.ringing) return;
          try { nav.vibrate(RING_VIBRATE); } catch (_) {}
          st.vibrateTimer = env.setTimeout(buzz, RING_VIBRATE[0] + RING_VIBRATE[1]);
        };
        buzz();
      }
      if (!pageVisible() && env.notifyCall) {
        const who = (st.callInfo && (st.callInfo.name || st.callInfo.from)) || 'Unknown caller';
        env.notifyCall('Incoming call', `${who} — open LUX Connect to answer`);
      }
    }
    function stopRinging(reason) {
      if (!st.ringing) return;
      st.ringing = false;
      st.diag.ringStops++;
      env.clearTimeout(st.vibrateTimer); st.vibrateTimer = null;
      if (nav.vibrate) { try { nav.vibrate(0); } catch (_) {} }
      if (env.clearCallNotification) env.clearCallNotification();
      // The SDK stops its own 'incoming' sound on accept/reject/cancel/
      // disconnect; this is a defensive stop for teardown/navigation paths.
      try { const s = st.device && st.device._soundcache && st.device._soundcache.get && st.device._soundcache.get('incoming'); s && s.stop && s.stop(); } catch (_) {}
    }

    /* Keep the screen on during a call (a sleeping screen suspends the PWA
       and drops the call on iOS). Best effort; re-acquired on resume. */
    function holdWakeLock() {
      if (st.wakeLock || !nav.wakeLock || !nav.wakeLock.request) return;
      st.wakeLock = 'pending';
      nav.wakeLock.request('screen').then((lock) => {
        if (st.wakeLock !== 'pending' || !hasCall()) { try { lock.release(); } catch (_) {} st.wakeLock = null; return; }
        st.wakeLock = lock; st.diag.wakeLocks++;
        try { lock.addEventListener('release', () => { if (st.wakeLock === lock) st.wakeLock = null; }); } catch (_) {}
      }, () => { st.wakeLock = null; });
    }
    function dropWakeLock() {
      const l = st.wakeLock; st.wakeLock = null;
      if (l && l !== 'pending') { try { l.release(); } catch (_) {} }
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
      const from = callParam(call, 'from_number') || (call.parameters && call.parameters.From) || '';
      let name = callParam(call, 'caller_name') || '';
      if (name === from) name = '';                              // server falls back to the number
      if (hasCall()) {
        // Busy: one call at a time (no call waiting in a browser softphone).
        // Reject the extra invite so that caller reaches voicemail promptly
        // instead of ringing out silently, and tell the user afterwards.
        try { call.reject(); } catch (_) {}
        st.notice = { kind: 'missed-busy', from, name, at: env.now() };
        emit();
        return;
      }
      endEndedScreen();
      st.pendingCall = call;
      st.callPhase = 'incoming';
      st.callInfo = { callLogId: callParam(call, 'call_log_id') || null, callSid: sid, from, name, direction: 'inbound' };
      bindCall(call);
      startRinging();
      emit();
    }

    function bindCall(call) {
      if (!call || !call.on) return;
      const mine = () => st.pendingCall === call || st.activeCall === call;
      const end = (why) => () => { if (mine()) { stopRinging(why); clearCall(why); } };
      call.on('accept', () => {
        stopRinging('accept');
        if (st.pendingCall === call) st.pendingCall = null;
        st.activeCall = call; st.callPhase = 'active';
        st.callStartedAt = env.now(); st.callNetwork = 'ok'; st.remoteRinging = false;
        holdWakeLock();
        emit();
      });
      call.on('ringing', () => { if (mine()) { st.remoteRinging = true; emit(); } });
      call.on('reconnecting', () => { if (mine()) { st.callNetwork = 'reconnecting'; emit(); } });
      call.on('reconnected', () => { if (mine()) { st.callNetwork = 'ok'; emit(); } });
      call.on('warning', () => { if (mine() && st.callNetwork === 'ok') { st.callNetwork = 'poor'; emit(); } });
      call.on('warning-cleared', () => { if (mine() && st.callNetwork === 'poor') { st.callNetwork = 'ok'; emit(); } });
      call.on('cancel', end('cancel'));
      call.on('reject', end('reject'));
      call.on('disconnect', end('disconnect'));
      call.on('error', end('error'));
    }

    function endEndedScreen() {
      env.clearTimeout(st.endedTimer); st.endedTimer = null;
      if (st.ended) { st.ended = null; return true; }
      return false;
    }

    function clearCall(why, o) {
      const info = st.callInfo;
      const answered = st.callPhase === 'active' && st.callStartedAt;
      const durationSec = answered ? Math.max(0, Math.round((env.now() - st.callStartedAt) / 1000)) : 0;
      st.pendingCall = null; st.activeCall = null; st.callPhase = 'idle';
      st.callInfo = null; st.muted = false; st.actionPending = false;
      st.callStartedAt = 0; st.callNetwork = 'ok'; st.remoteRinging = false;
      st.lastCallEnd = why || '';
      env.clearTimeout(st.vibrateTimer); st.vibrateTimer = null;
      dropWakeLock();
      // Brief "Call ended" screen so a hang-up (either side) is unmistakable,
      // then back to Ready. Not shown for teardown/navigation paths.
      endEndedScreen();
      if (info && why !== 'teardown') {
        st.ended = { why, name: info.name, from: info.from, direction: info.direction, durationSec, answered: !!answered };
        st.endedTimer = env.setTimeout(() => { st.endedTimer = null; st.ended = null; emit(); }, ENDED_HOLD_MS);
      }
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

    /* Microphone. Requested only inside a user gesture (answer / call /
       "Allow microphone"); the permission state drives the UI guidance. */
    function setMic(state) { if (state && state !== st.mic) { st.mic = state; emit(); } }
    function watchMicPermission() {
      try {
        if (!nav.permissions || !nav.permissions.query) return;
        nav.permissions.query({ name: 'microphone' }).then((p) => {
          setMic(p.state);
          try { p.addEventListener('change', () => setMic(p.state)); } catch (_) {}
        }, () => {});
      } catch (_) {}
    }
    async function ensureMic() {
      const md = nav.mediaDevices;
      if (!md || !md.getUserMedia) throw Object.assign(new Error('Microphone capture is not supported in this browser.'), { voiceCode: 'WEBRTC_UNSUPPORTED' });
      try {
        const s = await md.getUserMedia({ audio: true }); s.getTracks().forEach((t) => t.stop());
        setMic('granted');
        if (st.notice && st.notice.kind === 'mic-blocked') st.notice = null;
      } catch (e) {
        const blocked = ['NotAllowedError', 'SecurityError'].includes(e.name);
        if (blocked) setMic('denied');
        throw Object.assign(e, { voiceCode: blocked ? 'MICROPHONE_BLOCKED' : 'MICROPHONE_FAILED' });
      }
    }
    async function requestMicrophone() {
      try { await ensureMic(); emit(); return true; }
      catch (e) { st.notice = { kind: 'mic-blocked', context: 'test', at: env.now() }; emit(); return false; }
    }

    async function accept() {
      const call = st.pendingCall;
      if (!call || st.actionPending) return false;   // one activation per call, double taps ignored
      st.actionPending = true;
      st.diag.accepts++;
      stopRinging('accept');                     // silence immediately on tap
      const info = st.callInfo;
      st.callPhase = 'connecting'; emit();       // instant visual feedback: "Connecting…"
      try {
        await ensureMic();                       // inside the tap's user activation
        if (st.pendingCall !== call) return false;   // caller hung up meanwhile
        call.accept();
        markCall('accept', info);
        return true;
      } catch (e) {
        diagReport(e.voiceCode || 'CALL_FAILED', e.message);
        st.notice = e.voiceCode === 'MICROPHONE_BLOCKED'
          ? { kind: 'mic-blocked', context: 'answer', at: env.now() }
          : { kind: 'call-failed', message: `Could not answer: ${e.message}`, at: env.now() };
        if (st.pendingCall === call) { clearCall('accept-failed'); try { call.reject(); } catch (_) {} }
        return false;
      } finally { st.actionPending = false; }
    }
    function reject(action) {
      const call = st.pendingCall;
      if (!call || st.callPhase !== 'incoming') return false;
      const info = st.callInfo;
      stopRinging(action || 'decline');
      clearCall(action || 'decline');
      try { call.reject(); } catch (_) {}
      markCall(action || 'decline', info);
      return true;
    }
    function hangup() {
      const call = st.activeCall || st.pendingCall;
      if (!call && st.callPhase !== 'connecting') return false;
      const info = st.callInfo;
      // Only a still-pending INCOMING invite is rejected; anything the SDK
      // already accepted / is connecting (incl. every outbound leg) is disconnected.
      let sdkStatus = '';
      try { sdkStatus = call && call.status ? call.status() : ''; } catch (_) {}
      const pendingInvite = !!call && call === st.pendingCall && (sdkStatus ? sdkStatus === 'pending' : st.callPhase === 'incoming');
      stopRinging('hangup');
      // Engine state first: the SDK fires reject/disconnect synchronously and
      // the summary must say the USER ended it.
      clearCall('hangup');
      try { if (call) (pendingInvite ? call.reject() : call.disconnect()); } catch (_) {}
      markCall('end', info);
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

    /* Audio output (speaker / headset) — only where the browser supports
       setSinkId (Chromium desktop). iOS/Android route audio at the OS level. */
    function audioOutputs() {
      try {
        if (!outputSelectable()) return [];
        const a = st.device.audio; const sel = a.speakerDevices && a.speakerDevices.get ? Array.from(a.speakerDevices.get()).map((d) => d.deviceId) : [];
        return Array.from(a.availableOutputDevices.values()).map((d) => ({ id: d.deviceId, label: d.label || (d.deviceId === 'default' ? 'Default' : 'Audio output'), selected: sel.includes(d.deviceId) }));
      } catch (_) { return []; }
    }
    async function setAudioOutput(id) {
      if (!outputSelectable()) return false;
      try { await st.device.audio.speakerDevices.set(id); emit(); return true; } catch (_) { return false; }
    }

    async function placeCall(to, phoneNumberId) {
      if (!st.receiveCalls) throw Object.assign(new Error('Turn on Receive Calls to place calls from LUX Connect.'), { voiceCode: 'CALLING_DISABLED' });
      if (st.availability === 'away') throw Object.assign(new Error("You're Away. Set yourself Available to make calls."), { voiceCode: 'PHONE_AWAY' });
      if (hasCall() || st.callPhase !== 'idle') throw Object.assign(new Error('A call is already active.'), { voiceCode: 'CALL_ACTIVE' });
      try { await ensureMic(); }                  // inside the Call button's user activation
      catch (e) { if (e.voiceCode === 'MICROPHONE_BLOCKED') { st.notice = { kind: 'mic-blocked', context: 'call', at: env.now() }; emit(); } throw e; }
      if (!st.isOwner) {
        await Promise.race([acquireOwnership(), new Promise((r) => env.setTimeout(() => r(false), 400))]);
      }
      if (!st.isOwner) throw Object.assign(new Error('Calling is active in another LUX Connect window — choose “Use Calling Here” first.'), { voiceCode: 'VOICE_ACTIVE_ELSEWHERE' });
      if (!st.device || deviceState() === 'destroyed') await ensureRegistered('outbound');
      if (!st.device) throw Object.assign(new Error(st.detail || 'Phone is not connected.'), { voiceCode: (st.failure && st.failure.code) || 'NOT_CONNECTED' });
      if (st.tokenInfo && st.tokenInfo.outbound_enabled === false) throw Object.assign(new Error('Outbound calling isn’t enabled in this environment yet. Incoming calls still work.'), { voiceCode: 'OUTBOUND_DISABLED' });
      endEndedScreen();
      st.callPhase = 'connecting';
      st.remoteRinging = false;
      st.callInfo = { callLogId: null, callSid: '', from: String(to), name: '', direction: 'outbound' };
      const myInfo = st.callInfo;
      emit();
      try {
        const call = await st.device.connect({ params: { To: to, phone_number_id: phoneNumberId } });
        if (st.callInfo !== myInfo) {                // user pressed End while the call was being set up
          try { call.disconnect(); } catch (_) {}
          return null;
        }
        st.activeCall = call;
        bindCall(call);
        emit();
        return call;
      } catch (e) {
        if (st.callInfo === myInfo) clearCall('outbound-failed');
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
      if (st.activeCall) holdWakeLock();             // the OS drops wake locks when hidden
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
      watchMicPermission();
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
      requestMicrophone, dismissNotice() { if (st.notice) { st.notice = null; emit(); } },
      audioOutputs, setAudioOutput,
      get device() { return st.device; },
      /* test/support hooks */
      _internal: { st, onOnline, onOffline, onVisible, onPageHide, onPageShow, handleCoordMessage, derivePhase, TOKEN_KEY, LOCK_NAME },
    };
  }

  /* ── Shared call screen (incoming / in-call / ended) ────────────────────
     Mounted on EVERY PWA page so a call is presented wherever the user is,
     full-screen like a native phone: big green Answer / red Decline, and a
     large red End Call that is always visible during a call (keypad open
     or not). Pure view: renders engine snapshots, forwards taps. */
  const ICON = {
    phone: '<svg viewBox="0 0 24 24" width="32" height="32" fill="currentColor" aria-hidden="true"><path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.98.98 0 0 0-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.11-.56-2.3-.56-3.53 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/></svg>',
    hangup: '<svg viewBox="0 0 24 24" width="34" height="34" fill="currentColor" aria-hidden="true"><path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08a.956.956 0 0 1-.29-.7c0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.11-.7-.28a11.27 11.27 0 0 0-2.67-1.85.996.996 0 0 1-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/></svg>',
    mic: '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/></svg>',
    micOff: '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="1" y1="1" x2="23" y2="23"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/><path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2c0 .76-.12 1.5-.35 2.18"/><line x1="12" y1="19" x2="12" y2="23"/></svg>',
    keypad: '<svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor" aria-hidden="true"><circle cx="6" cy="5" r="1.8"/><circle cx="12" cy="5" r="1.8"/><circle cx="18" cy="5" r="1.8"/><circle cx="6" cy="11" r="1.8"/><circle cx="12" cy="11" r="1.8"/><circle cx="18" cy="11" r="1.8"/><circle cx="6" cy="17" r="1.8"/><circle cx="12" cy="17" r="1.8"/><circle cx="18" cy="17" r="1.8"/><circle cx="12" cy="22" r="1.5"/></svg>',
    speaker: '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>',
  };
  const fmtDuration = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
  function fmtPhone(n) {
    const d = String(n || '').replace(/\D/g, '');
    if (d.length === 11 && d[0] === '1') return `(${d.slice(1, 4)}) ${d.slice(4, 7)}-${d.slice(7)}`;
    if (d.length === 10) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
    return String(n || '');
  }
  function initials(name) {
    const parts = String(name || '').trim().split(/\s+/).filter((w) => /[A-Za-z]/.test(w[0] || ''));
    return parts.length ? (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase() : '';
  }
  const MIC_HELP = {
    ios: ['On your iPhone or iPad, open <b>Settings → Apps → Safari → Microphone</b> and choose <b>Ask</b> or <b>Allow</b>.',
      'In Safari you can also tap <b>aA → Website Settings → Microphone → Allow</b> for this site.',
      'Then close LUX Connect completely (swipe it away) and open it again.'],
    android: ['Long-press the <b>LUX Connect</b> icon → <b>App info → Permissions → Microphone → Allow</b>.',
      'In Chrome: <b>⋮ → Settings → Site settings → Microphone</b> → allow this site.',
      'Then come back here and tap <b>Try again</b>.'],
    desktop: ['Click the <b>lock / site-settings icon</b> at the left of the address bar.',
      'Set <b>Microphone</b> to <b>Allow</b>, then tap <b>Try again</b> (or reload the page).'],
  };

  function mountOverlay(engine, o) {
    o = Object.assign({ statusChip: true }, o || {});
    const doc = document;
    if (doc.getElementById('luxVoiceOverlay')) return;
    const style = doc.createElement('style');
    style.textContent = `
#luxVoiceOverlay{position:fixed;inset:0;z-index:1000;display:flex;flex-direction:column;align-items:center;font:inherit;color:#f8fafc;
  background:radial-gradient(120% 70% at 50% 0%,color-mix(in srgb,var(--pwa-primary,#2563eb) 35%,#0b1020) 0%,#05070d 70%);
  padding:max(18px,env(safe-area-inset-top)) 20px max(22px,env(safe-area-inset-bottom));overscroll-behavior:contain;touch-action:manipulation}
#luxVoiceOverlay[hidden],#luxVoiceOverlay [hidden],#luxVoiceNotice[hidden],#luxVoiceChip[hidden]{display:none!important}
#luxVoiceOverlay,#luxVoiceOverlay *{-webkit-tap-highlight-color:transparent}
#luxVoiceOverlay[data-keypad="1"] .lv-brand{display:none}
#luxVoiceOverlay[data-keypad="1"] .lv-ctl .lv-ico{width:50px;height:50px}
#luxVoiceOverlay[data-keypad="1"] .lv-ctl{font-size:.74rem;gap:4px}
#luxVoiceOverlay[data-keypad="1"] .lv-dtmf{min-height:1.3em;margin-bottom:2px}
#luxVoiceOverlay .lv-wrap{width:100%;max-width:420px;flex:1;display:flex;flex-direction:column;min-height:0}
#luxVoiceOverlay .lv-main{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column}
#luxVoiceOverlay .lv-foot{flex:none;padding-top:14px}
#luxVoiceOverlay[data-keypad="1"] .lv-avatar{display:none}
#luxVoiceOverlay[data-keypad="1"] .lv-who{margin-top:10px}
#luxVoiceOverlay[data-keypad="1"] h2{font-size:1.3rem}
#luxVoiceOverlay[data-keypad="1"] .lv-state{margin-top:6px}
#luxVoiceOverlay .lv-brand{font-size:.78rem;letter-spacing:.08em;text-transform:uppercase;opacity:.6;text-align:center}
#luxVoiceOverlay .lv-who{text-align:center;margin-top:clamp(18px,7vh,64px)}
#luxVoiceOverlay .lv-avatar{width:104px;height:104px;border-radius:50%;margin:0 auto 18px;display:grid;place-items:center;font-size:2.2rem;font-weight:600;
  background:color-mix(in srgb,var(--pwa-primary,#2563eb) 45%,#1e293b);color:#fff;position:relative}
#luxVoiceOverlay[data-screen="incoming"] .lv-avatar::after{content:"";position:absolute;inset:-10px;border-radius:50%;border:2px solid color-mix(in srgb,#22c55e 70%,transparent);animation:lvPulse 1.4s ease-out infinite}
@keyframes lvPulse{from{transform:scale(.9);opacity:1}to{transform:scale(1.35);opacity:0}}
@media (prefers-reduced-motion:reduce){#luxVoiceOverlay .lv-avatar::after{animation:none}}
#luxVoiceOverlay h2{margin:0;font-size:clamp(1.6rem,7vw,2.1rem);font-weight:600;overflow-wrap:anywhere}
#luxVoiceOverlay .lv-num{margin-top:6px;font-size:1.05rem;opacity:.75;font-variant-numeric:tabular-nums}
#luxVoiceOverlay .lv-state{margin-top:14px;font-size:1.05rem;font-variant-numeric:tabular-nums;min-height:1.4em}
#luxVoiceOverlay .lv-state[data-tone="warn"]{color:#fbbf24}
#luxVoiceOverlay .lv-state[data-tone="ok"]{color:#86efac}
#luxVoiceOverlay .lv-hint{margin:14px auto 0;max-width:320px;text-align:center;font-size:.85rem;color:#fbbf24}
#luxVoiceOverlay .lv-spacer{flex:1;min-height:16px}
#luxVoiceOverlay .lv-pair{display:flex;justify-content:space-between;padding:0 clamp(8px,6vw,36px)}
#luxVoiceOverlay .lv-act{display:flex;flex-direction:column;align-items:center;gap:10px;font-size:.95rem}
#luxVoiceOverlay .lv-round{width:80px;height:80px;border-radius:50%;border:0;display:grid;place-items:center;color:#fff;cursor:pointer;touch-action:manipulation;
  transition:transform .08s,filter .12s}
#luxVoiceOverlay .lv-round:active{transform:scale(.93);filter:brightness(.9)}
#luxVoiceOverlay .lv-round:focus-visible,#luxVoiceOverlay .lv-ctl:focus-visible,#luxVoiceOverlay .lv-text:focus-visible,#luxVoiceOverlay .lv-key:focus-visible{outline:3px solid #fff;outline-offset:3px}
#luxVoiceOverlay .lv-round[disabled]{opacity:.6;cursor:default}
#luxVoiceOverlay .lv-green{background:#16a34a;box-shadow:0 10px 30px rgba(22,163,74,.45)}
#luxVoiceOverlay .lv-red{background:#dc2626;box-shadow:0 10px 30px rgba(220,38,38,.45)}
#luxVoiceOverlay .lv-text{display:block;margin:22px auto 0;background:none;border:0;color:#cbd5e1;font:inherit;font-size:.92rem;padding:10px 14px;cursor:pointer;text-decoration:underline;text-underline-offset:3px}
#luxVoiceOverlay .lv-grid{display:grid;grid-template-columns:repeat(var(--lv-cols,3),1fr);gap:14px;justify-items:center;margin-bottom:8px}
#luxVoiceOverlay .lv-ctl{width:76px;display:flex;flex-direction:column;align-items:center;gap:8px;background:none;border:0;color:#f8fafc;font:inherit;font-size:.82rem;cursor:pointer;padding:0}
#luxVoiceOverlay .lv-ctl .lv-ico{width:68px;height:68px;border-radius:50%;display:grid;place-items:center;background:rgba(255,255,255,.14)}
#luxVoiceOverlay .lv-ctl[aria-pressed="true"] .lv-ico{background:#f8fafc;color:#0b1020}
#luxVoiceOverlay .lv-keypad{display:grid;grid-template-columns:repeat(3,1fr);gap:clamp(6px,1.4vh,12px);justify-items:center;margin-bottom:10px}
#luxVoiceOverlay .lv-key{width:clamp(50px,8.4vh,68px);height:clamp(50px,8.4vh,68px);border-radius:50%;border:0;background:rgba(255,255,255,.14);color:#fff;font:inherit;font-size:1.6rem;cursor:pointer;touch-action:manipulation}
#luxVoiceOverlay .lv-key:active{background:rgba(255,255,255,.3)}
#luxVoiceOverlay .lv-dtmf{text-align:center;min-height:1.6em;font-size:1.3rem;letter-spacing:.12em;margin-bottom:6px;font-variant-numeric:tabular-nums}
#luxVoiceOverlay .lv-end{display:flex;flex-direction:column;align-items:center;gap:8px;font-size:.95rem}
#luxVoiceOverlay .lv-outputs{margin:0 auto 18px;max-width:300px;background:rgba(15,23,42,.95);border:1px solid rgba(255,255,255,.15);border-radius:16px;padding:6px}
#luxVoiceOverlay .lv-outputs button{display:block;width:100%;text-align:left;background:none;border:0;color:#f8fafc;font:inherit;padding:11px 12px;border-radius:10px;cursor:pointer}
#luxVoiceOverlay .lv-outputs button[aria-checked="true"]{background:rgba(255,255,255,.12);font-weight:600}
#luxVoiceNotice{position:fixed;left:12px;right:12px;top:calc(10px + env(safe-area-inset-top));max-width:440px;margin:0 auto;z-index:1001;
  background:var(--pwa-surface,#0b0d14);color:var(--pwa-text,#f8fafc);border:1px solid var(--pwa-border,#273044);border-radius:18px;padding:16px;box-shadow:0 20px 60px rgba(0,0,0,.55);font-size:.92rem;line-height:1.4}
#luxVoiceNotice h3{margin:0 0 8px;font-size:1.05rem}
#luxVoiceNotice ol{margin:8px 0 12px;padding-left:20px}
#luxVoiceNotice li{margin:4px 0}
#luxVoiceNotice .lv-nbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:10px}
#luxVoiceNotice button{border:1px solid var(--pwa-border,#273044);background:var(--pwa-surface-2,#0f172a);color:inherit;border-radius:11px;padding:9px 14px;font:inherit;cursor:pointer;min-height:40px}
#luxVoiceNotice button.lv-primary{background:var(--pwa-primary,#2563eb);border-color:var(--pwa-primary,#2563eb);color:#fff}
#luxVoiceChip{position:fixed;top:calc(8px + env(safe-area-inset-top));left:50%;transform:translateX(-50%);z-index:119;background:var(--pwa-surface,#0b0d14);border:1px solid var(--pwa-border,#273044);color:var(--pwa-muted,#94a3b8);border-radius:999px;padding:5px 12px;font-size:.75rem;box-shadow:0 6px 18px rgba(0,0,0,.35);text-decoration:none}
`;
    doc.head.appendChild(style);

    const root = doc.createElement('div');
    root.id = 'luxVoiceOverlay';
    root.hidden = true;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', 'lvName');
    const keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#'];
    root.innerHTML = `<div class="lv-wrap"><div class="lv-main">
<div class="lv-brand">LUX Connect</div>
<div class="lv-who"><div class="lv-avatar" data-lv="avatar" aria-hidden="true"></div><h2 id="lvName" data-lv="name"></h2><div class="lv-num" data-lv="num"></div>
<div class="lv-state" data-lv="state" role="status" aria-live="assertive"></div></div>
<div class="lv-hint" data-lv="hint" hidden></div>
<div class="lv-spacer"></div>
<div data-lv="incall" hidden>
  <div class="lv-outputs" data-lv="outputs" role="menu" aria-label="Audio output" hidden></div>
  <div class="lv-dtmf" data-lv="dtmf" aria-live="off" hidden></div>
  <div class="lv-keypad" data-lv="keypad" role="group" aria-label="Keypad" hidden>${keys.map((k) => `<button type="button" class="lv-key" data-lv-digit="${k}" aria-label="${k}">${k}</button>`).join('')}</div>
  <div class="lv-grid" data-lv="controls">
    <button type="button" class="lv-ctl" data-lv-act="mute" aria-pressed="false"><span class="lv-ico" data-lv="muteIcon">${ICON.mic}</span><span data-lv="muteLabel">Mute</span></button>
    <button type="button" class="lv-ctl" data-lv-act="keypad" aria-pressed="false"><span class="lv-ico">${ICON.keypad}</span><span data-lv="keypadLabel">Keypad</span></button>
    <button type="button" class="lv-ctl" data-lv-act="audio" aria-haspopup="menu" aria-pressed="false" hidden><span class="lv-ico">${ICON.speaker}</span><span>Audio</span></button>
  </div>
</div>
</div>
<div class="lv-foot">
<div data-lv="incoming">
  <div class="lv-pair">
    <div class="lv-act"><button type="button" class="lv-round lv-red" data-lv-act="decline" aria-label="Decline call">${ICON.hangup}</button><span aria-hidden="true">Decline</span></div>
    <div class="lv-act"><button type="button" class="lv-round lv-green" data-lv-act="accept" aria-label="Answer call">${ICON.phone}</button><span aria-hidden="true">Answer</span></div>
  </div>
  <button type="button" class="lv-text" data-lv-act="voicemail">Send to voicemail</button>
</div>
<div class="lv-end" data-lv="endrow" hidden><button type="button" class="lv-round lv-red" data-lv-act="hangup" aria-label="End call">${ICON.hangup}</button><span aria-hidden="true">End</span></div>
</div>
</div>`;
    doc.body.appendChild(root);

    const notice = doc.createElement('div');
    notice.id = 'luxVoiceNotice'; notice.hidden = true; notice.setAttribute('role', 'alertdialog'); notice.setAttribute('aria-live', 'assertive');
    doc.body.appendChild(notice);
    const chip = doc.createElement('a');
    chip.id = 'luxVoiceChip'; chip.hidden = true; chip.setAttribute('role', 'status'); chip.href = '/app/phone';
    doc.body.appendChild(chip);

    const q = (k) => root.querySelector(`[data-lv="${k}"]`);
    const view = { keypad: false, outputs: false, dtmf: '', timer: null, startedWall: 0, screen: '', lastSnap: null, noticeKey: '' };

    root.addEventListener('click', async (e) => {
      const digit = e.target.closest && e.target.closest('[data-lv-digit]');
      if (digit) { const d = digit.dataset.lvDigit; engine.sendDigits(d); view.dtmf = (view.dtmf + d).slice(-18); q('dtmf').textContent = view.dtmf; return; }
      const act = e.target.closest && e.target.closest('[data-lv-act]');
      if (!act) return;
      const a = act.dataset.lvAct;
      if (a === 'accept') { act.disabled = true; await engine.accept(); act.disabled = false; }
      else if (a === 'decline') engine.decline();
      else if (a === 'voicemail') engine.sendToVoicemail();
      else if (a === 'hangup') engine.hangup();
      else if (a === 'mute') engine.toggleMute();
      else if (a === 'keypad') { view.keypad = !view.keypad; view.outputs = false; render(view.lastSnap); }
      else if (a === 'audio') { view.outputs = !view.outputs; render(view.lastSnap); }
      else if (a === 'output') { await engine.setAudioOutput(act.dataset.id); view.outputs = false; render(engine.snapshot()); }
    });
    notice.addEventListener('click', async (e) => {
      const b = e.target.closest && e.target.closest('[data-lv-n]');
      if (!b) return;
      if (b.dataset.lvN === 'close') engine.dismissNotice();
      else if (b.dataset.lvN === 'mic') { b.disabled = true; await engine.requestMicrophone(); b.disabled = false; }
    });

    function stopTimer() { if (view.timer) { clearInterval(view.timer); view.timer = null; } }
    function tick() { q('state').textContent = stateText(view.lastSnap); }
    function stateText(s) {
      if (!s) return '';
      if (s.phase === 'incoming') return 'Incoming call';
      if (s.phase === 'connecting') return s.call && s.call.direction === 'outbound' ? (s.remoteRinging ? 'Ringing…' : 'Calling…') : 'Connecting…';
      if (s.phase === 'connected') {
        const dur = fmtDuration(Math.max(0, (Date.now() - view.startedWall) / 1000));
        if (s.callNetwork === 'reconnecting') return `Reconnecting… · ${dur}`;
        return `${s.callNetwork === 'poor' ? 'Poor connection · ' : ''}${s.muted ? 'Muted · ' : ''}${dur}`;
      }
      if (s.phase === 'ended') {
        const e = s.ended || {};
        if (e.answered) return `Call ended · ${fmtDuration(e.durationSec || 0)}`;
        return { decline: 'Declined', voicemail: 'Sent to voicemail', cancel: 'Missed call', hangup: 'Call cancelled', 'accept-failed': 'Could not answer' }[e.why] || 'Call ended';
      }
      return '';
    }

    function renderNotice(s) {
      const n = s.notice;
      const key = n ? `${n.kind}:${n.at}` : '';
      if (key === view.noticeKey) return;
      view.noticeKey = key;
      if (!n) { notice.hidden = true; notice.replaceChildren(); return; }
      if (n.kind === 'mic-blocked') {
        const steps = MIC_HELP[(s.platform && s.platform.os) || 'desktop'] || MIC_HELP.desktop;
        const why = n.context === 'answer' ? 'The call went to voicemail because LUX Connect could not use your microphone.' : n.context === 'call' ? 'LUX Connect needs your microphone to place calls.' : 'LUX Connect needs your microphone to answer and place calls.';
        notice.innerHTML = `<h3>Microphone is blocked</h3><div>${why} Incoming calls will still ring.</div><ol>${steps.map((t) => `<li>${t}</li>`).join('')}</ol><div class="lv-nbtns"><button type="button" data-lv-n="close">Close</button><button type="button" class="lv-primary" data-lv-n="mic">Try again</button></div>`;
      } else {
        const text = n.kind === 'missed-busy' ? `Missed call from ${n.name || fmtPhone(n.from) || 'unknown caller'} — you were on another call. They were sent to voicemail.` : (n.message || 'Something went wrong with the call.');
        notice.innerHTML = '<div data-lv="ntext"></div><div class="lv-nbtns"><button type="button" data-lv-n="close">OK</button></div>';
        notice.querySelector('[data-lv="ntext"]').textContent = text;
      }
      notice.hidden = false;
    }

    function render(s) {
      if (!s) return;
      view.lastSnap = s;
      const screen = s.phase === 'incoming' ? 'incoming' : (s.phase === 'connecting' || s.phase === 'connected') ? 'call' : (s.phase === 'ended' && view.screen) ? 'ended' : '';
      if (screen !== view.screen) {
        if (screen !== 'call') { view.keypad = false; view.outputs = false; view.dtmf = ''; }
        const wasHidden = !view.screen;
        view.screen = screen;
        root.hidden = !screen;
        root.dataset.screen = screen;
        doc.documentElement.style.overflow = screen ? 'hidden' : '';
        if (screen === 'incoming') setTimeout(() => { const b = root.querySelector('[data-lv-act="accept"]'); b && b.focus({ preventScroll: true }); }, 0);
        else if (screen === 'call' && wasHidden) setTimeout(() => { const b = root.querySelector('[data-lv-act="hangup"]'); b && b.focus({ preventScroll: true }); }, 0);
      }
      if (screen) {
        const c = s.call || (s.ended ? { name: s.ended.name, from: s.ended.from } : {});
        const name = c.name || fmtPhone(c.from) || 'Unknown caller';
        q('name').textContent = name;
        q('num').textContent = c.name ? fmtPhone(c.from) : '';
        const ini = initials(c.name);
        if (ini) q('avatar').textContent = ini; else q('avatar').innerHTML = ICON.phone;
        q('incoming').hidden = screen !== 'incoming';
        q('incall').hidden = screen !== 'call';
        q('endrow').hidden = screen !== 'call';
        root.dataset.keypad = screen === 'call' && view.keypad ? '1' : '';
        const st = q('state');
        st.dataset.tone = s.phase === 'connected' ? (s.callNetwork === 'ok' ? 'ok' : 'warn') : '';
        if (s.phase === 'connected') {
          const wall = Date.now() - Math.max(0, (s.now || 0) - (s.callStartedAt || 0));
          if (!view.timer || Math.abs(wall - view.startedWall) > 1500) view.startedWall = wall;
          if (!view.timer) view.timer = setInterval(tick, 1000);
        } else stopTimer();
        tick();
        // in-call controls
        const mute = root.querySelector('[data-lv-act="mute"]');
        mute.setAttribute('aria-pressed', String(!!s.muted));
        q('muteLabel').textContent = s.muted ? 'Unmute' : 'Mute';
        q('muteIcon').innerHTML = s.muted ? ICON.micOff : ICON.mic;
        mute.disabled = s.phase !== 'connected';
        const audioBtn = root.querySelector('[data-lv-act="audio"]');
        audioBtn.hidden = !s.outputSelectable;
        audioBtn.setAttribute('aria-pressed', String(view.outputs));
        q('controls').style.setProperty('--lv-cols', s.outputSelectable ? '3' : '2');
        root.querySelector('[data-lv-act="keypad"]').setAttribute('aria-pressed', String(view.keypad));
        q('keypadLabel').textContent = view.keypad ? 'Hide' : 'Keypad';
        q('keypad').hidden = !view.keypad;
        q('dtmf').hidden = !view.keypad;
        const outs = q('outputs');
        outs.hidden = !view.outputs;
        if (view.outputs) {
          outs.replaceChildren(...engine.audioOutputs().map((d) => {
            const b = doc.createElement('button'); b.type = 'button'; b.setAttribute('role', 'menuitemradio');
            b.dataset.lvAct = 'output'; b.dataset.id = d.id; b.setAttribute('aria-checked', String(d.selected)); b.textContent = d.label; return b;
          }));
        }
        const hint = q('hint');
        const hintText = screen === 'incoming' && s.audioLocked ? 'Ringtone muted by the browser — tap the screen once on this page so future calls ring.'
          : screen === 'incoming' && s.mic === 'denied' ? 'Microphone is blocked — answering will fail. See the steps after this call.' : '';
        hint.textContent = hintText; hint.hidden = !hintText;
      } else stopTimer();
      renderNotice(s);
      if (o.statusChip) {
        let text = '';
        if (s.callPhase === 'idle' && s.receiveCalls && s.availability !== 'away') {
          if (s.phase === 'reconnecting') text = s.offline ? 'Phone offline — reconnects automatically' : 'Phone reconnecting…';
          else if (s.phase === 'error') text = 'Phone unavailable — tap for details';
          else if (s.phase === 'ready' && s.mic === 'denied') text = 'Microphone blocked — tap to fix';
          else if (s.phase === 'ready' && s.audioLocked) text = 'Tap anywhere to enable the ringtone';
        }
        chip.textContent = text;
        chip.hidden = !text;
      }
    }
    engine.subscribe(render);
  }

  return { create, mountOverlay, jwtExpMs, constants: { LOCK_NAME, BUS_KEY, STATUS_KEY, TOKEN_KEY, RINGTONE_URL, MAX_AUTO_ATTEMPTS, RECONNECT_GRACE_MS, SLOW_RETRY_MS, ENDED_HOLD_MS, TOKEN_ERROR_CODES, PHASES } };
});
