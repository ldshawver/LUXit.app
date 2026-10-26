-- Self-serve plan entitlements (T-8).
-- Forward-only, idempotent, additive: creates one empty table and touches no
-- existing rows. Workspaces without a plan-policy row keep unrestricted access.

CREATE TABLE IF NOT EXISTS company_plan_policy (
    company_id INTEGER PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    plan_source VARCHAR(32) NOT NULL DEFAULT 'self_serve',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
