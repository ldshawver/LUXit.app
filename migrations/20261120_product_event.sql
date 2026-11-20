-- First-party product analytics events (segment / SMS campaign actions).
-- Forward-only, idempotent, additive: one empty table, no existing rows touched.
-- Rows hold ids and counts only (no names, phones, emails or message bodies).
-- Until this is applied, services/product_events.py records nothing (it
-- writes inside a savepoint and swallows the missing-table error), so the
-- request that triggered the event is unaffected.

CREATE TABLE IF NOT EXISTS product_event (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    user_id INTEGER,
    event VARCHAR(80) NOT NULL,
    entity_type VARCHAR(40),
    entity_id INTEGER,
    properties JSONB,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_product_event_company_id ON product_event (company_id);
CREATE INDEX IF NOT EXISTS ix_product_event_event ON product_event (event);
CREATE INDEX IF NOT EXISTS ix_product_event_created_at ON product_event (created_at);
