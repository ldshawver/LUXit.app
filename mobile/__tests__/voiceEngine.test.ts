import { ApiError, NetworkError } from '../src/api/http';
import { classifyVoiceError } from '../src/api/endpoints';
import { ENDED_HOLD_MS, MIC_DENIED_MESSAGE, POLICY_RECHECK_MS, REREGISTER_AFTER_MS, RETRY_CAP_MS, VoiceEngine, type VoiceSnapshot } from '../src/voice/VoiceEngine';
import { voiceStatusText } from '../src/voice/statusText';
import { FakeCall, FakeClock, FakeInvite, FakeSdk, FakeVoiceApi, flush } from './helpers/fakes';

function setup(platform: 'ios' | 'android' = 'ios', opts: { mic?: boolean } = {}) {
  const sdk = new FakeSdk();
  const api = new FakeVoiceApi();
  const clock = new FakeClock();
  const micAsks: boolean[] = [];
  const engine = new VoiceEngine({
    sdk, api, platform, now: clock.now, setTimer: clock.set, clearTimer: clock.clear, random: () => 0.5,
    classify: classifyVoiceError,
    micPermission: async (ask) => { micAsks.push(ask); return opts.mic ?? true; },
  });
  const phases: string[] = [];
  engine.subscribe((s: VoiceSnapshot) => {
    if (phases[phases.length - 1] !== s.phase) phases.push(s.phase);
  });
  return { sdk, api, clock, engine, phases, micAsks };
}

describe('registration', () => {
  test('sign-in registers automatically and only then reports Ready', async () => {
    const { engine, sdk, api, phases } = setup();
    await engine.start();
    expect(sdk.pushInits).toBe(1);
    expect(sdk.registers).toEqual(['tok1']);
    expect(engine.snapshot().phase).toBe('ready');
    expect(phases).toEqual(['starting', 'registering', 'ready']);
    expect(api.reports).toEqual([{ registered: true, detail: { voiceDeviceToken: 'voip-device-token' } }]);
  });

  test('never Ready while offline even if registered', async () => {
    const { engine } = setup();
    await engine.start();
    engine.onNetwork(false);
    expect(engine.snapshot().phase).toBe('offline');
    expect(voiceStatusText(engine.snapshot(), 'ios').title).toBe('No internet connection');
  });

  test('transient failure retries with growing backoff capped at 5 minutes, no manual step', async () => {
    const { engine, sdk, clock } = setup();
    let fail = true;
    sdk.registerImpl = async () => {
      if (fail) throw { message: 'boom', code: 53000 };
    };
    await engine.start();
    expect(engine.snapshot().phase).toBe('reconnecting');
    const delays: number[] = [];
    for (let i = 0; i < 12; i++) {
      delays.push(clock.nextDelay()!);
      await clock.advance(clock.nextDelay()!);
    }
    expect(delays[1]!).toBeGreaterThan(delays[0]!);
    expect(Math.max(...delays)).toBeLessThanOrEqual(RETRY_CAP_MS);
    expect(engine.snapshot().attempts).toBe(13);
    fail = false;
    await clock.advance(clock.nextDelay()!);
    expect(engine.snapshot().phase).toBe('ready');
    expect(engine.snapshot().attempts).toBe(0);
    expect(clock.pending()).toBe(0); // nothing left scheduled once ready
  });

  test('server policy refusal is not retried in a loop and is described honestly', async () => {
    const { engine, api, clock } = setup();
    api.tokenImpl = async () => {
      throw new ApiError(403, 'PHONE_AWAY', 'You are Away.');
    };
    await engine.start();
    const s = engine.snapshot();
    expect(s.phase).toBe('unavailable');
    expect(s.blockReason).toBe('PHONE_AWAY');
    expect(voiceStatusText(s, 'ios').title).toBe('You’re Away');
    expect(clock.nextDelay()).toBe(POLICY_RECHECK_MS);
    expect(api.tokens).toBe(0);
  });

  test('no push credential on the server: outgoing only, never claims it will ring', async () => {
    const { engine, api, sdk } = setup();
    api.tokenImpl = async () => api.token({ incoming_enabled: false });
    await engine.start();
    expect(sdk.registers).toEqual([]);
    expect(engine.snapshot().phase).toBe('unavailable');
    expect(engine.snapshot().outgoingEnabled).toBe(true);
    expect(voiceStatusText(engine.snapshot(), 'android').title).toBe('Outgoing calls only');
  });

  test('auth failure stops quietly (the API client signs the app out)', async () => {
    const { engine, api, clock } = setup();
    api.tokenImpl = async () => {
      throw new ApiError(401, 'device_revoked', 'signed out');
    };
    await engine.start();
    expect(clock.pending()).toBe(0);
  });

  test('network loss then return re-registers once; concurrent triggers share one attempt', async () => {
    const { engine, sdk, api, clock } = setup();
    api.tokenImpl = async () => {
      throw new NetworkError('offline');
    };
    await engine.start();
    engine.onNetwork(false);
    expect(engine.snapshot().phase).toBe('offline');
    api.tokenImpl = async () => api.token();
    clock.timers.clear();
    engine.onNetwork(true);
    void engine.onForeground();
    void engine.refresh();
    await flush();
    expect(sdk.registers.length).toBe(1);
    expect(engine.snapshot().phase).toBe('ready');
  });

  test('foreground re-registers only when stale or not registered', async () => {
    const { engine, sdk, clock } = setup();
    await engine.start();
    await engine.onForeground();
    expect(sdk.registers.length).toBe(1);
    clock.t += REREGISTER_AFTER_MS + 1;
    await engine.onForeground();
    expect(sdk.registers.length).toBe(2);
  });

  test('SDK listeners are attached once regardless of restarts', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    await engine.start();
    await engine.onForeground();
    expect(sdk.count('callInvite')).toBe(1);
    expect(sdk.count()).toBe(4);
  });

  test('sign-out unregisters the handset and stops retries', async () => {
    const { engine, sdk, clock } = setup();
    await engine.start();
    await engine.signOut();
    expect(sdk.unregisters).toEqual(['tok1']);
    expect(engine.snapshot().phase).toBe('signed_out');
    expect(clock.pending()).toBe(0);
  });

  test('unregistered event from the SDK triggers automatic recovery', async () => {
    const { engine, sdk, clock } = setup();
    await engine.start();
    sdk.fire('unregistered');
    expect(engine.snapshot().phase).toBe('reconnecting');
    await clock.advance(clock.nextDelay()!);
    expect(engine.snapshot().phase).toBe('ready');
  });
});

