# Communications Deployment Verification Note

This feature is **not go-to-market complete** until the runtime checks below pass in staging with real Twilio numbers, real users, and the installed PWA. Automated tests cover permission, scoping, tab, campaign-calendar, and API smoke regressions, but live Twilio Voice, voicemail recording playback, and device networking must be proven outside unit tests.

## Deployment commands

### Apply migration

```bash
psql "$DATABASE_URL" -f migrations/20260617_comms_phone_number_permissions.sql
```

If your deployment uses the platform migration runner instead of direct `psql`, run the equivalent migration command for `migrations/20260617_comms_phone_number_permissions.sql` during the release step.

### Rollback command

Use only before the application writes production data to these fields/tables, or after exporting/backing up affected rows:

```sql
DROP INDEX IF EXISTS ix_sms_campaign_from_phone_number_id;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS from_phone_number;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS from_phone_number_id;
DROP TABLE IF EXISTS phone_number_user_permission;
ALTER TABLE user_company_access DROP COLUMN IF EXISTS manage_users_enabled;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS caller_id_display_name;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS fallback_behavior;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS wifi_only;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS mobile_data_allowed;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS cell_callback_enabled;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS browser_calling_enabled;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS after_hours_route;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS during_hours_route;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS timezone;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS business_hours;
```

## Required Twilio settings

Configure each purchased or connected Twilio number independently:

| Twilio setting | Required value |
| --- | --- |
| Messaging webhook | `https://<app-host>/twilio/sms` |
| Messaging status callback | `https://<app-host>/twilio/status` |
| Voice webhook | `https://<app-host>/twilio/voice` |
| Voice fallback/no-answer callback | `https://<app-host>/twilio/voice/no-answer` if configured in Twilio console |
| Recording status callback | `https://<app-host>/twilio/voice/recording` |
| Transcription callback | `https://<app-host>/twilio/voice/transcription` or `https://<app-host>/twilio/voice/voicemail` |
| Voice SDK / browser calling | Confirm Twilio Voice application credentials, token endpoint, and caller-ID restrictions are configured for the staging tenant before enabling browser calling. |

## Required feature/license flags

Do not enable for general users until these are confirmed for the tenant/company:

- Communications / SMS feature access is enabled for the tenant.
- PWA inbox/phone access is enabled only for assigned users or owner/admin testers.
- SMS Campaigns access is enabled for marketing users who have at least one permitted sending number.
- Browser/WiFi calling and cell-callback calling remain disabled by default for a number until live Voice SDK and callback tests pass.
- Development/staging admins may retain access during verification, but production rollout should use tenant/license checks plus per-number permissions.

## Post-deploy staging smoke checklist

### 1. User-management permissions

- Log in as the tenant owner and confirm `/user/manage-users` loads.
- As owner, edit another user role and save successfully.
- Log in as `info@adiken.com` or an equivalent admin with `manage_users_enabled=true`.
- Confirm the admin can open `/user/manage-users` and edit allowed user settings without “Access denied”.
- Log in as a non-authorized standard/viewer user and confirm user-management edit attempts return 403.
- Attempt cross-tenant user edits and confirm they are denied.

### 2. Multi-number isolation

Use two staging numbers:

| Scenario | Number A | Number B | Expected result |
| --- | --- | --- | --- |
| Business hours | Open schedule | Closed/different schedule | After-hours logic follows the receiving number only. |
| Auto replies | Rule/text A | Rule/text B | Replies do not leak between numbers. |
| Voicemail | Greeting A | Greeting B | Caller hears the selected number’s greeting. |
| Forwarding/routing | Destination A | Destination B | Calls/SMS route to the configured destination for that number. |
| Caller ID/display name | Identity A | Identity B | Outbound workflows show/send the selected business number. |
| Campaign sender identity | Permitted A | Permitted B | Campaign sender persists the chosen permitted number. |

### 3. PWA history persistence

- Send an SMS to an assigned number.
- Mark it read in the PWA.
- Close/reopen the PWA and confirm the conversation remains visible.
- Log in as another authorized user assigned to the same number and confirm the same SMS/call/voicemail history appears.
- Log in as an unauthorized user and confirm the restricted number and its history are hidden.
- Confirm server data remains the source of truth even if local/device cache is cleared.

### 4. Voicemail runtime

- Place an inbound call and leave voicemail.
- Confirm the Twilio recording callback succeeds.
- Confirm the recording URL appears in the Communications Hub Voicemail tab.
- Confirm the voicemail appears in the PWA visual voicemail list.
- Play the recording from admin and PWA.
- If transcription is enabled/returned by Twilio, confirm transcript text is stored and shown.
- Mark voicemail read/unread and confirm state persists after refresh and across authorized users.
- Confirm unauthorized users cannot list or play restricted-number voicemail.

