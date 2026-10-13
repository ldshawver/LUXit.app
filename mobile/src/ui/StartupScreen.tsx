import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { AccessibilityInfo, Animated, AppState, Easing, Image, StyleSheet, View } from 'react-native';
import * as SplashScreen from 'expo-splash-screen';
import { getController } from '../state/controller';
import {
  ANIMATION_MS, OVERLAY_FADE_MS, PULSE_GLOW, PULSE_SCALE, RING_TURN_DEG, SHIMMER_MS, SHIMMER_START_MS,
  SPLASH_BACKGROUND, SPLASH_LOGO_WIDTH, createStartupSplash, hasCall, pulseSteps, type StartupPhase,
} from './startupSplash';

export const startupSplash = createStartupSplash(SplashScreen, undefined, (msg, extra) => console.log(`[splash] ${msg}`, extra ?? ''));

// Layers of assets/splash-logo.png (scripts/startup-layers/build_layers.py):
// at rest static-over-ring is pixel-identical to the native splash image.
const RING = require('../../assets/startup/ring.png');
const RING_BRIGHT = require('../../assets/startup/ring_bright.png');
const STATIC = require('../../assets/startup/static.png');
const GLINT = require('../../assets/startup/glint.png');
const LOGO = require('../../assets/splash-logo.png');
const LAYERS_TO_LOAD = 3; // ring, ring_bright, static (glint is invisible at rest)

const SIZE = SPLASH_LOGO_WIDTH;
/** Soft shimmer edge: nested windows over the glint layer (half-width dp, opacity). */
const SHIMMER_BANDS = [
  { half: 13, opacity: 0.3 },
  { half: 7, opacity: 0.3 },
  { half: 3, opacity: 0.4 },
];
const SHIMMER_SKEW = '-18deg';
const UNSKEW = '18deg';

export function useStartupPhase(): StartupPhase {
  return useSyncExternalStore(startupSplash.subscribe, () => startupSplash.phase, () => startupSplash.phase);
}

/** Feeds boot and call state to the launch sequence (subscribes; never re-renders App). */
export function useStartupSignals(): void {
  useEffect(() => {
    const c = getController();
    const check = () => {
      if (hasCall(c.voice.snapshot())) startupSplash.callStarted();
      if (c.snapshot().status !== 'booting') startupSplash.bootDone();
    };
    check();
    const offSession = c.subscribe(check);
    const offVoice = c.voice.subscribe(check);
    return () => {
      offSession();
      offVoice();
    };
  }, []);
}

/** Static black + logo (what shows while the session restores, if ever visible). */
export function StartupScreen() {
  return (
    <View style={styles.fill}>
      <Image source={LOGO} style={styles.logo} resizeMode="contain" accessibilityLabel="LUX" />
    </View>
  );
}

/** Above everything while the launch sequence needs it; unmounts at once for a call. */
export function StartupOverlay() {
  const phase = useStartupPhase();
  if (phase !== 'native' && phase !== 'animating') return null;
  return <AnimatedLogo animate={phase === 'animating'} />;
}

