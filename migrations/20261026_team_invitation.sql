-- Team invitations (T-5).
-- Forward-only, idempotent, additive: creates one empty table.

CREATE TABLE IF NOT EXISTS team_invitation (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    email VARCHAR(120) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'member',
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    invited_by_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    accepted_at TIMESTAMP WITHOUT TIME ZONE,
    accepted_user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
    revoked_at TIMESTAMP WITHOUT TIME ZONE
);

CREATE INDEX IF NOT EXISTS ix_team_invitation_company_id ON team_invitation (company_id);
CREATE INDEX IF NOT EXISTS ix_team_invitation_email ON team_invitation (email);
