# SMS Phone-Line Campaign Deployment Verification

## Forward migration

The production workflow `.github/workflows/push-to-production.yml` runs every `migrations/*.sql` file with `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"` when `psql` and the migrations directory are present. Confirm that workflow is still active before deploy.

Run the migration manually on staging/production with PostgreSQL error-stop enabled if you need to verify outside the workflow:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260619_sms_phone_line_campaign_completion.sql
```

The migration is idempotent: it uses `ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`, does not drop data, and can be re-run safely during production deploy retries.

## Schema drift verification

After migration, verify the tables used by SMS, phone routing, campaign calendar, and PWA history still expose the expected columns:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c "\d twilio_phone_number" \
  -c "\d twilio_account" \
  -c "\d sms_campaign" \
  -c "\d sms_recipient" \
  -c "\d sms_template" \
  -c "\d twilio_conversation" \
  -c "\d twilio_message" \
  -c "\d calendar_event" \
  -c "\di ix_twilio_phone_number_company_campaign_sender" \
  -c "\di ix_sms_campaign_company_status_scheduled"
```

Expected evidence:

- `twilio_phone_number` includes `status_callback_webhook_url`, `number_auto_reply_text`, `campaign_sender_enabled`, `campaign_default_batch_size`, `campaign_send_rate_per_minute`, and `allow_global_fallback`.
- `sms_campaign` includes `media_urls`, `batch_size`, `send_rate_per_minute`, `canceled_at`, and `archived_at`.
- `sms_recipient`, `twilio_conversation`, and `twilio_message` remain present so existing PWA conversation history is preserved.
- Both new indexes are present.

## Route smoke checks

Authenticated browser checks:

```bash
curl -I https://<host>/sms/create
curl -I https://<host>/app/sms-campaigns
curl -I https://<host>/settings/phone-lines
curl -I https://<host>/admin/phone-lines
curl -I https://<host>/admin/communications
curl -I https://<host>/sms-dashboard
```

Expected evidence:

- `/sms/create`, `/app/sms-campaigns`, `/settings/phone-lines`, `/admin/phone-lines`, and `/admin/communications` return 200 for an authorized admin.
- `/sms-dashboard` redirects to a working SMS campaigns route.
- No `UndefinedColumn`, `BuildError`, or 500 appears in application logs.

## Conditional acceptance gate

This change is not production-certified from the local Codex environment alone. Treat merge/deploy as appropriate only after the live LUXit VPS passes the checks below and `journalctl` shows no `UndefinedColumn`, `ProgrammingError`, `BuildError`, or permission errors for the touched routes.

## Functional staging checks

1. Open **Phone Lines & Webhooks** and verify every number shows exact inbound SMS, inbound voice, and status callback webhook fields.
2. Confirm each line clearly shows whether it is line-specific or has global/API fallback enabled.
3. Send inbound SMS and inbound call traffic to each managed number and verify audit logs record the expected `company_id`, phone-number ID, and settings source.
4. Create a scheduled SMS campaign with a permitted sender number, edit message/media/sender/date/time before send, duplicate it, test-send it, cancel it, and archive it.
5. Upload the canonical CSV/XLSX contact template and confirm preview maps text opt-in, email opt-in, do-not-market, SMS opt-out, email opt-out, tags, notes, and duplicate status.
6. Confirm opted-out contacts are skipped and Twilio failures mark recipients failed instead of sent.
7. Confirm scheduled SMS campaigns appear on the marketing calendar, analytics, and AI SMS campaign context.
8. Confirm a staff/mobile-inbox user can open `/app/inbox` without manage/campaign permissions.
9. Confirm a non-admin cannot send from an unassigned number, while an admin/owner can send from permitted company numbers.
10. Confirm PWA inbox history remains visible for all permitted numbers after browser/PWA reopen, including read conversations in `filter=all`, and that no user-only filter hides shared-number history.

## Live log checks

```bash
journalctl -u luxit --since "30 minutes ago" --no-pager | egrep -i "UndefinedColumn|ProgrammingError|BuildError|permission|Traceback" || true
```

Expected evidence: no new errors tied to `/sms/create`, `/app/sms-campaigns`, `/twilio/comms`, `/app/inbox`, phone-line settings, campaign send, or contact import preview.

## Rollback notes

The preferred rollback is code rollback only because the added columns are additive and preserve data. If a database rollback is explicitly required after exporting/snapshotting data, drop only the migration-owned indexes and columns:

```sql
DROP INDEX IF EXISTS ix_twilio_phone_number_company_campaign_sender;
DROP INDEX IF EXISTS ix_sms_campaign_company_status_scheduled;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS status_callback_webhook_url;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS number_auto_reply_text;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS campaign_sender_enabled;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS campaign_default_batch_size;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS campaign_send_rate_per_minute;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS allow_global_fallback;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS media_urls;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS batch_size;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS send_rate_per_minute;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS canceled_at;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS archived_at;
```

Do not run the SQL rollback on production unless product owners accept losing values stored in the new settings fields.