function AnimatedLogo({ animate }: { animate: boolean }) {
  const turn = useRef(new Animated.Value(0)).current;
  const pulse = useRef(new Animated.Value(0)).current;
  const sweep = useRef(new Animated.Value(0)).current;
  const fade = useRef(new Animated.Value(1)).current;
  const [fading, setFading] = useState(false);
  const loaded = useRef(0);
  const reduceMotion = useRef<boolean | null>(null);
  const reported = useRef(false);

  const maybeReady = () => {
    if (reported.current || loaded.current < LAYERS_TO_LOAD || reduceMotion.current === null) return;
    reported.current = true;
    startupSplash.overlayReady({ reduceMotion: reduceMotion.current, appState: AppState.currentState });
  };
  const onLayerLoad = () => {
    loaded.current += 1;
    maybeReady();
  };

  useEffect(() => {
    let live = true;
    AccessibilityInfo.isReduceMotionEnabled()
      .catch(() => false)
      .then((on) => {
        if (!live) return;
        reduceMotion.current = Boolean(on);
        maybeReady();
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (!animate) return undefined;
    const beats = pulseSteps();
    let t = 0;
    const pulseSeq: Animated.CompositeAnimation[] = [];
    for (const s of beats) {
      if (s.at > t) pulseSeq.push(Animated.delay(s.at - t));
      pulseSeq.push(Animated.timing(pulse, {
        toValue: s.level, duration: s.duration, useNativeDriver: true,
        easing: s.kind === 'rise' ? Easing.out(Easing.quad) : Easing.in(Easing.quad),
      }));
      t = s.at + s.duration;
    }
    const run = Animated.sequence([
      Animated.parallel([
        Animated.timing(turn, { toValue: 1, duration: ANIMATION_MS, easing: Easing.bezier(0.33, 0, 0.2, 1), useNativeDriver: true }),
        Animated.sequence(pulseSeq),
        Animated.sequence([
          Animated.delay(SHIMMER_START_MS),
          Animated.timing(sweep, { toValue: 1, duration: SHIMMER_MS, easing: Easing.inOut(Easing.cubic), useNativeDriver: true }),
        ]),
      ]),
      Animated.timing(fade, { toValue: 0, duration: OVERLAY_FADE_MS, easing: Easing.out(Easing.quad), useNativeDriver: true }),
    ]);
    const fadeAt = setTimeout(() => setFading(true), ANIMATION_MS);
    run.start(({ finished }) => {
      if (finished) startupSplash.animationFinished();
    });
    return () => {
      clearTimeout(fadeAt);
      run.stop();
    };
  }, [animate, turn, pulse, sweep, fade]);

  const ringTransform = [
    { rotate: turn.interpolate({ inputRange: [0, 1], outputRange: ['0deg', `${RING_TURN_DEG}deg`] }) },
    { scale: pulse.interpolate({ inputRange: [0, 1], outputRange: [1, 1 + PULSE_SCALE] }) },
  ];
  const glow = pulse.interpolate({ inputRange: [0, 1], outputRange: [0, PULSE_GLOW] });

  return (
    <Animated.View
      style={[StyleSheet.absoluteFill, styles.fill, { opacity: fade }]}
      pointerEvents={fading ? 'none' : 'auto'}
      accessible
      accessibilityLabel="LUX"
    >
      <View style={styles.logo}>
        <Animated.Image source={RING} style={[styles.layer, { transform: ringTransform }]} onLoad={onLayerLoad} fadeDuration={0} />
        <Animated.Image source={RING_BRIGHT} style={[styles.layer, { opacity: glow, transform: ringTransform }]} onLoad={onLayerLoad} fadeDuration={0} />
        <Image source={STATIC} style={styles.layer} onLoad={onLayerLoad} fadeDuration={0} />
        {SHIMMER_BANDS.map((b) => {
          // A skewed window slides across; the glint inside is counter-moved so it stays put.
          const x = sweep.interpolate({ inputRange: [0, 1], outputRange: [-2 * b.half - SIZE * 0.2, SIZE * 1.2] });
          const back = sweep.interpolate({ inputRange: [0, 1], outputRange: [2 * b.half + SIZE * 0.2, -SIZE * 1.2] });
          return (
            <Animated.View
              key={b.half}
              style={[styles.band, { width: 2 * b.half, opacity: b.opacity, transform: [{ translateX: x }, { skewX: SHIMMER_SKEW }] }]}
            >
              <Animated.Image source={GLINT} style={[styles.glint, { transform: [{ skewX: UNSKEW }, { translateX: back }] }]} fadeDuration={0} />
            </Animated.View>
          );
        })}
      </View>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: SPLASH_BACKGROUND },
  logo: { width: SIZE, height: SIZE },
  layer: { position: 'absolute', top: 0, left: 0, width: SIZE, height: SIZE },
  band: { position: 'absolute', top: 0, left: 0, height: SIZE, overflow: 'hidden' },
  glint: { position: 'absolute', top: 0, left: 0, width: SIZE, height: SIZE },
});
