# LUX Connect native app: Phase A audit

Audited 2026-10-09 against the live environment, not the brief's assumptions.

## 0. Environment as verified

| Item | Brief | Verified |
|---|---|---|
| Production | `645dd65` | `645dd6530fdc…`, PID 2393265 since 2026-10-06 08:38 UTC, 0 restarts, release dir `/root/luxit-prod-645dd65-20261006` |
| Tuya worker | untouched | PID 1497252 since 2026-08-24, 0 restarts |
| Phone overhaul candidate | `32814a7` | `32814a7f44b9…`, branch `feat/phone-native-ux` |
| Staging | `3574ec7` | `3574ec71d1c4…` (before this work) |
| Staging number | +18302591310 | voice webhook → staging; **SMS webhook → production** (`https://luxit.app/adiken/sms/inbound`) |

Toolchain on this VPS: Node 24, **no Java / Android SDK, no macOS/Xcode**, 2 CPUs, about 1–1.7 GB free RAM. Native binaries therefore have to be built by EAS Build (cloud) or on a Mac. Everything else (TypeScript, tests, `expo prebuild`, Metro bundles) runs here.

## 1. Existing voice and messaging architecture

- **One Flask app** (gunicorn gthread 4×8) serves the CRM, the PWA (`inbox_pwa.py`) and Twilio webhooks (`twilio_sms.py`). PostgreSQL. All Twilio traffic goes through one account shared by staging and production.
- **Auth:** Flask-Login cookie session plus Flask-WTF CSRF on every POST. No token auth existed.
- **Voice:**
  - Twilio Voice JS SDK in the PWA.
  - Token from `/api/phone/voice-token` (identity HMAC-scoped per company/user, or per device when the tenant requires device approval).
  - Inbound `/twilio/voice/inbound` → `ring_pwa` builds one `<Dial>` with a `<Client>` per eligible user (Receive Calls ON ∩ Available), carrying the `call_log_id` / `from_number` / `caller_name` parameters, then falls back to voicemail through `/twilio/voice/no-answer`.
  - Outbound uses a TwiML App → `/twilio/voice/pwa-outbound`, which verifies the HMAC identity and chooses the caller ID **server-side** from the user's permitted numbers.
  - Call logs, voicemail, recordings and transcription are already stored (`TwilioCallLog`, `CallEvent`, voicemail audio proxy).
- **Messaging:**
  - Inbound `/twilio/sms/inbound` (signature-validated, fail-closed) stores `TwilioConversation` / `TwilioMessage` (including inbound MMS `media_urls`, served through an authenticated proxy).
  - Outbound goes through the single function `sendConversationSms`, which enforces STOP / opt-out, the license check, the canonical sender and the `LUXIT_TWILIO_MODE` gate.
  - Manual sends are idempotent (`SmsSendIdempotencyRecord`), and delivery-status callbacks are guarded against regressions.
- **Accounts:**
  - `UserCompanyAccess` holds membership, role, Mobile Inbox access, Receive Calls and availability.
  - `TwilioPhoneNumber` + `PhoneNumberUserPermission` hold number ownership and per-user call/SMS/voicemail rights.
  - `PWADevice` covers browser devices; `PushSubscription` covers Web Push.
- **Realtime:** SSE `/api/inbox/stream`; Web Push for the PWA.

## 2. Already implemented (reused as-is)

- Tenant isolation and per-number permission filters (`comms_permissions`).
- Voice authorization gates (Receive Calls, Away, number permission).
- Server-side caller ID.
- Inbound routing with simultaneous ring and first-answer-wins (Twilio `<Dial>` with multiple `<Client>`s).
- Voicemail, call history and transcription.
- Conversations, contact resolution and inbound MMS.
- STOP / consent enforcement in the single send path.
- Send idempotency and status-callback integrity.
- Per-user notification decisions (Away, quiet hours, preferences) and badge counts.
- The live event stream.

## 3. Missing for a native second-number app

