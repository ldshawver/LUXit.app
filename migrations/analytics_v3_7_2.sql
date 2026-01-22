-- Analytics tables for v3.7.2
CREATE TABLE IF NOT EXISTS raw_events (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    event_name VARCHAR(80) NOT NULL,
    occurred_at TIMESTAMP NOT NULL DEFAULT NOW(),
    session_id VARCHAR(64),
    user_id INTEGER,
    page_url TEXT,
    referrer TEXT,
    utm_source VARCHAR(120),
    utm_medium VARCHAR(120),
    utm_campaign VARCHAR(120),
    properties JSONB,
    ip_hash VARCHAR(128),
    email_hash VARCHAR(128),
    device_type VARCHAR(32),
    viewport_width INTEGER,
    orientation VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS sessions (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    user_id INTEGER,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS consent_suppressed (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analytics_daily_rollups (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    day DATE NOT NULL,
    total_events INTEGER NOT NULL DEFAULT 0,
    page_views INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS integration_status (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    integration_name VARCHAR(120) NOT NULL,
    is_configured BOOLEAN DEFAULT FALSE,
    last_success_at TIMESTAMP,
    last_webhook_at TIMESTAMP,
    error_count_24h INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_reports (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    agent_type VARCHAR(80) NOT NULL,
    period_start TIMESTAMP NOT NULL,
    period_end TIMESTAMP NOT NULL,
    compare_start TIMESTAMP,
    compare_end TIMESTAMP,
    content JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS action_proposals (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    agent_type VARCHAR(80) NOT NULL,
    title VARCHAR(200) NOT NULL,
    hypothesis TEXT,
    expected_impact TEXT,
    steps JSONB,
    status VARCHAR(40) DEFAULT 'proposed',
    created_at TIMESTAMP DEFAULT NOW(),
    approved_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS proposal_results (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    proposal_id INTEGER NOT NULL,
    measured_metrics JSONB,
    outcome VARCHAR(120),
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS signals_raw (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    agent_type VARCHAR(80) NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL DEFAULT NOW(),
    extracted_text_hash VARCHAR(128) NOT NULL,
    tags VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS signals_summary (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL,
    agent_type VARCHAR(80) NOT NULL,
    summary TEXT NOT NULL,
    citations JSONB,
    confidence DOUBLE PRECISION,
    created_at TIMESTAMP DEFAULT NOW()
);