describe('incoming calls', () => {
  test('invite shows caller, Answer goes straight to the call, exactly once', async () => {
    const { engine, sdk, api, phases } = setup();
    await engine.start();
    const invite = new FakeInvite();
    sdk.fire('callInvite', invite);
    expect(engine.snapshot().invite).toMatchObject({ name: 'Ann Lee', from: '+14155550100', callLogId: '77' });
    await Promise.all([engine.accept(), engine.accept(), engine.accept()]);
    expect(invite.accepts).toBe(1);
    expect(engine.snapshot().phase).toBe('connecting');
    expect(engine.snapshot().invite).toBeNull();
    const call = engine['callObj'] as FakeCall;
    call.connect();
    expect(engine.snapshot().phase).toBe('connected');
    expect(api.accepted).toEqual(['77']);
    expect(phases).toEqual(['starting', 'registering', 'ready', 'incoming', 'connecting', 'connected']);
  });

  test('the same invite delivered twice (push + foreground) rings once', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    const invite = new FakeInvite('CAdup');
    sdk.fire('callInvite', invite);
    sdk.fire('callInvite', invite);
    expect(invite.count('accepted')).toBe(1);
  });

  test('answered on the lock screen (native accepted event) adopts the call without a second accept', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    const invite = new FakeInvite();
    sdk.fire('callInvite', invite);
    const call = new FakeCall('CAin', invite.params);
    call.state = 'connected';
    call.connectedAt = new Date(5000);
    invite.fire('accepted', call);
    await engine.accept();
    expect(invite.accepts).toBe(0);
    expect(engine.snapshot().phase).toBe('connected');
    expect(engine.snapshot().call?.startedAt).toBe(5000);
  });

  test('cold start after answering from CallKit lands on the active call', async () => {
    const { engine, sdk } = setup();
    const call = new FakeCall('CAcold', { from_number: '+14155550111', caller_name: 'Bo', call_log_id: '9' });
    call.state = 'connected';
    call.connectedAt = new Date(2000);
    sdk.calls.set('uuid', call);
    await engine.start();
    expect(engine.snapshot().phase).toBe('connected');
    expect(engine.snapshot().call).toMatchObject({ remote: '+14155550111', name: 'Bo', direction: 'inbound' });
  });

  test('cold start with a still-pending invite shows the incoming screen', async () => {
    const { engine, sdk } = setup();
    sdk.invites.set('u', new FakeInvite('CApending'));
    await engine.start();
    expect(engine.snapshot().phase).toBe('incoming');
  });

  test('decline rejects once and shows Declined briefly, then Ready', async () => {
    const { engine, sdk, clock } = setup();
    await engine.start();
    const invite = new FakeInvite();
    sdk.fire('callInvite', invite);
    await engine.decline();
    await engine.decline();
    invite.fire('rejected');
    expect(invite.rejects).toBe(1);
    expect(engine.snapshot().phase).toBe('ended');
    expect(engine.snapshot().ended?.reason).toBe('declined');
    await clock.advance(ENDED_HOLD_MS);
    expect(engine.snapshot().phase).toBe('ready');
  });

  test('caller hangs up or another device answers: ringing stops', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    const invite = new FakeInvite();
    sdk.fire('callInvite', invite);
    invite.fire('cancelled');
    expect(engine.snapshot().invite).toBeNull();
    expect(engine.snapshot().ended?.reason).toBe('cancelled');
  });

  test('second call while busy is rejected at once and the first call is untouched', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    sdk.fire('callInvite', new FakeInvite('CA1'));
    await engine.accept();
    (engine['callObj'] as FakeCall).connect();
    const second = new FakeInvite('CA2', { from_number: '+14155550222', caller_name: '+14155550222' });
    sdk.fire('callInvite', second);
    expect(second.rejects).toBe(1);
    expect(engine.snapshot().phase).toBe('connected');
    expect(engine.snapshot().notice?.kind).toBe('missed_while_busy');
    expect(engine.snapshot().notice?.text).toContain('+14155550222');
  });

  test('name equal to the number is not shown twice', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    sdk.fire('callInvite', new FakeInvite('CAx', { from_number: '+14155550333', caller_name: '+14155550333' }));
    expect(engine.snapshot().invite?.name).toBe('');
  });

  test('End pressed while the answer is still connecting: call never comes back', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    const invite = new FakeInvite();
    let resolveAccept!: (c: FakeCall) => void;
    invite.acceptImpl = () => new Promise((r) => { resolveAccept = r; });
    sdk.fire('callInvite', invite);
    const accepting = engine.accept();
    await flush();
    await engine.hangup();
    expect(engine.snapshot().phase).toBe('ended');
    const late = new FakeCall('CAin', invite.params);
    resolveAccept(late);
    await accepting;
    expect(late.disconnects).toBe(1);
    expect(engine.snapshot().call).toBeNull();
  });

  test('accept failure reports clearly and does not leave a stuck call', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    const invite = new FakeInvite();
    invite.acceptImpl = async () => {
      throw { message: 'Microphone permission denied', code: 31401 };
    };
    sdk.fire('callInvite', invite);
    await engine.accept();
    expect(engine.snapshot().call).toBeNull();
    expect(engine.snapshot().ended?.reason).toBe('failed');
    expect(engine.snapshot().notice?.text).toContain('Microphone');
  });
});

