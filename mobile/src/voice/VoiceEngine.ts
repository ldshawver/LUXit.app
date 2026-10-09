import { backoffDelay } from '../lib/backoff';
import { Observable } from '../lib/emitter';
import { toE164 } from '../lib/phone';
import type { VoiceToken } from '../api/types';
import type { SdkAudioDevice, SdkCall, SdkCallInvite, SdkError, VoiceSdk } from './sdk';

/**
 * One state machine for calling on this phone.
 *
 * Registration on native is a *push binding*: once Twilio has this handset's
 * PushKit/FCM token, calls arrive even when the app is closed. So "Ready"
 * means: the server issued a token with incoming enabled, Voice.register()
 * succeeded, and the phone has a network. Nothing is persisted as "ready";
 * every launch and every return to the foreground re-proves it.
 *
 * Recovery is automatic and bounded: failures retry with exponential backoff
 * (capped at 5 minutes, never giving up while the app is alive); a policy
 * refusal from the server (calls turned off, Away, no number, device awaiting
 * approval) is not retried in a loop, only re-checked on resume / settings
 * change / every 30 minutes. There is no manual "Connect" step.
 */

export type Phase =
  | 'signed_out'
  | 'starting'
  | 'registering'
  | 'ready'
  | 'offline'
  | 'reconnecting'
  | 'unavailable'
  | 'incoming'
  | 'connecting'
  | 'ringing'
  | 'connected'
  | 'call_reconnecting'
  | 'ended';

export type Registration = 'idle' | 'registering' | 'registered' | 'failed' | 'blocked';

export interface InviteView {
  id: string;
  from: string;
  name: string;
  callLogId: string | null;
}

export interface CallView {
  id: string;
  sid: string | null;
  direction: 'inbound' | 'outbound';
  remote: string;
  name: string;
  state: 'connecting' | 'ringing' | 'connected' | 'reconnecting';
  muted: boolean;
  startedAt: number | null;
  qualityWarnings: string[];
  callLogId: string | null;
}

export type EndReason = 'completed' | 'declined' | 'cancelled' | 'failed' | 'busy_rejected';

export interface EndedView {
  reason: EndReason;
  direction: 'inbound' | 'outbound';
  remote: string;
  name: string;
  durationSec: number;
  answered: boolean;
  message: string | null;
}

export interface Notice {
  kind: 'missed_while_busy' | 'call_failed' | 'mic_denied';
  text: string;
  at: number;
}

export interface VoiceSnapshot {
  phase: Phase;
  registration: Registration;
  blockReason: string | null;
  blockMessage: string | null;
  online: boolean;
  attempts: number;
  nextRetryAt: number | null;
  lastRegisteredAt: number | null;
  incomingEnabled: boolean;
  outgoingEnabled: boolean;
  invite: InviteView | null;
  call: CallView | null;
  ended: EndedView | null;
  notice: Notice | null;
  lastError: string | null;
}

export interface VoiceApi {
  voiceToken(): Promise<VoiceToken>;
  reportRegistration(registered: boolean, detail: { voiceDeviceToken?: string; error?: string }): Promise<void>;
  markCallAccepted(callLogId: string): Promise<void>;
}

/** Error carrying the server's refusal code (HTTP 403 policy denials). */
export interface PolicyDenial {
  status: number;
  code: string;
  message: string;
}

export interface EngineDeps {
  sdk: VoiceSdk;
  api: VoiceApi;
  platform: 'ios' | 'android';
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
  random?: () => number;
  /** Classify an error thrown by api.voiceToken(). */
  classify?: (e: unknown) => 'policy' | 'auth' | 'transient';
  log?: (msg: string, extra?: Record<string, unknown>) => void;
}

export const REREGISTER_AFTER_MS = 6 * 60 * 60 * 1000;
export const POLICY_RECHECK_MS = 30 * 60 * 1000;
export const ENDED_HOLD_MS = 1800;
export const RETRY_CAP_MS = 5 * 60 * 1000;
const TOKEN_REUSE_MARGIN_MS = 5 * 60 * 1000;

