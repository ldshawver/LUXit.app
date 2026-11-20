"""First-party product analytics events.

``record()`` adds one ``ProductEvent`` row to the caller's transaction, so an
event is stored exactly when the action it describes is committed and
disappears with it on rollback. Failure events are recorded with
``record_failure()``, which commits on its own after the caller's rollback.

Properties are restricted to ids, counts, booleans and short codes: anything
that looks like contact data is dropped, so these rows can feed dashboards
without carrying PII. Recording never raises into the request.
"""
from __future__ import annotations

import logging

from extensions import db

logger = logging.getLogger(__name__)

EVENTS = {
    "segment_created", "segment_membership_changed", "segment_tagged", "segment_exported",
    "sms_campaign_draft_created", "sms_campaign_audience_changed", "sms_campaign_scheduled",
    "sms_campaign_failed", "segment_action_failed",
}
_PII_KEYS = {"name", "first_name", "last_name", "email", "phone", "phone_number", "message", "body", "tag", "tags"}
_MAX_STR = 64


def _clean(properties):
    out = {}
    for key, value in (properties or {}).items():
        if key in _PII_KEYS:
            continue
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:_MAX_STR]
        elif isinstance(value, (list, tuple, set)):
            out[key] = len(value)  # counts, never the members themselves
    return out


def _build(event, company_id, user_id, entity_type, entity_id, properties):
    from models import ProductEvent
    if event not in EVENTS:
        raise ValueError(f"unknown product event {event!r}")
    return ProductEvent(company_id=company_id, user_id=user_id, event=event, entity_type=entity_type,
                        entity_id=entity_id, properties=_clean(properties))


def record(event, *, company_id, user_id=None, entity_type=None, entity_id=None, properties=None):
    """Stage an event in the current transaction (committed with the action)."""
    if not company_id:
        return None
    try:
        with db.session.begin_nested():
            row = _build(event, company_id, user_id, entity_type, entity_id, properties)
            db.session.add(row)
        return row
    except Exception as exc:  # e.g. table not migrated yet -- never break the action
        logger.warning("product event %s not recorded: %s", event, type(exc).__name__)
        return None


def record_failure(*, company_id, user_id=None, entity_type=None, entity_id=None, code=None, status=None, step=None):
    """Record a refused/failed action. Call AFTER the caller rolled back; this
    commits only the event row."""
    if not company_id:
        return None
    try:
        row = _build("sms_campaign_failed" if entity_type == "sms_campaign" else "segment_action_failed",
                     company_id, user_id, entity_type, entity_id,
                     {"code": code, "status": status, "step": step, "failed": True})
        db.session.add(row)
        db.session.commit()
        return row
    except Exception as exc:
        db.session.rollback()
        logger.warning("product failure event not recorded: %s", type(exc).__name__)
        return None
