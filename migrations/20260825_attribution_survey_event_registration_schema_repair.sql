BEGIN;

-- Same class of drift as 20260825_lead_score_schema_repair.sql: these three
-- tables were never brought under the migration ledger and diverged from
-- models.py (all three model classes trace to commit a3613d7, 2026-03-29 --
-- the same commit that (re-)introduced LeadScore -- which strongly suggests
-- a bulk models.py sync that was never followed by a matching migration).
-- All three are genuinely live-used by reachable, @login_required routes +
-- templates, not dead scaffolding:
--   - AttributionModel.campaign_id: joined/filtered in routes.py's
--     /roi-analytics, /analytics/attribution, /analytics/ltv routes
--     (routes.py:7730, 11864, 11912).
--   - SurveyResponse: routes.py's /surveys route queries every declared
--     column and templates/surveys.html renders survey_type, score,
--     sentiment, feedback, responded_at directly.
--   - EventRegistration: routes.py's /events/<id> route (view_event, line
--     4629) filters by event_id and reads contact_id; templates/
--     view_event.html renders registered_at, payment_status, status.
-- This is also what breaks services/contact_dedupe.py::merge_contacts()'s
-- reference-repointing step for ANY contact merge, not just #1/#2088 --
-- _repoint_generic_relationship() issues a full ORM entity query against
-- every contact_id-bearing model, and these three tables' contact_id column
-- (the very column being repointed) doesn't exist live.
--
-- All three tables are empty (0 rows) in production, so this is purely
-- additive -- no data loss, no column removal, no rename, no type change.
-- Column types/defaults mirror the model declarations exactly, following
-- the same convention as the LeadScore repair (DB-level DEFAULT only where
-- the model declares a static Python-side default= on a simple scalar
-- column; no DEFAULT added for datetime columns, matching LeadScore's
-- last_calculated precedent).

ALTER TABLE attribution_model ADD COLUMN IF NOT EXISTS campaign_id INTEGER;
ALTER TABLE attribution_model ADD COLUMN IF NOT EXISTS contact_id INTEGER;
ALTER TABLE attribution_model ADD COLUMN IF NOT EXISTS attribution_model VARCHAR(50);
ALTER TABLE attribution_model ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION;

ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS contact_id INTEGER;
ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS survey_type VARCHAR(50);
ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS score INTEGER DEFAULT 0;
ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS feedback TEXT;
ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS sentiment VARCHAR(50);
ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS sentiment_score DOUBLE PRECISION;
ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS topics TEXT;
ALTER TABLE survey_response ADD COLUMN IF NOT EXISTS responded_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE event_registration ADD COLUMN IF NOT EXISTS event_id INTEGER;
ALTER TABLE event_registration ADD COLUMN IF NOT EXISTS contact_id INTEGER;
ALTER TABLE event_registration ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'registered';
ALTER TABLE event_registration ADD COLUMN IF NOT EXISTS payment_status VARCHAR(50) DEFAULT 'pending';
ALTER TABLE event_registration ADD COLUMN IF NOT EXISTS registered_at TIMESTAMP WITHOUT TIME ZONE;

COMMIT;
