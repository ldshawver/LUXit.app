/* The Android manifest must end up with exactly one live FirebaseMessagingService (Twilio's). */
jest.mock('expo/config-plugins', () => ({
  withAndroidManifest: (config: any, fn: (c: any) => any) => fn(config),
}), { virtual: true });
// eslint-disable-next-line @typescript-eslint/no-var-requires
const plugin = require('../plugins/withSingleFcmService');

test('removes expo-notifications FirebaseMessagingService via tools:node=remove', () => {
  const cfg = { modResults: { manifest: { $: {}, application: [{ service: [{ $: { 'android:name': 'com.twiliovoicereactnative.VoiceFirebaseMessagingService' } }] }] } } };
  const out = plugin(cfg);
  const services = out.modResults.manifest.application[0].service;
  expect(out.modResults.manifest.$['xmlns:tools']).toBe('http://schemas.android.com/tools');
  expect(services).toContainEqual({ $: { 'android:name': plugin.EXPO_FCM_SERVICE, 'tools:node': 'remove' } });
  expect(services.filter((s: any) => s.$['android:name'].includes('Twilio') || s.$['android:name'].includes('twilio'))).toHaveLength(1);
  // idempotent
  const again = plugin(out);
  expect(again.modResults.manifest.application[0].service.filter((s: any) => s.$['android:name'] === plugin.EXPO_FCM_SERVICE)).toHaveLength(1);
});
