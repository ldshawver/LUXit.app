import fs from 'fs';
import type { ConfigContext, ExpoConfig } from 'expo/config';

/**
 * Two installable variants so staging and production can sit side by side on
 * one handset:
 *   APP_VARIANT=staging     com.luxit.connect.staging  -> https://staging.luxit.app
 *   APP_VARIANT=production  com.luxit.connect          -> https://luxit.app
 *
 * APS_ENVIRONMENT must be "production" for TestFlight / App Store builds or the
 * PushKit VoIP token is issued against the APNs sandbox and incoming calls never
 * arrive (eas.json sets it per profile). The server picks the matching Twilio
 * push credential from the apns_environment the app reports.
 */
const variant = (process.env.APP_VARIANT ?? 'staging') as 'staging' | 'production';
const apsEnvironment = (process.env.APS_ENVIRONMENT ?? 'development') as 'development' | 'production';

const VARIANTS = {
  staging: { name: 'LUX Connect (Staging)', id: 'com.luxit.connect.staging', api: 'https://staging.luxit.app' },
  production: { name: 'LUX Connect', id: 'com.luxit.connect', api: 'https://luxit.app' },
} as const;

export default ({ config }: ConfigContext): ExpoConfig => {
  const v = VARIANTS[variant];
  const googleServicesFile = [process.env.GOOGLE_SERVICES_JSON, './google-services.json'].find((f) => f && fs.existsSync(f));
  return {
    ...config,
    name: v.name,
    slug: 'lux-connect',
    scheme: 'luxconnect',
    version: '1.0.0',
    orientation: 'portrait',
    userInterfaceStyle: 'automatic',
    icon: './assets/icon.png',
    ios: {
      bundleIdentifier: v.id,
      buildNumber: '1',
      supportsTablet: false,
      entitlements: { 'aps-environment': apsEnvironment },
      infoPlist: {
        NSMicrophoneUsageDescription: 'LUX Connect uses the microphone so the people you call can hear you.',
        NSPhotoLibraryUsageDescription: 'LUX Connect lets you attach photos to text messages.',
        NSCameraUsageDescription: 'LUX Connect lets you take photos to send in text messages.',
        ITSAppUsesNonExemptEncryption: false,
      },
    },
    android: {
      package: v.id,
      adaptiveIcon: { foregroundImage: './assets/adaptive-icon.png', backgroundColor: '#080A1A' },
      versionCode: 1,
      // Firebase project config (EAS file secret GOOGLE_SERVICES_JSON). Required
      // for Android incoming calls; without it the build still produces an app
      // that can sign in, text and place calls.
      ...(googleServicesFile ? { googleServicesFile } : {}),
      permissions: [
        'android.permission.RECORD_AUDIO',
        'android.permission.POST_NOTIFICATIONS',
        'android.permission.USE_FULL_SCREEN_INTENT',
        'android.permission.FOREGROUND_SERVICE',
        'android.permission.FOREGROUND_SERVICE_MICROPHONE',
        'android.permission.BLUETOOTH_CONNECT',
        'android.permission.WAKE_LOCK',
      ],
      blockedPermissions: ['android.permission.READ_CONTACTS', 'android.permission.READ_PHONE_STATE'],
    },
    plugins: [
      ['@twilio/voice-react-native-sdk', {
        apsEnvironment,
        microphoneUsageDescription: 'LUX Connect uses the microphone so the people you call can hear you.',
      }],
      ['expo-notifications', { color: '#0B5FFF', defaultChannel: 'messages' }],
      // The Twilio Voice SDK must be the only FirebaseMessagingService so call
      // invites are never swallowed; see plugins/withSingleFcmService.js.
      './plugins/withSingleFcmService',
      ['expo-image-picker', { photosPermission: 'LUX Connect lets you attach photos to text messages.' }],
      ['expo-build-properties', { android: { minSdkVersion: 26 }, ios: { deploymentTarget: '16.4' } }],
      'expo-secure-store',
    ],
    extra: {
      variant,
      apiBaseUrl: process.env.LUX_API_BASE_URL ?? v.api,
      apsEnvironment: apsEnvironment === 'production' ? 'production' : 'sandbox',
      eas: { projectId: process.env.EAS_PROJECT_ID },
    },
  };
};