function errMessage(e: unknown): string {
  if (!e) return 'Unknown error';
  const anyE = e as SdkError & { code?: number | string };
  return anyE.code ? `${anyE.message} (${anyE.code})` : String(anyE.message ?? e);
}

export class VoiceEngine extends Observable<VoiceSnapshot> {
  private sdk: VoiceSdk;
  private api: VoiceApi;
  private platform: 'ios' | 'android';
  private now: () => number;
  private setTimer: (fn: () => void, ms: number) => unknown;
  private clearTimer: (h: unknown) => void;
  private random: () => number;
  private classify: (e: unknown) => 'policy' | 'auth' | 'transient';
  private log: (msg: string, extra?: Record<string, unknown>) => void;

  private signedIn = false;
  private started = false;
  private registration: Registration = 'idle';
  private blockReason: string | null = null;
  private blockMessage: string | null = null;
  private online = true;
  private attempts = 0;
  private nextRetryAt: number | null = null;
  private lastRegisteredAt: number | null = null;
  private incomingEnabled = false;
  private outgoingEnabled = false;
  private lastError: string | null = null;
  private registering: Promise<void> | null = null;
  private retryTimer: unknown = null;
  private endedTimer: unknown = null;
  private cachedToken: { value: VoiceToken; expiresAt: number } | null = null;

  private inviteObj: SdkCallInvite | null = null;
  private invite: InviteView | null = null;
  private seenInvites = new Set<string>();
  private accepting = false;
  private callObj: SdkCall | null = null;
  private call: CallView | null = null;
  private boundCalls = new WeakSet<SdkCall>();
  private finishedCalls = new WeakSet<SdkCall>();
  private hangupRequested = false;
  /** Invites the user hung up on while the answer was still connecting. */
  private abandonedInvites = new Set<string>();
  private ended: EndedView | null = null;
  private notice: Notice | null = null;
  private seq = 0;

  /** Counters for diagnostics and tests. */
  readonly stats = { registrations: 0, tokenFetches: 0, sdkListeners: 0, accepts: 0, connects: 0 };

  constructor(deps: EngineDeps) {
    super();
    this.sdk = deps.sdk;
    this.api = deps.api;
    this.platform = deps.platform;
    this.now = deps.now ?? (() => Date.now());
    this.setTimer = deps.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = deps.clearTimer ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
    this.random = deps.random ?? Math.random;
    this.classify = deps.classify ?? (() => 'transient');
    this.log = deps.log ?? (() => undefined);
    // SDK listeners are attached exactly once for the life of the engine.
    this.sdk.on('callInvite', (invite: SdkCallInvite) => this.onCallInvite(invite));
    this.sdk.on('registered', () => this.onSdkRegistered());
    this.sdk.on('unregistered', () => this.onSdkUnregistered());
    this.sdk.on('error', (e: SdkError) => this.onSdkError(e));
    this.stats.sdkListeners += 4;
  }

  // ── snapshot ──────────────────────────────────────────────────────────── //

  snapshot(): VoiceSnapshot {
    return {
      phase: this.derivePhase(),
      registration: this.registration,
      blockReason: this.blockReason,
      blockMessage: this.blockMessage,
      online: this.online,
      attempts: this.attempts,
      nextRetryAt: this.nextRetryAt,
      lastRegisteredAt: this.lastRegisteredAt,
      incomingEnabled: this.incomingEnabled,
      outgoingEnabled: this.outgoingEnabled,
      invite: this.invite,
      call: this.call,
      ended: this.ended,
      notice: this.notice,
      lastError: this.lastError,
    };
  }

