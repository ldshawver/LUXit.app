import { getRecordingPermissionsAsync, requestRecordingPermissionsAsync } from 'expo-audio';
import { notificationPermission } from './pushRegistration';

/**
 * Microphone: granted, or (when `ask`) prompt if the OS still allows it.
 * Android needs RECORD_AUDIO granted at runtime before a call has audio; iOS
 * would prompt on first use, which is too late for a call answered from CallKit.
 */
export async function microphonePermission(ask: boolean): Promise<boolean> {
  const current = await getRecordingPermissionsAsync();
  if (current.granted || !ask || !current.canAskAgain) return current.granted;
  return (await requestRecordingPermissionsAsync()).granted;
}

/**
 * First-run prompts right after sign-in so the phone is reachable without a
 * trip to Settings: microphone (call audio) and notifications (Android 13+
 * cannot show Twilio's incoming-call notification without POST_NOTIFICATIONS;
 * texts / voicemail alerts on both platforms). Each OS prompt appears at most
 * once; afterwards this is a cheap no-op.
 */
export async function requestCallingPermissions(): Promise<{ microphone: boolean; notifications: boolean }> {
  const microphone = await microphonePermission(true).catch(() => false);
  const notifications = (await notificationPermission(true).catch(() => 'denied')) === 'granted';
  return { microphone, notifications };
}
