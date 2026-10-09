# LUX Connect native: test results and physical-device acceptance

**Nothing here proves real background or locked-screen calling.** That needs the Apple and Google push credentials and real handsets. Simulators cannot receive PushKit at all. The matrix says exactly which layer each result comes from.

## Automated and staging results (2026-10-09)

| Layer | Result |
|---|---|
| Backend contract `tests/test_lux_connect_native_api.py` | **44 / 44 pass** (both the prod-lineage branch and the staging branch) |
| Existing voice/PWA/SMS/Twilio/consent suites (592 tests) | 558 pass, **34 fail — the identical 34 fail on the untouched baseline 32814a7; 0 regressions** |
| Mobile unit tests (jest) | **58 / 58 pass**: VoiceEngine 31, API client 8, outbox 8, sync 5, utils 4, manifest plugin 1 |
| TypeScript 6 strict typecheck | clean |
| `expo-doctor` | 21 / 21 |
| `expo prebuild` (iOS + Android) | succeeds; Twilio Kotlin proxies wired, Expo FCM service removed, Google Services applied, `UIBackgroundModes=[audio, voip]`, `aps-environment=production` |
| Metro production bundles | iOS and Android Hermes bundles compile (2.8 MB each) |
| Staging migration | applied through the canonical runner; re-run is a no-op; 5 new tables |
| Live staging API (through nginx) | 13 / 14. The one miss is staging data: company 1's Twilio account has no stored credentials, so texting is refused before sending, exactly as the PWA's send is |
| Live staging real call | 5 / 5 plus Twilio-side verification (below) |
| Live staging revocation | 2 / 2 |

### Live staging details

- **S1–S4:**
  - Bearer sign-in sets no cookie.
  - `/me` shows number +18302591310, texting `registered`, and calling "Outgoing calls only" (honest: no push credential yet).
  - The native token has the per-device identity `luxit_c1_u47_d1_…` and the outbound app.
  - Refresh rotates and the old token gets 401.
- **S5:** SSE through nginx with bearer auth delivers `event: connected`.
- **S7:** MMS upload served to Twilio at `/mobile-media/<token>` (200, image/png); the authenticated route refuses anonymous readers.
- **S9–S11:** a signed synthetic inbound MMS webhook is stored **once** even when delivered twice. `/sync` returns the conversation, and media is only exposed through the authenticated proxy.
- **V0–V4 (real Twilio):** a call placed **as the native per-device identity**:
  - passed `pwa-outbound` verification;
  - dialed out with caller ID **+18302591310**;
  - rang the staging number in 1.85 s and was answered on the PWA (user 14);
  - carried **two-way audio**: 11.5 KB / 9.3 KB per 3 s on the PWA side, 11.3 KB / 9.0 KB on the native side;
  - after the native side hung up, the PWA updated in **207 ms**.

  Twilio's records show the outbound parent `client:luxit_c1_u47_d1_… → completed`. The inbound `<Dial>` rang **four legs in the same second**: the native handset identity plus three PWA identities. One answered and the other three were cancelled. Staging call logs: outbound row `from +18302591310, user 47`; inbound row answered by user 14.
- **L1–L2:** sign-out returns 401 for both access and refresh. A real call placed afterwards dialed only the three PWA identities, **not** the signed-out handset.

No real SMS was sent (the staging Twilio gate is closed). Every Twilio call in these tests was on the staging number and was ended immediately.

## Brief's test table: status

| Test | Required result | Status |
|---|---|---|
| iPhone locked | Native calling interface | **Needs device + credentials** (CallKit wiring present; PushKit not testable off-device) |
| Android locked | Notification / full-screen UI | **Needs device + credentials** |
| App backgrounded | Calls still available | **Needs device**. Server side verified: registered handsets are dialed. |
| Incoming call | Answer / Decline work | Engine verified (unit); real answer path verified on PWA; **native UI needs device** |
| Outgoing call | Correct caller ID | **Verified on real Twilio** as the native identity: +18302591310 |
| Two-way audio | Both parties hear | **Verified on real Twilio** (bytes both ways); handset audio needs device |
| Hang up | Ends immediately | **Verified** (207 ms remote update) |
| Incoming SMS | Saved + notification | Saved once (staging webhook); push needs APNs/FCM credentials |
| Outgoing SMS | Sent from assigned number | Server path + idempotency verified (unit); **real send blocked** by staging gate and staging SMS webhook (see CREDENTIALS.md) |
| Incoming MMS | Attachment received | **Verified on staging** (stored, proxied) |
| Outgoing MMS | Attachment delivered | Upload + Twilio-fetchable URL verified on staging; real delivery blocked as above |
| Network interruption | Auto-recovery | Engine / sync / outbox verified (unit); needs device for real radio changes |
| Token expiration | Auto-refresh | Verified (unit + staging refresh rotation) |
| Multiple devices | Correct routing | **Verified on real Twilio**: simultaneous ring, first answer wins, others cancelled |
| Duplicate callbacks | No duplicates | **Verified on staging** (inbound SMS twice → 1 row); call log idempotent per CallSid (unit) |
| Logout | Registration revoked | **Verified on staging with a real call** |
| Account isolation | No cross-account access | Verified (unit, two tenants); staging has no second tenant with conversations to probe |

## Physical-device checklist (run on staging builds)

Prerequisites:
- the credentials in CREDENTIALS.md;
- an EAS `staging` build installed from TestFlight / Play internal;
- a test user with a number assigned;
- a second phone to place calls from.

Record pass/fail, device model and OS for each.

**iPhone (iOS 17/18/26)**
1. Fresh install → sign in → allow microphone and notifications → Keypad shows **Ready to receive calls** within 5 s.
2. Lock the phone. Call the number from another phone. CallKit shows "LUX Connect" with the caller's name/number. Slide to answer → audio both ways → the in-app call screen appears when you unlock.
3. App force-quit (swipe away) → call → still rings through CallKit.
4. Decline from CallKit → the caller reaches voicemail promptly; the app shows Declined, then Ready.
5. In a call: Mute, Keypad (DTMF reaches an IVR), speaker and Bluetooth through the Audio button, and End. End stays visible with the keypad open.
6. Caller hangs up → the app returns to Ready within a second.
7. Second incoming call during a call → the caller goes to voicemail; the app shows "Missed call … while you were on a call".
8. Silent switch on / Focus mode → behaviour matches iOS settings.
9. Wi-Fi ↔ cellular during a call → "Reconnecting call…" then continues, or ends clearly.
10. Airplane mode 2 min → "No internet connection" → off → Ready again with no tap.
11. Text from the other phone → notification (app closed) → tap opens the thread; unread badge clears.
12. Send a text and a photo → delivered on the other phone from the LUX Connect number (needs the staging SMS decision).
13. Settings → Sign out → call the number → this iPhone does not ring.

**Android (Pixel on Android 15/16, plus one Samsung)**
- Steps 1–13 equivalents, using the full-screen incoming notification on the lock screen.
- Also: battery set to *Optimized* versus *Unrestricted* with the app swiped away and the phone idle for 15+ min, then call. Record the delay or failure; it is OEM-dependent.
- Also: deny notifications or microphone, then confirm the app explains and links to Settings.

**Multi-device**
1. Same user signed in on the iPhone, the Android phone and the desktop PWA → one call rings all three → answer on one → the other two stop ringing within about a second.
2. Settings → Ring this phone off on the iPhone → the iPhone does not ring; the others do.
3. Devices → sign out the Android phone from the iPhone → the Android phone stops ringing and asks for sign-in on next use.

**Isolation**
- A user in another company signed in on a second phone never rings for, or sees texts of, this number.