  private derivePhase(): Phase {
    if (this.call) {
      if (this.call.state === 'connected') return 'connected';
      if (this.call.state === 'reconnecting') return 'call_reconnecting';
      if (this.call.state === 'ringing') return 'ringing';
      return 'connecting';
    }
    if (this.invite) return 'incoming';
    if (this.ended) return 'ended';
    if (!this.signedIn) return 'signed_out';
    if (!this.online) return 'offline';
    switch (this.registration) {
      case 'registered':
        return 'ready';
      case 'registering':
        return this.lastRegisteredAt ? 'reconnecting' : 'registering';
      case 'failed':
        return 'reconnecting';
      case 'blocked':
        return 'unavailable';
      default:
        return 'starting';
    }
  }

  private changed(): void {
    this.emit(this.snapshot());
  }

  // ── lifecycle ─────────────────────────────────────────────────────────── //

  /** Call after sign-in and on every cold start while signed in. Idempotent. */
  async start(): Promise<void> {
    this.signedIn = true;
    if (!this.started) {
      this.started = true;
      if (this.platform === 'ios' && this.sdk.initializePushRegistry) {
        try {
          await this.sdk.initializePushRegistry();
        } catch (e) {
          this.log('initializePushRegistry failed', { error: errMessage(e) });
        }
      }
    }
    this.changed();
    await this.adoptExisting();
    await this.ensureRegistered('start');
  }

  /** App returned to the foreground. */
  async onForeground(): Promise<void> {
    if (!this.signedIn) return;
    await this.adoptExisting();
    const stale = !this.lastRegisteredAt || this.now() - this.lastRegisteredAt > REREGISTER_AFTER_MS;
    if (this.registration !== 'registered' || stale) {
      this.cancelRetry();
      await this.ensureRegistered('foreground');
    }
  }

  onNetwork(online: boolean): void {
    if (online === this.online) return;
    this.online = online;
    this.changed();
    if (online && this.signedIn && this.registration !== 'registered' && this.registration !== 'registering') {
      this.cancelRetry();
      void this.ensureRegistered('network');
    }
  }

  /** Settings that affect eligibility changed (Receive Calls, Available, push token). */
  async refresh(): Promise<void> {
    if (!this.signedIn) return;
    this.cancelRetry();
    this.cachedToken = null;
    await this.ensureRegistered('refresh');
  }

  /** Unregister this handset from Twilio and stop all retries. Server sign-out is separate. */
  async signOut(): Promise<void> {
    this.cancelRetry();
    const token = this.cachedToken && this.cachedToken.expiresAt > this.now() ? this.cachedToken.value.token : null;
    this.signedIn = false;
    if (token && this.registration === 'registered') {
      try {
        await this.sdk.unregister(token);
      } catch (e) {
        this.log('unregister failed', { error: errMessage(e) });
      }
    }
    this.registration = 'idle';
    this.cachedToken = null;
    this.lastRegisteredAt = null;
    this.attempts = 0;
    this.changed();
  }

  // ── registration ──────────────────────────────────────────────────────── //

  ensureRegistered(reason: string): Promise<void> {
    if (!this.signedIn || !this.online) return Promise.resolve();
    if (this.registering) return this.registering;
    this.registering = this.register(reason).finally(() => {
      this.registering = null;
    });
    return this.registering;
  }

