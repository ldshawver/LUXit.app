-- Workspace closure (account cancellation + data retention).
-- Forward-only, idempotent, additive: one empty table, no existing rows touched.
-- A row means an owner closed the workspace: members lose access while it is
-- open (reopened_at NULL) and an operator purge deletes the workspace's data
-- once purge_after has passed, leaving the company row as a tombstone.
-- Without this table closing is refused and nothing else changes.

CREATE TABLE IF NOT EXISTS workspace_closure (
    company_id INTEGER PRIMARY KEY REFERENCES company(id) ON DELETE CASCADE,
    closed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    closed_by_user_id INTEGER,
    reason VARCHAR(120),
    purge_after TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    reopened_at TIMESTAMP WITHOUT TIME ZONE,
    purged_at TIMESTAMP WITHOUT TIME ZONE
);
