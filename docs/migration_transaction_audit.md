# Migration transaction audit

Static audit date: 2026-07-18.

The runner defaults migrations to `psql --single-transaction -v ON_ERROR_STOP=1` unless a file is already explicitly wrapped in `BEGIN;`/`COMMIT;`. Files containing transaction-incompatible statements such as `CREATE INDEX CONCURRENTLY`, `VACUUM`, `REINDEX`, `CLUSTER`, `CREATE DATABASE`, `DROP DATABASE`, or `ALTER TYPE ... ADD VALUE` are rejected unless they carry explicit nontransactional metadata and are allowlisted at runtime.

## Classification

| Migration | Classification | Notes |
|---|---|---|
| `20260613_sms_campaign_compliance.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260615_pwa_phone_system.sql` | Already transaction wrapped | Contains explicit `BEGIN;` and `COMMIT;`; runner does not add `--single-transaction`. |
| `20260616_agent_company_scope.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260616_sms_campaign_segment.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260617_comms_phone_number_permissions.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260617_sms_campaign_audit_fix.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260618_segment_management_suppression.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260619_sms_phone_line_campaign_completion.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260620_voice_notification_calllog_compat.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260621_contact_name_backfill_fields.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260621_license_billing_feature_management.sql` | Safe with `--single-transaction` | Includes idempotent seed/upsert statements; no transaction-incompatible statements found. |
| `20260621_pwa_alerts_greetings.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260622_after_hours_sms_canonical.sql` | Safe with `--single-transaction` | Includes data backfill `UPDATE`; no transaction-incompatible statements found. |
| `20260623_paylink_documenso_production_fix.sql` | Already transaction wrapped | Contains explicit `BEGIN;` and `COMMIT;`; runner does not add `--single-transaction`. |
| `20260630_contact_source_dedupe.sql` | Safe with `--single-transaction` | Includes data normalization `UPDATE`; no transaction-incompatible statements found. |
| `20260702_pwa_device_approval.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260705_pwa_sound_forwarding_autoreply.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `20260714_contact_intelligence_crm.sql` | Safe with `--single-transaction` | Additive contact-intelligence migration; no transaction-incompatible statements found. |
| `20260718_user_archive_restore.sql` | Safe with `--single-transaction` | Additive user/archive columns and idempotent active backfill; no transaction-incompatible statements found. |
| `analytics_v3_7_2.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |
| `marketing_hub_sms_20260615.sql` | Safe with `--single-transaction` | Contains PL/pgSQL `END;` block terminators but no top-level `COMMIT;` or transaction-incompatible statements. |
| `phase_2_6_schema.sql` | Safe with `--single-transaction` | Static scan found no transaction-incompatible statements. |

## Rollback consequences

No migration currently requires nontransactional execution. If a future migration is marked `-- luxit-migration: transaction=off`, the operator must allowlist its basename with `--allow-nontransactional` and document why PostgreSQL cannot roll it back atomically before deployment.
