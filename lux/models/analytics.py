"""Analytics models (tenant-scoped)."""
from datetime import datetime

from lux.extensions import db


class RawEvent(db.Model):
    __tablename__ = "raw_events"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    event_name = db.Column(db.String(80), nullable=False, index=True)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    session_id = db.Column(db.String(64), index=True)
    user_id = db.Column(db.Integer)
    page_url = db.Column(db.Text)
    referrer = db.Column(db.Text)
    utm_source = db.Column(db.String(120))
    utm_medium = db.Column(db.String(120))
    utm_campaign = db.Column(db.String(120))
    properties = db.Column(db.JSON)
    ip_hash = db.Column(db.String(128))
    email_hash = db.Column(db.String(128))
    device_type = db.Column(db.String(32))
    viewport_width = db.Column(db.Integer)
    orientation = db.Column(db.String(20))


class Session(db.Model):
    __tablename__ = "sessions"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    session_id = db.Column(db.String(64), nullable=False, index=True)
    user_id = db.Column(db.Integer)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ConsentSuppressed(db.Model):
    __tablename__ = "consent_suppressed"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    day = db.Column(db.Date, nullable=False, index=True)
    count = db.Column(db.Integer, default=0, nullable=False)


class AnalyticsDailyRollup(db.Model):
    __tablename__ = "analytics_daily_rollups"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    day = db.Column(db.Date, nullable=False, index=True)
    total_events = db.Column(db.Integer, default=0, nullable=False)
    page_views = db.Column(db.Integer, default=0, nullable=False)
    sessions = db.Column(db.Integer, default=0, nullable=False)


class IntegrationStatus(db.Model):
    __tablename__ = "integration_status"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    integration_name = db.Column(db.String(120), nullable=False)
    is_configured = db.Column(db.Boolean, default=False)
    last_success_at = db.Column(db.DateTime)
    last_webhook_at = db.Column(db.DateTime)
    error_count_24h = db.Column(db.Integer, default=0, nullable=False)


class AgentReport(db.Model):
    __tablename__ = "agent_reports"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    agent_type = db.Column(db.String(80), nullable=False)
    period_start = db.Column(db.DateTime, nullable=False)
    period_end = db.Column(db.DateTime, nullable=False)
    compare_start = db.Column(db.DateTime)
    compare_end = db.Column(db.DateTime)
    content = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ActionProposal(db.Model):
    __tablename__ = "action_proposals"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    agent_type = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    hypothesis = db.Column(db.Text)
    expected_impact = db.Column(db.Text)
    steps = db.Column(db.JSON)
    status = db.Column(db.String(40), default="proposed")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime)


class ProposalResult(db.Model):
    __tablename__ = "proposal_results"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    proposal_id = db.Column(db.Integer, nullable=False, index=True)
    measured_metrics = db.Column(db.JSON)
    outcome = db.Column(db.String(120))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SignalsRaw(db.Model):
    __tablename__ = "signals_raw"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    agent_type = db.Column(db.String(80), nullable=False)
    source_url = db.Column(db.Text, nullable=False)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    extracted_text_hash = db.Column(db.String(128), nullable=False)
    tags = db.Column(db.String(255))


class SignalsSummary(db.Model):
    __tablename__ = "signals_summary"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    agent_type = db.Column(db.String(80), nullable=False)
    summary = db.Column(db.Text, nullable=False)
    citations = db.Column(db.JSON)
    confidence = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
