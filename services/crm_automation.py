"""Tenant-scoped CRM behavioral automation with an explicit JSON contract."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Contact, CRMAutomationExecution, Segment, SegmentMember

MY_ORDER_CUSTOMER_NAME = "My Order Customer"
MY_ORDER_CUSTOMER_ALIASES = {"my order customer", "myorder customer", "my order"}
FIRST_INBOUND_TRIGGER = "inbound_sms.first_for_contact"
TAG_ADDED_TRIGGER = "tag_added"
SUPPORTED_TRIGGERS = {FIRST_INBOUND_TRIGGER, TAG_ADDED_TRIGGER}
SUPPORTED_MATCH_MODES = {"all", "any"}
SUPPORTED_CONDITIONS = {
    "direction", "channel", "first_inbound_sms", "contact_state", "tag_ids",
}
SUPPORTED_ACTIONS = {"contact.add_tag", "segment.add_contact"}


class AutomationContractError(ValueError):
    pass


@dataclass
class DuplicateCanonicalRecord(AutomationContractError):
    role: str
    records: list[dict]

    def __str__(self):
        return f"duplicate canonical {self.role} records: {self.records}"


def normalize_label(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def is_my_order_customer_label(value: Any) -> bool:
    return normalize_label(value) in MY_ORDER_CUSTOMER_ALIASES


def split_tags(value: Any) -> list[str]:
    import re
    values = value if isinstance(value, (list, tuple, set)) else re.split(r"[,;|]", str(value or ""))
    result, seen = [], set()
    for value in values:
        label = " ".join(str(value).split())
        key = normalize_label(label)
        if label and key not in seen:
            result.append(label)
            seen.add(key)
    return result


def _record_usage(row: Segment) -> dict:
    rule_references = 0
    for rule in Segment.query.filter_by(company_id=row.company_id, segment_type="automation_rule").all():
        document = {"conditions": rule.conditions or {}, "actions": rule.actions or []}
        if row.id in (document["conditions"].get("tag_ids") or []):
            rule_references += 1
        rule_references += sum(
            1 for action in document["actions"]
            if action.get("tag_id") == row.id or action.get("segment_id") == row.id
        )
    return {
        "id": row.id,
        "name": row.name,
        "segment_type": row.segment_type,
        "membership_count": SegmentMember.query.filter_by(segment_id=row.id).count(),
        "active_membership_count": SegmentMember.query.filter_by(
            segment_id=row.id, removed_at=None, is_excluded=False
        ).count(),
        "automation_rule_references": rule_references,
    }


def _canonical_candidates(company_id: int, role: str) -> list[Segment]:
    rows = Segment.query.filter_by(company_id=company_id).all()
    named = [row for row in rows if is_my_order_customer_label(row.name)]
    if role == "tag":
        return [row for row in named if row.segment_type == "contact_tag"]
    return [row for row in named if row.segment_type not in {"contact_tag", "automation_rule"}]


def _resolve_canonical(company_id: int, role: str, create_missing: bool) -> Segment | None:
    candidates = _canonical_candidates(company_id, role)
    if len(candidates) > 1:
        raise DuplicateCanonicalRecord(role, [_record_usage(row) for row in candidates])
    if candidates:
        row = candidates[0]
        if create_missing:
            row.name = MY_ORDER_CUSTOMER_NAME
        return row
    if not create_missing:
        return None
    row = Segment(
        company_id=company_id,
        name=MY_ORDER_CUSTOMER_NAME,
        segment_type="contact_tag" if role == "tag" else "behavioral",
        match_mode="all",
        is_dynamic=False,
        is_active=True,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
        return row
    except IntegrityError:
        candidates = _canonical_candidates(company_id, role)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise DuplicateCanonicalRecord(role, [_record_usage(value) for value in candidates])
        raise


def validate_definition(triggers, conditions, actions, match_mode="all") -> dict:
    if not isinstance(triggers, list) or not triggers or not all(isinstance(v, str) for v in triggers):
        raise AutomationContractError("triggers must be a non-empty JSON array of strings")
    unknown_triggers = set(triggers) - SUPPORTED_TRIGGERS
    if unknown_triggers:
        raise AutomationContractError(f"unsupported triggers: {sorted(unknown_triggers)}")
    if match_mode not in SUPPORTED_MATCH_MODES:
        raise AutomationContractError("match_mode must be 'all' or 'any'")
    if not isinstance(conditions, dict):
        raise AutomationContractError("conditions must be a JSON object")
    unknown_conditions = set(conditions) - SUPPORTED_CONDITIONS
    if unknown_conditions:
        raise AutomationContractError(f"unsupported condition keys: {sorted(unknown_conditions)}")
    if "direction" in conditions and conditions["direction"] != "inbound":
        raise AutomationContractError("direction supports only 'inbound'")
    if "channel" in conditions and conditions["channel"] != "sms":
        raise AutomationContractError("channel supports only 'sms'")
    if "first_inbound_sms" in conditions and not isinstance(conditions["first_inbound_sms"], bool):
        raise AutomationContractError("first_inbound_sms must be boolean")
    if "contact_state" in conditions and conditions["contact_state"] != "canonical_active":
        raise AutomationContractError("contact_state supports only 'canonical_active'")
    if "tag_ids" in conditions and (
        not isinstance(conditions["tag_ids"], list)
        or not conditions["tag_ids"]
        or any(not isinstance(v, int) for v in conditions["tag_ids"])
    ):
        raise AutomationContractError("tag_ids must be a non-empty array of integer IDs")
    if not isinstance(actions, list) or not actions:
        raise AutomationContractError("actions must be a non-empty JSON array")
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in SUPPORTED_ACTIONS:
            raise AutomationContractError("each action requires a supported 'type'")
        expected = "tag_id" if action["type"] == "contact.add_tag" else "segment_id"
        if not isinstance(action.get(expected), int):
            raise AutomationContractError(f"{action['type']} requires integer {expected}")
        extras = set(action) - {"type", expected}
        if extras:
            raise AutomationContractError(f"unsupported action keys: {sorted(extras)}")
    return {"triggers": triggers, "conditions": conditions, "actions": actions, "match_mode": match_mode}


def _rule(company_id: int, name: str) -> Segment | None:
    matches = Segment.query.filter_by(company_id=company_id, segment_type="automation_rule").all()
    matches = [row for row in matches if normalize_label(row.name) == normalize_label(name)]
    if len(matches) > 1:
        raise DuplicateCanonicalRecord("automation_rule", [_record_usage(row) for row in matches])
    return matches[0] if matches else None


def _upsert_rule(company_id: int, name: str, triggers, conditions, actions) -> Segment:
    definition = validate_definition(triggers, conditions, actions, "all")
    row = _rule(company_id, name)
    if row is None:
        row = Segment(company_id=company_id, name=name, segment_type="automation_rule")
        try:
            with db.session.begin_nested():
                db.session.add(row)
                db.session.flush()
        except IntegrityError:
            row = _rule(company_id, name)
            if row is None:
                raise
    row.match_mode = definition["match_mode"]
    row.triggers = definition["triggers"]
    row.conditions = definition["conditions"]
    row.actions = definition["actions"]
    row.is_dynamic = True
    row.is_active = True
    db.session.flush()
    return row


def ensure_my_order_automation(company_id: int, *, create_missing=True) -> dict:
    if not company_id:
        raise AutomationContractError("company_id is required")
    tag = _resolve_canonical(company_id, "tag", create_missing)
    segment = _resolve_canonical(company_id, "segment", create_missing)
    if not tag or not segment:
        return {"tag": tag, "segment": segment, "rules": []}
    if not create_missing:
        rules = [
            row for row in (
                _rule(company_id, "My Order Customer — First inbound SMS"),
                _rule(company_id, "My Order Customer — Tag added"),
            ) if row is not None
        ]
        return {"tag": tag, "segment": segment, "rules": rules}
    rule1 = _upsert_rule(
        company_id,
        "My Order Customer — First inbound SMS",
        [FIRST_INBOUND_TRIGGER],
        {"direction": "inbound", "channel": "sms", "first_inbound_sms": True,
         "contact_state": "canonical_active"},
        [{"type": "contact.add_tag", "tag_id": tag.id}],
    )
    rule2 = _upsert_rule(
        company_id,
        "My Order Customer — Tag added",
        [TAG_ADDED_TRIGGER],
        {"tag_ids": [tag.id]},
        [{"type": "segment.add_contact", "segment_id": segment.id}],
    )
    return {"tag": tag, "segment": segment, "rules": [rule1, rule2]}


def _condition_matches(key, expected, event) -> bool:
    if key == "contact_state":
        contact = event["contact"]
        actual = "canonical_active" if contact.is_active and not contact.merged_into_contact_id else "inactive_or_merged"
        return actual == expected
    if key == "tag_ids":
        return event.get("tag_id") in expected
    return event.get(key) == expected


def _matches(rule: Segment, event: dict) -> bool:
    conditions = rule.conditions or {}
    results = [_condition_matches(k, v, event) for k, v in conditions.items()]
    return any(results) if rule.match_mode == "any" else all(results)


def _reserve_execution(rule, contact, trigger, event_key, action_index, details) -> CRMAutomationExecution | None:
    existing = CRMAutomationExecution.query.filter_by(
        company_id=contact.company_id, rule_id=rule.id, event_key=event_key,
        action_index=action_index,
    ).first()
    if existing:
        return None
    try:
        with db.session.begin_nested():
            execution = CRMAutomationExecution(
                company_id=contact.company_id, rule_id=rule.id, contact_id=contact.id,
                event_key=event_key, action_index=action_index, trigger_name=trigger,
                status="running", details=details,
            )
            db.session.add(execution)
            db.session.flush()
        return execution
    except IntegrityError:
        return None


def _owned_segment(company_id: int, segment_id: int, segment_type: str | None = None) -> Segment:
    query = Segment.query.filter_by(id=segment_id, company_id=company_id)
    if segment_type:
        query = query.filter_by(segment_type=segment_type)
    row = query.first()
    if not row:
        raise AutomationContractError("action reference is not owned by the event tenant")
    return row


def _add_membership(contact: Contact, segment: Segment, source="automation") -> bool:
    member = SegmentMember.query.filter_by(segment_id=segment.id, contact_id=contact.id).first()
    if member:
        changed = bool(member.removed_at or member.is_excluded)
        member.removed_at = None
        member.removed_by_user_id = None
        member.is_excluded = False
        member.exclusion_reason = None
        if changed:
            member.source = source
            member.added_at = datetime.utcnow()
        return changed
    db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id, source=source))
    db.session.flush()
    return True


def dispatch_event(company_id: int, contact_id: int, trigger: str, event_key: str, **payload) -> dict:
    if trigger not in SUPPORTED_TRIGGERS:
        raise AutomationContractError(f"unsupported trigger: {trigger}")
    contact = Contact.query.filter_by(id=contact_id, company_id=company_id).first()
    if not contact:
        raise AutomationContractError("contact is not owned by the event tenant")
    visited = set()
    while contact.merged_into_contact_id and contact.id not in visited:
        visited.add(contact.id)
        contact = Contact.query.filter_by(
            id=contact.merged_into_contact_id, company_id=company_id
        ).first()
        if not contact:
            raise AutomationContractError("merged contact does not resolve within the event tenant")
    if not contact.is_active:
        return {"trigger": trigger, "event_key": event_key, "results": [], "skipped": "inactive_contact"}
    contact_id = contact.id
    event = {"company_id": company_id, "contact_id": contact_id, "contact": contact,
             "event_key": event_key, **payload}
    results = []
    rules = Segment.query.filter_by(company_id=company_id, segment_type="automation_rule", is_active=True).all()
    for rule in rules:
        if trigger not in (rule.triggers or []):
            continue
        validate_definition(rule.triggers, rule.conditions or {}, rule.actions or [], rule.match_mode)
        if not _matches(rule, event):
            continue
        for index, action in enumerate(rule.actions or []):
            execution = _reserve_execution(rule, contact, trigger, event_key, index, {
                key: value for key, value in event.items() if key != "contact"
            })
            if not execution:
                results.append({"rule_id": rule.id, "action_index": index, "duplicate": True})
                continue
            changed = False
            if action["type"] == "contact.add_tag":
                tag = _owned_segment(company_id, action["tag_id"], "contact_tag")
                tags = split_tags(contact.tags)
                if normalize_label(tag.name) not in {normalize_label(v) for v in tags}:
                    tags.append(tag.name)
                    contact.tags = ", ".join(tags)
                    changed = True
                dispatch_event(
                    company_id, contact.id, TAG_ADDED_TRIGGER,
                    f"{event_key}:tag:{tag.id}", tag_id=tag.id, tag_name=tag.name,
                    source="automation",
                )
            else:
                segment = _owned_segment(company_id, action["segment_id"])
                if segment.segment_type in {"contact_tag", "automation_rule"}:
                    raise AutomationContractError("segment.add_contact requires a CRM segment")
                changed = _add_membership(contact, segment)
            execution.status = "completed"
            execution.details = {**(execution.details or {}), "changed": changed, "action": action}
            results.append({"rule_id": rule.id, "action_index": index, "changed": changed})
    db.session.flush()
    return {"trigger": trigger, "event_key": event_key, "results": results}


def assign_contact_tag(contact: Contact, tag_name: str, *, source="unknown", event_key=None) -> bool:
    """Canonical tag mutation entry point; dispatches tag_added exactly once per key."""
    if not contact.id:
        db.session.flush()
    if contact.merged_into_contact_id:
        canonical = Contact.query.filter_by(
            id=contact.merged_into_contact_id, company_id=contact.company_id
        ).first()
        if not canonical:
            raise AutomationContractError("merged contact does not resolve within its tenant")
        contact = canonical
    records = ensure_my_order_automation(contact.company_id, create_missing=True)
    wanted = normalize_label(tag_name)
    if is_my_order_customer_label(tag_name):
        tag = records["tag"]
    else:
        candidates = [row for row in Segment.query.filter_by(company_id=contact.company_id, segment_type="contact_tag").all()
                      if normalize_label(row.name) == wanted]
        if len(candidates) > 1:
            raise DuplicateCanonicalRecord("tag", [_record_usage(row) for row in candidates])
        tag = candidates[0] if candidates else Segment(company_id=contact.company_id, name=" ".join(tag_name.split()), segment_type="contact_tag")
        if tag.id is None:
            db.session.add(tag); db.session.flush()
    tags = split_tags(contact.tags)
    if is_my_order_customer_label(tag_name):
        had_alias = any(is_my_order_customer_label(value) for value in tags)
        tags = [value for value in tags if not is_my_order_customer_label(value)]
        tags.append(MY_ORDER_CUSTOMER_NAME)
        changed = not had_alias
        contact.tags = ", ".join(tags)
    else:
        changed = wanted not in {normalize_label(value) for value in tags}
        if changed:
            tags.append(tag.name)
            contact.tags = ", ".join(tags)
    dispatch_event(
        contact.company_id, contact.id, TAG_ADDED_TRIGGER,
        event_key or f"tag-state:{contact.id}:{tag.id}", tag_id=tag.id,
        tag_name=tag.name, source=source,
    )
    return changed


def synchronize_contact_tags(contact: Contact, *, source="unknown", event_key_prefix=None) -> None:
    for tag in split_tags(contact.tags):
        if is_my_order_customer_label(tag):
            assign_contact_tag(contact, MY_ORDER_CUSTOMER_NAME, source=source,
                               event_key=f"{event_key_prefix}:my-order" if event_key_prefix else None)


def backfill_preview(company_id: int) -> dict:
    records = ensure_my_order_automation(company_id, create_missing=False)
    tag, segment = records["tag"], records["segment"]
    if not tag or not segment:
        return {"company_id": company_id, "tag_id": getattr(tag, "id", None),
                "segment_id": getattr(segment, "id", None), "tagged_contact_count": 0,
                "already_in_segment": 0, "missing_from_segment": 0, "proposed_inserts": 0}
    contacts = Contact.query.filter_by(company_id=company_id).all()
    tagged = [c for c in contacts if any(is_my_order_customer_label(v) for v in split_tags(c.tags))]
    rows = SegmentMember.query.filter_by(segment_id=segment.id).all()
    by_contact = {}
    for row in rows:
        by_contact.setdefault(row.contact_id, []).append(row)
    active_ids = {cid for cid, memberships in by_contact.items() if any(not m.removed_at and not m.is_excluded for m in memberships)}
    missing = [c for c in tagged if c.id not in active_ids and c.is_active and not c.merged_into_contact_id]
    cross_tenant = SegmentMember.query.filter(SegmentMember.segment_id == segment.id).join(Contact).filter(Contact.company_id != company_id).count()
    return {
        "company_id": company_id, "tag_id": tag.id, "tag_name": tag.name,
        "segment_id": segment.id, "segment_name": segment.name,
        "tagged_contact_count": len(tagged), "already_in_segment": len([c for c in tagged if c.id in active_ids]),
        "missing_from_segment": len(missing),
        "duplicate_tag_assignments": sum(
            max(sum(is_my_order_customer_label(v) for v in re.split(r"[,;|]", str(c.tags or ""))) - 1, 0)
            for c in tagged
        ),
        "duplicate_segment_memberships": sum(max(len(v) - 1, 0) for v in by_contact.values()),
        "inactive_contacts": sum(not c.is_active for c in tagged),
        "merged_contacts": sum(bool(c.merged_into_contact_id) for c in tagged),
        "cross_tenant_exclusions": cross_tenant,
        "proposed_inserts": len(missing), "contact_ids": [c.id for c in missing],
    }
