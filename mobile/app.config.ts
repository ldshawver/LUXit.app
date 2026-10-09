import fs from 'fs';
import type { ConfigContext, ExpoConfig } from 'expo/config';

/**
 * Two installable variants so staging and production can sit side by side on
 * one handset:
 *   APP_VARIANT=staging     com.luxit.connect.staging  -> https://staging.luxit.app
 *   APP_VARIANT=production  com.luxit.connect          -> https://luxit.app
 *
 * APS_ENVIRONMENT: every EAS build is signed with an ad hoc (internal) or App
 * Store profile, and both use the *production* APNs environment, so eas.json
 * sets "production" for all profiles (including the dev client). "development"
 * (the default here) is only for a local Xcode run with a development profile,
 * which then needs the iOS *sandbox* Twilio Push Credential. The server picks
 * the matching credential from the apns_environment the app reports.
 *
 * Build numbers / versionCodes come from EAS (appVersionSource "remote").
 */
/**
 * Export compliance (docs/lux-connect-native/EXPORT_COMPLIANCE.md). The Twilio
 * Voice SDK bundles BoringSSL/WebRTC (DTLS-SRTP, TLS): standard, non-proprietary
 * encryption that is NOT only Apple's OS. Whether that counts as "exempt" for
 * ITSAppUsesNonExemptEncryption is not settled by leaving France out, so the
 * default is to write no value: App Store Connect then asks the export
 * questions for each build and the account holder answers them there.
 *   ITS_USES_NON_EXEMPT_ENCRYPTION=false|true  declare it once confirmed
 *   ITS_ENCRYPTION_EXPORT_COMPLIANCE_CODE      code Apple issues after approved
 *                                              documents (e.g. France); forces YES
 */
const exportComplianceCode = process.env.ITS_ENCRYPTION_EXPORT_COMPLIANCE_CODE || undefined;
const declaredNonExempt = ({ true: true, false: false } as Record<string, boolean>)[process.env.ITS_USES_NON_EXEMPT_ENCRYPTION ?? ''];
const usesNonExemptEncryption = exportComplianceCode ? true : declaredNonExempt;

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
    ...(process.env.EXPO_OWNER ? { owner: process.env.EXPO_OWNER } : {}),
    scheme: 'luxconnect',
    version: '1.0.0',
    orientation: 'portrait',
    userInterfaceStyle: 'automatic',
    icon: './assets/icon.png',
    ios: {
      bundleIdentifier: v.id,
      supportsTablet: false,
      entitlements: { 'aps-environment': apsEnvironment },
      ...(usesNonExemptEncryption === undefined ? {} : { config: { usesNonExemptEncryption } }),
      infoPlist: {
        NSMicrophoneUsageDescription: 'LUX Connect uses the microphone so the people you call can hear you.',
        NSPhotoLibraryUsageDescription: 'LUX Connect lets you attach photos to text messages.',
        NSCameraUsageDescription: 'LUX Connect lets you take photos to send in text messages.',
        ...(exportComplianceCode ? { ITSEncryptionExportComplianceCode: exportComplianceCode } : {}),
      },
    },
    android: {
      package: v.id,
      adaptiveIcon: { foregroundImage: './assets/adaptive-icon.png', backgroundColor: '#080A1A' },
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
