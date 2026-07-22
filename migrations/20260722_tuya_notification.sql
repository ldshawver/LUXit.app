BEGIN;

CREATE TABLE IF NOT EXISTS tuya_notification_activation (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL UNIQUE REFERENCES company(id),
    desired_state BOOLEAN NOT NULL DEFAULT FALSE,
    off_requested BOOLEAN NOT NULL DEFAULT FALSE,
    off_deadline TIMESTAMPTZ NULL,
    last_event_sid VARCHAR(100) NULL,
    last_operation VARCHAR(10) NULL,
    last_operation_status VARCHAR(20) NULL,
    last_error VARCHAR(500) NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ NULL,
    last_on_at TIMESTAMPTZ NULL,
    last_off_at TIMESTAMPTZ NULL,
    last_reconciled_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_tuya_notification_activation_off_deadline
    ON tuya_notification_activation(off_deadline);
CREATE INDEX IF NOT EXISTS ix_tuya_notification_activation_pending
    ON tuya_notification_activation(desired_state, off_requested, off_deadline, retry_count);

CREATE TABLE IF NOT EXISTS tuya_notification_event (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    message_sid VARCHAR(160) NOT NULL,
    activation_id INTEGER NOT NULL REFERENCES tuya_notification_activation(id),
    trigger_source VARCHAR(30) NOT NULL DEFAULT 'inbound_sms',
    customer_phone_masked VARCHAR(32) NULL,
    requested_state BOOLEAN NOT NULL DEFAULT TRUE,
    result VARCHAR(20) NOT NULL DEFAULT 'queued',
    off_deadline TIMESTAMPTZ NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sanitized_error VARCHAR(500) NULL,
    initiated_by_user_id INTEGER NULL REFERENCES "user"(id),
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_tuya_notification_event_company_sid UNIQUE (company_id, message_sid)
);
CREATE INDEX IF NOT EXISTS ix_tuya_notification_event_company_id
    ON tuya_notification_event(company_id);
CREATE INDEX IF NOT EXISTS ix_tuya_notification_event_company_received
    ON tuya_notification_event(company_id, received_at DESC);
CREATE INDEX IF NOT EXISTS ix_tuya_notification_event_company_result
    ON tuya_notification_event(company_id, result, trigger_source);

CREATE TABLE IF NOT EXISTS tuya_notification_worker_heartbeat (
    worker_id VARCHAR(160) PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tuya_notification_worker_heartbeat_last_seen
    ON tuya_notification_worker_heartbeat(last_seen_at);

COMMIT;
