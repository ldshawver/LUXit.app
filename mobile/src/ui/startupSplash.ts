/* Launch sequence. The static native splash (app.config.ts, expo-splash-screen)
 * is held from index.ts; once React Native has drawn the identical first frame
 * of the animated overlay (StartupScreen.tsx) the native splash is dropped
 * instantly and the animation loops until boot is done and it has shown for
 * MIN_DISPLAY_MS, then fades into the app.
 *
 *   native     native splash up; overlay mounted underneath, loading the animation
 *   animating  native splash gone; animation looping above the app
 *   leaving    boot done (+ minimum shown): animation keeps playing while it fades out
 *   still      Reduce Motion: native splash stays until boot is done, then fades
 *   done       nothing over the app
 *
 * - Shown for at least MIN_DISPLAY_MS from its first frame on a normal cold
 *   launch, and loops for as long as boot takes beyond that.
 * - Calls first: an invite/active call, or a launch into the background (a VoIP
 *   or FCM push woke the app), skips straight to "done".
 * - Notification taps are not delayed: expedite() drops the minimum, so the
 *   tapped thread shows as soon as boot is done.
 * - Never stuck: no overlay within READY_TIMEOUT_MS -> plain fade; SPLASH_MAX_MS
 *   caps the native splash and ANIMATION_MAX_MS the animation whatever happens.
 * - Only overlays: PushKit/CallKit setup in index.ts runs before hold() and
 *   boot/auth/navigation run underneath, never waiting on it. */

export const SPLASH_BACKGROUND = '#000000';
/** dp; must match imageWidth in app.config.ts so the hand-off does not jump. */
export const SPLASH_LOGO_WIDTH = 150;
export const SPLASH_FADE_MS = 250;
export const READY_TIMEOUT_MS = 1500;
export const SPLASH_MAX_MS = 3000;
/** From the animation's first frame; normal cold launches only. */
export const MIN_DISPLAY_MS = 2500;
/** From hold(): the animation fades out even if boot never finishes. */
export const ANIMATION_MAX_MS = 8000;
export const OVERLAY_FADE_MS = 350;

export type StartupPhase = 'native' | 'animating' | 'leaving' | 'still' | 'done';

export interface SplashNative {
  preventAutoHideAsync(): Promise<unknown>;
  hide(): void;
  setOptions(options: { duration?: number; fade?: boolean }): void;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface OverlayReady {
  reduceMotion: boolean;
  /** AppState at launch; anything but "background" counts as a user launch. */
  appState: string | null | undefined;
}

export interface StartupSplash {
  readonly phase: StartupPhase;
  subscribe(listener: (phase: StartupPhase) => void): () => void;
  /** Keep the native splash up; call once at module load, before render. */
  hold(): void;
  /** The overlay has drawn its first frame (== the native splash). */
  overlayReady(info: OverlayReady): void;
  /** Session left "booting". */
  bootDone(): void;
  /** A call invite or active call: get out of the way now. */
  callStarted(): void;
  /** Launched or opened from a notification tap: leave as soon as boot is done. */
  expedite(): void;
  /** The overlay finished its fade. */
  animationFinished(): void;
}

const defaultTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

export function createStartupSplash(
  native: SplashNative,
  timers: Timers = defaultTimers,
  log: (msg: string, extra?: unknown) => void = () => undefined,
  now: () => number = Date.now,
): StartupSplash {
  let phase: StartupPhase = 'native';
  let held = false;
  let nativeHidden = false;
  let booted = false;
  let minShown = false;
  let expedited = false;
  let heldAt = 0;
  const pending: unknown[] = [];
  const listeners = new Set<(p: StartupPhase) => void>();

  const set = (next: StartupPhase) => {
    if (phase === next) return;
    phase = next;
    if (held) log(`${next} +${now() - heldAt}ms`);
    if (next === 'done') pending.splice(0).forEach((t) => timers.clearTimeout(t));
    for (const l of Array.from(listeners)) {
      try {
        l(next);
      } catch (e) {
        log('startup listener failed', String(e));
      }
    }
  };

  const hideNative = (fade: boolean) => {
    if (nativeHidden) return;
    nativeHidden = true;
    try {
      if (!fade) native.setOptions({ duration: 0, fade: false });
    } catch (e) {
      log('splash setOptions failed', String(e));
    }
    try {
      native.hide();
    } catch (e) {
      log('splash hide failed', String(e));
    }
  };

  const finish = (fade: boolean) => {
    hideNative(fade);
    set('done');
  };

  const maybeLeave = () => {
    if (phase === 'animating' && booted && (minShown || expedited)) set('leaving');
  };

  return {
    get phase() {
      return phase;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    hold() {
      if (held || phase !== 'native') return;
      held = true;
      heldAt = now();
      try {
        native.setOptions({ duration: SPLASH_FADE_MS, fade: true });
      } catch (e) {
        log('splash setOptions failed', String(e));
      }
      native.preventAutoHideAsync().catch((e) => log('splash preventAutoHide failed', String(e)));
      pending.push(timers.setTimeout(() => {
        if (phase === 'native') finish(true);
      }, READY_TIMEOUT_MS));
      pending.push(timers.setTimeout(() => {
        if (phase === 'native' || phase === 'still') finish(true);
      }, SPLASH_MAX_MS));
      pending.push(timers.setTimeout(() => {
        if (phase === 'animating') set('leaving'); // boot stalled: reveal whatever is underneath
      }, ANIMATION_MAX_MS));
    },
    overlayReady({ reduceMotion, appState }) {
      if (phase !== 'native') return;
      if (appState === 'background') return finish(false);
      if (reduceMotion) {
        if (booted) return finish(true);
        return set('still');
      }
      hideNative(false); // overlay shows the identical frame: no visible change
      set('animating');
      pending.push(timers.setTimeout(() => {
        minShown = true;
        maybeLeave();
      }, MIN_DISPLAY_MS));
      maybeLeave();
    },
    bootDone() {
      booted = true;
      if (phase === 'still') finish(true);
      else maybeLeave();
    },
    callStarted() {
      if (phase !== 'done') finish(false);
    },
    expedite() {
      expedited = true;
      maybeLeave();
    },
    animationFinished() {
      if (phase === 'leaving') set('done');
    },
  };
}

/** True when a call needs the screen. */
export function hasCall(voice: { invite: unknown; call: unknown }): boolean {
  return voice.invite != null || voice.call != null;
}
