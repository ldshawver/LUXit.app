# LUX Connect native app (iOS / Android)

A second-number phone app on the existing LUXit backend: calls through Twilio Voice (CallKit / PushKit on iOS, FCM + full-screen call notification on Android) and texts / MMS through the existing conversation system.

| Doc | What |
|---|---|
| [AUDIT.md](AUDIT.md) | Phase A: existing architecture, reuse, gaps, versions, risks, plan |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, auth, voice routing, state machine, messaging, multi-device, platform limits |
| [CREDENTIALS.md](CREDENTIALS.md) | Apple / Google / Twilio / Expo accounts and server env needed |
| [MESSAGING_10DLC.md](MESSAGING_10DLC.md) | Carrier registration rules, what the app enforces, onboarding |
| [DEVICE_ACCEPTANCE.md](DEVICE_ACCEPTANCE.md) | Test results to date and the physical-device checklist |
| [RELEASE.md](RELEASE.md) | TestFlight / Play internal, production requirements, rollback |

## Code map

- Backend:
  - `mobile_api.py` (`/api/mobile/v1`)
  - `services/mobile_auth.py`, `services/mobile_voice.py`, `services/native_push.py`, `services/lux_numbers.py`
  - `migrations/20261009_lux_connect_native.sql`
  - `scripts/check_messaging_registration.py`
  - `tests/test_lux_connect_native_api.py`
- App: `mobile/` (Expo SDK 57, RN 0.86.3, TypeScript, `@twilio/voice-react-native-sdk` 1.8.0)
  - `src/voice/VoiceEngine.ts`: calling state machine
  - `src/api/http.ts`: bearer client
  - `src/messaging/outbox.ts`, `src/messaging/sync.ts`
  - `src/screens/*`: Keypad · Recents · Messages · Contacts · Voicemail, Settings, call screen
  - `plugins/withSingleFcmService.js`

## Develop

```bash
cd mobile && npm ci
npm run typecheck && npm test           # 58 tests
npx expo-doctor                         # 21/21
npx eas build --profile staging-dev     # dev client on a real phone (needs credentials)
```

Backend tests: `python -m pytest tests/test_lux_connect_native_api.py`.
