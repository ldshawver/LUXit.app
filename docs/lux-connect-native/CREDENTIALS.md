# LUX Connect native: credentials and accounts

Nothing below exists yet for LUX Connect. The Twilio account's existing push credentials (`CR470b87…` "Talkyto Voip 2026", `CR69c6fa…`, `CRfd4bf5…`) belong to a different app and **must not be reused**: a Push Credential is bound to one bundle id / Firebase project.

Secrets never go in the app or in git. The app holds only bearer tokens it receives at sign-in.

## Apple (Apple Developer Program, team admin)

0. **Bundle ID decision (open, 2026-10-09):** the app uses `com.luxit.connect` / `com.luxit.connect.staging`. An earlier planning doc proposed `app.luxit.connect` ("or similar"); nothing on this server shows which one is actually registered in the Apple Developer account. If `app.luxit.connect` is already registered (or has an App Store Connect record), switch the two ids in `mobile/app.config.ts` `VARIANTS` before the first build. A bundle id cannot be renamed once an App Store Connect record uses it. The Android package and Firebase apps must use the same final ids.
1. **Identifiers:** create App IDs `com.luxit.connect` (production) and `com.luxit.connect.staging`, each with *Push Notifications* enabled.
2. **VoIP Services certificate:** one per App ID. Export it as .p12 and convert it to PEM cert + key.
   - Create Twilio Push Credentials of type APN from it (Console → Voice → Credentials, or `twilio api:chat:v2:credentials:create`):
     - `LUX Connect iOS (production)`: sandbox = false. **Required, including for the first EAS dev-client build**: EAS internal (ad hoc) and store builds all use the production APNs environment.
     - `LUX Connect iOS (sandbox)`: sandbox = true. **Optional**: only for a local Xcode run signed with a development profile.
   - Server env: `TWILIO_PUSH_CREDENTIAL_SID_IOS` (required), `TWILIO_PUSH_CREDENTIAL_SID_IOS_SANDBOX` (optional).
   - When a phone switches between a sandbox and a production build, delete and reinstall the app: PushKit can hand back the previous environment's token, which Twilio then rejects as an invalid device token.
3. **APNs Auth Key (.p8)** for text / voicemail alerts. Server env:
   - `LUXIT_APNS_KEY_FILE`: path to the .p8, mode 600, outside the repo
   - `LUXIT_APNS_KEY_ID`, `LUXIT_APNS_TEAM_ID`
   - `LUXIT_APNS_TOPIC`: the bundle id; staging and production each use their own
   - The `h2` Python package (now in `requirements.txt`) must be installed in that environment's venv.
4. **App Store Connect:** app records for both bundle ids. Put the numeric `ascAppId` into `mobile/eas.json` (`submit`). Add TestFlight internal testers.
5. **Privacy:**
   - Microphone, photos and camera strings are already in `app.config.ts`.
   - App Privacy questionnaire: phone number, contacts (from LUXit, not the device), messages content, audio (calls only), diagnostics.
   - Encryption export: standard HTTPS only (`ITSAppUsesNonExemptEncryption=false`).

## Google (Firebase and Play Console)

1. **Firebase project** (e.g. `lux-connect`): add Android apps `com.luxit.connect` and `com.luxit.connect.staging` and download `google-services.json`.
   - Give it to EAS as a file variable for each environment the profiles use (`staging-dev` → development, `staging` / `testflight-staging` → preview, `production` → production):
     `eas env:create --name GOOGLE_SERVICES_JSON --type file --value ./google-services.json --environment development --environment preview --visibility secret`
     `app.config.ts` reads `GOOGLE_SERVICES_JSON`.
   - No iOS Firebase app is needed. iOS alerts go to APNs directly and VoIP goes through Twilio.
2. **Twilio Android Push Credential** (type FCM) from the Firebase project's FCM server credential (Twilio supports FCM HTTP v1 service-account credentials). Server env: `TWILIO_PUSH_CREDENTIAL_SID_ANDROID`.
3. **Server FCM sender:** a service-account JSON with the *Firebase Cloud Messaging API Admin* role. Server env: `LUXIT_FCM_SERVICE_ACCOUNT_FILE` (path, mode 600).
4. **Play Console:**
   - App records for both package names and the internal testing track.
   - Upload key / Play App Signing (EAS can manage the keystore).
   - **Declarations:**
     - Full-screen intent: "calling app" category.
     - Foreground service type *microphone*: show the incoming / active call use.
     - Data safety form.

## Twilio

