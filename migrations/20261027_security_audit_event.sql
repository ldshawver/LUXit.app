-- Security audit trail (T-26).
-- Forward-only, idempotent, additive: creates one empty table plus a trigger
-- that makes it append-only. Retention/purge is a policy decision (tracker §7);
-- a purge job would have to drop and re-create the trigger explicitly.

CREATE TABLE IF NOT EXISTS security_audit_event (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    action VARCHAR(64) NOT NULL,
    outcome VARCHAR(16) NOT NULL DEFAULT 'success',
    actor_user_id INTEGER,
    company_id INTEGER,
    target_user_id INTEGER,
    ip VARCHAR(64),
    detail TEXT
);

CREATE INDEX IF NOT EXISTS ix_security_audit_event_created_at ON security_audit_event (created_at);
CREATE INDEX IF NOT EXISTS ix_security_audit_event_action ON security_audit_event (action);
CREATE INDEX IF NOT EXISTS ix_security_audit_event_actor_user_id ON security_audit_event (actor_user_id);
CREATE INDEX IF NOT EXISTS ix_security_audit_event_company_id ON security_audit_event (company_id);

CREATE OR REPLACE FUNCTION luxit_security_audit_event_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'security_audit_event is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS luxit_security_audit_event_no_update ON security_audit_event;
CREATE TRIGGER luxit_security_audit_event_no_update
    BEFORE UPDATE OR DELETE ON security_audit_event
    FOR EACH ROW EXECUTE FUNCTION luxit_security_audit_event_append_only();

DROP TRIGGER IF EXISTS luxit_security_audit_event_no_truncate ON security_audit_event;
CREATE TRIGGER luxit_security_audit_event_no_truncate
    BEFORE TRUNCATE ON security_audit_event
    FOR EACH STATEMENT EXECUTE FUNCTION luxit_security_audit_event_append_only();
