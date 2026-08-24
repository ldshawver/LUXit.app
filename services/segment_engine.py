"""Canonical segment condition schema, evaluator, and membership-refresh service.

This is the ONE place that interprets a Segment's stored ``conditions`` and
computes/mutates its ``SegmentMember`` rows. Every route, API endpoint, and
background job that creates, edits, previews, or refreshes segment
membership must go through this module rather than reimplementing matching
logic independently.

Why this exists
----------------
Before this module, three independent, mutually incompatible interpretations
of "does this contact belong in this segment" existed:

  * routes.py::refresh_segment() -- deleted all non-excluded members FIRST,
    then only matched two hardcoded segment_type values ('newsletter', 'all').
    For any other segment_type (e.g. 'custom'), it silently wiped membership
    to zero.
  * marketing_api.py::refresh_dynamic_segment() -- ran automatically on
    every GET request for a segment or its contact list. It expected
    conditions as [{"field": ..., "value": ...}], but production data is
    stored as {"tag": [...]}. Because the shapes didn't match, it applied
    ZERO filters and matched every contact in the company -- a silent
    fail-OPEN bug that would have added ~2,230 contacts to a segment meant
    to hold ~70.
  * services/crm_automation.py's rule matcher -- a third, deliberately
    separate schema for event-triggered automation actions (not "does this
    contact currently qualify", but "did this incoming event satisfy this
    rule"). That module is untouched by this refactor; it is a different
    domain (event matching) and keeps its own schema by design (see
    ``AUTOMATION_VS_SEGMENT_DOMAIN`` note below).

Design contract
----------------
1. ONE schema (see ``validate_conditions``), versioned via "schema_version".
2. ONE evaluator (``evaluate``) used for both preview and refresh.
3. Fail CLOSED: a malformed/unknown condition raises ``SegmentConditionError``
   and the caller MUST treat that as zero membership mutation. An invalid
   condition must never be interpreted as "no filter, match everyone."
4. PREVIEW (``compute_membership_delta``) never writes to the database.
   MUTATION (``refresh``) is a separate, explicit call that computes the
   same delta and then commits it atomically -- additions and removals only,
   never a delete-everything-then-reinsert.
5. Static and system-managed segment types are never touched by dynamic
   evaluation (see ``SYSTEM_SEGMENT_TYPES`` / the ``is_dynamic`` check).

AUTOMATION_VS_SEGMENT_DOMAIN: CRM automation rule conditions
(services/crm_automation.py, segment_type="automation_rule") describe "does
this one incoming event satisfy this rule", evaluated against an event
dict with keys like direction/channel/tag_ids/first_inbound_sms. Segment
membership conditions (this module) describe "does this contact, right now,
belong in this population", evaluated against a Contact row. These are
different domains with different inputs and different failure semantics,
so they intentionally do NOT share a schema -- only the tag-normalization
primitives (``normalize_label``/``split_tags``) are shared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from extensions import db
from models import Contact, Segment, SegmentMember
from services.crm_automation import MY_ORDER_CUSTOMER_ALIASES, normalize_label, split_tags

SCHEMA_VERSION = 1

# Contact attributes the schema may compare against. "tag" is special-cased
# (multi-value, normalized CSV membership) rather than a plain column.
SIMPLE_FIELDS = {
    "approval_status", "identity_status", "source", "normalized_phone",
    "normalized_email", "email", "phone", "first_name", "last_name",
    "company", "status", "google_match_status",
}
DATE_FIELDS = {"created_at"}
SUPPORTED_FIELDS = SIMPLE_FIELDS | DATE_FIELDS | {"tag"}

SIMPLE_OPS = {"equals", "not_equals", "contains", "not_contains", "exists", "not_exists", "in", "not_in"}
DATE_OPS = {"after", "before", "between", "exists", "not_exists"}
SUPPORTED_OPS = SIMPLE_OPS | DATE_OPS

# Segment types this engine will dynamically (re)compute membership for.
DYNAMIC_EVALUABLE_TYPES = {"custom", "behavioral", "newsletter", "all"}
# Segment types this engine NEVER touches -- owned by another mechanism:
#   contact_tag: a tag-identity anchor row consumed by crm_automation.py's
#     canonical-tag resolution (see services/crm_automation.py::_resolve_canonical,
#     role="tag"). It intentionally holds no SegmentMember rows of its own.
#   automation_rule: event-triggered action rules, owned by crm_automation.py.
SYSTEM_SEGMENT_TYPES = {"contact_tag", "automation_rule"}


class SegmentConditionError(ValueError):
    """Raised for ANY malformed/unknown/ambiguous condition data.

    Callers MUST catch this and guarantee zero SegmentMember mutation --
    never fall back to "no filter" behavior.
    """


@dataclass
class RefreshResult:
    segment_id: int
    current_member_ids: set[int] = field(default_factory=set)
    desired_member_ids: set[int] = field(default_factory=set)
    additions: set[int] = field(default_factory=set)
    removals: set[int] = field(default_factory=set)
    applied: bool = False
    skipped_reason: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Schema validation / legacy coercion
# ---------------------------------------------------------------------------

def _coerce_legacy(conditions: Any) -> list[dict]:
    """Deterministically convert a recognized legacy shape into a canonical
    rule list. Anything not deterministically recognized raises -- ambiguous
    legacy data is never guessed at silently.
    """
    if isinstance(conditions, dict) and "rules" in conditions and "schema_version" in conditions:
        rules = conditions["rules"]
        if not isinstance(rules, list):
            raise SegmentConditionError("canonical 'rules' must be a list")
        return rules

    if isinstance(conditions, dict) and set(conditions.keys()) == {"tag"}:
        # Legacy format: {"tag": ["My Order Customer"]} or {"tag": "My Order Customer"}
        value = conditions["tag"]
        value = value if isinstance(value, list) else [value]
        if not value or not all(isinstance(v, str) and v.strip() for v in value):
            raise SegmentConditionError(f"legacy 'tag' condition has an empty/invalid value: {conditions!r}")
        return [{"field": "tag", "op": "in", "value": value}]

    if isinstance(conditions, list):
        # A list of rule dicts. If an item already carries an explicit "op",
        # it's already canonical-shaped -- pass it through untouched so the
        # main validation loop is the sole authority on whether that op is
        # legal (never silently rewritten). Only items with NO "op" key at
        # all are legacy format B: [{"field": ..., "value": ...}]
        # (marketing_api.py's old _matching_dynamic_contacts contract, whose
        # original runtime behavior was an ilike-contains for 'tag' and
        # exact-equals for everything else).
        out = []
        for item in conditions:
            if not isinstance(item, dict) or "field" not in item:
                raise SegmentConditionError(f"cannot convert legacy list condition: {item!r}")
            if "op" in item:
                out.append(item)
                continue
            field_name = item["field"]
            op = "contains" if field_name == "tag" else "equals"
            out.append({"field": field_name, "op": op, "value": item.get("value")})
        return out

    raise SegmentConditionError(f"unrecognized/ambiguous legacy condition shape: {conditions!r}")


def validate_conditions(conditions: Any, match_mode: str) -> dict:
    """Normalize + validate. Raises SegmentConditionError on any problem.

    Never returns a partially-valid result -- either the whole thing is
    valid and normalized, or nothing is returned at all.
    """
    if match_mode not in {"all", "any"}:
        raise SegmentConditionError(f"match_mode must be 'all' or 'any', got {match_mode!r}")

    if conditions in (None, {}, [], ""):
        return {"schema_version": SCHEMA_VERSION, "match_mode": match_mode, "rules": []}

    rules = _coerce_legacy(conditions)
    if not isinstance(rules, list):
        raise SegmentConditionError("conditions must normalize to a list of rules")

    normalized = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise SegmentConditionError(f"each rule must be an object, got {rule!r}")
        field_name = rule.get("field")
        if field_name not in SUPPORTED_FIELDS:
            raise SegmentConditionError(f"unsupported field: {field_name!r}")
        op = rule.get("op")
        if op is None:
            raise SegmentConditionError(f"rule for field {field_name!r} is missing 'op'")
        allowed_ops = DATE_OPS if field_name in DATE_FIELDS else SUPPORTED_OPS
        if op not in allowed_ops or op not in SUPPORTED_OPS:
            raise SegmentConditionError(f"unsupported operator {op!r} for field {field_name!r}")
        value = rule.get("value")
        if op in {"in", "not_in"} and not isinstance(value, list):
            raise SegmentConditionError(f"operator {op!r} requires a list value")
        if op == "between" and not (isinstance(value, list) and len(value) == 2):
            raise SegmentConditionError("operator 'between' requires a two-element [start, end] value")
        if op not in {"exists", "not_exists"} and value is None:
            raise SegmentConditionError(f"operator {op!r} on field {field_name!r} requires a value")
        normalized.append({"field": field_name, "op": op, "value": value})

    return {"schema_version": SCHEMA_VERSION, "match_mode": match_mode, "rules": normalized}


def _effective_conditions(segment: Segment) -> dict:
    """Resolve a segment's effective canonical ruleset, including translation
    of purely-structural legacy segment_type behavior (segment_type='all'
    matches every tenant contact by design; that's not a fail-open bug --
    it's the documented purpose of that type).
    """
    if segment.segment_type == "all":
        return {"schema_version": SCHEMA_VERSION, "match_mode": "any", "rules": [], "match_all": True}
    if segment.segment_type == "newsletter" and not segment.conditions:
        return validate_conditions([{"field": "tag", "value": "newsletter"}], "any")
    return validate_conditions(segment.conditions, segment.match_mode or "all")


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _tag_key(label: Any) -> str:
    """Canonicalize a tag label for equality/membership comparisons.

    My Order Customer's known historical spelling variants (My Order
    Customer / MyOrder Customer / My Order) are folded to one canonical
    key via MY_ORDER_CUSTOMER_ALIASES -- the same registry
    services/crm_automation.py uses to canonicalize tags on write (see
    assign_contact_tag) and to name-match the CRM segment/tag rows. This
    is the single source of truth: a condition written against the
    canonical "My Order Customer" name transparently matches contacts
    still carrying any historical variant, and any tag/value NOT in that
    registry is normalized (case/whitespace only) exactly as before.

    Bare "MyOrder"/"myorder" (no "Customer"/"Order" qualifier) is
    deliberately NOT in the registry -- see
    tests/test_my_order_crm_automation.py::test_bare_myorder_is_not_a_customer_tag_alias,
    added with the original CRM automation feature (commit 5c78657) as a
    considered product decision, not an oversight. Production data has zero
    contacts with that bare tag, so this alone accounts for none of Segment
    #10's expected membership.
    """
    normalized = normalize_label(label)
    return "my order customer" if normalized in MY_ORDER_CUSTOMER_ALIASES else normalized


def _eval_rule(contact: Contact, rule: dict) -> bool:
    field_name, op, value = rule["field"], rule["op"], rule.get("value")

    if field_name == "tag":
        tags = {_tag_key(t) for t in split_tags(contact.tags)}
        if op == "exists":
            return bool(tags)
        if op == "not_exists":
            return not tags
        if op == "equals":
            return _tag_key(value) in tags
        if op == "not_equals":
            return _tag_key(value) not in tags
        if op == "in":
            return bool(tags & {_tag_key(v) for v in value})
        if op == "not_in":
            return not (tags & {_tag_key(v) for v in value})
        if op == "contains":
            needle = _tag_key(value)
            return any(needle in t for t in tags)
        if op == "not_contains":
            needle = _tag_key(value)
            return not any(needle in t for t in tags)
        raise SegmentConditionError(f"unsupported op {op!r} for field 'tag'")

    if field_name in DATE_FIELDS:
        actual = getattr(contact, field_name, None)
        if op == "exists":
            return actual is not None
        if op == "not_exists":
            return actual is None
        if actual is None:
            return False
        if op == "after":
            return actual > value
        if op == "before":
            return actual < value
        if op == "between":
            lo, hi = value
            return lo <= actual <= hi
        raise SegmentConditionError(f"unsupported op {op!r} for field {field_name!r}")

    actual = getattr(contact, field_name, None)
    if op == "exists":
        return actual is not None and actual != ""
    if op == "not_exists":
        return actual is None or actual == ""
    if op == "equals":
        if isinstance(value, str) and isinstance(actual, str):
            return actual.strip().casefold() == value.strip().casefold()
        return actual == value
    if op == "not_equals":
        if isinstance(value, str) and isinstance(actual, str):
            return actual.strip().casefold() != value.strip().casefold()
        return actual != value
    if op == "contains":
        return isinstance(actual, str) and str(value).casefold() in actual.casefold()
    if op == "not_contains":
        return not (isinstance(actual, str) and str(value).casefold() in actual.casefold())
    if op == "in":
        norm_values = {v.strip().casefold() if isinstance(v, str) else v for v in value}
        actual_norm = actual.strip().casefold() if isinstance(actual, str) else actual
        return actual_norm in norm_values
    if op == "not_in":
        norm_values = {v.strip().casefold() if isinstance(v, str) else v for v in value}
        actual_norm = actual.strip().casefold() if isinstance(actual, str) else actual
        return actual_norm not in norm_values
    raise SegmentConditionError(f"unsupported op {op!r} for field {field_name!r}")


def evaluate(segment: Segment, contact: Contact) -> bool:
    """Does this single contact currently qualify for this segment?

    Raises SegmentConditionError for malformed segment.conditions -- never
    silently treats malformed data as "matches everyone."
    """
    normalized = _effective_conditions(segment)
    if normalized.get("match_all"):
        return True
    if not normalized["rules"]:
        return False
    results = [_eval_rule(contact, rule) for rule in normalized["rules"]]
    return all(results) if normalized["match_mode"] == "all" else any(results)


# ---------------------------------------------------------------------------
# Membership computation / refresh
# ---------------------------------------------------------------------------

def _canonical_candidate_contacts(company_id: int):
    """Only canonical (active, non-merged) contacts are eligible for dynamic
    membership. Suppression (SMS/email opt-out) is a campaign-consumption-time
    concern, not a membership-eligibility concern -- a suppressed contact can
    legitimately remain a segment member; it simply won't be sent to.
    """
    return Contact.query.filter_by(company_id=company_id, is_active=True, merged_into_contact_id=None)


def compute_membership_delta(segment: Segment) -> RefreshResult:
    """Pure computation. Issues ZERO writes -- safe to call from a GET route.

    On a validation failure, returns a RefreshResult with .error set instead
    of raising, so preview callers can render the error without a 500; the
    result carries empty current/desired/additions/removals, guaranteeing
    that a caller who forgets to check .error still applies nothing.
    """
    if segment.segment_type in SYSTEM_SEGMENT_TYPES:
        return RefreshResult(
            segment.id, skipped_reason=(
                f"segment_type {segment.segment_type!r} is system-managed by a different "
                "mechanism and is never dynamically evaluated by this engine"
            ),
        )
    if not segment.is_dynamic and segment.segment_type != "all":
        current = {
            m.contact_id for m in SegmentMember.query.filter_by(segment_id=segment.id).all()
            if not m.is_excluded and not m.removed_at
        }
        return RefreshResult(
            segment.id, current_member_ids=current, desired_member_ids=current,
            skipped_reason="segment is static (is_dynamic=False); membership is manually managed",
        )

    try:
        normalized = _effective_conditions(segment)
    except SegmentConditionError as exc:
        return RefreshResult(segment.id, error=str(exc))

    rows = SegmentMember.query.filter_by(segment_id=segment.id).all()
    current_active_ids = {m.contact_id for m in rows if not m.is_excluded and not m.removed_at}
    excluded_ids = {m.contact_id for m in rows if m.is_excluded}

    desired_ids = set()
    try:
        for contact in _canonical_candidate_contacts(segment.company_id):
            if contact.id in excluded_ids:
                continue
            matched = (
                True if normalized.get("match_all") else
                (False if not normalized["rules"] else (
                    all(_eval_rule(contact, r) for r in normalized["rules"])
                    if normalized["match_mode"] == "all" else
                    any(_eval_rule(contact, r) for r in normalized["rules"])
                ))
            )
            if matched:
                desired_ids.add(contact.id)
    except SegmentConditionError as exc:
        return RefreshResult(segment.id, error=str(exc))

    additions = desired_ids - current_active_ids
    removals = current_active_ids - desired_ids
    return RefreshResult(
        segment.id, current_member_ids=current_active_ids, desired_member_ids=desired_ids,
        additions=additions, removals=removals,
    )


def refresh(segment: Segment, *, actor_user_id: int | None = None) -> RefreshResult:
    """The ONLY function permitted to mutate SegmentMember rows for
    dynamically-evaluated segments. Atomic: computes the full delta first;
    if that computation fails validation, ZERO rows are touched. Additions
    and removals only -- never delete-then-reinsert.
    """
    result = compute_membership_delta(segment)
    if result.error or result.skipped_reason:
        return result
    if not result.additions and not result.removals:
        result.applied = True
        return result

    now = datetime.utcnow()
    existing_by_contact = {m.contact_id: m for m in SegmentMember.query.filter_by(segment_id=segment.id).all()}

    for contact_id in result.additions:
        member = existing_by_contact.get(contact_id)
        if member:
            member.removed_at = None
            member.removed_by_user_id = None
            member.added_at = now
            member.source = member.source or "dynamic_rule"
        else:
            db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact_id, source="dynamic_rule", added_at=now))

    for contact_id in result.removals:
        member = existing_by_contact.get(contact_id)
        if member:
            member.removed_at = now
            member.removed_by_user_id = actor_user_id

    db.session.flush()
    segment.updated_at = now
    result.applied = True
    return result
