# PWA Phone Deployment & Manual QA Checklist

Do not mark the PWA phone system production-ready until every required QA item has dated evidence, tester initials, and screenshots, browser logs, or Twilio Call SIDs.

## 0. Current production-gate status

Status: **not production-ready**. Staging PostgreSQL migration, real Twilio Voice calls, browser/PWA media QA, and real call-triggered SMS QA are still required. Use `PHONE_PRODUCTION_GATE_STATUS.md` as the release evidence log and do not mark this feature ready until every gate there is complete.

## 1. Migration command

Run against staging PostgreSQL first:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260615_pwa_phone_system.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260615_pwa_phone_system.sql  # repeat to prove idempotency
```

Verification queries:

```sql
SELECT COUNT(*) AS existing_call_logs FROM twilio_call_log;
SELECT conname FROM pg_constraint WHERE conname IN ('fk_twilio_call_log_contact','fk_twilio_call_log_assigned_user','fk_twilio_call_log_answered_user','uq_call_event_idempotency');
SELECT indexname FROM pg_indexes WHERE indexname IN ('ix_twilio_call_log_company_created','ix_twilio_call_log_company_status','ix_twilio_call_log_twilio_sid','ix_phone_settings_company','ix_call_event_call_log_id');
INSERT INTO phone_settings (company_id) SELECT id FROM company LIMIT 1 ON CONFLICT (company_id) DO NOTHING;
```

## 2. Rollback plan

This migration is additive. If rollback is required before app traffic uses the new columns/tables:

```sql
DROP TABLE IF EXISTS call_event;
DROP TABLE IF EXISTS phone_settings;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS parent_call_sid;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS forwarded_to_number;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS contact_id;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS customer_id;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS assigned_user_id;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS answered_by_user_id;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS answered_at;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS ended_at;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS recording_url;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS recording_sid;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS voicemail_url;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS voicemail_sid;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS transcription_text;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS transcription_status;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS transcription_provider;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS metadata_json;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS is_read;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS is_archived;
ALTER TABLE twilio_call_log DROP COLUMN IF EXISTS updated_at;
```

Take a PostgreSQL snapshot/backup before rollback. Do not drop columns after production call data has been written unless data retention has been approved.

## 3. Required environment variables

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_API_KEY`
- `TWILIO_API_SECRET`
- `TWILIO_PHONE_NUMBER`
- `PUBLIC_APP_URL`
- `DEFAULT_PHONE_TIMEZONE`
- Recommended: `PWA_VOICE_IDENTITY_SECRET`
- Optional for outgoing SDK calls: `TWILIO_TWIML_APP_SID`

## 4. Twilio Console settings

For the purchased voice number, configure:

- Incoming Voice webhook: `https://<public-domain>/api/twilio/voice/incoming` with HTTP POST
- Status callback: `https://<public-domain>/api/twilio/voice/status` with HTTP POST
- Recording callback: `https://<public-domain>/api/twilio/voice/recording` with HTTP POST
- Transcription callback: `https://<public-domain>/api/twilio/voice/transcription` with HTTP POST

Confirm webhook signature validation works behind the staging/prod proxy by testing with `TWILIO_STRICT_SIGNATURE=1`.

## 5. Smoke test URLs

- PWA calls page: `https://<public-domain>/app/calls`
- Phone settings API: `https://<public-domain>/api/phone/settings`
- Voice token API: `https://<public-domain>/api/phone/voice-token`
- Recent calls API: `https://<public-domain>/api/calls/recent`
- Voicemails API: `https://<public-domain>/api/calls/voicemails`

## 6. Test phone numbers used

Record the actual values before QA:

- Twilio number under test:
- Desktop browser CSR login/user:
- iPhone Safari browser CSR login/user:
- iPhone installed PWA CSR login/user:
- External forwarding destination:
- Fallback forwarding destination:
- Caller phone number:

## 7. Expected TwiML examples

Business-hours `ring_pwa` should contain:

