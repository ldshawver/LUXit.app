/* Launch sequence: static native splash -> (RN ready) animated overlay -> app.
 * Calls and background launches bypass it, Reduce Motion gets no animation,
 * nothing can leave the splash stuck, and native failures never throw. */
import {
  ANIMATION_MS, BEATS, BEAT_FALL_MS, BEAT_RISE_MS, READY_TIMEOUT_MS, SPLASH_FADE_MS, SPLASH_MAX_MS,
  createStartupSplash, hasCall, pulseSteps, type SplashNative, type StartupPhase, type Timers,
} from '../src/ui/startupSplash';

function fakes() {
  const calls: string[] = [];
  const native = {
    preventAutoHideAsync: jest.fn(async () => { calls.push('prevent'); return true; }),
    hide: jest.fn(() => { calls.push('hide'); }),
    setOptions: jest.fn((o: { duration?: number; fade?: boolean }) => { calls.push(`options:${o.duration}:${o.fade}`); }),
  } satisfies SplashNative;
  let now = 0;
  const pending: Array<{ at: number; fn: () => void; cleared: boolean }> = [];
  const timers: Timers = {
    setTimeout: (fn, ms) => {
      const t = { at: now + ms, fn, cleared: false };
      pending.push(t);
      return t;
    },
    clearTimeout: (h) => {
      (h as { cleared: boolean }).cleared = true;
    },
  };
  const advance = (ms: number) => {
    now += ms;
    pending.filter((t) => !t.cleared && t.at <= now).forEach((t) => {
      t.cleared = true;
      t.fn();
    });
  };
  const splash = createStartupSplash(native, timers);
  const phases: StartupPhase[] = [];
  splash.subscribe((p) => phases.push(p));
  return { native, calls, timers, advance, pending, splash, phases };
}

const userLaunch = { reduceMotion: false, appState: 'active' };

