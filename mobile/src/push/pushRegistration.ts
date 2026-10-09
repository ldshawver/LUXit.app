import { Platform } from 'react-native';
import * as Notifications from 'expo-notifications';
import type { LuxApi } from '../api/endpoints';
import type { VoiceSdk } from '../voice/sdk';

/**
 * Alert pushes (texts, missed calls, voicemail).
 *   iOS:     the APNs device token, sent straight to Apple by our server.
 *   Android: the FCM token the Twilio Voice SDK already holds (one token per
 *            app); texts arrive as FCM notification messages.
 * Incoming *calls* never depend on this: they use Twilio's VoIP push binding.
 */
export async function ensureNotificationChannels(): Promise<void> {
  if (Platform.OS !== 'android') return;
  await Notifications.setNotificationChannelAsync('messages', {
    name: 'Text messages', importance: Notifications.AndroidImportance.HIGH, sound: 'default',
    vibrationPattern: [0, 250, 150, 250], lockscreenVisibility: Notifications.AndroidNotificationVisibility.PRIVATE,
  });
  await Notifications.setNotificationChannelAsync('calls', {
    name: 'Missed calls and voicemail', importance: Notifications.AndroidImportance.HIGH, sound: 'default',
  });
}

export async function notificationPermission(ask: boolean): Promise<'granted' | 'denied' | 'undetermined'> {
  const current = await Notifications.getPermissionsAsync();
  if (current.granted || !ask || !current.canAskAgain) return current.granted ? 'granted' : current.status;
  const next = await Notifications.requestPermissionsAsync({ ios: { allowAlert: true, allowBadge: true, allowSound: true } });
  return next.granted ? 'granted' : next.status;
}

export async function registerAlertPush(api: LuxApi, sdk: VoiceSdk, apnsEnvironment: 'sandbox' | 'production'): Promise<boolean> {
  const perm = await notificationPermission(false);
  if (perm !== 'granted') return false;
  if (Platform.OS === 'ios') {
    const t = await Notifications.getDevicePushTokenAsync();
    await api.setPushToken(String(t.data), 'apns', apnsEnvironment);
  } else {
    const token = await sdk.getDeviceToken();
    if (!token) return false;
    await api.setPushToken(token, 'fcm');
  }
  return true;
}

export function setBadge(count: number): void {
  Notifications.setBadgeCountAsync(Math.max(0, count)).catch(() => undefined);
}
