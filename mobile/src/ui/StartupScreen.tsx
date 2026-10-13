import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { AccessibilityInfo, Animated, AppState, Easing, Image as RNImage, StyleSheet, View } from 'react-native';
import { Image } from 'expo-image';
import * as Notifications from 'expo-notifications';
import * as SplashScreen from 'expo-splash-screen';
import { getController } from '../state/controller';
import { OVERLAY_FADE_MS, SPLASH_BACKGROUND, SPLASH_LOGO_WIDTH, createStartupSplash, hasCall, type StartupPhase } from './startupSplash';

export const startupSplash = createStartupSplash(SplashScreen, undefined, (msg, extra) => console.log(`[splash] ${msg}`, extra ?? ''));

// The supplied LUX animation, flattened onto black and looped
// (scripts/startup-animation/build_animation.py). Its first frame is the
// native splash image (LOGO), pixel for pixel.
const ANIMATION = require('../../assets/startup/lux.webp');
const LOGO = require('../../assets/splash-logo.png');

const SIZE = SPLASH_LOGO_WIDTH;

export function useStartupPhase(): StartupPhase {
  return useSyncExternalStore(startupSplash.subscribe, () => startupSplash.phase, () => startupSplash.phase);
}

/** Feeds boot, call and notification-tap state to the launch sequence (subscribes; never re-renders App). */
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
    // A tapped notification (cold launch or during the animation) skips the
    // minimum display time; useNotificationRouting opens the thread underneath.
    void Notifications.getLastNotificationResponseAsync()
      .then((resp) => {
        if (resp) startupSplash.expedite();
      })
      .catch(() => undefined);
    const tapSub = Notifications.addNotificationResponseReceivedListener(() => startupSplash.expedite());
    return () => {
      offSession();
      offVoice();
      tapSub.remove();
    };
  }, []);
}

/** Static black + logo (what shows while the session restores, if ever visible). */
export function StartupScreen() {
  return (
    <View style={styles.fill}>
      <RNImage source={LOGO} style={styles.logo} resizeMode="contain" accessibilityLabel="LUX" />
    </View>
  );
}

/** Above everything while the launch sequence needs it; unmounts at once for a call. */
export function StartupOverlay() {
  const phase = useStartupPhase();
  if (phase !== 'native' && phase !== 'animating' && phase !== 'leaving') return null;
  return <AnimatedLogo phase={phase} />;
}

function AnimatedLogo({ phase }: { phase: 'native' | 'animating' | 'leaving' }) {
  const image = useRef<Image>(null);
  const fade = useRef(new Animated.Value(1)).current;
  const displayed = useRef(false);
  const posterShown = useRef(false);
  const [poster, setPoster] = useState(true);
  const reduceMotion = useRef<boolean | null>(null);
  const reported = useRef(false);
  const started = useRef(false);

  const maybeReady = () => {
    if (reported.current || !displayed.current || !posterShown.current || reduceMotion.current === null) return;
    reported.current = true;
    startupSplash.overlayReady({ reduceMotion: reduceMotion.current, appState: AppState.currentState });
  };
  const onDisplay = () => {
    displayed.current = true;
    maybeReady();
  };
  const onPosterLoad = () => {
    posterShown.current = true;
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

  // Frame 0 is guaranteed at the hand-off on both platforms: a still poster of
  // it (LOGO, the native splash image) covers the animated view, which is
  // paused (autoplay off; Android's Glide may tick a frame before pausing).
  // Once the native splash is gone, startAnimating() restarts from frame 0
  // (Android) / plays on from the paused first frame (iOS), and only then is
  // the poster removed. If it never starts, the poster stays: no blank frame.
  useEffect(() => {
    if (phase === 'native' || started.current) return;
    started.current = true;
    Promise.resolve(image.current?.startAnimating())
      .then(() => setPoster(false))
      .catch((e) => console.log('[splash] startAnimating failed', String(e)));
  }, [phase]);

  useEffect(() => {
    if (phase !== 'leaving') return undefined;
    const run = Animated.timing(fade, { toValue: 0, duration: OVERLAY_FADE_MS, easing: Easing.out(Easing.quad), useNativeDriver: true });
    run.start(({ finished }) => {
      if (finished) startupSplash.animationFinished();
    });
    return () => run.stop();
  }, [phase, fade]);

  return (
    <Animated.View
      style={[StyleSheet.absoluteFill, styles.fill, { opacity: fade }]}
      pointerEvents={phase === 'leaving' ? 'none' : 'auto'}
      accessible
      accessibilityLabel="LUX"
    >
      <View style={styles.logo}>
        <Image
          ref={image}
          source={ANIMATION}
          style={styles.layer}
          contentFit="contain"
          autoplay={false}
          transition={null}
          priority="high"
          onDisplay={onDisplay}
          accessible={false}
        />
        {poster ? <RNImage source={LOGO} style={styles.layer} onLoad={onPosterLoad} fadeDuration={0} accessible={false} /> : null}
      </View>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: SPLASH_BACKGROUND },
  logo: { width: SIZE, height: SIZE },
  layer: { position: 'absolute', top: 0, left: 0, width: SIZE, height: SIZE },
});
