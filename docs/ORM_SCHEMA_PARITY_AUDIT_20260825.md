# ORM / Live-Schema Parity Audit — 2026-08-25

Produced during the Contact Integrity Remediation work (Segment Engine acceptance →
Contact Integrity audit → Code/Schema Safety phase → this prerequisites phase).
Full comparison of all 184 mapped SQLAlchemy models in `models.py` against the live
`luxdb` production schema (200 live tables total; 16 have no model).

**Method:** `sqlalchemy.inspect(db.engine)` reflected live table columns, diffed
column-by-column against each model's declared `__table__.columns`.

## Root cause, all instances

Every mismatch below traces to the same pattern: a model class was added to
`models.py` in a large, topically-unrelated commit (most trace to `a3613d7`,
2026-03-29, "Task #11: Brand Kit Phase 1 — Token System Expansion", a ~1,721-line
diff whose commit message has nothing to do with any of these tables) without a
matching migration. The live table was left at whatever `db.create_all()` produced
at an earlier point, so the model runs ahead of the database. No migration file or
`schema_migrations` ledger entry ever touched any of the affected tables before this
audit.

## Fixed (blocking) — repaired 2026-08-25

| Model / Table | Missing live columns | Live-used by | Migration |
|---|---|---|---|
| `LeadScore` / `lead_score` | `lead_score`, `behavior_score`, `last_calculated` | `routes.py` dashboard/lead-scoring views | `migrations/20260825_lead_score_schema_repair.sql` |
| `AttributionModel` / `attribution_model` | `campaign_id`, `contact_id`, `attribution_model`, `confidence_score` | `routes.py:7730,11864,11912` (`/roi-analytics`, `/analytics/attribution`, `/analytics/ltv`) | `migrations/20260825_attribution_survey_event_registration_schema_repair.sql` |
| `SurveyResponse` / `survey_response` | `contact_id`, `survey_type`, `score`, `feedback`, `sentiment`, `sentiment_score`, `topics`, `responded_at` | `routes.py:7743` (`/surveys`) + `templates/surveys.html` | same as above |
| `EventRegistration` / `event_registration` | `event_id`, `contact_id`, `status`, `payment_status`, `registered_at` | `routes.py:4629` (`view_event`) + `templates/view_event.html` | same as above |

All four: additive-only (`ADD COLUMN IF NOT EXISTS`), 0 rows in every affected table
at time of repair, backed up before applying
(`/root/luxit-database-backups/luxit-before-lead_score_schema_repair-20260825T012309Z.dump`
and `luxit-before-attribution_survey_event_registration_repair-20260825T031242Z.dump`),
verified idempotent, verified parity post-repair.

These four were also what broke `services/contact_dedupe.py::merge_contacts()`'s
generic contact-reference repointing step
(`_repoint_generic_relationship`/`related_record_counts`, which issue full ORM
entity queries against every `contact_id`-bearing model) — for **any** contact
merge, not specific to one contact pair.

## Deferred (non-blocking) — 29 tables, not repaired

None declare a `contact_id` column, so none affect `merge_contacts()`. Concentrated
in evidently dormant/half-built feature areas:

- **AI-agent scaffolding:** `agent_automation`, `agent_configuration`, `agent_deliverable`, `agent_memory`, `agent_report`
- **SEO/competitor tooling:** `seo_backlink`, `seo_competitor`, `competitor`, `competitor_profile`, `keyword_research`, `market_signal`, `strategy_recommendation`
- **Social/OAuth integrations:** `instagram_oauth`, `tiktok_oauth`, `social_media_account`, `social_post`
- **Marketing scaffolding:** `ab_test`, `multivariate_test`, `automation`, `automation_step`, `campaign_cost`, `newsletter_archive`, `personalization_rule`, `event`, `demo_request`, `wordpress_integration`, `company_integration_config`, `approval_queue`

Each of these needs the same per-table forensic pass (git origin, runtime
read/write grep, row count) that `LeadScore`/`AttributionModel`/`SurveyResponse`/
`EventRegistration` got before any migration is written — do not bulk-fix by
column-name similarity alone.

## Known related, separate issue (not schema drift)

`templates/surveys.html:54` references `response.contact`, but `SurveyResponse`
has no `contact` relationship/backref declared in `models.py`. Currently
non-fatal (Jinja's `Undefined` is falsy, so the `if response.contact else
'Anonymous'` ternary degrades gracefully), but is a latent bug worth a follow-up
ticket once `SurveyResponse` gets real data.

## Recommended follow-up (not implemented)

`_repoint_generic_relationship`/`related_record_counts` in
`services/contact_dedupe.py` currently issue full ORM entity SELECTs against
every `contact_id`-bearing model, so *any* future column drift on *any* such
table reintroduces this exact failure mode. No `@validates`/`event.listens_for`
hooks exist on `contact_id` anywhere in `models.py` (confirmed by grep), so
narrowing these two functions to Core-level queries selecting only
`(primary_key, contact_id)` would be safe and would make the merge path robust
against future drift, independent of fixing today's three tables. Also fixes a
confirmed inconsistency: `related_record_counts()` currently swallows
`UndefinedColumn` per-table and silently reports `0`, while `merge_contacts()`
lets the same exception abort the whole transaction — same bug, two different
(both wrong) behaviors.
