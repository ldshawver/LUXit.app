/* Launch splash: held before render, released once (no artificial delay),
 * yields to calls, never stuck, never throws into startup. */
import { SPLASH_FADE_MS, createStartupSplash, splashCanRelease, type SplashNative, type Timers } from '../src/ui/startupSplash';

function fakes() {
  const native = {
    preventAutoHideAsync: jest.fn(async () => true),
    hide: jest.fn(),
    setOptions: jest.fn(),
  } satisfies SplashNative;
  const pending: Array<{ fn: () => void; cleared: boolean }> = [];
  const timers: Timers = {
    setTimeout: (fn) => {
      const t = { fn, cleared: false };
      pending.push(t);
      return t;
    },
    clearTimeout: (h) => {
      (h as { cleared: boolean }).cleared = true;
    },
  };
  const fire = () => pending.filter((t) => !t.cleared).forEach((t) => t.fn());
  return { native, timers, pending, fire };
}

describe('createStartupSplash', () => {
  test('hold keeps the native splash and configures a fade', () => {
    const { native, timers } = fakes();
    createStartupSplash(native, timers).hold();
    expect(native.setOptions).toHaveBeenCalledWith({ duration: SPLASH_FADE_MS, fade: true });
    expect(native.preventAutoHideAsync).toHaveBeenCalledTimes(1);
    expect(native.hide).not.toHaveBeenCalled();
  });

  test('release hides immediately, once, and cancels the safety timer', () => {
    const { native, timers, pending, fire } = fakes();
    const s = createStartupSplash(native, timers);
    s.hold();
    s.release();
    s.release();
    fire();
    expect(native.hide).toHaveBeenCalledTimes(1);
    expect(pending[0]?.cleared).toBe(true);
    expect(s.released).toBe(true);
  });

  test('a stalled boot is released by the timeout', () => {
    const { native, timers, fire } = fakes();
    const s = createStartupSplash(native, timers, 3000);
    s.hold();
    fire();
    expect(native.hide).toHaveBeenCalledTimes(1);
    expect(s.released).toBe(true);
  });

  test('native failures are logged, never thrown into startup', async () => {
    const log = jest.fn();
    const native: SplashNative = {
      preventAutoHideAsync: jest.fn(async () => { throw new Error('no module'); }),
      hide: jest.fn(() => { throw new Error('gone'); }),
      setOptions: jest.fn(() => { throw new Error('old sdk'); }),
    };
    const s = createStartupSplash(native, fakes().timers, 3000, log);
    expect(() => s.hold()).not.toThrow();
    expect(() => s.release()).not.toThrow();
    await Promise.resolve();
    expect(log).toHaveBeenCalledTimes(3);
  });

  test('hold after release is a no-op (no second preventAutoHide)', () => {
    const { native, timers } = fakes();
    const s = createStartupSplash(native, timers);
    s.release();
    s.hold();
    expect(native.preventAutoHideAsync).not.toHaveBeenCalled();
  });
});

describe('splashCanRelease', () => {
  const idle = { invite: null, call: null };
  test('waits only while booting', () => {
    expect(splashCanRelease('booting', idle)).toBe(false);
    expect(splashCanRelease('signed_out', idle)).toBe(true);
    expect(splashCanRelease('signed_in', idle)).toBe(true);
  });
  test('an incoming or active call releases it even mid-boot', () => {
    expect(splashCanRelease('booting', { invite: { from: '+1' }, call: null })).toBe(true);
    expect(splashCanRelease('booting', { invite: null, call: { remote: '+1' } })).toBe(true);
  });
});
