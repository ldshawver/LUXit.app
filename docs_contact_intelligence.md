# Contact Intelligence / CRM Rollout

## Audit findings
LUXit is a Flask + Flask-SQLAlchemy application. The canonical tenant is `Company` and the canonical contact model/table is `Contact` / `contact`. Existing contact-related tables include campaign recipients, SMS recipients, segment members, Twilio conversations/messages/call logs, contact activities, Google OAuth/sync jobs, integration audit logs, CRM deals/tasks, and conversion/analytics records.

Known creation/update paths found in the audit:
- `/contacts/add`, `/contacts/import`, import preview/template, and `/api/contacts/add-subscriber` in `routes.py`.
- Marketing API duplicate review/merge and SMS audience workflows in `marketing_api.py`.
- CSV/XLSX import and Twilio/marketing upsert through `services/contact_audience.py`.
- Twilio inbound/outbound SMS/calls in `twilio_sms.py`, `inbox_pwa.py`, and `services/integrations/twilio_service.py`.
- Existing Google Contacts OAuth/sync in `google_contacts.py` with `contacts.readonly` behavior and sync job auditing.
- Existing merge helpers in `services/contact_dedupe.py` and source helpers in `services/contact_source.py`.

Existing risks: duplicate logic previously included name+company as an automatic merge key; phone normalization was duplicated and regex-based; source fields were mutable single fields without append-only event history; Google token storage exists in a legacy table and should be migrated to the encrypted/ref based connection table for new work.

## Schema changes
Apply `migrations/20260714_contact_intelligence_crm.sql`. It adds nullable/backward-compatible Contact intelligence fields and creates `contact_phone_number`, `contact_email_address`, `contact_source_event`, `google_contact_connection`, `opportunity`, and `contact_task`.

## Migration command
Back up production first, then run the project’s normal SQL migration mechanism, for example:

```bash
psql "$DATABASE_URL" -f migrations/20260714_contact_intelligence_crm.sql
```

## Rollback procedure
Because the migration is additive, preferred rollback is application rollback without dropping data. If required after backup, drop the added tables and columns in reverse order. Do not drop audit/source tables until exported.

## Google Cloud Console
Enable People API. Configure OAuth consent and redirect URI matching the existing `/twilio/google-contacts/callback`. Use only `https://www.googleapis.com/auth/contacts.readonly` until write/export is explicitly required.

## Administrator cleanup order
1. Audit only with `cleanup_audit(company_id)`.
2. Back up `contact`, Twilio, campaign, segment, order/contract/invoice, and activity tables.
3. Run idempotent phone normalization/backfill in batches.
4. Backfill source attribution only from provable records; otherwise mark legacy/unknown.
5. Generate duplicate candidates.
6. Preview high-confidence merges.
7. Merge only after administrator confirmation.
8. Review ambiguous duplicates.
9. Connect Google Contacts.
10. Match unnamed contacts.
11. Review ambiguous Google matches.
12. Enable automatic enrichment.
13. Monitor sync jobs/errors and retry failed batches.

## Privacy and secrets
Do not place OAuth/Twilio credentials in source, browser storage, logs, or diagnostics. Google matching defaults to company/user ownership and read-only contacts. Disable Google enrichment by disconnecting the connection or disabling the matching job; Twilio/contact creation continues.

## Supported admin/API workflows
All commands below are development/staging examples and must be run by an authenticated company admin through the existing Flask app; do not run production cleanup from a shell without a backup and approval.

### Admin APIs
- Audit / create a resumable job: `POST /api/marketing/contacts/intelligence/jobs` with JSON `{ "job_type": "duplicate_scan", "dry_run": true, "batch_size": 100 }`.
- Normalize/backfill phones: same endpoint with `job_type=phone_backfill`; dry-run defaults to true; set `dry_run=false` only after backup.
- Backfill provable attribution: `job_type=attribution_backfill`.
- Generate duplicate candidates: `job_type=duplicate_scan`.
- Match unnamed contacts from local Google cache: `job_type=unnamed_match` or `POST /api/marketing/contacts/google/match-unnamed`.
- Run/resume a job: `POST /api/marketing/contacts/intelligence/jobs/<job_id>/run` with optional `{ "max_batches": 1 }`.
- View job progress: `GET /api/marketing/contacts/intelligence/jobs/<job_id>`.
- Google status: `GET /api/marketing/contacts/google/status`.
- Google connect URL: `GET /api/marketing/contacts/google/connect`.
- Google sync: `POST /api/marketing/contacts/google/sync`.
- Disconnect Google: `POST /api/marketing/contacts/google/disconnect`.
- Recheck one contact: `POST /api/marketing/contacts/<contact_id>/google/recheck`.
- Review ambiguous Google matches: `GET /api/marketing/contacts/google/ambiguous`.
- Accept/reject Google suggestion: `POST /api/marketing/contacts/<contact_id>/google/suggestion`.
- Duplicate review UI: `GET /contacts/duplicates/review`.
- Mark not duplicate: `POST /api/marketing/contacts/duplicates/not-duplicate`.

