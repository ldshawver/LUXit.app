"""Guided segment templates.

Each template turns a few plain inputs into canonical segment-engine
conditions (services/segment_engine.py), so a guided segment is an ordinary
dynamic segment: the same validator, evaluator and refresh apply. Invalid
input raises SegmentConditionError -- nothing is created with a guessed rule.
"""
from __future__ import annotations

from services.segment_engine import SegmentConditionError, validate_conditions

MAX_DAYS = 3650


def _text(params, key, label):
    value = " ".join(str((params or {}).get(key) or "").split())
    if not value or len(value) > 120:
        raise SegmentConditionError(f"Enter a {label} (1-120 characters).")
    return value


def _days(params, key="days"):
    raw = (params or {}).get(key)
    if isinstance(raw, (bool, float)):
        raise SegmentConditionError("days must be a whole number")
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        raise SegmentConditionError("days must be a whole number")
    if not 1 <= days <= MAX_DAYS:
        raise SegmentConditionError(f"days must be between 1 and {MAX_DAYS}")
    return days


def _tag(p):
    tag = _text(p, "tag", "tag")
    return f"Contacts tagged {tag}", [{"field": "tag", "op": "in", "value": [tag]}]


def _source(p):
    src = _text(p, "source", "source")
    return f"Contacts from {src}", [{"field": "source", "op": "equals", "value": src}]


def _new(p):
    d = _days(p)
    return f"Added in the last {d} days", [{"field": "created_at", "op": "within_last_days", "value": d}]


def _inactive(p):
    d = _days(p)
    return f"No activity in {d} days", [{"field": "last_activity_at", "op": "more_than_days_ago", "value": d}]


def _stage(p):
    stage = _text(p, "lifecycle_stage", "lifecycle stage")
    return f"Lifecycle stage: {stage}", [{"field": "lifecycle_stage", "op": "equals", "value": stage}]


def _open_opp(_p):
    return "Contacts with an open opportunity", [{"field": "has_open_opportunity", "op": "equals", "value": True}]


TEMPLATES = {
    "tag": ("Contacts with a tag", _tag),
    "source": ("Contacts from a source", _source),
    "new_contacts": ("Contacts added recently", _new),
    "inactive": ("Contacts with no recent activity", _inactive),
    "lifecycle_stage": ("Contacts in a lifecycle stage", _stage),
    "open_opportunity": ("Contacts with an open opportunity", _open_opp),
}


def list_templates():
    return [{"key": key, "label": label} for key, (label, _) in TEMPLATES.items()]


def build(template, params):
    """Return ``(default_name, normalized_conditions)`` for a guided template."""
    entry = TEMPLATES.get(str(template or ""))
    if entry is None:
        raise SegmentConditionError(f"unknown segment template {template!r}")
    name, rules = entry[1](params or {})
    return name, validate_conditions({"schema_version": 1, "rules": rules}, "all")
