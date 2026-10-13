/* Launch screen lifecycle. The native splash (app.config.ts, expo-splash-screen)
 * is held from index.ts until the app knows what to show, then faded out. The
 * supplied LUX artwork is a single still frame, so there is no animated stage:
 * the hand-off is the native splash fading straight into the app.
 *
 * - No artificial delay: released the moment boot leaves "booting" (local
 *   storage only, no network), so a fast start never waits.
 * - Calls first: an invite or active call releases it immediately.
 * - Never stuck: a timeout releases it even if boot stalls.
 * - It only controls an overlay; PushKit/CallKit setup in index.ts runs before
 *   it and never waits on it. */

export const SPLASH_BACKGROUND = '#000000';
/** dp; must match imageWidth in app.config.ts so the hand-off does not jump. */
export const SPLASH_LOGO_WIDTH = 150;
export const SPLASH_FADE_MS = 250;
export const SPLASH_MAX_MS = 3000;

export interface SplashNative {
  preventAutoHideAsync(): Promise<unknown>;
  hide(): void;
  setOptions(options: { duration?: number; fade?: boolean }): void;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface StartupSplash {
  /** Keep the native splash up; call once at module load, before render. */
  hold(): void;
  /** Fade it out. Idempotent. */
  release(): void;
  readonly released: boolean;
}

const defaultTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export function createStartupSplash(
  native: SplashNative,
  timers: Timers = defaultTimers,
  maxMs: number = SPLASH_MAX_MS,
  log: (msg: string, extra?: unknown) => void = () => undefined,
): StartupSplash {
  let held = false;
  let released = false;
  let timer: unknown = null;

  const release = () => {
    if (released) return;
    released = true;
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    try {
      native.hide();
    } catch (e) {
      log('splash hide failed', String(e));
    }
  };

  return {
    hold() {
      if (held || released) return;
      held = true;
      try {
        native.setOptions({ duration: SPLASH_FADE_MS, fade: true });
      } catch (e) {
        log('splash setOptions failed', String(e));
      }
      native.preventAutoHideAsync().catch((e) => log('splash preventAutoHide failed', String(e)));
      timer = timers.setTimeout(release, maxMs);
    },
    release,
    get released() {
      return released;
    },
  };
}

/** True once the app can replace the splash: boot finished, or a call needs the screen. */
export function splashCanRelease(sessionStatus: string, voice: { invite: unknown; call: unknown }): boolean {
  return sessionStatus !== 'booting' || voice.invite != null || voice.call != null;
}
