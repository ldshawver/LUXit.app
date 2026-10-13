/* Launch sequence. The static native splash (app.config.ts, expo-splash-screen)
 * is held from index.ts; once React Native has drawn the identical first frame
 * of the animated overlay (StartupScreen.tsx) the native splash is dropped
 * instantly and the overlay plays a ~2 s animation, then fades into the app.
 *
 *   native     native splash up; overlay mounted underneath, loading layers
 *   animating  native splash gone; overlay animating above the app
 *   still      Reduce Motion: native splash stays until boot is done, then fades
 *   done       nothing over the app
 *
 * - Calls first: an invite/active call, or a launch into the background (a VoIP
 *   or FCM push woke the app), skips straight to "done".
 * - Never stuck: no overlay within READY_TIMEOUT_MS -> plain fade; MAX_MS caps
 *   the native splash whatever happens.
 * - Only overlays: PushKit/CallKit setup in index.ts runs before hold() and
 *   boot/auth/navigation run underneath, never waiting on it. */

export const SPLASH_BACKGROUND = '#000000';
/** dp; must match imageWidth in app.config.ts so the hand-off does not jump. */
export const SPLASH_LOGO_WIDTH = 150;
export const SPLASH_FADE_MS = 250;
export const READY_TIMEOUT_MS = 1500;
export const SPLASH_MAX_MS = 3000;

/** Overlay timeline (ms). */
export const ANIMATION_MS = 2000;
export const OVERLAY_FADE_MS = 350;
export const RING_TURN_DEG = 10;
export const PULSE_SCALE = 0.035;
export const PULSE_GLOW = 0.55;
export const SHIMMER_START_MS = 450;
export const SHIMMER_MS = 1000;

export type StartupPhase = 'native' | 'animating' | 'still' | 'done';

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
): StartupSplash {
  let phase: StartupPhase = 'native';
  let held = false;
  let nativeHidden = false;
  let booted = false;
  const pending: unknown[] = [];
  const listeners = new Set<(p: StartupPhase) => void>();

  const set = (next: StartupPhase) => {
    if (phase === next) return;
    phase = next;
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
    },
    bootDone() {
      booted = true;
      if (phase === 'still') finish(true);
    },
    callStarted() {
      if (phase !== 'done') finish(false);
    },
    animationFinished() {
      if (phase === 'animating') set('done');
    },
  };
}

/** True when a call needs the screen. */
export function hasCall(voice: { invite: unknown; call: unknown }): boolean {
  return voice.invite != null || voice.call != null;
}

/** One "beat" of the waveform pulse: rise to `amp` of full strength, then decay. */
export interface PulseStep {
  /** ms from the start of the animation to the start of this step */
  at: number;
  duration: number;
  /** 0..1 strength at the end of this step */
  level: number;
  kind: 'rise' | 'fall';
}

/** Irregular, music-like beats; strength 0..1. Ends at rest well before ANIMATION_MS. */
export const BEATS: ReadonlyArray<{ at: number; amp: number }> = [
  { at: 120, amp: 1.0 },
  { at: 430, amp: 0.55 },
  { at: 650, amp: 0.85 },
  { at: 980, amp: 0.45 },
  { at: 1180, amp: 1.0 },
  { at: 1500, amp: 0.6 },
  { at: 1700, amp: 0.35 },
];
export const BEAT_RISE_MS = 70;
export const BEAT_FALL_MS = 200;

/** Beats -> contiguous rise/fall steps (a beat that arrives mid-decay starts from there). */
export function pulseSteps(beats = BEATS): PulseStep[] {
  const steps: PulseStep[] = [];
  beats.forEach((b, i) => {
    const next = beats[i + 1]?.at ?? Infinity;
    steps.push({ at: b.at, duration: BEAT_RISE_MS, level: b.amp, kind: 'rise' });
    const fallStart = b.at + BEAT_RISE_MS;
    const fall = Math.max(0, Math.min(BEAT_FALL_MS, next - fallStart));
    if (fall > 0) steps.push({ at: fallStart, duration: fall, level: fall === BEAT_FALL_MS ? 0 : b.amp * (1 - fall / BEAT_FALL_MS), kind: 'fall' });
  });
  return steps;
}