describe('active call', () => {
  async function connected() {
    const ctx = setup();
    await ctx.engine.start();
    ctx.sdk.fire('callInvite', new FakeInvite());
    await ctx.engine.accept();
    const call = ctx.engine['callObj'] as FakeCall;
    call.connect(new Date(ctx.clock.t));
    return { ...ctx, call };
  }

  test('local hang-up updates the UI immediately and reports duration', async () => {
    const { engine, call, clock } = await connected();
    clock.t += 84_000;
    await engine.hangup();
    expect(call.disconnects).toBe(1);
    expect(engine.snapshot().phase).toBe('ended');
    expect(engine.snapshot().ended).toMatchObject({ reason: 'completed', durationSec: 84, answered: true });
    call.fire('disconnected');
    expect(engine.snapshot().ended?.durationSec).toBe(84);
  });

  test('remote hang-up updates the UI', async () => {
    const { engine, call } = await connected();
    call.fire('disconnected');
    expect(engine.snapshot().phase).toBe('ended');
  });

  test('mute, keypad tones, reconnecting and quality warnings', async () => {
    const { engine, call } = await connected();
    await engine.setMuted(true);
    expect(engine.snapshot().call?.muted).toBe(true);
    await engine.sendDigits('5');
    expect(call.digits).toEqual(['5']);
    call.fire('reconnecting', { message: 'net' });
    expect(engine.snapshot().phase).toBe('call_reconnecting');
    call.fire('reconnected');
    expect(engine.snapshot().phase).toBe('connected');
    call.fire('qualityWarningsChanged', ['high-jitter']);
    expect(engine.snapshot().call?.qualityWarnings).toEqual(['high-jitter']);
  });

  test('repeat calls leave no stuck state and no listener growth', async () => {
    const { engine, sdk, clock } = setup();
    await engine.start();
    for (let i = 0; i < 10; i++) {
      const inv = new FakeInvite(`CArep${i}`);
      sdk.fire('callInvite', inv);
      await engine.accept();
      const call = engine['callObj'] as FakeCall;
      call.connect();
      expect(call.count('disconnected')).toBe(1);
      call.fire('disconnected');
      await clock.advance(ENDED_HOLD_MS);
      expect(engine.snapshot().phase).toBe('ready');
    }
    expect(sdk.count()).toBe(4);
    expect(sdk.registers.length).toBe(1);
  });
});

