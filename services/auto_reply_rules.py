"""Validation and serialization for AutoReplyRule, shared by the CRM rules
editor and the LUX Connect app (/api/mobile/v1/automation/rules).

Execution lives in twilio_sms._apply_auto_reply_rules; this module only
decides what a valid rule is. STOP / START / HELP keywords never reach rules
(they are answered earlier in the inbound webhook), so no rule can override
an opt-out.
"""
from __future__ import annotations

import re

TRIGGER_TYPES = ("keyword_contains", "keyword_exact", "regex", "first_contact", "after_hours", "always")
KEYWORD_TRIGGERS = ("keyword_contains", "keyword_exact", "regex")
ACTIONS = ("reply", "forward", "tag", "opt_out")
MAX_RESPONSE_CHARS = 1600
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class RuleError(ValueError):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_rule"):
        super().__init__(message)
        self.status = status
        self.code = code


def find_keyword_conflict(company_id: int, trigger_type: str, keywords, exclude_rule_id: int | None = None):
    """(keyword, other_rule) for the first keyword already used by another
    active rule with the same trigger type, else None."""
    from models import AutoReplyRule
    if not keywords or trigger_type not in ("keyword_contains", "keyword_exact"):
        return None
    q = AutoReplyRule.query.filter_by(company_id=company_id, trigger_type=trigger_type, is_active=True)
    if exclude_rule_id:
        q = q.filter(AutoReplyRule.id != exclude_rule_id)
    others = q.all()
    for kw in keywords:
        for other in others:
            if other.keywords and kw.lower() in [k.lower() for k in other.keywords]:
                return kw, other
    return None


def _e164(value) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if str(value or "").strip().startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def validate(company_id: int, data: dict, existing=None) -> dict:
    """Return the normalized field values for a create (existing=None) or a
    partial update. Raises RuleError."""
    from models import AutoReplyRule, TwilioPhoneNumber

    def pick(key, default=None):
        if key in data:
            return data[key]
        return getattr(existing, key) if existing is not None else default

    name = str(pick("name", "") or "").strip()[:200]
    if not name:
        raise RuleError("Give the rule a name.")

    trigger = pick("trigger_type", "keyword_contains")
    if trigger not in TRIGGER_TYPES:
        raise RuleError("Choose a supported trigger.")
    action = pick("action", "reply")
    if action not in ACTIONS:
        raise RuleError("Choose a supported action.")

    raw_kw = pick("keywords", []) or []
    if isinstance(raw_kw, str):
        raw_kw = raw_kw.split(",")
    keywords = [str(k).strip()[:100] for k in raw_kw if str(k).strip()][:50]
    if trigger in KEYWORD_TRIGGERS and not keywords:
        raise RuleError("Add at least one keyword.")
    if trigger == "regex":
        for k in keywords:
            try:
                re.compile(k)
            except re.error:
                raise RuleError(f"“{k}” is not a valid pattern.")
    if not (trigger in KEYWORD_TRIGGERS):
        keywords = []

    response = str(pick("response", "") or "").strip()
    if len(response) > MAX_RESPONSE_CHARS:
        raise RuleError(f"Replies are limited to {MAX_RESPONSE_CHARS} characters.")
    if action == "reply" and not response and trigger != "after_hours":
        raise RuleError("Response message cannot be blank for Reply action.")

    phone_number_id = pick("phone_number_id")
    if phone_number_id in ("", 0):
        phone_number_id = None
    numbers = TwilioPhoneNumber.query.filter_by(company_id=company_id, is_active=True).all()
    if phone_number_id is not None:
        try:
            phone_number_id = int(phone_number_id)
        except (TypeError, ValueError):
            raise RuleError("Unknown phone number.")
        if phone_number_id not in {n.id for n in numbers}:
            raise RuleError("Unknown phone number.", status=404, code="not_found")

    forward_to = None
    if action == "forward":
        forward_to = _e164(pick("forward_to"))
        if not forward_to:
            raise RuleError("Enter the forwarding number with area code, e.g. +15551234567.")
        if forward_to in {n.phone_number for n in numbers}:
            raise RuleError("A rule cannot forward to one of this account's own numbers.")

    tag_value = str(pick("tag_value", "") or "").strip()[:100] or None
    if action == "tag" and not tag_value:
        raise RuleError("Enter the tag to add.")

    days = pick("active_days")
    if days in ([], None, ""):
        days = None
    else:
        try:
            days = sorted({int(d) for d in days})
        except (TypeError, ValueError):
            raise RuleError("Days must be 0 (Monday) to 6 (Sunday).")
        if any(d < 0 or d > 6 for d in days):
            raise RuleError("Days must be 0 (Monday) to 6 (Sunday).")

    start = pick("active_hours_start") or None
    end = pick("active_hours_end") or None
    if (start is None) != (end is None):
        raise RuleError("Set both a start and an end time, or neither.")
    if start and (not _HHMM.match(start) or not _HHMM.match(end)):
        raise RuleError("Times must be HH:MM (24-hour, UTC).")

    try:
        priority = int(pick("priority", 0) or 0)
    except (TypeError, ValueError):
        raise RuleError("Priority must be a number.")

    is_active = bool(pick("is_active", True))
    if is_active:
        conflict = find_keyword_conflict(company_id, trigger, keywords, getattr(existing, "id", None))
        if conflict:
            kw, other = conflict
            raise RuleError(f"Keyword '{kw}' already used in active rule \"{other.name}\".", status=409, code="duplicate_keyword")

    return {
        "name": name, "phone_number_id": phone_number_id, "trigger_type": trigger, "keywords": keywords,
        "response": response, "action": action, "forward_to": forward_to, "tag_value": tag_value,
        "priority": priority, "active_days": days, "active_hours_start": start, "active_hours_end": end,
        "is_active": is_active,
    }


def to_dict(rule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "phone_number_id": rule.phone_number_id,
        "trigger_type": rule.trigger_type,
        "keywords": list(rule.keywords or []),
        "response": rule.response or "",
        "action": rule.action,
        "forward_to": rule.forward_to or None,
        "tag_value": rule.tag_value or None,
        "priority": rule.priority or 0,
        "active_days": rule.active_days or [],
        "active_hours_start": rule.active_hours_start,
        "active_hours_end": rule.active_hours_end,
        "is_active": bool(rule.is_active),
        "match_count": rule.match_count or 0,
        "system": rule.trigger_type == "stop_keyword",
    }