```xml
<Response>
  <Dial action="/twilio/voice/no-answer" method="POST">
    <Client>
      <Identity>luxit_cTENANT_HASH</Identity>
      <Parameter name="call_log_id" value="..."/>
    </Client>
  </Dial>
</Response>
```

After-hours voicemail should contain:

```xml
<Response>
  <Say>...</Say>
  <Record recordingStatusCallback="/twilio/voice/recording" transcribeCallback="/twilio/voice/transcription" />
</Response>
```

Forwarding should contain:

```xml
<Response>
  <Dial action="/twilio/voice/no-answer" method="POST">
    <Number>+1...</Number>
  </Dial>
</Response>
```

## 8. Required manual QA evidence

- [ ] Real inbound call during business hours rings `/app/calls` on Desktop Chrome.
- [ ] Real inbound call during business hours rings iPhone Safari browser.
- [ ] Real inbound call during business hours rings iPhone installed PWA.
- [ ] PWA user answers and two-way audio works.
- [ ] PWA user declines and the call is logged as declined/missed.
- [ ] Send-to-voicemail creates a voicemail record and playable audio.
- [ ] End-call works and updates the call log.
- [ ] Mute works and unmutes cleanly.
- [ ] Token refresh/reconnect state is user-friendly.
- [ ] Microphone permission denied state is clear.
- [ ] Twilio transcription appears in Recent Calls.
- [ ] Business-hours forwarding reaches the external phone and logs destination.
- [ ] After-hours forwarding reaches the external phone and logs destination.
- [ ] Forwarded calls save recording URL when recording is enabled.
- [ ] Missed calls appear under the Missed tab.
- [ ] Missed-call SMS auto-reply sends only when enabled.
- [ ] After-hours SMS auto-reply sends when enabled.
- [ ] STOP/opt-out contacts do not receive automated SMS.
- [ ] Existing PWA inbox and messaging still work.
- [ ] Old `/twilio/calls` call log still loads for desktop/admin workflows.

## 9. Browser Voice SDK live verification gate

Status: **not production-certified** until every item below has dated evidence in the release log. The current implementation intentionally loads the Twilio Voice SDK from the CDN script on `/app/calls`; if the product moves to a bundled frontend, create a separate task to install `@twilio/voice-sdk` in that frontend package and replace the CDN/global access with `import { Device } from "@twilio/voice-sdk"`.

Record the browser, OS/device, signed-in user, assigned business number, and Twilio Call SID for each successful call. Attach screenshots of the page state and browser console/network evidence.

- [ ] Open `https://<public-domain>/app/calls` over HTTPS as a logged-in user with an assigned calling number.
- [ ] Confirm the browser console has no `Device undefined`, module load, or Twilio SDK load errors.
- [ ] Confirm `GET /api/phone/voice-token` returns `Content-Type: application/json` with `success: true`, `token`, `identity`, `calling_number`, and `permitted_numbers` — not login HTML, a redirect page, or a 403/500 response.
- [ ] Confirm the browser microphone permission prompt appears during voice initialization.
- [ ] Allow microphone access and confirm the page status reaches `Browser phone registered for <assigned number>`.
- [ ] Confirm inbound readiness status changes correctly between registered and unregistered states during reload/offline/reconnect testing.
- [ ] Confirm the outbound call control is not usable until SDK loaded, token received, an assigned number exists, microphone permission is allowed, and the Twilio `Device` is registered.
- [ ] Place a real outbound call and confirm two-way audio.
- [ ] Confirm the call log records the assigned business number as `from_number`, the dialed customer as `to_number`, and final duration/status after completion.
- [ ] Deny microphone access and confirm the UI says microphone permission is blocked, not `Twilio Voice SDK unavailable`.
- [ ] Sign in as a user with no number permission and confirm the UI says `No calling number assigned`.
- [ ] Sign in as a user with a number permission where `can_call=false` and confirm calling is blocked with `No calling number assigned`/permission-denied JSON, and no token is issued.
- [ ] Confirm `/api/phone/voice-client-error` receives/logs exact client setup failures with `voice_error_code`, `voice_error_message`, user id, and company id.
