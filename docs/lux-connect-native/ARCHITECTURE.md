# LUX Connect native: architecture

## 1. Components

```
 iPhone / Android                                   LUXit (one Flask app, PostgreSQL)
 ┌───────────────────────────────┐   HTTPS bearer   ┌──────────────────────────────────────────┐
 │ App.tsx  tabs + call screen   │ ───────────────▶ │ /api/mobile/v1  (mobile_api.py)          │
 │ AppController (1 per process) │                  │   auth / devices / voice token / media   │
 │  ├ ApiClient (Keychain tokens)│                  │   messages / sync / admin numbers        │
 │  ├ VoiceEngine ─ Twilio RN SDK│                  │   + existing PWA views mounted again     │
 │  ├ SyncManager ─ SSE + /sync  │ ◀─ SSE ───────── │ /api/mobile/v1/inbox/stream               │
 │  └ Outbox (AsyncStorage)      │                  │ services: mobile_auth, mobile_voice,     │
 └──────────────┬────────────────┘                  │   native_push, lux_numbers               │
                │ VoIP push (PushKit / FCM)         │ twilio_sms.py: ring_pwa, pwa-outbound,   │
        ┌───────┴────────┐                          │   sms inbound/status (unchanged + hooks) │
        │ Twilio Voice   │◀── <Dial><Client>… ──────┤                                          │
        │ (Push Credential)                         └──────────────┬───────────────────────────┘
        └────────────────┘                                         │ alert push (texts, missed, VM)
                                                       APNs (HTTP/2) / FCM HTTP v1
```

- **Calls never depend on our push code.** Twilio sends the VoIP push (PushKit on iOS, FCM data message on Android) using the Push Credential bound into the device's access token. Our server pushes only alerts: texts, missed calls and voicemail.
- **The backend stays authoritative.** The app caches nothing that decides behaviour. Conversations, calls, permissions, availability and caller ID all come from the server.

## 2. Identity and authentication

- **Sign-in:** `POST /auth/login` (email + password + device info) → opaque `lxa_` access token (1 h) and `lxr_` refresh token (60 days, rotating). Only SHA-256 hashes are stored.
- **Every request** revalidates the session, device status, user active flag, `session_revoked_at`, membership, Mobile Inbox access and active company. If the user's default company changed, the app is asked to sign in again.
- **Refresh:** single-flight on the client. Reuse of a rotated refresh token revokes the device's sessions, except within 60 s when the successor was never used (a lost response).
- **Cookies** are never read or written under `/api/mobile/v1`, which is what makes the CSRF exemption safe.

## 3. Voice

### Identity

`luxit_c{company}_u{user}_d{device_row}_{hmac16(company:user:mobile:device_uid)}`, one per installed device. The PWA keeps `luxit_c{c}_u{u}_{hmac}`.

### Token

`GET /voice/token` runs the **same gate** as the PWA token (`_voice_token_gate`): Receive Calls, Away, assigned number, call permission and credentials. It also needs the device to be active, not pending approval. The token's VoiceGrant carries:
- `incoming_allow` only when this platform's Twilio Push Credential is configured, so the app never claims it will ring when it can't;
- `push_credential_sid` (iOS production, iOS sandbox or Android);
- `outgoing_application_sid`.

### Inbound

`ring_pwa` builds the eligible set exactly as before. `mobile_voice.merge_dial_identities` then adds every native handset of those users that is all of:
- active and `ring_enabled`;
- `voice_registered_at` set;
- allowed to take calls on the dialed number.

