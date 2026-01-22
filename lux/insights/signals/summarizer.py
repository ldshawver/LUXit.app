"""Signals summarizer stub."""
from __future__ import annotations

from datetime import datetime

from lux.extensions import db
from lux.models.analytics import SignalsSummary


def store_summary(company_id: int, agent_type: str, summary: str, citations: list[str], confidence: float) -> None:
    record = SignalsSummary(
        company_id=company_id,
        agent_type=agent_type,
        summary=summary,
        citations=citations,
        confidence=confidence,
        created_at=datetime.utcnow(),
    )
    db.session.add(record)
    db.session.commit()
