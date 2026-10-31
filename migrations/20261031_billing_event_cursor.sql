-- Stripe webhook ordering (billing lifecycle hardening).
-- Forward-only, idempotent, additive: one empty table, no existing rows touched.
-- Holds the `created` time of the newest status-changing Stripe event applied
-- to each workspace so a late/retried older event cannot overwrite newer state.
-- Without this table the webhook keeps working and simply skips the check.

CREATE TABLE IF NOT EXISTS billing_event_cursor (
    company_id INTEGER PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    last_event_created BIGINT NOT NULL,
    last_event_id VARCHAR(255),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
