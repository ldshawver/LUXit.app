-- LUX Connect native app (iOS / Android): devices, bearer sessions, sign-in
-- throttling, outbound MMS uploads and per-number messaging registration.
-- Forward-only, idempotent, purely additive: new tables only. No existing
-- table, column, default or row is changed, so SMS STOP / consent /
-- suppression / campaign behaviour is untouched.

CREATE TABLE IF NOT EXISTS mobile_device (
    id                       SERIAL PRIMARY KEY,
    company_id               INTEGER NOT NULL REFERENCES company(id),
    user_id                  INTEGER NOT NULL REFERENCES "user"(id),
    device_uid               VARCHAR(64) NOT NULL,
    platform                 VARCHAR(16) NOT NULL,
    device_name              VARCHAR(120),
    model                    VARCHAR(120),
    os_version               VARCHAR(40),
    app_version              VARCHAR(40),
    status                   VARCHAR(20) NOT NULL DEFAULT 'active',
    ring_enabled             BOOLEAN NOT NULL DEFAULT true,
    push_token               VARCHAR(512),
    push_token_type          VARCHAR(16),
    apns_environment         VARCHAR(16),
    push_token_updated_at    TIMESTAMP WITHOUT TIME ZONE,
    voice_registered_at      TIMESTAMP WITHOUT TIME ZONE,
    voice_registration_error VARCHAR(300),
    voice_token_hash         VARCHAR(64),
    last_seen_at             TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
    revoked_at               TIMESTAMP WITHOUT TIME ZONE,
    revoked_by_user_id       INTEGER REFERENCES "user"(id),
    created_at               TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
    updated_at               TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),
    CONSTRAINT uq_mobile_device_user_uid UNIQUE (company_id, user_id, device_uid),
    CONSTRAINT ck_mobile_device_platform CHECK (platform IN ('ios', 'android')),
    CONSTRAINT ck_mobile_device_status CHECK (status IN ('active', 'pending_approval', 'signed_out', 'revoked'))
);
CREATE INDEX IF NOT EXISTS ix_mobile_device_company_id ON mobile_device (company_id);
CREATE INDEX IF NOT EXISTS ix_mobile_device_user_id ON mobile_device (user_id);
CREATE INDEX IF NOT EXISTS ix_mobile_device_status ON mobile_device (status);
CREATE INDEX IF NOT EXISTS ix_mobile_device_voice_token_hash ON mobile_device (voice_token_hash);
CREATE INDEX IF NOT EXISTS ix_mobile_device_push_token ON mobile_device (push_token);

CREATE TABLE IF NOT EXISTS mobile_session (
    id                 SERIAL PRIMARY KEY,
    mobile_device_id   INTEGER NOT NULL REFERENCES mobile_device(id),
    user_id            INTEGER NOT NULL REFERENCES "user"(id),
    company_id         INTEGER NOT NULL REFERENCES company(id),
    access_token_hash  VARCHAR(64) NOT NULL UNIQUE,
    access_expires_at  TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    refresh_token_hash VARCHAR(64) NOT NULL UNIQUE,
    refresh_expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    rotated_at         TIMESTAMP WITHOUT TIME ZONE,
    rotated_to_id      INTEGER,
    revoked_at         TIMESTAMP WITHOUT TIME ZONE,
    revoke_reason      VARCHAR(40),
    last_used_at       TIMESTAMP WITHOUT TIME ZONE,
    created_ip         VARCHAR(64),
    created_at         TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mobile_session_mobile_device_id ON mobile_session (mobile_device_id);
CREATE INDEX IF NOT EXISTS ix_mobile_session_user_id ON mobile_session (user_id);
CREATE INDEX IF NOT EXISTS ix_mobile_session_company_id ON mobile_session (company_id);

CREATE TABLE IF NOT EXISTS mobile_auth_attempt (
    id              SERIAL PRIMARY KEY,
    identifier_hash VARCHAR(64) NOT NULL,
    ip              VARCHAR(64),
    created_at      TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mobile_auth_attempt_identifier_hash ON mobile_auth_attempt (identifier_hash);
CREATE INDEX IF NOT EXISTS ix_mobile_auth_attempt_ip ON mobile_auth_attempt (ip);
CREATE INDEX IF NOT EXISTS ix_mobile_auth_attempt_created_at ON mobile_auth_attempt (created_at);

CREATE TABLE IF NOT EXISTS mobile_media_upload (
    id                   SERIAL PRIMARY KEY,
    company_id           INTEGER NOT NULL REFERENCES company(id),
    user_id              INTEGER NOT NULL REFERENCES "user"(id),
    public_token         VARCHAR(64) NOT NULL UNIQUE,
    content_type         VARCHAR(100) NOT NULL,
    size_bytes           INTEGER NOT NULL,
    sha256               VARCHAR(64) NOT NULL,
    data                 BYTEA NOT NULL,
    expires_at           TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    attached_message_sid VARCHAR(64),
    created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mobile_media_upload_company_id ON mobile_media_upload (company_id);

CREATE TABLE IF NOT EXISTS phone_number_messaging_registration (
    id                    SERIAL PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES company(id),
    phone_number_id       INTEGER NOT NULL UNIQUE REFERENCES twilio_phone_number(id),
    status                VARCHAR(30) NOT NULL DEFAULT 'unknown',
    messaging_service_sid VARCHAR(60),
    detail                VARCHAR(300),
    checked_at            TIMESTAMP WITHOUT TIME ZONE DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_phone_number_messaging_registration_company_id
    ON phone_number_messaging_registration (company_id);