### 5. Outbound calling runtime

This release is not production-ready for live calling until all of these pass:

- Browser/WiFi calling works from a no-cell-service device when enabled for the selected number.
- Cell callback calling works when enabled for the selected number.
- Browser/WiFi calling is blocked when disabled for the selected number.
- Cell callback calling is blocked when disabled for the selected number.
- WiFi-only/data-usage settings are respected in the PWA/device workflow.
- If browser calling fails, the configured fallback behavior is used.
- Outbound calls display the selected assigned business number as caller ID.

### 6. SMS Campaign integration

- Create a campaign and choose a permitted sending number.
- Confirm users without permission for a number cannot send from it.
- Schedule an SMS campaign and confirm it appears on `/marketing-calendar` and `/api/calendar/events`.
- Send a small test campaign and confirm delivery analytics/reporting update.
- Confirm STOP, START, and HELP inbound compliance handling still works.
- Confirm opt-out recipients are excluded from campaign sends.

### 7. UI and navigation

- Verify Communications Hub tabs on desktop and mobile widths.
- Verify SMS Campaigns page on desktop and mobile widths.
- Confirm only Communications Hub and SMS Campaigns appear as communications-related left-nav items.
- Confirm legacy deep links still resolve or redirect cleanly.

## Known follow-up items

- QR-code rendering remains a follow-up because no existing QR-code helper was confirmed in this pass. The PWA tab should continue linking to `/app/inbox` until a QR helper is added.
- Live Twilio Voice SDK/device behavior must be verified in staging before claiming outbound calling is go-to-market complete.
- Staging screenshots should be attached to the release ticket after completing the desktop/mobile UI checklist.

## Final hardening additions for staging certification

### Devices tab and APIs

The Communications Hub now includes `/twilio/comms?tab=devices` for registered PWA/work-phone telemetry. Staging should verify that each device row shows user, default/assigned line, browser/device type, online status, last seen, push status, microphone permission, installed-PWA status, WiFi-only, cell callback, mobile data calling, and default calling method.

Device APIs to validate:

- `GET /api/pwa/devices`
- `POST /api/pwa/devices/register`
- `POST /api/pwa/devices/heartbeat`
- `PATCH /api/pwa/devices/<id>/settings`

Tenant isolation and line permissions must be verified by registering a device for one assigned number and confirming unauthorized users cannot view or reassign it to a restricted line.

### PWA palette persistence

Palette selection is server-backed per user and mirrored locally for fast/offline rendering. Staging should verify that `lux`, `ocean`, `forest`, and `sunset` persist after logout/login, PWA reinstall, and cross-device login. The server-side source of truth is loaded through the PWA preferences/device registration flow.

### Voicemail transcription and read state

Voicemail certification must verify these metadata fields flow end-to-end when Twilio returns them: recording URL, recording duration, transcription text, transcription status, transcription provider, transcription error, transcribed timestamp, read timestamp, and read-by user. Admin Hub and PWA visual voicemail should show playback, transcription status/text, and read/unread state.

### Call-log schema and UX readiness

Call-log API responses should include direction, from/to number, contact/caller name, assigned business number, status, missed/incoming/outbound label, duration, started/ended timestamps, voicemail indicator, recording URL, transcription preview/status, read/unread metadata, and callback target. PWA mobile checks should confirm selected-number filtering and callback/message options from call rows.

### Twilio Voice runtime checklist

Before production enablement, validate browser/WiFi calling and cell callback with real Twilio Voice credentials and real devices. Disabled methods must be blocked by API, WiFi-only/data restrictions must be respected, failed/reconnect states must be visible, and selected business number must be used as outbound caller ID.

### Final staging certification checklist

- Devices tab registers and updates real work-phone devices.
- Palette persistence survives logout/login, reinstall, and another device.
- Voicemail recording and transcription metadata appear in Admin and PWA.
- Call logs show contact names, duration, direction labels, voicemail indicators, and callback targets.
- PWA search finds CRM/Google contacts, companies, emails, recent SMS conversations, and recent call-log numbers.
- Browser/WiFi and cell-callback calling obey per-number settings.
- SMS Campaign sender number, marketing calendar visibility, analytics, and STOP/START/HELP compliance remain intact.
- Communications Hub, Devices tab, SMS Campaigns, and PWA have no horizontal overflow at mobile widths.

## Staging credential handling

A separate staging certification plan now exists in `COMMUNICATIONS_STAGING_CERTIFICATION_PLAN.md`. Do not commit login passwords, Twilio auth tokens, API secrets, or other live staging credentials. Supply those through the staging secret manager or an uncommitted operator handoff only.
