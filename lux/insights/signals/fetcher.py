"""Signals fetcher (server-side scheduled)."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from lux.extensions import db
from lux.models.analytics import SignalsRaw

logger = logging.getLogger(__name__)


def record_signal(company_id: int, agent_type: str, source_url: str, extracted_text: str, tags: str = "") -> None:
    """Record raw signals in append-only table."""
    text_hash = hashlib.sha256(extracted_text.encode("utf-8")).hexdigest()
    signal = SignalsRaw(
        company_id=company_id,
        agent_type=agent_type,
        source_url=source_url,
        extracted_text_hash=text_hash,
        fetched_at=datetime.utcnow(),
        tags=tags,
    )
    db.session.add(signal)
    db.session.commit()


def fetch_sources(company_id: int, agent_type: str, sources: list[str]) -> None:
    """Placeholder fetcher: requires server-side safe fetch implementation."""
    for source in sources:
        logger.info("Signals fetcher configured for %s (%s)", agent_type, source)