All of them go into **one `<Dial>`** (capped at Twilio's 10 clients). Twilio rings them at once; the first answer wins and the other legs are cancelled. In the app, that cancel event clears the ringing screen and the CallKit / notification UI on the other devices.

Verified on staging with a real call: four legs rang in the same second (this native handset plus three PWAs); one answered, the rest were cancelled.

### Outbound

The SDK calls `connect(token, {To, phone_number_id})` → TwiML App → `/twilio/voice/pwa-outbound`:
- `verify_outbound_identity` checks the native identity's HMAC against an **active** device row;
- the caller ID is chosen server-side from the user's permitted numbers;
- the `<Dial answerOnBridge>` goes to the PSTN;
- the call log is written once per CallSid.

### Client state machine (`mobile/src/voice/VoiceEngine.ts`)

```
              sign-in / launch
                    │
                 starting ──▶ registering ──▶ READY ◀───────────────────────────┐
                    │              │  ▲          │                              │
           policy 403 (Away,      │  │ backoff   │ invite (push)               │
           calls off, pending,    ▼  │ ≤5 min    ▼                              │
           no push credential) reconnecting   incoming ──decline──▶ ended ──1.8s─┤
                    │                           │   └─cancel (caller hung up /  │
                    ▼                           │      answered elsewhere)──────┤
              unavailable                    answer (once)                      │
           (re-check on resume,                 ▼                               │
            settings change,                connecting ──▶ connected ──hang up──┘
            every 30 min)                      ▲   │  ▲        │ ▲
                                    outbound ──┘   │  └ reconnected ─┘ call_reconnecting
                                    (ringing)      └── failure ──▶ ended
  any state ── network lost ──▶ offline ── network back ──▶ registering (one attempt)
```

Rules:
- **Ready** = the server said incoming is enabled, `Voice.register()` succeeded **and** the phone is online. Nothing is persisted as "ready"; every launch and every return to the foreground re-proves it (registration older than 6 h is refreshed).
- **No manual Connect.** Transient errors back off exponentially up to 5 minutes and never give up while the app is alive. Policy refusals are re-checked on resume or settings change and every 30 minutes, not hammered. Concurrent triggers share one attempt. SDK listeners are attached once per process.
- **Answer is idempotent:** one `accept()` per invite. An answer made on the lock screen (CallKit / notification) is adopted, not repeated. On cold start the engine adopts calls and invites the native layer already holds, so answering from the lock screen opens straight onto the in-call screen.
- **Busy:** a second invite is rejected at once, so the caller reaches another device or voicemail, and a "missed while on a call" notice appears.
- **End is final:** End pressed during an answer or dial that is still connecting disconnects the late leg when it arrives.

### Call UI

A full-screen modal above all tabs shows:
- **incoming:** avatar, name, number, Decline (red) and Answer (green);
- **in call:** duration or status, quality warning, Mute, Keypad (DTMF), Audio (iOS AVRoutePicker; Android earpiece / speaker / Bluetooth list), and a red End button **pinned to the bottom safe area in every layout, including with the keypad open**;
- **ended:** a short summary.

On a locked or backgrounded phone the OS UI takes over: CallKit on iOS, Twilio's full-screen-intent notification on Android.

## 4. Messaging

- **Send:** `POST /messages {conversation_id | to, body, media_ids[], idempotency_key}`.
  - The key is minted once on Send and stored with the outbox item.
  - The server prefixes it (`app:`) and uses the existing `send_with_idempotency` → `sendConversationSms` path, so STOP / opt-out, the license check, the canonical sender and the Twilio gate all apply.
  - Retries with the same key never send twice. `send_in_progress` becomes "unknown" in the app and is never auto-resent.
- **MMS:** `POST /media` (≤5 MB, allow-listed types) stores the bytes in `mobile_media_upload`. Twilio fetches `/mobile-media/<256-bit token>` within 4 h. The app and PWA read through the authenticated `/api/inbox/media-uploads/<token>`. Inbound MMS uses the existing authenticated Twilio media proxy.
- **Sync:** full load, then `/sync?since=` (with 30 s overlap) on resume, on reconnect, on each live event and every 60 s in the foreground, plus SSE while foregrounded. Push covers the background.
- **Alert push:** `send_pwa_push_notification` already makes the per-user decision (Away, quiet hours, preferences, badge); the same decision now also queues native pushes.
  - Calls are excluded, because the VoIP push handles them.
  - iOS: APNs alert with badge, thread-id and a deep link.
  - Android: an FCM **notification** message on the `messages` / `calls` channels. The Twilio SDK owns the only FirebaseMessagingService (`plugins/withSingleFcmService.js` removes Expo's) and drops non-call data messages, while the OS displays notification messages by itself in the background.
  - Dead tokens are cleared.

## 5. Second-number and multi-device model

- A LUX Connect number is an existing `TwilioPhoneNumber` of the tenant, granted through `PhoneNumberUserPermission` (same as the web hub). `/admin/numbers` lists, assigns and unassigns; buying, releasing and porting are deliberately not in the app (405).
- Each install is a `mobile_device` with its own identity, push token, ring toggle and status (`active` / `pending_approval` / `signed_out` / `revoked`).
  - Tenants with `require_approved_pwa_devices` get new phones as `pending_approval`; admins approve in the app.
  - Remote sign-out revokes sessions and stops ringing immediately.
  - A handset that changes user retires the previous row.

## 6. Platform limits (honest)

| | iPhone | Android |
|---|---|---|
| App closed or phone locked | Rings through CallKit when VoIP push arrives (needs network and the Push Credential). | Rings through Twilio's full-screen notification when FCM high-priority arrives. OEM battery savers (Xiaomi, Huawei, some Samsung modes) can delay or kill FCM; the app tells users to set battery to Unrestricted. |
| Do Not Disturb / Focus | Calls follow the user's Focus settings (CallKit). | Follows DND rules for the calls channel. |
| Force-quit by the user | iOS still delivers VoIP pushes. | Android "Force stop" blocks FCM until the app is opened again; this can't be overridden. |
| No data connection | Cannot ring; the caller goes to voicemail. Shown as "No internet connection". | Same. |
| Audio routes | CallKit and the system route picker (Bluetooth, speaker). | Earpiece, speaker or Bluetooth from the in-call Audio menu (Twilio AudioSwitch). |
| Full-screen intent | n/a | Android 14+: Play requires the "calling app" declaration for `USE_FULL_SCREEN_INTENT`; without it the incoming call shows as a heads-up notification only. |

## 7. Android Telecom / ConnectionService

The brief asks for "Android Telecom/ConnectionService or the appropriate supported calling integration". Twilio's RN SDK 1.8 implements the latter: a microphone foreground service, a full-screen-intent incoming notification, and AudioSwitch for routing. That is supported and is what this build uses.

A self-managed `ConnectionService` (system call log, car / Wear integration, the system in-call UI on some OEMs) would need a custom native module bridging Twilio `CallInvite`s into `TelecomManager.addNewIncomingCall`. That is out of scope for 1.0 and listed as a follow-up.
