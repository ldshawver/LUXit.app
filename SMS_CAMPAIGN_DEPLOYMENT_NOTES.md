# SMS Campaign Compliance Deployment Notes

Status: not production-complete until a real staging PostgreSQL migration and Twilio staging send are verified.

## Migration

Forward migration:

```bash
psql "$DATABASE_URL" -f migrations/20260613_sms_campaign_compliance.sql
```

Rollback migration:

```bash
psql "$DATABASE_URL" -f migrations/20260613_sms_campaign_compliance_rollback.sql
```

Safety notes:

- Forward migration uses `ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` guards for partial-apply/idempotency safety.
- Rollback migration uses `DROP COLUMN IF EXISTS` and `DROP INDEX IF EXISTS` guards.
- Rollback drops the new SMS consent/campaign metadata columns; export or snapshot data first if any production/staging sends have occurred after applying the forward migration.

## Required Twilio configuration

Tenant-level Twilio config is preferred for bulk SMS and after-hours/inbox sends:

- `twilio_account.account_sid`
- `twilio_account.auth_token`
- either `twilio_account.messaging_service_sid` or `twilio_account.from_phone`

Platform fallback env vars remain supported when no tenant Twilio account is configured:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`

## Post-deploy smoke tests

```bash
pytest tests/test_sms_campaign_after_hours.py tests/test_comms_access.py tests/test_market_intelligence.py -q
```

```bash
python -m compileall lux/models/user.py models.py routes.py services/sms_service.py twilio_sms.py tests/test_sms_campaign_after_hours.py
```

## Browser QA checklist

- Login as tenant admin/admin and open `/app/campaigns`.
- Open `/app/social`.
- Open `/app/sms-campaigns`.
- Open `/campaigns`.
- Open `/social-media`.
- Open `/sms/campaigns`.
- Confirm each route loads without a 500.
- Confirm viewer role gets a clean 403.
- Confirm unauthenticated users redirect/login cleanly.
- Confirm theme, logo, CSS, navigation, sidebar, dashboard, and global branding are unchanged.

## Twilio staging checklist

- Configure tenant-level Twilio account on the staging tenant.
- Create one opted-in test contact.
- Create one opted-out test contact.
- Create one unknown-consent contact.
- Create a draft SMS campaign.
- Confirm the estimated/actual recipient list includes only the opted-in contact.
- Send the campaign to the opted-in test number.
- Confirm STOP language is present.
- Confirm sent/failed recipient statuses and Twilio SID/error metadata persist.
- Force one Twilio failure and confirm remaining recipients continue.
- Confirm after-hours custom tenant text sends after hours.
- Confirm disabled after-hours sends nothing.
- Confirm cooldown prevents repeat auto-replies.
- Confirm tenant timezone controls business-hours calculation.

## Credential and secret handling notes

- Twilio account SID/auth token access should continue to use the tenant `TwilioAccount` helpers, and diagnostics/audit logs must only store provider identifiers such as message SID/status/error code, never auth tokens.
- Social route responses and rendered pages must not include social access tokens, refresh tokens, client secrets, or API keys.
- **Known blocker before production hardening:** new `SocialMediaAccount` writes now pass through encrypted model setters, but existing plaintext social credential rows still require a verified staging/production backfill after `ENCRYPTION_MASTER_KEY` is stable. Keep those values out of responses/logs and complete the backfill before treating social publishing credentials as production-hardened.

## Follow-up issue to open

Title: Migrate social media credentials to encrypted tenant secret storage

Scope:
- Ensure `SocialMediaAccount.access_token`, refresh tokens, and provider client secrets are encrypted at rest in existing model columns or migrated to `CompanySecret` without plaintext exposure.
- Store/backfill social credentials with the app's encrypted tenant/company secret storage path and stable `ENCRYPTION_MASTER_KEY`.
- Backfill existing staging/production values through a one-time migration script that masks logs and creates a rollback/export plan.
- Update social publishing code to read credentials from encrypted storage only.
- Add tests that rendered pages, JSON responses, logs, and snapshots never include raw social tokens.

## Staging evidence log

- 2026-06-15T23:25:00Z / local Codex container / tester: OA
  - `command -v psql || true` produced no executable path.
  - `DATABASE_URL`, `PGHOST`, `PGDATABASE`, and `PGUSER` were missing.
  - Forward migration, idempotency re-run, schema verification, and disposable rollback could not be executed from this environment.
- 2026-06-15T23:25:00Z / local Codex container / tester: OA
  - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_PHONE_NUMBER` were missing, and no tenant staging Twilio account/test number was available.
  - Live Twilio STOP/START/HELP, after-hours, callback, tenant-To-number isolation, and campaign smoke tests could not be executed from this environment.

## Social credential encryption status

- `SocialMediaAccount.access_token` and `SocialMediaAccount.refresh_token` now write through `services.secret_vault.vault` encryption helpers while preserving property access for provider calls.
- Existing plaintext rows require a one-time staging/production backfill after `ENCRYPTION_MASTER_KEY` is stable: read each token through the model property and reassign it so the setter stores the encrypted value.
- Production merge remains blocked until that backfill is run and verified on staging data, or production social publishing is explicitly excluded/disabled.


## PostgreSQL migration evidence commands

Forward migration:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260613_sms_campaign_compliance.sql
```

Idempotency re-run:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260613_sms_campaign_compliance.sql
```