1. Non-cookie auth for a mobile client: bearer sessions, refresh, revocation.
2. Native device registry and push tokens (APNs / FCM), plus logout and remote revoke.
3. A native Voice token carrying a Twilio **Push Credential**, a per-device identity, and inclusion of native handsets in `<Dial>` and outbound verification.
4. Native alert push for texts, missed calls and voicemail.
5. Outbound MMS (the send path was text-only) and media hosting for Twilio to fetch.
6. Idempotent send to a *new* number (the PWA's new-conversation route had no idempotency key).
7. Incremental sync after reconnect.
8. Carrier messaging registration state (10DLC / toll-free) per number.
9. Number assignment as a JSON API (web-form only before).
10. The native app itself.

## 4. Recommended versions (verified on npm 2026-10-09)

| Component | Version | Why |
|---|---|---|
| `@twilio/voice-react-native-sdk` | **1.8.0** (2026-09-24) | Latest stable; first release with official Expo config-plugin support; verified by Twilio against Expo SDK 52–57 and RN 0.86.3. 2.0 is preview only. Bundles TwilioVoice iOS 6.13.6. |
| Expo SDK | **57.0.27** | Newest SDK in Twilio's verified matrix; EAS Build gives iOS/Android binaries without local Xcode / Android SDK. |
| React Native / React | **0.86.3 / 19.2.3** | Pinned by Expo 57; the exact RN version Twilio verified. New Architecture (mandatory in Expo 57). |
| TypeScript | 6.0.x | Expo 57 expectation (`expo-doctor` 21/21). |
| iOS minimum | 16.4 | Expo 57 floor. |
| Android minimum | API 26 | Twilio needs 24; 26 avoids legacy notification-channel paths. |

**React Native was kept as the brief asked.** The only material risk is the Android calling integration: Twilio's RN SDK presents incoming calls with a foreground service and a full-screen-intent notification, **not** Android Telecom `ConnectionService`. That is a supported pattern, but it is not the system dialer UI. A self-managed `ConnectionService` would need a custom native module (see ARCHITECTURE.md §7).

## 5. Required backend changes (implemented)

- New `mobile_api.py` (`/api/mobile/v1`, bearer-only, CSRF-exempt as a whole) that **mounts the existing PWA views** under the prefix and adds: sign-in, refresh, logout, me, devices, native voice token, MMS upload, unified idempotent send, sync, and number admin.
- New services: `mobile_auth`, `mobile_voice`, `native_push`, `lux_numbers`.
- Small edits to existing code:
  - `_voice_token_gate()` extracted so the PWA and the app share one gate.
  - `ring_pwa` appends native identities to the same `<Dial>`.
  - `pwa-outbound` verifies native identities.
  - `sendConversationSms` / `send_with_idempotency` take an optional `media_urls`.
  - The push fan-out also queues native pushes.
  - The authenticated `/api/inbox/media-uploads/<token>` route was added.

## 6. Database migrations

`migrations/20261009_lux_connect_native.sql` contains **new tables only**:
- `mobile_device`
- `mobile_session`
- `mobile_auth_attempt`
- `mobile_media_upload`
- `phone_number_messaging_registration`

No existing table, column, default or row is touched.

## 7. Apple and Google credentials required (none exist yet for LUX Connect)

The account's only push credentials belong to another app ("Talkyto") and **must not be reused**. Full steps are in CREDENTIALS.md.

- **Apple Developer Program:**
  - App IDs `com.luxit.connect` / `com.luxit.connect.staging` with Push Notifications.
  - A VoIP Services certificate (for the Twilio iOS Push Credential, sandbox and production).
  - An APNs Auth Key (.p8) for alert pushes.
  - App Store Connect records.
  - TestFlight testers.
- **Google:**
  - A Firebase project with Android apps for both package names (`google-services.json`).
  - An FCM service account JSON for the server.
  - The FCM server credential for the Twilio Android Push Credential.
  - Play Console app records, the internal testing track, and the `USE_FULL_SCREEN_INTENT` declaration.
- **Twilio:** 3 Push Credentials (iOS sandbox, iOS production, Android), with their SIDs in server env.
- **Expo:** an EAS account / project (EXPO_TOKEN) for cloud builds.

## 8. Estimated complexity

The backend and app code delivered here is roughly 2.5k lines of server code and tests, plus 5k lines of app code and tests. The remaining work is mostly credentials, store setup and on-device validation: about 2–4 working days once the Apple and Google accounts are available. Budget a further 1–2 weeks for device-matrix fixes (Android OEM battery behaviour, Bluetooth routing, CallKit edge cases) before production.

## 9. Security and compliance risks

| Risk | Mitigation in this build |
|---|---|
| Bearer token theft | Opaque tokens; only SHA-256 hashes stored; 1 h access; rotating 60-day refresh with reuse detection; device revoke; every request revalidates the user, membership and device; tokens kept in the iOS Keychain / Android Keystore (`AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY`). |
| CSRF on a CSRF-exempt prefix | Cookies are never read on `/api/mobile/v1` (identity set only from the bearer) and `Set-Cookie` is stripped; tested. |
| Password guessing | 10 failures per identifier / 30 per IP in 15 min → 429. |
| Cross-tenant access | All data routes are the existing tenant-filtered views; native-specific routes filter by session company; tested against two tenants. |
| A handset ringing for a previous user | Push-token / VoIP-token reassignment retires the old row; signed-out or revoked devices are never put in `<Dial>` (verified with a real call). |
| Spoofed outbound caller | Native identity verified by HMAC against an active device row; caller ID is chosen by the server. |
| MMS media exposure | Public URL is 256-bit random and valid for 4 h; readers use authenticated routes. |
| STOP / consent | Unchanged single send path; verified that a STOP'd conversation is refused before any Twilio client is built. |
| A2P 10DLC | Registration status recorded per number; app-originated texts are refused only when status is known `not_registered` / `pending`; `unknown` never blocks (existing behaviour preserved). |
| Push content on lock screen | Message previews appear in notifications. Android channel visibility is PRIVATE; a "hide previews" option is a follow-up. |

## 10. Implementation plan (as executed)

| Phase | Work | Status |
|---|---|---|
| B | Mobile auth API, Expo project, secure storage, API client | done |
| C | Native voice token, ring/outbound routing, VoiceEngine, CallKit/FCM wiring, call UI | done; needs credentials and devices |
| D | Unified idempotent send, MMS, outbox, sync, alert push | done; APNs/FCM need credentials |
| E | Devices, revoke/approve, number assignment, 10DLC status | done |
| F | Unit, integration and live staging tests; prebuild; Metro bundles | done (see DEVICE_ACCEPTANCE.md) |
| G | EAS profiles, TestFlight / Play internal steps, rollback | documented; blocked on credentials |
