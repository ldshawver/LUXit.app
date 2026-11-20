"""CRM event bus (M5): turns relationship events into automation runs.

``emit`` never breaks its caller: the dispatch runs in a savepoint and any
error is logged. Events raised by automation actions themselves are
processed up to ``MAX_DEPTH`` levels deep, which stops rule loops (e.g. a
stage change whose rule changes the stage again).
"""
from __future__ import annotations

import contextvars
import logging
import uuid

from extensions import db

logger = logging.getLogger(__name__)

MAX_DEPTH = 2
_depth = contextvars.ContextVar("crm_event_depth", default=0)


def emit(company_id, event_type, payload):
    """Dispatch ``event_type`` for ``payload['contact_id']``; returns the
    dispatch result, or None when skipped/failed."""
    from services import crm_automation as engine
    if event_type not in engine.RELATIONSHIP_TRIGGERS or not company_id or not payload.get("contact_id"):
        return None
    depth = _depth.get()
    if depth >= MAX_DEPTH:
        logger.info("crm event %s not dispatched: nested %s levels deep", event_type, depth)
        return None
    event_key = payload.get("event_key") or f"{event_type}:{uuid.uuid4().hex}"
    extra = {k: v for k, v in payload.items() if k not in {"contact_id", "event_key"}}
    token = _depth.set(depth + 1)
    try:
        with db.session.begin_nested():
            return engine.dispatch_event(company_id, payload["contact_id"], event_type, event_key, **extra)
    except Exception:
        logger.exception("crm event %s failed for company_id=%s", event_type, company_id)
        return None
    finally:
        _depth.reset(token)
