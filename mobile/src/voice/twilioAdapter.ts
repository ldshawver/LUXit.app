import { Platform } from 'react-native';
import { Voice } from '@twilio/voice-react-native-sdk';
import type { VoiceSdk } from './sdk';

/**
 * The real Twilio Voice React Native SDK behind the engine's VoiceSdk
 * interface. Event names passed through are the SDK's own string values.
 */
export function createTwilioSdk(): VoiceSdk {
  const voice = new Voice();
  const sdk: VoiceSdk = {
    on: (event, listener) => voice.on(event as Voice.Event, listener as never),
    register: (token) => voice.register(token),
    unregister: (token) => voice.unregister(token),
    connect: (token, opts) => voice.connect(token, opts) as never,
    getCalls: () => voice.getCalls() as never,
    getCallInvites: () => voice.getCallInvites() as never,
    getDeviceToken: () => voice.getDeviceToken(),
    getAudioDevices: () => voice.getAudioDevices() as never,
  };
  if (Platform.OS === 'ios') {
    sdk.initializePushRegistry = async () => {
      await voice.initializePushRegistry();
      await voice.setCallKitConfiguration({
        callKitIncludesCallsInRecents: true,
        callKitMaximumCallGroups: 1,
        callKitMaximumCallsPerCallGroup: 1,
        callKitSupportedHandleTypes: [2], // CXHandleType.phoneNumber
      } as never);
      // CallKit shows the caller name we pass as a <Parameter>, else the number.
      await voice.setIncomingCallContactHandleTemplate('${caller_name}');
    };
    sdk.showAvRoutePickerView = () => voice.showAvRoutePickerView();
  }
  return sdk;
}
