# LUX Connect: LUXit Phone release feature audit

Audited 2026-10-09 against the code on `feat/lux-connect-native`. Status is about the **native app**. Backend support alone does not count as "in the app". Nothing here has been tried on a physical phone.

| Feature | Native app | LUXit web (CRM / PWA) | Status |
|---|---|---|---|
| SMS forwarding | none | number settings + legacy account setting | **Web only** |
| Call forwarding | none | number settings (route = forward) | **Web only** |
| Automatic SMS replies | none | number settings + Rules page | **Web only** |
| Rule-based automation | none | Rules page; the *Forward* action does nothing | **Web only, partial (defect)** |
| Editable caller ID | always the first assigned number; no picker | number chosen per call in the PWA; display name stored, unused | **Partial** |
| Customizable color themes | system light/dark only | 6 palettes per user | **Missing in app** (web has it) |
| Calling and texting over cellular data | no Wi-Fi gate; uses any connection | no Wi-Fi gate | **Implemented in code; not verified on a device** |

## Details

### SMS forwarding
- **Runtime:** `twilio_sms.py` inbound handler, step 5. When `sms_forward_to` and `sms_forwarding_enabled` are set, it sends "New SMS for …" to the forward number. Replies from that number are relayed with `reply +1… msg` / `r msg` (step 2b).
  - Per-number values override the account values through `_NumberScopedTwilioConfig` (applied at `twilio_sms.py` ~1430).
- **Settings:**
  - PWA: `PUT /api/phone/numbers/<id>/settings` (`inbox_pwa.py` `api_phone_number_settings`).
  - CRM: `/twilio/numbers/<id>/edit`.
  - Neither is mounted under `/api/mobile/v1`.
- **Tests:** owner-relay replies are covered (`test_inbound_identity_postgres.py`). **No test asserts that the forward SMS itself is sent.**

### Call forwarding
- **Runtime:** inbound voice routes `during_hours_route` / `after_hours_route = forward` to `forward_number` / `call_forwarding_number`, with a 25 s timeout and voicemail fallback (`twilio_sms.py` ~2319, ~2481, ~2516, ~2658).
- **Tests:**
  - `test_pwa_phone_system.py::test_forwarding_route_logs_destination_and_records`
  - `::test_inbound_voice_forwarding_number_is_selected_by_to_number_company`
  - `test_twilio_voice_inbound_resilience.py::test_voice_inbound_during_hours_forward_returns_twiml`
- **In the app:** nothing. If the number is set to forward, app handsets are not rung at all, because forward replaces ring_pwa. The app gives no sign of this.

### Automatic SMS replies
- **Runtime:**
  - per-number business-hours / after-hours replies (`auto_reply_enabled`, `number_auto_reply_text`, `business_hours_auto_reply_*`, after-hours cooldown);
  - `AutoReplyRule` keyword / after-hours rules.
  - STOP / opt-out is respected.
- **Tests:**
  - `test_pwa_phone_system.py`: per-number replies, after-hours cooldown and opt-out, STOP.
  - `test_comms_access.py`: four auto-reply tests.
  - `test_sms_campaign_after_hours.py`.
  - One existing failure: `test_after_hours_auto_reply_fallback_creates_outbound_and_marks_inbound`. It fails identically on the untouched baseline.

### Rule-based automation
- **Model:** `AutoReplyRule` (`models.py`): triggers, keywords, days/hours, priority; actions `reply | forward | opt_out | tag`.
- **Web UI:** `templates/twilio/rules.html` (`/twilio/rules`).
- **Defect:** the UI offers **Forward**, but `twilio_sms.py` (~1177–1217) only executes `opt_out`, `tag` and `reply`. A Forward rule matches, is counted, and silently does nothing. Not fixed: fixing it sends real SMS to `forward_to`, which is a behaviour change needing a decision.
- **Separate:** `VoiceRoutingRule` / `VoiceForwardingRule` / IVR models exist. They were not audited as part of this release.

### Editable caller ID
- **Server:** `/twilio/voice/pwa-outbound` takes `phone_number_id` and uses it only if the number is active, in the caller's company, and permitted to the caller; otherwise it falls back to the first permitted number. Arbitrary caller IDs are impossible by design.
  - Tests: `test_lux_connect_native_api.py::test_native_outbound_identity_verified_and_caller_id_is_assigned_number` and `::test_native_outbound_rejects_unverified_identity`.
- **App:** Keypad, Recents, Contacts, Voicemail and Conversation always use `me.numbers[0]`. There is no picker, so users with 2+ numbers cannot choose.
- **Display name:** `caller_id_display_name` is stored by the web form and API, and is not used anywhere. The name a called party sees comes from carrier CNAM, which needs Twilio CNAM / Branded Calling registration, not an app setting.

### Customizable color themes
- **PWA:** six palettes (LUX, Ocean, Forest, Sunset, Slate, Rose), saved per user (`pwa_palette_id`), plus `theme_mode`. Both are readable through the mounted `/api/mobile/v1/pwa/preferences`.
- **App:** `mobile/src/ui/theme.ts` follows the system light/dark only.

### Calling and texting over cellular data
- **Server:** the Wi-Fi gate was deliberately removed (`inbox_pwa.py` ~3484: "no canonical calling decision may require Wi-Fi"). `wifi_only` / `mobile_data_allowed` are stored but do not block anything.
- **App:** the Twilio SDK and the API client use whatever connection exists. NetInfo is used only for online/offline. The engine handles call reconnection; that behaviour is unit-tested only.
- **Needs a device:** device checklist steps 9 and 17 (Wi-Fi ↔ cellular handover; cellular-only calls and texts).
