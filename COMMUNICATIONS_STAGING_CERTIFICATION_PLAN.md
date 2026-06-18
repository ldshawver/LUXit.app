# Communications / PWA Phone Staging Certification Plan

This plan turns the staging requirements into a safe, repeatable UAT checklist. It intentionally **does not store passwords, Twilio auth tokens, API secrets, or other live credentials**. Those values must be supplied through the staging secret manager or a local, uncommitted operator note.

## Safety rules

- Do not send bulk real SMS.
- Only send SMS/calls to approved test numbers.
- Do not use production contacts or production customer segments.
- Do not use production Twilio numbers unless explicitly authorized for this staging certification.
- Do not mark Communications / PWA Phone go-to-market complete unless real staging device tests pass.
- Do not commit staging passwords, Twilio auth tokens, API keys, API secrets, or personal device numbers to the repository.

## Required staging inputs

| Input | Required value / handling |
| --- | --- |
| Owner account | Use the provided owner email in the staging credential handoff; keep password out of git. |
| Admin/manage-users account | Use `info@adiken.com`; keep password out of git. |
| Restricted standard user | Use the provided restricted-user email in the staging credential handoff; keep password out of git. |
| Number A | `+18302591310` |
| Number B | `+19165989519` |
| Public app URL | Use the deployed staging URL, or the approved public URL when staging is mapped there. |
| Twilio credentials | Provide via environment/secret manager only: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY_SID` or `TWILIO_API_KEY`, `TWILIO_API_KEY_SECRET`, `TWILIO_TWIML_APP_SID`, `TWILIO_MESSAGING_SERVICE_SID`, and voice app SID if separate. |
| Webhook base URL | Public HTTPS URL reachable by Twilio. |
| Devices | iPhone Safari PWA, Android Chrome PWA if available, and Desktop Chrome. |
| Test contacts | Internal CRM/contact table and Google Contacts test-only records. |
| SMS Campaign data | Test-only segment, opted-in recipients, opted-out recipient, scheduled campaigns for Number A and Number B. |

## Twilio staging configuration checklist

Configure both staging numbers independently in Twilio:

| Twilio setting | Expected staging URL pattern |
| --- | --- |
| Messaging request URL | `<PUBLIC_BASE_URL>/twilio/sms/inbound` or the deployed equivalent used by the app |
| Messaging fallback URL | `<PUBLIC_BASE_URL>/twilio/fallback` |
| Messaging status callback | `<PUBLIC_BASE_URL>/twilio/sms/status` |
| Voice request URL | `<PUBLIC_BASE_URL>/twilio/voice/inbound` or the deployed equivalent used by the app |
| Voice recording callback | `<PUBLIC_BASE_URL>/twilio/voice/recording` |
| Voice transcription callback | `<PUBLIC_BASE_URL>/twilio/voice/transcription` or `<PUBLIC_BASE_URL>/twilio/voice/voicemail` |
| Voice application SID | Must match the staging TwiML app configured for browser/WiFi calling. |

Before running live tests, confirm the app route aliases match the Twilio console URLs. If Twilio is configured to `/twilio/sms/inbound` or `/twilio/voice/inbound`, those routes must resolve or redirect to the canonical handlers without 404/500.

## Staging account tests

### Owner

- Log in as the owner account from the credential handoff.
- Open `/user/manage-users`.
- Edit a user role and save.
- Grant/revoke Communications permissions for a test user.
- Confirm no Access Denied error appears.

### Admin/manage-users

- Log in as `info@adiken.com` using the staging credential handoff.
- Open `/user/manage-users`.
- Edit allowed user settings and save.
- Confirm role normalization works for owner/admin/super-admin/supervisor/user aliases.
- Confirm tenant isolation prevents editing users outside the tenant.

### Restricted user

- Log in as the restricted standard user from the credential handoff.
- Confirm user-management edits are denied.
- Confirm restricted phone numbers, SMS history, call logs, and voicemails are hidden.
- Confirm any allowed assigned line still shows complete server history for that line.

## Multi-number staging matrix

Configure Number A and Number B with intentionally different settings:

| Area | Number A | Number B | Expected result |
| --- | --- | --- | --- |
| Business hours | Schedule A | Schedule B | After-hours auto replies and routing follow the receiving number. |
| Voicemail | Greeting/settings A | Greeting/settings B | Caller hears/stores voicemail on the called number. |
| Forwarding | Forwarding/routing A | Forwarding/routing B | Calls/SMS route independently. |
| Auto replies | Auto reply A | Auto reply B | Replies do not leak across numbers. |
| Assigned users | User set A | User set B | PWA history is scoped by assigned line permission. |
| Caller ID | Number A | Number B | Outbound call/SMS/campaign identity matches selected line. |
| Campaign sender | Campaign A uses Number A | Campaign B uses Number B | Campaign sender identity and permissions remain separate. |

## Real-device PWA tests

Run on iPhone Safari PWA, Android Chrome PWA if available, and Desktop Chrome:

- Install/open the PWA.
- Confirm device appears in Communications Hub → Devices.
- Confirm device name, browser/device type, online status, last seen, push status, microphone permission, installed status, WiFi-only/cell-callback/mobile-data settings, and default calling method are shown.
- Select assigned Number A and Number B where permitted.
- Confirm restricted users cannot select restricted numbers.
- Send/read SMS, close/reopen PWA, and confirm conversations remain visible.
- Log in as another authorized user and confirm the same server-backed line history appears.
- Confirm unauthorized users cannot see restricted line history.
- Change all 4 palettes and confirm the selected palette persists after logout/login, reinstall, and another device.

## Voice and voicemail runtime tests

Do not certify live Voice until all pass:

- Browser/WiFi call succeeds from an enabled line.
- Browser/WiFi call is blocked from a disabled line.
- Cell callback succeeds from an enabled line.
- Cell callback is blocked from a disabled line.
- WiFi-only and mobile-data restrictions are respected.
- Outbound caller ID shows the selected business number.
- Inbound call rings/reroutes according to the called number.
- No-answer/fallback voicemail records successfully.
- Recording appears in Communications Hub and PWA visual voicemail.
- Playback works on admin desktop and PWA devices.
- Transcription displays when Twilio returns transcription data.
- Voicemail read/unread state persists and is permission-filtered.

## Contacts and dialer matching tests

Create test-only records in Google Contacts and the internal CRM/contact table with:

- Name
- Company
- Email
- Mobile number

Verify:

- PWA search finds by contact name.
- PWA search finds by company.
- PWA search finds by email.
- PWA search finds by phone number.
- Dial pad live-matches normalized numbers while typing.
- Incoming SMS resolves the Google/CRM contact name.
- Recent SMS conversations and recent call-log numbers appear in search.

## SMS Campaign staging tests

- Create a test-only contact segment.
- Include opted-in contacts.
- Include one opted-out contact.
- Create a scheduled SMS campaign using Number A.
- Create a scheduled SMS campaign using Number B.
- Confirm unauthorized users cannot send from restricted numbers.
- Confirm scheduled campaigns appear on the marketing calendar.
- Send only to approved test recipients.
- Confirm analytics/reporting update after send/status callback.
- Confirm STOP/START/HELP compliance remains unchanged.

## Final sign-off criteria

Communications / PWA Phone may be certified for staging only after:

- Owner/admin/restricted account tests pass.
- Number A/B isolation passes across hours, routing, voicemail, forwarding, auto replies, assigned users, caller ID, and campaigns.
- PWA persistence passes across close/reopen, logout/login, reinstall, and multiple authorized users.
- Real-device Voice and voicemail tests pass.
- SMS Campaign scheduling, sending-number permissions, analytics, and compliance pass.
- Mobile UI checks pass for Communications Hub, Devices, SMS Campaigns, and PWA.
- No duplicate SMS/Text/Phone/Twilio/Inbox/Call Settings left-nav items are visible.
