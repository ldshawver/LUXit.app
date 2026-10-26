-- Deal timeline. The CRM deal routes (detail, stage change, activity, json,
-- delete) all import models.DealActivity, which never existed, so every
-- per-deal page and API returned 500. Forward-only, idempotent, additive:
-- creates an empty table and touches no existing rows.

CREATE TABLE IF NOT EXISTS deal_activity (
    id SERIAL PRIMARY KEY,
    deal_id INTEGER NOT NULL REFERENCES deal(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES "user"(id),
    activity_type VARCHAR(50) NOT NULL DEFAULT 'note',
    description TEXT,
    activity_date TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_deal_activity_deal_id ON deal_activity (deal_id);
