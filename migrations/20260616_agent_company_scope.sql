-- Add durable company scoping and request metadata for AI agent artifacts.
-- Safe/idempotent for production: no destructive changes and no data rewrites.
ALTER TABLE agent_report
    ADD COLUMN IF NOT EXISTS company_id INTEGER;

ALTER TABLE agent_log
    ADD COLUMN IF NOT EXISTS company_id INTEGER;

ALTER TABLE agent_deliverable
    ADD COLUMN IF NOT EXISTS company_id INTEGER,
    ADD COLUMN IF NOT EXISTS priority VARCHAR(50) DEFAULT 'normal',
    ADD COLUMN IF NOT EXISTS requested_by_id INTEGER;

CREATE INDEX IF NOT EXISTS ix_agent_report_company_id ON agent_report(company_id);
CREATE INDEX IF NOT EXISTS ix_agent_log_company_id ON agent_log(company_id);
CREATE INDEX IF NOT EXISTS ix_agent_deliverable_company_id ON agent_deliverable(company_id);
