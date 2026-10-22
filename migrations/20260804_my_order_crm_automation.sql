BEGIN;

CREATE TABLE IF NOT EXISTS crm_automation_execution (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    rule_id INTEGER NOT NULL REFERENCES segment(id),
    contact_id INTEGER NOT NULL REFERENCES contact(id),
    event_key VARCHAR(255) NOT NULL,
    action_index INTEGER NOT NULL DEFAULT 0,
    trigger_name VARCHAR(100) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'completed',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_crm_automation_execution_event_action
        UNIQUE (company_id, rule_id, event_key, action_index)
);

CREATE INDEX IF NOT EXISTS ix_crm_automation_execution_company_id ON crm_automation_execution(company_id);
CREATE INDEX IF NOT EXISTS ix_crm_automation_execution_rule_id ON crm_automation_execution(rule_id);
CREATE INDEX IF NOT EXISTS ix_crm_automation_execution_contact_id ON crm_automation_execution(contact_id);

-- These partial indexes deliberately fail installation when legacy aliases
-- already exist more than once in a tenant.  Run the read-only duplicate
-- preview and consolidate deliberately before retrying the migration.
CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_my_order_contact_tag_per_company
    ON segment(company_id)
    WHERE segment_type = 'contact_tag'
      AND lower(regexp_replace(btrim(name), '\s+', ' ', 'g'))
          IN ('my order customer', 'myorder customer', 'my order');
CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_my_order_crm_segment_per_company
    ON segment(company_id)
    WHERE segment_type NOT IN ('contact_tag', 'automation_rule')
      AND lower(regexp_replace(btrim(name), '\s+', ' ', 'g'))
          IN ('my order customer', 'myorder customer', 'my order');
CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_automation_rule_name_per_company
    ON segment(company_id, lower(regexp_replace(btrim(name), '\s+', ' ', 'g')))
    WHERE segment_type = 'automation_rule';

-- Existing deployments already rely on this uniqueness in the ORM. Keep the
-- migration explicit so PostgreSQL is the final concurrency backstop.
CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_member_segment_contact
    ON segment_member(segment_id, contact_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_twilio_message_sid_not_null
    ON twilio_message(twilio_sid) WHERE twilio_sid IS NOT NULL;

COMMIT;
