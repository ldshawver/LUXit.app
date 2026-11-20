# LUX Connect: LUXit Phone release feature audit

Audited 2026-10-09 against the code on `feat/lux-connect-native`. Status is about the **native app**. Backend support alone does not count as "in the app". Nothing here has been tried on a physical phone.

**Update (same day, dev only, not on staging):** the native app now has all six. See "Native implementation" at the end.

| Feature | Native app | LUXit web (CRM / PWA) | Status |
|---|---|---|---|
| SMS forwarding | Number settings screen (admins edit; staff read-only) | number settings + legacy account setting | **Implemented in code; device NOT TESTED** |
| Call forwarding | Number settings screen + live "where calls go" status (Settings, Keypad banner) | number settings (route = forward) | **Implemented in code; device NOT TESTED** |
| Automatic SMS replies | Number settings screen (business hours / after hours) | number settings + Rules page | **Implemented in code; device NOT TESTED** |
| Rule-based automation | Rules list + editor; Forward action now executes | Rules page | **Implemented in code; defect fixed; device NOT TESTED** |
| Outgoing caller ID | Settings → Caller ID (assigned numbers only); New message "From" | number chosen per call in the PWA | **Implemented in code; device NOT TESTED** |
| Color themes | Settings → Appearance (same six palettes, saved on the account) | 6 palettes | **Implemented in code; device NOT TESTED** |
| Calling and texting over cellular data | no Wi-Fi gate; uses any connection | no Wi-Fi gate | **Implemented in code; device NOT TESTED** |

<details><summary>Audit as first written (before the native work)</summary>

| Feature | Native app | LUXit web (CRM / PWA) | Status |
|---|---|---|---|
| SMS forwarding | none | number settings + legacy account setting | Web only |
| Call forwarding | none | number settings (route = forward) | Web only |
| Automatic SMS replies | none | number settings + Rules page | Web only |
| Rule-based automation | none | Rules page; the *Forward* action does nothing | Web only, partial (defect) |
| Editable caller ID | always the first assigned number; no picker | number chosen per call in the PWA; display name stored, unused | Partial |
| Customizable color themes | system light/dark only | 6 palettes per user | Missing in app |
| Calling and texting over cellular data | no Wi-Fi gate | no Wi-Fi gate | Implemented in code; not verified on a device |

</details>

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

## Native implementation (2026-10-09, branch `feat/lux-connect-native`)

Server (reuses existing logic; no new business rules):
- `/api/mobile/v1/phone/numbers/<id>/settings` is the existing PWA view, now mounted for the app.
  - Who may edit was extracted to `inbox_pwa.phone_settings_editable`.
  - The SMS forward number is now normalized to E.164. Forwarding to one of the account's own numbers is refused (it would loop).
- Call-routing status:
  - `twilio_sms.resolve_inbound_voice_route` / `inbound_voice_destination` were **extracted unchanged** from the voice webhook. `number_call_routing_status` uses them, so the app shows the webhook's own decision.
  - Exposed through `/me` (`numbers[].call_routing`) and `/phone/numbers/<id>/routing`.
  - Forward destinations are masked (`•••1234`) for users who cannot edit them.
- Rules:
  - `services/auto_reply_rules.py` holds the validation, shared with the CRM editor's duplicate-keyword check.
  - `/api/mobile/v1/automation/rules[/<id>]` gives everyone read access; admins can create, update and delete. The system STOP rule is read-only.
  - Rules on numbers the user cannot access are hidden.
- **Forward action fixed** (`_apply_auto_reply_rules`):
  - The message is sent through the same durable, idempotent outbox as everything else, with effect `rule_forward:<id>`.
  - It isn't sent twice when the number's own text forwarding already goes to that phone.
  - It offers no "reply" hint, because replies from rule numbers are not relayed.
  - It never runs for STOP (STOP is answered before rules) or for opted-out contacts.
- **Sender for new texts:**
  - `POST /messages` takes `from_phone_number_id`, accepted only for an active company number the user may text from.
  - There is one thread per customer (DB unique constraint), and the thread moves to the chosen line, as inbound texts already do.
  - The server still enforces the conversation's line on every send.

App: `PhoneSettingsScreen`, `RulesScreen`, `RuleEditScreen`, Settings → Caller ID / Appearance / numbers, the New message "From" choice, a forwarding warning in the status banner, `state/prefs.ts` (palette + caller choice) and `ui/palettes.ts`.

Tests:
- Backend `tests/test_lux_connect_native_api.py` **54/54**. New cases:
  - settings: admin vs staff, validation, tenant isolation;
  - routing status equals the real webhook TwiML, with masking;
  - rules CRUD, validation (10 bad inputs), admin-only, isolation;
  - system STOP rule is read-only;
  - Forward rule executes with mocked delivery;
  - no double forward; STOP skips;
  - sender choice limited to permitted numbers of the tenant.
- Regression set (12 files): 286 pass / 13 fail, **identical failures on the untouched baseline 7fc8eb3**.
- App jest **73/73**; tsc clean; Metro iOS + Android bundles build.

Not changed: carrier caller name (CNAM). `caller_id_display_name` is still unused and still not shown in the app.

Web app (PWA) cosmetic issue noticed, not fixed: in `templates/inbox_pwa/index.html`, `--color-primary` and `--pwa-primary` reference each other, so the default LUX palette has no primary colour on that page. `calls.html` defines it as `#2563eb`.
