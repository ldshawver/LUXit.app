BEGIN;

-- The live `lead_score` table was never brought under the migration ledger and
-- diverged from models.py::LeadScore (stable since commit a3613d7, 2026-03-29).
-- routes.py (dashboard/lead-scoring views) reads LeadScore.lead_score,
-- .behavior_score, .engagement_score, .last_calculated via the ORM; the live
-- table has score/intent_score/scoring_factors/last_activity_date instead,
-- none of which any application code references. This crashes every ORM
-- query against LeadScore, including services/contact_dedupe.py::merge_contacts()'s
-- reference-repointing step. Table is empty (0 rows) in production, so this is
-- purely additive -- no data loss, no column removal, no rename.

ALTER TABLE lead_score ADD COLUMN IF NOT EXISTS lead_score INTEGER DEFAULT 0;
ALTER TABLE lead_score ADD COLUMN IF NOT EXISTS behavior_score INTEGER DEFAULT 0;
ALTER TABLE lead_score ADD COLUMN IF NOT EXISTS last_calculated TIMESTAMP WITHOUT TIME ZONE;

COMMIT;