- **Push Credentials:** the three above. Optionally rename the TwiML App to show it serves the PWA and native (same `TWILIO_TWIML_APP_SID`; the app reuses `/twilio/voice/pwa-outbound`).
- **SMS on staging (re-audited 2026-10-09, read-only).** The earlier "repoint +18302591310's SMS URL" option **would not work**:
  - +18302591310 is in Messaging Service `MGa8e0cd…` "Default Messaging Service for Conversations" with **"use inbound webhook on number" = off**, so its inbound texts go to the **service** URL `https://luxit.app/twilio/sms/inbound` (production). The number's own SMS URL (`/adiken/sms/inbound`) is ignored.
  - That service also holds production's +19165989519 and +18664868340, and carries the account's only **VERIFIED** A2P 10DLC campaign (low volume). Changing the service's inbound URL or its "use number webhook" flag changes production routing. Not done.
  - Outbound from +18302591310 *is* A2P-registered (through that service), so staging can **send** from it once its gate allows.

  **Recommended (full fidelity, needs your approval + purchase):**
  1. Buy one local number for staging. Do **not** add it to `MGa8e0cd…`.
  2. Create a new Messaging Service "LUX Connect STAGING" with inbound + status callback → `https://staging.luxit.app/twilio/sms/inbound` / staging status route, and add the new number to it. Set the number's voice URL to `https://staging.luxit.app/twilio/voice/inbound`.
  3. Register a second A2P campaign on the existing verified brand for that service (low-volume mixed). Until it is approved, **inbound** SMS/MMS and voice work immediately; **outbound** texts from that number are carrier-blocked (Twilio 30034).
  4. In staging's DB, point the QA tenant's `TwilioPhoneNumber` at the new number and record its messaging registration.

  **Interim (no purchase, outbound only):** set staging to the new gate mode
  `LUXIT_TWILIO_MODE=allowlist` + `LUXIT_TWILIO_ALLOWED_RECIPIENTS=+1<your test phones>`. Staging may then send SMS/MMS **only** to those numbers (calls, number/webhook config and every other Twilio API stay blocked; the mode is refused in production). Send from +18302591310. **Do not reply** from the test phones: replies land in production's inbound handler (and may trigger production auto-replies). This needs your approval and a staging restart; it is not enabled.

## Expo / EAS

- An EAS account. If the project should belong to an Expo organization, set `EXPO_OWNER` to it.
- In `mobile/`, run `eas login` and then `eas init`. Put the printed project id into `EAS_PROJECT_ID` (or hard-code it in `app.config.ts` `extra.eas.projectId`; it is not a secret).
- For CI or for Claude to run builds: an `EXPO_TOKEN` (Account settings → Access tokens).
- EAS CLI 19.1 or later (24.12.1 is current). `eas.json` validates against EAS's own schema.
- iOS internal builds: register each test iPhone with `eas device:create`. Then run the first `eas build` **interactively**, signed in to Apple, so EAS can create the ad hoc profile containing those devices. Add `--refresh-ad-hoc-provisioning-profile` to later non-interactive builds.
- Build numbers and versionCodes are managed by EAS (`appVersionSource: remote`, `autoIncrement` on store profiles).

| Profile | Variant | APNs | EAS environment | Output |
|---|---|---|---|---|
| `staging-dev` | staging | production | development | dev client: ad hoc IPA + APK |
| `staging` | staging | production | preview | ad hoc IPA + APK |
| `testflight-staging` | staging | production | preview | TestFlight / Play internal |
| `production` | production | production | production | stores |

First physical test: `eas build --profile staging-dev --platform all`.

## Server environment summary

```
# Twilio Voice push (per platform); absent => app says "Outgoing calls only"
TWILIO_PUSH_CREDENTIAL_SID_IOS=CR…
TWILIO_PUSH_CREDENTIAL_SID_IOS_SANDBOX=CR…   # optional, local Xcode builds only
TWILIO_PUSH_CREDENTIAL_SID_ANDROID=CR…
# Alert push; absent => no native alert push (calls unaffected)
LUXIT_APNS_KEY_FILE=/etc/luxit-staging/apns/AuthKey_XXXX.p8
LUXIT_APNS_KEY_ID=XXXX
LUXIT_APNS_TEAM_ID=YYYY
LUXIT_APNS_TOPIC=com.luxit.connect.staging
LUXIT_FCM_SERVICE_ACCOUNT_FILE=/etc/luxit-staging/fcm/service-account.json
# Optional
LUX_CONNECT_MIN_APP_VERSION=1.0.0
```

`GET /api/mobile/v1/me` → `push: {apns, fcm}` and `calling.incoming` show at a glance what the server can do.
