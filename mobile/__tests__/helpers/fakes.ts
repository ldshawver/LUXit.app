import type { SdkCall, SdkCallInvite, VoiceSdk } from '../../src/voice/sdk';
import type { VoiceToken } from '../../src/api/types';

type Fn = (...args: any[]) => void;

class Emitter {
  handlers = new Map<string, Fn[]>();
  on(ev: string, fn: Fn) {
    this.handlers.set(ev, [...(this.handlers.get(ev) ?? []), fn]);
    return this;
  }
  fire(ev: string, ...args: unknown[]) {
    for (const fn of this.handlers.get(ev) ?? []) fn(...args);
  }
  count(ev?: string) {
    if (ev) return (this.handlers.get(ev) ?? []).length;
    let n = 0;
    for (const v of this.handlers.values()) n += v.length;
    return n;
  }
}

export class FakeCall extends Emitter implements SdkCall {
  state = 'connecting';
  muted = false;
  digits: string[] = [];
  disconnects = 0;
  connectedAt?: Date;
  constructor(public sid = 'CA1', public params: Record<string, string> = {}, public from = '+14155550100', public to = '+14155550199') {
    super();
  }
  getSid() { return this.sid; }
  getState() { return this.state; }
  getFrom() { return this.from; }
  getTo() { return this.to; }
  getCustomParameters() { return this.params; }
  getInitialConnectedTimestamp() { return this.connectedAt; }
  async disconnect() { this.disconnects += 1; this.state = 'disconnected'; }
  async mute(m: boolean) { this.muted = m; return m; }
  isMuted() { return this.muted; }
  async hold(h: boolean) { return h; }
  async sendDigits(d: string) { this.digits.push(d); }
  connect(at = new Date(1000)) { this.state = 'connected'; this.connectedAt = at; this.fire('connected'); }
}

export class FakeInvite extends Emitter implements SdkCallInvite {
  accepts = 0;
  rejects = 0;
  state = 'pending';
  acceptImpl: () => Promise<SdkCall>;
  constructor(public sid = 'CAin', public params: Record<string, string> = { from_number: '+14155550100', caller_name: 'Ann Lee', call_log_id: '77' }) {
    super();
    this.acceptImpl = async () => new FakeCall(this.sid, this.params);
  }
  getCallSid() { return this.sid; }
  getFrom() { return 'client:caller'; }
  getTo() { return 'client:me'; }
  getState() { return this.state; }
  getCustomParameters() { return this.params; }
  async accept() { this.accepts += 1; this.state = 'accepted'; return this.acceptImpl(); }
  async reject() { this.rejects += 1; this.state = 'rejected'; }
}

export class FakeSdk extends Emitter implements VoiceSdk {
  registers: string[] = [];
  unregisters: string[] = [];
  connects: { token: string; params: Record<string, string> }[] = [];
  registerImpl: (token: string) => Promise<void> = async () => undefined;
  connectImpl: () => Promise<SdkCall> = async () => new FakeCall('CAout');
  calls = new Map<string, SdkCall>();
  invites = new Map<string, SdkCallInvite>();
  pushInits = 0;
  async register(token: string) { this.registers.push(token); await this.registerImpl(token); }
  async unregister(token: string) { this.unregisters.push(token); }
  async connect(token: string, opts: { params: Record<string, string> }) { this.connects.push({ token, params: opts.params }); return this.connectImpl(); }
  async getCalls() { return this.calls; }
  async getCallInvites() { return this.invites; }
  async getDeviceToken() { return 'voip-device-token'; }
  async getAudioDevices() { return { audioDevices: [] }; }
  async initializePushRegistry() { this.pushInits += 1; }
}

export class FakeVoiceApi {
  tokens = 0;
  reports: { registered: boolean; detail: Record<string, unknown> }[] = [];
  accepted: string[] = [];
  tokenImpl: () => Promise<VoiceToken> = async () => this.token();
  token(over: Partial<VoiceToken> = {}): VoiceToken {
    this.tokens += 1;
    return { token: `tok${this.tokens}`, identity: 'id', expires_in: 3600, calling_number: '+19165550123', permitted_numbers: [], incoming_enabled: true, outbound_enabled: true, ...over };
  }
  voiceToken() { return this.tokenImpl(); }
  async reportRegistration(registered: boolean, detail: Record<string, unknown>) { this.reports.push({ registered, detail }); }
  async markCallAccepted(id: string) { this.accepted.push(id); }
}

/** Manual clock + timers so tests control time exactly. */
export class FakeClock {
  t = 1_000_000;
  timers = new Map<number, { at: number; fn: () => void }>();
  seq = 0;
  now = () => this.t;
  set = (fn: () => void, ms: number) => {
    const id = ++this.seq;
    this.timers.set(id, { at: this.t + ms, fn });
    return id;
  };
  clear = (h: unknown) => {
    this.timers.delete(h as number);
  };
  pending() { return this.timers.size; }
  nextDelay() {
    const ats = [...this.timers.values()].map((x) => x.at - this.t);
    return ats.length ? Math.min(...ats) : null;
  }
  async advance(ms: number) {
    const end = this.t + ms;
    for (;;) {
      const due = [...this.timers.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      this.timers.delete(due[0]);
      this.t = due[1].at;
      due[1].fn();
      await flush();
    }
    this.t = end;
    await flush();
  }
}

export async function flush() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}
