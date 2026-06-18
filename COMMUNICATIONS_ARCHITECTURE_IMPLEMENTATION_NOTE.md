# Communications / Phone / SMS Implementation Note

Date: 2026-06-17

## Current routes/pages found

- Admin/desktop Communications: `/twilio/comms`, `/twilio/comms/settings`, `/twilio/numbers`, `/twilio/settings`, `/twilio/rules`, `/twilio/hours`, `/twilio/calls`, `/twilio/inbox`, `/twilio/inbox/<id>`.
- PWA phone/inbox: `/app/inbox`, `/app/calls`, `/app/calls/settings`, `/api/inbox/*`, `/api/calls/*`, `/api/phone/*`.
- Twilio webhooks: `/twilio/sms/inbound`, `/twilio/sms/status`, `/twilio/voice/inbound`, `/twilio/voice/no-answer`, `/twilio/voice/recording`, `/twilio/voice/status`, plus `/api/twilio/voice/*` aliases.
- Google Contacts: `/twilio/google-contacts/connect`, `/callback`, `/sync`, `/status`, `/disconnect`.
- SMS Campaigns: `/app/sms-campaigns`, legacy `/sms*` redirects, and `/api/marketing/sms-campaigns*`.

## Duplicate pages/settings found

- Left navigation exposed `Communications Hub`, `SMS & Calls Admin`, and `Phone Numbers` separately. The duplicate admin/number links are now hidden from the left menu; the routes remain for deep links/backward compatibility.
- Business hours existed at tenant/global levels (`PhoneSettings`, `BusinessHours`) while `TwilioPhoneNumber` held per-number routing/forwarding/voicemail values. Per-number business-hours fields are now added to `TwilioPhoneNumber`; tenant-level hours remain as defaults.

## Shared/global settings moved toward per-number

- Added per-number fields on `TwilioPhoneNumber` for business hours, timezone, during/after-hours routes, browser/WiFi calling, cell callback, data-use controls, fallback behavior, and caller-ID display name.
- Twilio inbound SMS/voice resolution already selects a `TwilioPhoneNumber`; business-hours checks now prefer the selected number’s hours before falling back to tenant defaults.

## New/updated backend APIs

- `GET /api/phone/numbers`: returns the signed-in user’s accessible phone lines for PWA selectors.
- `GET/PUT /api/phone/numbers/<id>/settings`: reads/updates per-number routing, business hours, voicemail, forwarding, and calling options. Updates require owner/platform-admin/manage-users level permission.
- Existing PWA conversation/call/voicemail APIs now filter server-side history by phone-number access instead of relying on device/session state.

## Permissions behavior

- `UserCompanyAccess` now normalizes role aliases (`super-admin`, `supervisor`, `user`, etc.) to canonical roles.
- Tenant owner can manage users and roles.
- Tenant admins require `manage_users_enabled=True` unless they are platform admins.
- Unauthorized users receive 403.
- Tenant isolation is preserved: non-platform admins cannot edit users outside their tenant membership.
- Added `PhoneNumberUserPermission` for explicit per-line access to PWA, SMS, calls, voicemail, number management, and campaigns.

## PWA complete-history behavior

- Server APIs remain the source of truth; the PWA may cache locally, but conversation/call/voicemail lists are fetched from server APIs.
- Owners/admins see all tenant line history.
- Standard users with explicit number permission see all SMS/call/voicemail history for their assigned number(s), including read messages.
- Unauthorized users cannot fetch restricted number history or detail endpoints.
- The PWA now includes an assigned-number selector when the user has multiple accessible lines.

## Tests added

- Owner can edit tenant users and role aliases normalize correctly.
- Admin with manage-users permission can manage users.
- Unauthorized users are denied.
- Tenant isolation is preserved for user-management APIs.
- PWA read SMS remains visible across repeated loads.
- Authorized users see shared number history; unauthorized users cannot fetch restricted history.
- Voicemail listing includes playback metadata.
- Per-number settings remain independent.
- Left navigation no longer includes duplicate SMS/Text/Phone admin items.

## Remaining risks / follow-up items

- Communications Hub UI should be expanded further into the full tabbed editor described in the product brief; this change adds backend/API foundations and removes duplicate navigation without replacing existing pages.
- SMS Campaign per-number sender selection is still routed through existing Twilio account defaults; next step is adding campaign `from_phone_number_id` selection and enforcing `can_send_campaigns`.
- Full browser/WiFi calling runtime controls are represented in per-number settings; Twilio Voice SDK behavior should be wired to those settings in a follow-up UI pass.

## Old-to-new settings map

| Old page / setting | New Communications Hub tab | API / model used | Test coverage |
| --- | --- | --- | --- |
| `/twilio/numbers` number list | Numbers | `TwilioPhoneNumber` | `test_communications_hub_tabs_and_pwa_api_smoke` |
| Twilio from/caller identity | Number Settings | `TwilioPhoneNumber.caller_id_display_name`, `friendly_name` | `test_number_settings_are_independent_per_number` |
| `/twilio/hours` tenant hours | Business Hours | `TwilioPhoneNumber.business_hours`, fallback `PhoneSettings` / `BusinessHours` | `test_number_settings_are_independent_per_number`, existing after-hours tests |
| SMS forwarding | Call Routing / Forwarding | `TwilioPhoneNumber.sms_forward_to`, `sms_forwarding_enabled` | smoke + form render coverage |
| Voice forwarding/routing | Call Routing / Forwarding | `TwilioPhoneNumber.call_forward_to`, `voice_forwarding_enabled`, route fields | smoke + form render coverage |
| `/twilio/rules` auto replies | Auto Replies | `AutoReplyRule`, `TwilioPhoneNumber.auto_reply_enabled`, `after_hours_text` | smoke + existing Twilio auto-reply tests |
| Voicemail greeting/recordings | Voicemail / Number Settings | `VoiceVoicemailMessage`, `TwilioCallLog.voicemail_url`, `TwilioPhoneNumber.voicemail_*` | `test_authorized_admin_sees_all_number_history_and_voicemail_metadata` |
| PWA install/access | PWA Phone App | `/app/inbox`, `/api/phone/numbers`, `PhoneNumberUserPermission` | `test_pwa_history_persists_and_is_scoped_to_assigned_number` |
| Per-user Communications flags | Users & Permissions | `UserCompanyAccess`, `PhoneNumberUserPermission` | owner/admin/denied tests |
| SMS inbox | SMS Conversations | `TwilioConversation`, `TwilioMessage` | PWA history tests + smoke tests |
| Call log | Call Logs | `TwilioCallLog` | call-log scope tests + smoke tests |
| Google Contacts | Integrations | `GoogleOAuthToken`, `Contact`, Google Contacts service | contact matching tests |
| Marketing/audit activity | Reports / Activity | `MarketingAuditLog`, campaign analytics routes | smoke tests |
| SMS campaign sending identity | SMS Campaigns page | `SMSCampaign.from_phone_number_id`, `from_phone_number`, `TwilioPhoneNumber` | sender permission logic smoke via route coverage |