describe('createStartupSplash', () => {
  test('hold keeps the native splash, with a fade for the fallback paths', () => {
    const { calls, splash } = fakes();
    splash.hold();
    expect(calls).toEqual([`options:${SPLASH_FADE_MS}:true`, 'prevent']);
    expect(splash.phase).toBe('native');
  });

  test('normal launch: overlay ready -> native splash dropped instantly -> animating -> done', () => {
    const { calls, splash, phases } = fakes();
    splash.hold();
    splash.overlayReady(userLaunch);
    expect(calls.slice(-2)).toEqual(['options:0:false', 'hide']); // identical frame underneath: no fade
    expect(splash.phase).toBe('animating');
    splash.bootDone(); // boot finishing does not cut the animation short
    expect(splash.phase).toBe('animating');
    splash.animationFinished();
    expect(phases).toEqual(['animating', 'done']);
  });

  test('the animation does not wait for boot (auth/navigation run underneath)', () => {
    const { splash } = fakes();
    splash.hold();
    splash.overlayReady(userLaunch);
    expect(splash.phase).toBe('animating');
  });

  test('"inactive"/unknown launch states still animate (iOS cold launch reports inactive)', () => {
    for (const appState of ['inactive', 'unknown', null, undefined]) {
      const { splash } = fakes();
      splash.hold();
      splash.overlayReady({ reduceMotion: false, appState });
      expect(splash.phase).toBe('animating');
    }
  });

  test('a launch into the background (VoIP/FCM push woke the app) never animates', () => {
    const { native, splash, phases } = fakes();
    splash.hold();
    splash.overlayReady({ reduceMotion: false, appState: 'background' });
    expect(phases).toEqual(['done']);
    expect(native.hide).toHaveBeenCalledTimes(1);
  });

  test.each(['native', 'animating'] as const)('a call during %s bypasses everything at once', (at) => {
    const { native, splash, phases } = fakes();
    splash.hold();
    if (at === 'animating') splash.overlayReady(userLaunch);
    splash.callStarted();
    expect(splash.phase).toBe('done');
    expect(phases[phases.length - 1]).toBe('done');
    expect(native.hide).toHaveBeenCalledTimes(1);
    splash.animationFinished();
    splash.overlayReady(userLaunch);
    expect(splash.phase).toBe('done');
  });

  test('Reduce Motion: no animation; native splash fades once boot is done', () => {
    const { native, splash, calls } = fakes();
    splash.hold();
    splash.overlayReady({ reduceMotion: true, appState: 'active' });
    expect(splash.phase).toBe('still');
    expect(native.hide).not.toHaveBeenCalled();
    splash.bootDone();
    expect(splash.phase).toBe('done');
    expect(calls).not.toContain('options:0:false'); // the configured fade, not a cut
  });

  test('Reduce Motion with boot already done fades straight away', () => {
    const { splash } = fakes();
    splash.hold();
    splash.bootDone();
    splash.overlayReady({ reduceMotion: true, appState: 'active' });
    expect(splash.phase).toBe('done');
  });

  test('overlay never ready -> plain fade after READY_TIMEOUT_MS', () => {
    const { native, splash, advance } = fakes();
    splash.hold();
    advance(READY_TIMEOUT_MS - 1);
    expect(native.hide).not.toHaveBeenCalled();
    advance(1);
    expect(splash.phase).toBe('done');
    expect(native.hide).toHaveBeenCalledTimes(1);
    splash.overlayReady(userLaunch); // late: ignored, nothing replays
    expect(splash.phase).toBe('done');
  });

  test('Reduce Motion with a stalled boot is capped at SPLASH_MAX_MS', () => {
    const { splash, advance } = fakes();
    splash.hold();
    splash.overlayReady({ reduceMotion: true, appState: 'active' });
    advance(SPLASH_MAX_MS);
    expect(splash.phase).toBe('done');
  });

  test('timers cannot cut a running animation and are cleared when done', () => {
    const { splash, advance, pending } = fakes();
    splash.hold();
    splash.overlayReady(userLaunch);
    advance(SPLASH_MAX_MS);
    expect(splash.phase).toBe('animating');
    splash.animationFinished();
    expect(pending.every((t) => t.cleared)).toBe(true);
  });

  test('native failures are logged, never thrown into startup', async () => {
    const log = jest.fn();
    const native: SplashNative = {
      preventAutoHideAsync: jest.fn(async () => { throw new Error('no module'); }),
      hide: jest.fn(() => { throw new Error('gone'); }),
      setOptions: jest.fn(() => { throw new Error('old sdk'); }),
    };
    const s = createStartupSplash(native, fakes().timers, log);
    expect(() => s.hold()).not.toThrow();
    expect(() => s.overlayReady(userLaunch)).not.toThrow();
    await Promise.resolve();
    expect(s.phase).toBe('animating');
    expect(log).toHaveBeenCalledTimes(4); // setOptions x2, hide, preventAutoHide
  });
});

describe('hasCall', () => {
  test('invite or active call', () => {
    expect(hasCall({ invite: null, call: null })).toBe(false);
    expect(hasCall({ invite: { from: '+1' }, call: null })).toBe(true);
    expect(hasCall({ invite: null, call: { remote: '+1' } })).toBe(true);
  });
});

describe('pulseSteps', () => {
  const steps = pulseSteps();
  test('contiguous, in order, and finished inside the animation', () => {
    for (let i = 1; i < steps.length; i += 1) expect(steps[i]!.at).toBeGreaterThanOrEqual(steps[i - 1]!.at + steps[i - 1]!.duration);
    const last = steps[steps.length - 1]!;
    expect(last.at + last.duration).toBeLessThanOrEqual(ANIMATION_MS);
    expect(last.level).toBe(0); // ring back at rest before the fade
  });
  test('every beat rises to its strength; a beat cut short decays only partway', () => {
    expect(steps.filter((s) => s.kind === 'rise').map((s) => s.level)).toEqual(BEATS.map((b) => b.amp));
    const short = pulseSteps([{ at: 0, amp: 1 }, { at: BEAT_RISE_MS + BEAT_FALL_MS / 2, amp: 0.5 }]);
    expect(short[1]).toMatchObject({ kind: 'fall', duration: BEAT_FALL_MS / 2, level: 0.5 });
  });
});
