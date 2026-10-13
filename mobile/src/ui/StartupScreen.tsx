import React, { useEffect } from 'react';
import { Image, View } from 'react-native';
import * as SplashScreen from 'expo-splash-screen';
import { getController } from '../state/controller';
import { SPLASH_BACKGROUND, SPLASH_LOGO_WIDTH, createStartupSplash, splashCanRelease } from './startupSplash';

export const startupSplash = createStartupSplash(SplashScreen, undefined, undefined, (msg, extra) => console.log(`[splash] ${msg}`, extra ?? ''));

/** Fades the native splash out once boot is done or a call arrives (subscribes; never re-renders App). */
export function useReleaseSplashWhenReady(): void {
  useEffect(() => {
    const c = getController();
    const check = () => {
      if (splashCanRelease(c.snapshot().status, c.voice.snapshot())) startupSplash.release();
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

/** Same black + centered logo as the native splash, so the hand-off is seamless. */
export function StartupScreen() {
  return (
    <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: SPLASH_BACKGROUND }}>
      <Image
        source={require('../../assets/splash-logo.png')}
        style={{ width: SPLASH_LOGO_WIDTH, height: SPLASH_LOGO_WIDTH }}
        resizeMode="contain"
        accessibilityLabel="LUX"
      />
    </View>
  );
}