  private async register(reason: string): Promise<void> {
    this.registration = 'registering';
    this.nextRetryAt = null;
    this.changed();
    let tok: VoiceToken;
    try {
      tok = await this.fetchToken(true);
    } catch (e) {
      const kind = this.classify(e);
      if (kind === 'auth') {
        // ApiClient already signed the app out; nothing to retry.
        this.registration = 'idle';
        this.changed();
        return;
      }
      if (kind === 'policy') {
        const d = e as PolicyDenial;
        this.block(d.code, d.message);
        return;
      }
      this.fail(errMessage(e));
      return;
    }
    if (!this.signedIn) return;
    this.outgoingEnabled = tok.outbound_enabled;
    this.incomingEnabled = tok.incoming_enabled;
    if (!tok.incoming_enabled) {
      // Outbound still works; say plainly that this phone will not ring.
      this.block('PUSH_CREDENTIAL_NOT_CONFIGURED', 'Incoming calls to this phone are not set up on the server yet.');
      return;
    }
    try {
      await this.sdk.register(tok.token);
    } catch (e) {
      await this.reportQuietly(false, { error: errMessage(e) });
      this.fail(errMessage(e));
      return;
    }
    this.stats.registrations += 1;
    this.registration = 'registered';
    this.blockReason = null;
    this.blockMessage = null;
    this.lastError = null;
    this.attempts = 0;
    this.lastRegisteredAt = this.now();
    this.log('registered', { reason });
    this.changed();
    let voiceDeviceToken: string | undefined;
    try {
      voiceDeviceToken = await this.sdk.getDeviceToken();
    } catch {
      voiceDeviceToken = undefined;
    }
    await this.reportQuietly(true, { voiceDeviceToken });
  }

  private async reportQuietly(registered: boolean, detail: { voiceDeviceToken?: string; error?: string }) {
    try {
      await this.api.reportRegistration(registered, detail);
    } catch (e) {
      // The server learns on the next registration; ringing never depends on
      // this report succeeding *right now* except for the first time.
      this.log('reportRegistration failed', { error: errMessage(e) });
    }
  }

  private block(code: string, message: string): void {
    this.registration = 'blocked';
    this.blockReason = code;
    this.blockMessage = message;
    this.attempts = 0;
    this.scheduleRetry(POLICY_RECHECK_MS);
    this.changed();
  }

  private fail(message: string): void {
    this.registration = 'failed';
    this.lastError = message;
    const delay = backoffDelay(this.attempts, { baseMs: 1000, capMs: RETRY_CAP_MS, random: this.random });
    this.attempts += 1;
    this.scheduleRetry(delay);
    this.changed();
  }

  private scheduleRetry(ms: number): void {
    this.cancelRetry();
    this.nextRetryAt = this.now() + ms;
    this.retryTimer = this.setTimer(() => {
      this.retryTimer = null;
      this.nextRetryAt = null;
      void this.ensureRegistered('retry');
    }, ms);
  }

  private cancelRetry(): void {
    if (this.retryTimer !== null) this.clearTimer(this.retryTimer);
    this.retryTimer = null;
    this.nextRetryAt = null;
  }

  private async fetchToken(forceFresh: boolean): Promise<VoiceToken> {
    const c = this.cachedToken;
    if (!forceFresh && c && c.expiresAt - this.now() > TOKEN_REUSE_MARGIN_MS) return c.value;
    this.stats.tokenFetches += 1;
    const tok = await this.api.voiceToken();
    this.cachedToken = { value: tok, expiresAt: this.now() + tok.expires_in * 1000 };
    return tok;
  }

  private onSdkRegistered(): void {
    // register() resolves on the same event; nothing extra to do.
  }

  private onSdkUnregistered(): void {
    if (this.signedIn && this.registration === 'registered') {
      this.fail('Unregistered by the calling service');
    }
  }

  private onSdkError(e: SdkError): void {
    this.lastError = errMessage(e);
    this.log('sdk error', { error: this.lastError });
    if (this.registration === 'registered' && !this.call && !this.invite) {
      // A registration-level error (expired binding, revoked credential).
      this.fail(this.lastError);
    } else {
      this.changed();
    }
  }

  // ── incoming ──────────────────────────────────────────────────────────── //

