/**
 * Keep exactly one FirebaseMessagingService in the merged Android manifest:
 * the Twilio Voice SDK's. expo-notifications ships its own service for the
 * same MESSAGING_EVENT intent; with two receivers Android delivers each FCM
 * message to only one of them, so a call invite could land in the Expo
 * service and the phone would never ring.
 *
 * Text-message alerts are sent as FCM *notification* messages, which the OS
 * displays itself while the app is backgrounded, so no second service is
 * needed. expo-notifications still provides channels, badges, local
 * notifications and permission prompts.
 */
const { withAndroidManifest } = require('expo/config-plugins');

const EXPO_FCM_SERVICE = 'expo.modules.notifications.service.ExpoFirebaseMessagingService';

module.exports = function withSingleFcmService(config) {
  return withAndroidManifest(config, (cfg) => {
    const manifest = cfg.modResults.manifest;
    manifest.$ = manifest.$ || {};
    manifest.$['xmlns:tools'] = manifest.$['xmlns:tools'] || 'http://schemas.android.com/tools';
    const app = manifest.application && manifest.application[0];
    if (!app) throw new Error('withSingleFcmService: no <application> in AndroidManifest');
    app.service = (app.service || []).filter((s) => s.$['android:name'] !== EXPO_FCM_SERVICE);
    app.service.push({ $: { 'android:name': EXPO_FCM_SERVICE, 'tools:node': 'remove' } });
    return cfg;
  });
};
module.exports.EXPO_FCM_SERVICE = EXPO_FCM_SERVICE;
