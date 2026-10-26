-- Self-serve signup: email-verification state, one row per signup account.
-- Forward-only, idempotent, additive: creates an empty table and touches no
-- existing rows. Existing accounts have no row and stay able to sign in.

CREATE TABLE IF NOT EXISTS user_email_verification (
    user_id INTEGER PRIMARY KEY REFERENCES "user"(id) ON DELETE CASCADE,
    email VARCHAR(120) NOT NULL,
    verified_at TIMESTAMP WITHOUT TIME ZONE,
    terms_accepted_at TIMESTAMP WITHOUT TIME ZONE,
    signup_ip VARCHAR(64),
    last_sent_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