  private onCallInvite(invite: SdkCallInvite): void {
    const id = invite.getCallSid();
    if (this.seenInvites.has(id)) return;
    this.seenInvites.add(id);
    const params = invite.getCustomParameters() ?? {};
    const from = params.from_number || invite.getFrom() || '';
    const rawName = params.caller_name || '';
    const name = rawName && rawName !== from ? rawName : '';
    if (this.call || this.invite) {
      // One call at a time. Rejecting this leg lets the caller reach another
      // ringing device or voicemail immediately instead of ringing out.
      invite.reject().catch(() => undefined);
      this.notice = { kind: 'missed_while_busy', text: `Missed call from ${name || from} while you were on a call`, at: this.now() };
      this.changed();
      return;
    }
    this.clearEnded();
    this.inviteObj = invite;
    this.invite = { id, from, name, callLogId: params.call_log_id || null };
    invite.on('accepted', (call: SdkCall) => {
      if (this.abandonedInvites.has(id)) {
        this.discard(call);
        return;
      }
      // Answered from CallKit / the Android notification while JS was alive.
      if (this.inviteObj === invite) this.takeInvite();
      this.adoptCall(call, 'inbound', { from, name, callLogId: params.call_log_id || null });
    });
    invite.on('rejected', () => {
      if (this.inviteObj === invite) this.finishInvite('declined');
    });
    invite.on('cancelled', () => {
      // The caller hung up, or another of the user's devices answered.
      if (this.inviteObj === invite) this.finishInvite('cancelled');
    });
    this.changed();
  }

  private takeInvite(): SdkCallInvite | null {
    const obj = this.inviteObj;
    this.inviteObj = null;
    this.invite = null;
    return obj;
  }

  private finishInvite(reason: EndReason): void {
    const inv = this.invite;
    this.takeInvite();
    if (inv) this.setEnded({ reason, direction: 'inbound', remote: inv.from, name: inv.name, durationSec: 0, answered: false, message: null });
    else this.changed();
  }

  /** Answer. Safe to call repeatedly: only the first call does anything. */
  async accept(): Promise<void> {
    if (!this.invite || !this.inviteObj || this.accepting) return;
    this.accepting = true;
    this.stats.accepts += 1;
    const view = this.invite;
    const invite = this.takeInvite()!;
    this.call = {
      id: `in-${view.id}`, sid: view.id, direction: 'inbound', remote: view.from, name: view.name,
      state: 'connecting', muted: false, startedAt: null, qualityWarnings: [], callLogId: view.callLogId,
    };
    this.changed();
    try {
      const call = await invite.accept();
      if (this.abandonedInvites.has(view.id)) {
        this.discard(call);
        return;
      }
      this.adoptCall(call, 'inbound', { from: view.from, name: view.name, callLogId: view.callLogId });
      if (view.callLogId) this.api.markCallAccepted(view.callLogId).catch(() => undefined);
    } catch (e) {
      this.call = null;
      this.notice = { kind: 'call_failed', text: `Couldn't answer: ${errMessage(e)}`, at: this.now() };
      this.setEnded({ reason: 'failed', direction: 'inbound', remote: view.from, name: view.name, durationSec: 0, answered: false, message: errMessage(e) });
    } finally {
      this.accepting = false;
    }
  }

  async decline(): Promise<void> {
    if (!this.invite || !this.inviteObj) return;
    const view = this.invite;
    const invite = this.takeInvite()!;
    this.setEnded({ reason: 'declined', direction: 'inbound', remote: view.from, name: view.name, durationSec: 0, answered: false, message: null });
    try {
      await invite.reject();
    } catch (e) {
      this.log('reject failed', { error: errMessage(e) });
    }
  }

  // ── outbound ──────────────────────────────────────────────────────────── //