Schema verification queries:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "\d contact" -c "\d sms_campaign" -c "\d sms_recipient" -c "\d twilio_account" -c "\di ix_sms_campaign_company_id" -c "\di ix_sms_recipient_campaign_status"
```

Expected evidence snippets to attach:
- `ALTER TABLE` / `CREATE INDEX` commands complete without error on the first run.
- The second forward run completes without error due `IF NOT EXISTS` guards.
- `contact` shows `sms_marketing_opt_in`, `sms_marketing_opt_in_at`, `sms_marketing_opt_in_source`, `sms_opt_out_at`, and `sms_consent_status`.
- `sms_campaign` shows `company_id`, `audience_filter`, `estimated_recipient_count`, and `test_sent_at`.
- `sms_recipient` shows `phone_number`, `message_sid`, and `error_code`.
- `twilio_account` shows `after_hours_cooldown_minutes`.
- Index output includes `ix_sms_campaign_company_id` and `ix_sms_recipient_campaign_status`.

Disposable/staging rollback test:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260613_sms_campaign_compliance_rollback.sql
```

Expected rollback evidence: only migration-owned columns/indexes are removed on the disposable/staging DB; shared table rows remain present. Do not run rollback on production unless explicitly approved.

## Social token backfill runbook

Prerequisites:
- Set a stable `ENCRYPTION_MASTER_KEY` in staging/production before scanning or applying.
- Take a database snapshot before apply mode.

Dry run (counts only, no token values):

```bash
ENCRYPTION_MASTER_KEY="$ENCRYPTION_MASTER_KEY" python scripts/backfill_social_tokens.py --dry-run
```

Expected dry-run evidence format:

```text
Social token backfill summary (counts only; no secret values):
access_token_already_encrypted=<n>
access_token_plaintext=<n>
accounts_seen=<n>
accounts_updated=0
refresh_token_already_encrypted=<n>
refresh_token_plaintext=<n>
mode=dry_run
```

Apply mode:

```bash
ENCRYPTION_MASTER_KEY="$ENCRYPTION_MASTER_KEY" python scripts/backfill_social_tokens.py --apply
```

Verification:

```bash
ENCRYPTION_MASTER_KEY="$ENCRYPTION_MASTER_KEY" python scripts/backfill_social_tokens.py --dry-run
```

Expected post-apply evidence: plaintext counts are `0`; already-encrypted counts match populated token fields; no token values appear in output.

Rollback guidance:
- Prefer restoring the pre-apply database snapshot if rollback is required.
- Do not decrypt and dump social tokens into logs, files, PR comments, or support tickets.
- If emergency rollback requires decrypting individual credentials, do it through an audited admin-only maintenance shell and rotate affected provider tokens afterward.

## Production gate checklist

- [ ] Staging PostgreSQL forward migration succeeds.
- [ ] Forward migration idempotency re-run succeeds.
- [ ] Disposable/staging rollback succeeds and removes only migration-owned objects.
- [ ] Social token plaintext backfill dry-run/apply/verification completed with counts-only evidence.
- [ ] Live Twilio STOP/START/HELP smoke tests pass.
- [ ] Opt-out exclusion test passes.
- [ ] Re-opt-in inclusion test passes.
- [ ] Inbound reply attribution maps to the correct tenant/conversation.
- [ ] After-hours auto-reply sends correct tenant text and cooldown works.
- [ ] Delivery status callback persists recipient status without secrets.
- [ ] Tenant routing by To number / tenant Twilio config verified.
- [ ] Browser QA confirms Social and SMS Campaign pages load and branding/navigation/responsive layout are unchanged.

## 2026-06-17 SMS campaign audit hotfix post-merge checklist

Apply this checklist immediately after deploying the SMS campaign audit fix that added
`migrations/20260617_sms_campaign_audit_fix.sql`.

### 1. Apply migration

Run with `ON_ERROR_STOP` so deployment fails fast if PostgreSQL rejects any
schema repair statement:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260617_sms_campaign_audit_fix.sql
```

Recommended idempotency confirmation:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260617_sms_campaign_audit_fix.sql
```

### 2. Restart services

Use the command that matches the production process manager:

```bash
systemctl restart lux-email-bot
```

or

```bash
docker compose restart web lux-email-bot
```

or

```bash
pm2 restart all
```

### 3. Smoke test the SMS campaign flow

In a browser session for an authorized tenant admin/manager/editor:

- Open `/app/sms-campaigns` and confirm it loads.
- Click the left sidebar **SMS Campaigns** link and confirm it lands on the canonical campaign list.
- Open `/sms` and confirm it redirects to `/app/sms-campaigns`.
- Open `/sms-dashboard` and confirm it redirects to `/app/sms-campaigns`.
- Open `/sms/create` and confirm it loads without a 500.
- Create a draft SMS campaign.
- Schedule an SMS campaign.
- Confirm the scheduled campaign appears on the marketing calendar.
- Confirm the calendar SMS campaign edit link opens the campaign edit page.
- Confirm the analytics page loads SMS metrics without crashing.
- Confirm AI reports/agent context include only the active tenant's SMS campaigns.

### 4. Watch production logs

Watch the web worker and background worker logs during the smoke test and verify
there are no new occurrences of:

- `UndefinedColumn`
- `BuildError`
- `500`
- `Company.query.first()`
- cross-tenant SMS campaign/template/contact leakage

Example journal command:

```bash
journalctl -u lux-email-bot -f | rg "UndefinedColumn|BuildError|\b500\b|Company\.query\.first\(|cross-tenant|sms_campaign|sms_template"
```

Example Docker command:

```bash
docker compose logs -f web lux-email-bot | rg "UndefinedColumn|BuildError|\b500\b|Company\.query\.first\(|cross-tenant|sms_campaign|sms_template"
```
