# PWA Phone Production Gate Status

Status: **not production-ready**.

This file is the deployment evidence log for the PWA phone system. Do not move this feature to production until every gate below has dated staging evidence, tester initials, Twilio Call SIDs where applicable, and screenshots/log excerpts attached to the release ticket.

## Gate 1: staging PostgreSQL migration

Current status: **pending** — this repository change cannot apply the staging database migration from the local container.

Required commands:

```bash
psql "$STAGING_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260615_pwa_phone_system.sql
psql "$STAGING_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260615_pwa_phone_system.sql
```

Required verification queries:

```sql
SELECT to_regclass('public.phone_settings') AS phone_settings_table,
       to_regclass('public.call_event') AS call_event_table;

SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'twilio_call_log'
  AND column_name IN (
    'parent_call_sid', 'forwarded_to_number', 'contact_id', 'customer_id',
    'assigned_user_id', 'answered_by_user_id', 'answered_at', 'ended_at',
    'recording_url', 'recording_sid', 'voicemail_url', 'voicemail_sid',
    'transcription_text', 'transcription_status', 'transcription_provider',
    'metadata_json', 'is_read', 'is_archived', 'updated_at'
  )
ORDER BY column_name;

SELECT indexname
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN (
    'ix_twilio_call_log_company_created',
    'ix_twilio_call_log_company_status',
    'ix_twilio_call_log_twilio_sid',
    'ix_twilio_call_log_recording_sid',
    'ix_twilio_call_log_voicemail_sid',
    'ix_phone_settings_company',
    'ix_call_event_call_log_id',
    'ix_call_event_type_created'
  )
ORDER BY indexname;

SELECT conname
FROM pg_constraint
WHERE conname IN (
    'fk_twilio_call_log_contact',
    'fk_twilio_call_log_assigned_user',
    'fk_twilio_call_log_answered_user',
    'uq_call_event_idempotency'
  )
ORDER BY conname;

SELECT COUNT(*) AS existing_call_logs_after_migration FROM twilio_call_log;
```

Pass criteria:

- Both migration runs complete with no errors.
- Existing `twilio_call_log` row count matches the pre-migration count.
- `phone_settings` and `call_event` exist.
- All expected columns, indexes, and constraints are present.
- A backup/snapshot exists before rollout.
- Rollback has been rehearsed or explicitly approved as snapshot restore only.

## Gate 2: real Twilio Voice QA

Current status: **pending** — no real Twilio call SID evidence is present in this repository.

| Scenario | Required evidence | Result |
| --- | --- | --- |
| Business-hours inbound call rings PWA | Twilio Call SID, screenshot/video of `/app/calls` ringing, server logs | Pending |
| Accept connects two-way browser audio | Call SID, tester notes from caller and CSR, browser console clean/error notes | Pending |
| Decline works | Call SID, status callback payload, recent-call row screenshot | Pending |
| Send to voicemail works | Call SID, voicemail recording SID/URL, playback screenshot | Pending |
| Forwarding works | Parent/child Call SIDs, destination number masked in notes, call log status | Pending |
| Missed call logs correctly | Call SID, Recent Calls Missed tab screenshot | Pending |
| Recording callback saves playback URL | Recording SID, persisted `recording_url`, Recent Calls playback screenshot | Pending |
| Transcription callback saves text | Transcription callback payload, persisted transcript screenshot | Pending |
| Status callbacks update call log | Twilio callback logs and final DB call status | Pending |

## Gate 3: PWA device QA

Current status: **pending**.

| Device/browser | Required checks | Result |
| --- | --- | --- |
| Desktop Chrome | receive invite, audible/visible ring, microphone prompt, accept, mute, end, reconnect/token refresh | Pending |
| iPhone Safari browser | receive invite, visible ring, microphone prompt, decline, voicemail, Recent Calls refresh | Pending |
| Installed iPhone PWA | launch from home screen, receive/ring behavior, accept audio, end call, reconnect/error UX | Pending |

## Gate 4: call-triggered SMS behavior

Current status: **covered by focused automated tests, pending real Twilio SMS QA**.

| Scenario | Required evidence | Result |
| --- | --- | --- |
| Missed-call SMS enabled | Call SID, outbound SMS SID, settings screenshot | Pending |
| Missed-call SMS disabled | Call SID, proof no outbound SMS sent | Pending |
| After-hours SMS | Call SID, outbound SMS SID, after-hours settings screenshot | Pending |
| STOP suppression | Opted-out recipient fixture/proof and no outbound SMS SID | Pending |
| Cooldown suppression | Repeated call evidence and no duplicate outbound SMS within cooldown | Pending |
| Tenant isolation | Tenant A/B settings and logs proving no cross-tenant SMS/call access | Pending |

## Gate 5: automated-suite status

Current status: **focused phone gate passes; full repository suite is not green**.

Required phone gate:

```bash
uv run --python 3.12 pytest tests/test_pwa_phone_system.py tests/test_feedback.py tests/test_market_intelligence.py -q
```

Latest focused result: `41 passed`.

Broad-suite tracking command:

```bash
uv run --python 3.12 pytest -q
```

Latest documented broad result: `231 passed`, `35 failed`, `24 errors`. Do not describe the whole app as green until the broad suite is fixed or the remaining legacy tests are formally retired.

## Final release rule

The PWA phone system can be called production-ready only after Gates 1 through 4 are complete with evidence and Gate 5 has an approved CI policy for the release branch.