  async placeCall(rawNumber: string, opts: { name?: string; phoneNumberId?: number } = {}): Promise<void> {
    if (this.call || this.invite) throw new Error('You are already on a call.');
    const to = toE164(rawNumber);
    if (!to) throw new Error('Enter a valid phone number.');
    if (!this.online) throw new Error('No internet connection.');
    this.clearEnded();
    this.hangupRequested = false;
    this.stats.connects += 1;
    const localId = `out-${++this.seq}`;
    this.call = {
      id: localId, sid: null, direction: 'outbound', remote: to, name: opts.name ?? '',
      state: 'connecting', muted: false, startedAt: null, qualityWarnings: [], callLogId: null,
    };
    this.changed();
    try {
      const tok = await this.fetchToken(false);
      if (!tok.outbound_enabled) throw new Error('Outgoing calls are not set up on the server yet.');
      if (this.hangupRequested) return;
      const params: Record<string, string> = { To: to };
      if (opts.phoneNumberId) params.phone_number_id = String(opts.phoneNumberId);
      const call = await this.sdk.connect(tok.token, { params, contactHandle: opts.name || to, notificationDisplayName: opts.name || to });
      if (this.hangupRequested || this.call?.id !== localId) {
        // The user pressed End before the connect resolved.
        this.finishedCalls.add(call);
        await call.disconnect().catch(() => undefined);
        return;
      }
      this.adoptCall(call, 'outbound', { from: to, name: opts.name ?? '', callLogId: null }, localId);
    } catch (e) {
      if (this.call?.id === localId) {
        this.call = null;
        this.notice = { kind: 'call_failed', text: errMessage(e), at: this.now() };
        this.setEnded({ reason: 'failed', direction: 'outbound', remote: to, name: opts.name ?? '', durationSec: 0, answered: false, message: errMessage(e) });
      }
    }
  }

  // ── active call ───────────────────────────────────────────────────────── //

  private adoptCall(call: SdkCall, direction: 'inbound' | 'outbound', meta: { from: string; name: string; callLogId: string | null }, localId?: string): void {
    if (this.finishedCalls.has(call)) return;
    const sid = call.getSid() ?? null;
    const sdkState = call.getState();
    const state: CallView['state'] = sdkState === 'connected' ? 'connected' : sdkState === 'ringing' ? 'ringing' : sdkState === 'reconnecting' ? 'reconnecting' : 'connecting';
    const startedTs = call.getInitialConnectedTimestamp();
    const prev = this.call;
    this.callObj = call;
    this.call = {
      id: localId ?? prev?.id ?? `call-${sid ?? ++this.seq}`,
      sid,
      direction,
      remote: meta.from || (direction === 'inbound' ? call.getFrom() : call.getTo()) || '',
      name: meta.name,
      state,
      muted: Boolean(call.isMuted()),
      startedAt: state === 'connected' ? (startedTs ? startedTs.getTime() : prev?.startedAt ?? this.now()) : null,
      qualityWarnings: [],
      callLogId: meta.callLogId,
    };
    if (!this.boundCalls.has(call)) {
      this.boundCalls.add(call);
      call.on('ringing', () => this.patchCall(call, { state: 'ringing' }));
      call.on('connected', () => this.patchCall(call, { state: 'connected', startedAt: call.getInitialConnectedTimestamp()?.getTime() ?? this.now() }));
      call.on('reconnecting', () => this.patchCall(call, { state: 'reconnecting' }));
      call.on('reconnected', () => this.patchCall(call, { state: 'connected' }));
      call.on('qualityWarningsChanged', (current: string[]) => this.patchCall(call, { qualityWarnings: [...(current ?? [])] }));
      call.on('disconnected', (e?: SdkError) => this.finishCall(call, e ? 'failed' : 'completed', e));
      call.on('connectFailure', (e?: SdkError) => this.finishCall(call, 'failed', e));
    }
    this.changed();
  }

  private patchCall(call: SdkCall, patch: Partial<CallView>): void {
    if (this.callObj !== call || !this.call) return;
    const next = { ...this.call, ...patch };
    if (patch.state === 'connected' && this.call.startedAt) next.startedAt = this.call.startedAt;
    this.call = next;
    this.changed();
  }

  private finishCall(call: SdkCall, reason: EndReason, e?: SdkError): void {
    if (this.finishedCalls.has(call)) return;
    this.finishedCalls.add(call);
    if (this.callObj !== call || !this.call) return;
    const c = this.call;
    this.callObj = null;
    this.call = null;
    this.setEnded({
      reason, direction: c.direction, remote: c.remote, name: c.name,
      durationSec: c.startedAt ? Math.round((this.now() - c.startedAt) / 1000) : 0,
      answered: Boolean(c.startedAt), message: e ? errMessage(e) : null,
    });
  }