### Job semantics
Jobs are company scoped, admin controlled, batched by `batch_size`, resumed from `checkpoint.last_contact_id`, and safe to retry. Each job persists status, cursor/checkpoint, totals, processed, updated, skipped, ambiguous, failed, timestamps, initiating user, and sanitized error details in `contact_intelligence_job`. Logs remain in normal application logs; per-record failures are retained in the job row. Rollback is limited to restoring the pre-run database backup or manually reversing reviewed changes; merged contacts are archived rather than hard-deleted.

## Contact path integration checklist
| Contact path | Source value | Canonical resolver used | Exact-match behavior | Test added |
|---|---|---|---|---|
| `/contacts/add` | `manual_entry` | `resolve_contact` | Reuses same-company exact normalized phone/email | `test_contacts_add_route_uses_canonical_resolution` |
| `/contacts/import` CSV | `csv_import` | `import_contacts` → `upsert_contact_from_source` → canonical normalizer/source events | Reuses same-company exact normalized phone/email | existing import tests + route coverage |
| `/api/contacts/add-subscriber` | `manual_entry` | `resolve_contact` | Reuses same-company exact email | route covered indirectly through resolver tests |
| Public newsletter form | `website_form` | `resolve_contact` with fallback company | Reuses exact email within fallback company | not yet route-tested |
| Twilio inbound SMS conversation/capture | `twilio_inbound_sms` | `upsert_contact_from_source` | Reuses exact normalized phone within company | existing Twilio/contact tests |
| Twilio integration unknown lead | `twilio_inbound_sms` | `resolve_contact` | Reuses exact normalized phone within company | syntax/target tests |
| Google Contacts sync-created contacts | `google_contacts` | `resolve_contact` | Reuses exact normalized phone/email within company | Google matching tests |
| Duplicate review mark-not-duplicate | n/a | persistent exclusion | Pair excluded from future candidates | `test_not_duplicate_pair_is_persisted_and_excluded` |
| Contact intelligence jobs | `legacy` / job type | `sync_contact_points`, `apply_source_attribution`, Google local cache | Resumable by contact id cursor | `test_contact_intelligence_job_api_requires_admin_and_resumes` |

## Current PR review of `f29fbb1`
- Branch: `work`.
- PR URL: unavailable in this container because no git remote/hosting metadata is configured.
- Diff summary: 9 files, 690 insertions and 21 deletions in contact schema, migration, normalizer, intelligence service, dedupe hardening, import/upsert wiring, docs, and tests.
- Production DB compatibility: LUXit production docs point to PostgreSQL. The migration uses PostgreSQL-compatible `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `SERIAL`, partial indexes, and quoted `"user"`; it is not a SQLite migration and was not run against production.
- Foreign keys: new FKs reference `company(id)`, `contact(id)`, and `"user"(id)`, matching the existing integer primary-key models.
- Model/migration names: model columns match the migration for the deployed fields; `ContactSourceEvent.event_metadata` maps to SQL column `metadata` intentionally to avoid SQLAlchemy's reserved `metadata` attribute name.
- Idempotency: migration uses `IF NOT EXISTS` for columns/tables/indexes and is designed to be re-runnable on PostgreSQL.
- Model registration: new models import through `models.py`; circular import checks pass via `py_compile`.
- Dependency conflicts: `phonenumbers==8.13.52` is a leaf dependency and no installed package in this repo pins an incompatible version.
- Test caveat: most existing tests use SQLite/in-memory `db.create_all()`, which can conceal PostgreSQL-specific SQL migration errors; migration validation is therefore a separate staging step.
- Scope: `f29fbb1` contained only LUXit contact/CRM work.