describe('outbound calls', () => {
  test('dials E.164 with the selected number, shows Calling then Ringing then connected', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    const out = new FakeCall('CAout');
    sdk.connectImpl = async () => out;
    await engine.placeCall('(415) 555-0100', { phoneNumberId: 7, name: 'Cy' });
    expect(sdk.connects[0]!.params).toEqual({ To: '+14155550100', phone_number_id: '7' });
    expect(engine.snapshot().phase).toBe('connecting');
    out.fire('ringing');
    expect(engine.snapshot().phase).toBe('ringing');
    out.connect();
    expect(engine.snapshot().phase).toBe('connected');
  });

  test('End before connect resolves cancels the late call leg', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    let resolve!: (c: FakeCall) => void;
    sdk.connectImpl = () => new Promise((r) => { resolve = r; });
    const placing = engine.placeCall('4155550100');
    await flush();
    await engine.hangup();
    expect(engine.snapshot().phase).toBe('ended');
    const late = new FakeCall('CAlate');
    resolve(late);
    await placing;
    expect(late.disconnects).toBe(1);
    expect(engine.snapshot().call).toBeNull();
  });

  test('reuses a fresh token for outbound instead of minting one per call', async () => {
    const { engine, api } = setup();
    await engine.start();
    await engine.placeCall('4155550100');
    await engine.hangup();
    await engine.placeCall('4155550101');
    expect(api.tokens).toBe(1);
  });

  test('expired cached token is refreshed before dialing', async () => {
    const { engine, api, clock, sdk } = setup();
    await engine.start();
    clock.t += 3600 * 1000;
    await engine.placeCall('4155550100');
    expect(api.tokens).toBe(2);
    expect(sdk.connects[0]!.token).toBe('tok2');
  });

  test('invalid number and busy are refused without touching the SDK', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    await expect(engine.placeCall('12')).rejects.toThrow('valid phone number');
    sdk.fire('callInvite', new FakeInvite());
    await expect(engine.placeCall('4155550100')).rejects.toThrow('already on a call');
    expect(sdk.connects).toEqual([]);
  });

  test('connect failure ends cleanly with the reason', async () => {
    const { engine, sdk } = setup();
    await engine.start();
    const out = new FakeCall('CAf');
    sdk.connectImpl = async () => out;
    await engine.placeCall('4155550100');
    out.fire('connectFailure', { message: 'Busy', code: 31486 });
    expect(engine.snapshot().ended).toMatchObject({ reason: 'failed', message: 'Busy (31486)' });
  });
});

describe('microphone permission', () => {
  test('outbound is refused with a fix-it message when the mic is off, SDK untouched', async () => {
    const { engine, sdk, micAsks } = setup('android', { mic: false });
    await engine.start();
    await expect(engine.placeCall('4155550100')).rejects.toThrow(MIC_DENIED_MESSAGE);
    expect(micAsks).toEqual([true]);
    expect(sdk.connects).toEqual([]);
    expect(engine.snapshot().call).toBeNull();
  });

  test('inbound is still answered without the mic, with a notice saying why', async () => {
    const { engine, sdk } = setup('android', { mic: false });
    await engine.start();
    const inv = new FakeInvite();
    sdk.fire('callInvite', inv);
    await engine.accept();
    expect(engine.snapshot().phase).not.toBe('ended');
    expect(engine.snapshot().call).not.toBeNull();
    expect(engine.snapshot().notice).toMatchObject({ kind: 'mic_denied' });
  });
});
