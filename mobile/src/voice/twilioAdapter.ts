import { Platform } from 'react-native';
import { Voice } from '@twilio/voice-react-native-sdk';
import type { VoiceSdk } from './sdk';
import { createPushKitBootstrap, type PushKitBootstrap } from './pushKitBootstrap';

/** One SDK instance per JS runtime, shared by the early bootstrap and the engine. */
let voice: Voice | null = null;
function getVoice(): Voice {
  if (!voice) voice = new Voice();
  return voice;
}

let iosBootstrap: PushKitBootstrap | null = null;
function getIosBootstrap(): PushKitBootstrap {
  if (!iosBootstrap) {
    const v = getVoice();
    iosBootstrap = createPushKitBootstrap(
      {
        setCallKitConfiguration: (cfg) => v.setCallKitConfiguration(cfg as never),
        setIncomingCallContactHandleTemplate: (t) => v.setIncomingCallContactHandleTemplate(t),
        initializePushRegistry: () => v.initializePushRegistry(),
      },
      (msg, extra) => console.log(`[voice] ${msg}`, extra ?? ''),
    );
  }
  return iosBootstrap;
}

/**
 * Called from index.ts before the app renders: lets a VoIP push that launched
 * the app be reported to CallKit without waiting for sign-in or the network.
 * No-op on Android (Twilio's FirebaseMessagingService is native). Idempotent.
 */
export function prepareIncomingCalls(): Promise<void> {
  if (Platform.OS !== 'ios') return Promise.resolve();
  return getIosBootstrap()();
}

/**
 * The real Twilio Voice React Native SDK behind the engine's VoiceSdk
 * interface. Event names passed through are the SDK's own string values.
 */
export function createTwilioSdk(): VoiceSdk {
  const v = getVoice();
  const sdk: VoiceSdk = {
    on: (event, listener) => v.on(event as Voice.Event, listener as never),
    register: (token) => v.register(token),
    unregister: (token) => v.unregister(token),
    connect: (token, opts) => v.connect(token, opts) as never,
    getCalls: () => v.getCalls() as never,
    getCallInvites: () => v.getCallInvites() as never,
    getDeviceToken: () => v.getDeviceToken(),
    getAudioDevices: () => v.getAudioDevices() as never,
  };
  if (Platform.OS === 'ios') {
    // Same memoized bootstrap as index.ts: never a second registry or provider.
    sdk.initializePushRegistry = () => getIosBootstrap()();
    sdk.showAvRoutePickerView = () => v.showAvRoutePickerView();
  }
  return sdk;
}