  /** End whatever is happening. Idempotent; the UI updates immediately. */
  async hangup(): Promise<void> {
    if (this.invite && !this.call) return this.decline();
    if (!this.call) return;
    const call = this.callObj;
    if (!call) {
      // Outbound still dialing (connect() not resolved yet).
      this.hangupRequested = true;
      const c = this.call;
      if (c.direction === 'inbound' && c.sid) this.abandonedInvites.add(c.sid);
      this.call = null;
      this.setEnded({ reason: 'cancelled', direction: c.direction, remote: c.remote, name: c.name, durationSec: 0, answered: false, message: null });
      return;
    }
    this.finishCall(call, 'completed');
    try {
      await call.disconnect();
    } catch (e) {
      this.log('disconnect failed', { error: errMessage(e) });
    }
  }

  private discard(call: SdkCall): void {
    if (this.finishedCalls.has(call)) return;
    this.finishedCalls.add(call);
    call.disconnect().catch(() => undefined);
  }

  async setMuted(muted: boolean): Promise<void> {
    if (!this.callObj || !this.call) return;
    const result = await this.callObj.mute(muted);
    this.patchCall(this.callObj, { muted: Boolean(result) });
  }

  async sendDigits(digits: string): Promise<void> {
    if (!this.callObj || this.call?.state !== 'connected') return;
    await this.callObj.sendDigits(digits);
  }

  async audioDevices(): Promise<{ devices: SdkAudioDevice[]; selected?: SdkAudioDevice }> {
    const r = await this.sdk.getAudioDevices();
    return { devices: r.audioDevices, selected: r.selectedDevice };
  }

  async showRoutePicker(): Promise<boolean> {
    if (this.platform !== 'ios' || !this.sdk.showAvRoutePickerView) return false;
    await this.sdk.showAvRoutePickerView();
    return true;
  }

  dismissNotice(): void {
    this.notice = null;
    this.changed();
  }

  // ── ended summary ─────────────────────────────────────────────────────── //

  private setEnded(view: EndedView): void {
    if (this.endedTimer !== null) this.clearTimer(this.endedTimer);
    this.ended = view;
    this.endedTimer = this.setTimer(() => {
      this.endedTimer = null;
      this.ended = null;
      this.changed();
    }, ENDED_HOLD_MS);
    this.changed();
  }

  private clearEnded(): void {
    if (this.endedTimer !== null) this.clearTimer(this.endedTimer);
    this.endedTimer = null;
    this.ended = null;
  }

  // ── cold start / resume ───────────────────────────────────────────────── //

  /**
   * Pick up a call or invite the native layer already holds. This is how a
   * call answered on the lock screen (CallKit / Android full-screen
   * notification) before the JS bundle finished loading lands directly on the
   * in-call screen.
   */
  private async adoptExisting(): Promise<void> {
    try {
      const calls = await this.sdk.getCalls();
      for (const call of calls.values()) {
        if (this.boundCalls.has(call) || this.finishedCalls.has(call)) continue;
        if (call.getState() === 'disconnected') continue;
        const params = call.getCustomParameters() ?? {};
        const name = params.caller_name && params.caller_name !== params.from_number ? params.caller_name : '';
        const inbound = Boolean(params.from_number || params.call_log_id);
        this.adoptCall(call, inbound ? 'inbound' : 'outbound', {
          from: params.from_number || (inbound ? call.getFrom() : call.getTo()) || '',
          name, callLogId: params.call_log_id || null,
        });
        break;
      }
      if (!this.call) {
        const invites = await this.sdk.getCallInvites();
        for (const inv of invites.values()) {
          if (inv.getState() === 'pending') this.onCallInvite(inv);
        }
      }
    } catch (e) {
      this.log('adoptExisting failed', { error: errMessage(e) });
    }
  }
}
