---
name: Phase A VoIP multi-number architecture
description: TwilioPhoneNumber DB table is the single routing source of truth; key design decisions and SQLAlchemy gotchas.
---

## The rule
`TwilioPhoneNumber` is the single source of truth for all inbound SMS/call routing. No phone numbers or routing config live in environment variables.

## _resolve_number() lookup order (twilio_sms.py)
1. `TwilioPhoneNumber.phone_number == to_number` (primary path — new multi-number system)
2. `TwilioAccount.messaging_service_sid == msg_service_sid` (messaging service SID)
3. `TwilioAccount.from_phone == to_number` (legacy single-number accounts)
4. First active `TwilioAccount` (absolute backward-compat fallback)

Returns `(pn_or_None, ta_or_None)`. All three webhook handlers use this: `inbound_sms`, `inbound_call`, `voice_no_answer`.

## _seed_phone_numbers_from_accounts()
Idempotent: migrates any `TwilioAccount.from_phone` into a `TwilioPhoneNumber` row. Called automatically on `GET /twilio/numbers` and via `POST /twilio/numbers/seed`.

## SQLAlchemy gotcha — dual FK on same table
`VoiceIVROption` has two FKs pointing at `VoiceIVRMenu` (`menu_id` and `submenu_id`). The `VoiceIVRMenu.options` relationship **must** specify `foreign_keys="VoiceIVROption.menu_id"` or SQLAlchemy raises a mapper init error that blocks all models from loading.

**Why:** SQLAlchemy cannot auto-determine which FK to use for the backref when two paths exist between the same pair of tables.

**How to apply:** Any time a model has two FKs pointing at the same parent table, always specify `foreign_keys=` on the relationship.

## Admin UI
- Route: `GET /twilio/numbers` — lists all `TwilioPhoneNumber` rows for the company, triggers seed on load
- Routes: add / edit / toggle / delete / seed — all at `/twilio/numbers/*`
- Template: `templates/twilio/numbers.html`
- Nav link: shown in sidebar only for `is_admin` or `is_platform_admin` users (under Communications Hub)
- Access: admin-only (`abort(403)` for non-admins)

## New tables added (Phase A)
TwilioPhoneNumber, VoiceVoicemailBox, VoiceExtension, VoiceIVRMenu, VoiceIVROption, VoiceRoutingRule, VoiceForwardingRule, VoiceVoicemailMessage, CallRecording, PinnedPhoneFavorite

## New columns added via migration
- `twilio_conversation.phone_number_id` (INTEGER, nullable)
- `twilio_call_log.phone_number_id` (INTEGER, nullable)
- `twilio_call_log.voicemail_url` (VARCHAR 500, nullable)
- `twilio_call_log.recording_url` (VARCHAR 500, nullable)
