/* Launch sequence: static native splash -> (RN ready) looping animation, shown
 * for at least MIN_DISPLAY_MS and until boot is done -> fade into the app.
 * Calls and background launches bypass it, notification taps skip the minimum,
 * Reduce Motion gets no animation, nothing can leave the splash stuck, and
 * native failures never throw. */
import {
  ANIMATION_MAX_MS, MIN_DISPLAY_MS, READY_TIMEOUT_MS, SPLASH_FADE_MS, SPLASH_MAX_MS,
  createStartupSplash, hasCall, type SplashNative, type StartupPhase, type Timers,
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

  test('normal launch: overlay ready -> native splash dropped instantly -> loops until boot -> fades -> done', () => {
    const { calls, splash, phases, advance } = fakes();
    splash.hold();
    splash.overlayReady(userLaunch);
    expect(calls.slice(-2)).toEqual(['options:0:false', 'hide']); // identical frame underneath: no fade
    expect(splash.phase).toBe('animating');
    splash.bootDone();
    expect(splash.phase).toBe('animating'); // minimum display time not reached yet
    advance(MIN_DISPLAY_MS);
    expect(splash.phase).toBe('leaving');
    splash.animationFinished();
    expect(phases).toEqual(['animating', 'leaving', 'done']);
  });

  test('boot already done when the overlay is ready: still shown for the minimum', () => {
    const { splash, phases, advance } = fakes();
    splash.hold();
    splash.bootDone();
    expect(splash.phase).toBe('native'); // native splash until the overlay has drawn frame 0
    splash.overlayReady(userLaunch);
    expect(phases).toEqual(['animating']);
    advance(MIN_DISPLAY_MS);
    expect(phases).toEqual(['animating', 'leaving']);
  });

  test('minimum counts from the first animated frame, not from hold', () => {
    const { splash, advance } = fakes();
    splash.hold();
    advance(1000); // JS + image load while the native splash shows
    splash.overlayReady(userLaunch);
    splash.bootDone();
    advance(MIN_DISPLAY_MS - 1);
    expect(splash.phase).toBe('animating');
    advance(1);
    expect(splash.phase).toBe('leaving');
  });

  test('keeps looping past the minimum while boot runs, then leaves as soon as it is done', () => {
    const { splash, advance } = fakes();
    splash.hold();
    splash.overlayReady(userLaunch);
    advance(MIN_DISPLAY_MS + 2000);
    expect(splash.phase).toBe('animating');
    splash.bootDone();
    expect(splash.phase).toBe('leaving');
  });

  test('notification tap on a cold launch skips the minimum (leaves once boot is done)', () => {
    const { splash, phases } = fakes();
    splash.hold();
    splash.expedite();
    splash.bootDone();
    splash.overlayReady(userLaunch);
    expect(phases).toEqual(['animating', 'leaving']);
  });

  test('notification tap during the animation leaves at once if booted, else at boot', () => {
    const booted = fakes();
    booted.splash.hold();
    booted.splash.overlayReady(userLaunch);
    booted.splash.bootDone();
    booted.splash.expedite();
    expect(booted.splash.phase).toBe('leaving');

    const booting = fakes();
    booting.splash.hold();
    booting.splash.overlayReady(userLaunch);
    booting.splash.expedite();
    expect(booting.splash.phase).toBe('animating'); // the app underneath is not ready yet
    booting.splash.bootDone();
    expect(booting.splash.phase).toBe('leaving');
  });

  test('expedite after the sequence is over changes nothing', () => {
    const { splash } = fakes();
    splash.hold();
    splash.callStarted();
    splash.expedite();
    expect(splash.phase).toBe('done');
  });

  test('animationFinished only ends the fade, never a running loop', () => {
    const { splash } = fakes();
    splash.hold();
    splash.overlayReady(userLaunch);
    splash.animationFinished();
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

  test.each(['native', 'animating', 'leaving'] as const)('a call during %s bypasses everything at once', (at) => {
    const { native, splash, phases, advance } = fakes();
    splash.hold();
    if (at !== 'native') splash.overlayReady(userLaunch);
    if (at === 'leaving') {
      splash.bootDone();
      advance(MIN_DISPLAY_MS);
    }
    expect(splash.phase).toBe(at);
    splash.callStarted();
    expect(splash.phase).toBe('done');
    expect(phases[phases.length - 1]).toBe('done');
    expect(native.hide).toHaveBeenCalledTimes(1);
    splash.animationFinished();
    splash.overlayReady(userLaunch);
    expect(splash.phase).toBe('done');
  });

  test('Reduce Motion: no animation and no minimum; native splash fades once boot is done', () => {
    const { native, splash, calls } = fakes();
    splash.hold();
    splash.overlayReady({ reduceMotion: true, appState: 'active' });
    expect(splash.phase).toBe('still');
    expect(native.hide).not.toHaveBeenCalled();
    splash.bootDone(); // well inside MIN_DISPLAY_MS
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

  test('a stalled boot cannot trap the user: the animation fades at ANIMATION_MAX_MS', () => {
    const { splash, advance, pending } = fakes();
    splash.hold();
    splash.overlayReady(userLaunch);
    advance(ANIMATION_MAX_MS - 1);
    expect(splash.phase).toBe('animating');
    advance(1);
    expect(splash.phase).toBe('leaving'); // reveals whatever boot has reached
    splash.animationFinished();
    expect(splash.phase).toBe('done');
    expect(pending.every((t) => t.cleared)).toBe(true);
  });

  test('phase changes are logged with time since hold', () => {
    const log = jest.fn();
    let t = 1000;
    const s = createStartupSplash(fakes().native, fakes().timers, log, () => t);
    s.hold();
    t = 1240;
    s.overlayReady(userLaunch);
    expect(log).toHaveBeenCalledWith('animating +240ms');
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
    const failures = log.mock.calls.filter(([msg]) => String(msg).includes('failed'));
    expect(failures).toHaveLength(4); // setOptions x2, hide, preventAutoHide
  });
});

describe('hasCall', () => {
  test('invite or active call', () => {
    expect(hasCall({ invite: null, call: null })).toBe(false);
    expect(hasCall({ invite: { from: '+1' }, call: null })).toBe(true);
    expect(hasCall({ invite: null, call: { remote: '+1' } })).toBe(true);
  });
});
