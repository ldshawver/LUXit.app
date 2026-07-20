# Audience PostgreSQL compatibility matrix

The incident journal was not available in the development container. A PostgreSQL 16 reproduction of the pre-CRM deployment produced `sqlalchemy.exc.ProgrammingError` wrapping `psycopg2.errors.UndefinedColumn: column contact.display_name does not exist` in the `Contact` pagination query in `routes.py`. The ORM-generated statement selected all mapped `contact` columns, filtered by the authenticated user's `company_id`, `is_active`, and `archived_at`, and failed at the first absent cumulative-CRM column (`display_name`).

| ORM field/table | Migration introducing it | Ledger registration | Production presence at incident recovery | Action required |
|---|---|---|---|---|
| `contact` legacy identity, tenant (`company_id`, `tenant_id`), name, phone, email, tags, segment, `is_active` | legacy schema; additive compatibility through `20260630_contact_source_dedupe.sql` | Historical deployment state; runner records SQL filename/checksum | Base table present; `is_active` was queried (not `active`) | Preserve company predicate; no rename |
| `contact` lifecycle, owner, archive, source, Google-match, dedupe, lead/opportunity summary, consent fields | `20260714_contact_intelligence_crm.sql` | `schema_migrations` via `scripts/apply_migrations.py` | Cumulative CRM fields were absent in the reproduced pre-repair schema; first failing field was `display_name` | Apply the ledgered CRM migration before restart |
| `contact.updated_at` | `20260718_audience_schema_repair.sql` | `schema_migrations` via canonical runner | Omitted by the original CRM SQL although mapped by the ORM | Add, backfill from `created_at`, and set default |
| `contact_phone_number` / `contact_email_address` | `20260714_contact_intelligence_crm.sql` | Same | Not used by list query; required by Audience detail/intelligence paths | Verify table presence before restart |
| `contact_source_event` | `20260714_contact_intelligence_crm.sql` | Same | Optional relationship; not joined by list query | Verify table presence before restart |
| `google_contact_connection` / `google_contact_lookup` | `20260714_contact_intelligence_crm.sql` | Same | Optional; list uses scalar `contact.google_match_status`, not a join | Verify connection table before restart |
| `opportunity` | `20260714_contact_intelligence_crm.sql` | Same | List aggregate is explicitly scoped by `company_id` and grouped by `contact_id` | Keep aggregate and verify table before restart |
| `contact_task` | `20260714_contact_intelligence_crm.sql` | Same | Optional relationship; not joined by list query | Verify table before restart |
| `segment` / `segment_member` | `20260618_segment_management_suppression.sql` | Same | List uses legacy scalar `contact.segment`/`tags`; no segment-table join | Verify tables and retain tenant ownership on `segment.company_id` |
| User archive/session fields | `20260718_user_archive_restore.sql` | Same | Manually confirmed present during recovery | Runner records migration and deploy verification checks columns |

All Audience list predicates derive the tenant from `current_user.default_company_id`; no request parameter can replace it. The opportunity aggregate independently repeats the same company predicate, preventing cross-tenant aggregate leakage. Optional relationships are not inner-joined by the list query, so missing related rows remain valid. PostgreSQL—not SQLite—is required for migration and missing-column regression validation.
