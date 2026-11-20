-- Relationship memory (differentiators M2): reviewable commitments, follow-ups,
-- facts and summaries per contact. Forward-only, idempotent, additive.

CREATE TABLE IF NOT EXISTS relationship_memory (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    kind VARCHAR(30) NOT NULL,
    text TEXT NOT NULL,
    corrected_text TEXT,
    due_at TIMESTAMP WITHOUT TIME ZONE,
    origin VARCHAR(20) NOT NULL DEFAULT 'rule',
    confidence DOUBLE PRECISION,
    review_state VARCHAR(20) NOT NULL DEFAULT 'pending',
    source_refs JSON,
    dedupe_key VARCHAR(160),
    task_id INTEGER REFERENCES contact_task(id) ON DELETE SET NULL,
    created_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
    reviewed_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT uq_relationship_memory_dedupe UNIQUE (company_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS ix_relationship_memory_company_id ON relationship_memory (company_id);
CREATE INDEX IF NOT EXISTS ix_relationship_memory_contact_id ON relationship_memory (contact_id);
CREATE INDEX IF NOT EXISTS ix_relationship_memory_due_at ON relationship_memory (due_at);
CREATE INDEX IF NOT EXISTS ix_relationship_memory_review_state ON relationship_memory (review_state);
