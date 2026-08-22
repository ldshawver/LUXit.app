"""Contact duplicate detection and merge utilities."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from extensions import db
from sqlalchemy import UniqueConstraint, or_

from models import (
    Contact,
    ContactDuplicateExclusion,
    ContactEmailAddress,
    ContactPhoneNumber,
    IntegrationAuditLog,
    SegmentMember,
    SMSRecipient,
    SMSRecipientDeliveryAttempt,
)

logger = logging.getLogger(__name__)


from services.phone_normalization import normalize_phone_e164
from services.contact_consent import (
    apply_email_opt_out,
    has_explicit_email_opt_in,
    has_explicit_email_opt_out,
)

def normalize_phone(raw: str | None) -> str:
    return normalize_phone_e164(raw)


def normalize_email(raw: str | None) -> str:
    return (raw or "").strip().lower()


def _full_name(contact: Contact) -> str:
    return (contact.name or f"{contact.first_name or ''} {contact.last_name or ''}").strip().lower()


def _split_tags(value: str | None) -> list[str]:
    seen = set()
    tags = []
    for raw in (value or "").replace(";", ",").replace("|", ",").split(","):
        tag = raw.strip()
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def _merge_text(*values: str | None) -> str | None:
    parts = []
    for value in values:
        value = (value or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " | ".join(parts) if parts else None


def _merge_date_min(a, b):
    if a and b:
        return min(a, b)
    return a or b


def _merge_date_max(a, b):
    if a and b:
        return max(a, b)
    return a or b


_VERIFICATION_RANK = {
    None: 0, "unverified": 0, "pending": 1, "trusted": 2, "verified": 3,
    "confirmed": 4,
}
_IDENTITY_RANK = {
    None: 0, "pending_identity": 0, "awaiting_name": 1, "awaiting_email": 1,
    "awaiting_confirmation": 2, "minimum_established": 3, "confirmed": 4,
}
_CONSENT_DENIALS = {"opted_out", "unsubscribed", "denied", "suppressed", "revoked"}
_CONSENT_GRANTS = {"opted_in", "subscribed", "granted", "confirmed"}


def _stronger_status(a, b, ranks):
    return b if ranks.get(b, 0) > ranks.get(a, 0) else a


def _merge_consent_status(a, b):
    rank = {
        "suppressed": 600, "opted_out": 590, "unsubscribed": 580,
        "revoked": 570, "denied": 560, "confirmed": 300,
        "opted_in": 290, "subscribed": 280, "granted": 270,
        "unknown": 0, "": 0,
    }
    values = [str(value or "").casefold() for value in (a, b)]
    return max(values, key=lambda value: rank.get(value, 1)) or "unknown"


def _contact_keys(contact: Contact) -> set[str]:
    """Duplicate keys scoped by tenant/company to prevent cross-tenant merges."""
    tenant = contact.company_id or "none"
    keys = set()
    email = normalize_email(contact.email)
    if email:
        keys.add(f"company:{tenant}:email:{email}")
    phone = (contact.normalized_phone or "").strip() or normalize_phone(contact.phone or "")
    if phone:
        keys.add(f"company:{tenant}:phone:{phone}")
    name = _full_name(contact)
    company = (contact.company or "").strip().lower()
    if name and company:
        keys.add(f"company:{tenant}:possible_name_company:{name}:{company}")
    return keys


def find_duplicate_contacts(company_id: int | None = None) -> list[dict]:
    query = Contact.query.filter_by(is_active=True)
    if company_id is not None:
        query = query.filter_by(company_id=company_id)
    contacts = query.order_by(Contact.created_at.asc(), Contact.id.asc()).all()

    buckets: dict[str, list[Contact]] = defaultdict(list)
    for contact in contacts:
        if contact.phone and not contact.normalized_phone:
            contact.normalized_phone = normalize_phone(contact.phone)
        for key in _contact_keys(contact):
            buckets[key].append(contact)

    excluded_pairs = set()
    exq = ContactDuplicateExclusion.query
    if company_id is not None:
        exq = exq.filter_by(company_id=company_id)
    for ex in exq.all():
        excluded_pairs.add(tuple(sorted((ex.contact_id_a, ex.contact_id_b))))

    groups = []
    seen_sets = set()
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        ids = tuple(sorted(c.id for c in group))
        if ids in seen_sets:
            continue
        if len(ids) == 2 and tuple(ids) in excluded_pairs:
            continue
        seen_sets.add(ids)
        groups.append({"match_key": key, "contact_ids": list(ids), "contacts": group})
    return groups


def preview_duplicate_contacts(company_id: int | None = None) -> dict[str, int]:
    groups = find_duplicate_contacts(company_id)
    return {"duplicate_groups": len(groups), "duplicate_contacts": sum(max(len(g["contact_ids"]) - 1, 0) for g in groups), "dry_run": True}


def merge_contacts(
    primary_contact_id: int,
    duplicate_contact_ids: list[int],
    *,
    actor_user_id: int | None = None,
    company_id: int | None = None,
    dry_run: bool = False,
    commit: bool = True,
) -> dict:
    """Merge selected duplicate contacts into an admin/user-chosen primary.

    All contacts must belong to the same company/tenant. Historical rows with
    contact_id FKs are reassigned to the primary; duplicate Contact rows are
    deactivated rather than deleted so no history is destroyed.
    """
    duplicate_contact_ids = [int(x) for x in duplicate_contact_ids if int(x) != int(primary_contact_id)]
    all_ids = tuple(sorted({primary_contact_id, *duplicate_contact_ids}))
    locked = (
        Contact.query.filter(Contact.id.in_(all_ids))
        .order_by(Contact.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    by_id = {row.id: row for row in locked}
    primary = by_id.get(primary_contact_id)
    if not primary or not primary.is_active:
        raise ValueError("primary contact not found")
    if primary.merged_into_contact_id is not None:
        raise ValueError("primary contact is already merged")
    expected_company_id = company_id if company_id is not None else primary.company_id
    if primary.company_id != expected_company_id:
        raise ValueError("primary contact is outside the requested company/tenant")
    if db.session.query(Contact.id).filter(
        Contact.id.in_(duplicate_contact_ids),
        Contact.company_id != expected_company_id,
    ).first():
        raise ValueError("cannot merge contacts across companies/tenants")
    duplicates = [
        by_id[contact_id] for contact_id in duplicate_contact_ids
        if contact_id in by_id
        and by_id[contact_id].company_id == expected_company_id
        and by_id[contact_id].is_active
        and by_id[contact_id].merged_into_contact_id is None
    ]
    if len(duplicates) != len(set(duplicate_contact_ids)):
        raise ValueError("one or more duplicate contacts were not found")

    audit = {
        "surviving_contact_id": primary.id,
        "merged_contact_ids": [],
        "actor_user_id": actor_user_id,
        "fields_transferred": {},
        "tags_transferred": [],
        "lists_transferred": [],
        "references_reassigned": {},
        "opt_out_preservation": {
            "sms_opted_out_before": bool(primary.sms_opted_out or primary.do_not_sms or primary.sms_opt_out_at),
            "email_unsubscribed_before": bool(primary.email_unsubscribed or primary.do_not_email),
            "sms_opted_out_after": False,
            "email_unsubscribed_after": False,
        },
        "dry_run": dry_run,
    }

    for dup in duplicates:
        audit["merged_contact_ids"].append(dup.id)
        _merge_contact_data(primary, dup, audit)
        _merge_segment_memberships(primary, dup, audit)
        changed = _repoint_contact_references(dup.id, primary.id)
        for table, count in changed.items():
            audit["references_reassigned"][table] = audit["references_reassigned"].get(table, 0) + count
        dup.is_active = False
        dup.status = "merged"
        dup.archived_at = datetime.utcnow()
        dup.merged_into_contact_id = primary.id
        dup.merged_at = datetime.utcnow()
        dup.merged_by_user_id = actor_user_id
        dup.duplicate_status = "merged"

    audit["opt_out_preservation"]["sms_opted_out_after"] = bool(primary.sms_opted_out or primary.do_not_sms or primary.sms_opt_out_at)
    audit["opt_out_preservation"]["email_unsubscribed_after"] = bool(primary.email_unsubscribed or primary.do_not_email)
    from services.crm_automation import synchronize_contact_tags
    synchronize_contact_tags(
        primary, source="contact_merge",
        event_key_prefix=f"merge:{primary.id}:{'-'.join(str(v) for v in sorted(duplicate_contact_ids))}",
    )
    _audit_merge(primary, audit, actor_user_id=actor_user_id)

    if dry_run:
        db.session.rollback()
    elif commit:
        db.session.commit()
    else:
        db.session.flush()
    return audit


def _set_if_blank(primary: Contact, dup: Contact, field: str, audit: dict):
    if not getattr(primary, field, None) and getattr(dup, field, None):
        setattr(primary, field, getattr(dup, field))
        audit["fields_transferred"].setdefault(field, []).append(dup.id)


def _merge_contact_data(primary: Contact, dup: Contact, audit: dict) -> None:
    primary_original_marker = (
        primary.first_touch_at or primary.source_added_at or primary.created_at
    )
    duplicate_original_marker = (
        dup.first_touch_at or dup.source_added_at or dup.created_at
    )
    if primary.last_touch_at is not None or dup.last_touch_at is not None:
        primary_latest_marker = primary.last_touch_at
        duplicate_latest_marker = dup.last_touch_at
    elif primary.last_seen_at is not None or dup.last_seen_at is not None:
        primary_latest_marker = primary.last_seen_at
        duplicate_latest_marker = dup.last_seen_at
    else:
        primary_latest_marker = primary.updated_at
        duplicate_latest_marker = dup.updated_at
    # Field matrix: blanks may be supplemented, but verified canonical identity
    # and explicit compliance choices are never weakened by a merge.
    for field in (
        "email", "company", "phone", "normalized_phone", "segment", "source",
        "source_channel", "source_phone_number", "source_provider", "source_context",
        "imported_batch_id", "imported_list", "display_name", "business_name",
        "primary_phone", "phone_extension", "primary_email", "normalized_email",
        "contact_type", "lifecycle_stage", "owner_user_id", "lead_status",
        "estimated_value", "won_revenue", "lost_reason", "avatar_url",
        "external_google_contact_id", "google_contact_resource_id",
        "google_contact_etag",
    ):
        _set_if_blank(primary, dup, field, audit)

    if _VERIFICATION_RANK.get(dup.name_verification_level, 0) > _VERIFICATION_RANK.get(primary.name_verification_level, 0):
        for field in (
            "name", "first_name", "last_name", "display_name", "name_source",
            "name_verification_level", "name_verified_at", "name_verified_by_user_id",
            "name_provenance",
        ):
            value = getattr(dup, field, None)
            if value is not None:
                setattr(primary, field, value)
                audit["fields_transferred"].setdefault(field, []).append(dup.id)
    else:
        for field in ("name", "first_name", "last_name"):
            _set_if_blank(primary, dup, field, audit)

    if _IDENTITY_RANK.get(dup.identity_status, 0) > _IDENTITY_RANK.get(primary.identity_status, 0):
        for field in (
            "identity_status", "identity_confirmed_at", "identity_confirmation_sid",
            "identity_verified_at", "identity_verification_source",
        ):
            value = getattr(dup, field, None)
            if value is not None:
                setattr(primary, field, value)
                audit["fields_transferred"].setdefault(field, []).append(dup.id)

    before_tags = _split_tags(primary.tags)
    before_keys = {t.lower() for t in before_tags}
    new_tags = []
    for tag in _split_tags(dup.tags):
        if tag.lower() not in before_keys:
            before_tags.append(tag)
            before_keys.add(tag.lower())
            new_tags.append(tag)
    if new_tags:
        primary.tags = ", ".join(before_tags)
        audit["tags_transferred"].extend(new_tags)

    if getattr(dup, "imported_list", None) and dup.imported_list not in audit["lists_transferred"]:
        audit["lists_transferred"].append(dup.imported_list)

    primary.source_detail = _merge_text(primary.source_detail, dup.source_detail or dup.source)
    primary.source_added_at = _merge_date_min(primary.source_added_at, dup.source_added_at or dup.created_at)
    primary.source_added_by_user_id = primary.source_added_by_user_id or dup.source_added_by_user_id
    primary.first_seen_at = _merge_date_min(primary.first_seen_at, dup.first_seen_at or dup.created_at)
    primary.last_seen_at = _merge_date_max(primary.last_seen_at, dup.last_seen_at or dup.created_at)
    primary.created_at = _merge_date_min(primary.created_at, dup.created_at)
    primary.updated_at = _merge_date_max(primary.updated_at, dup.updated_at)
    primary.first_touch_at = _merge_date_min(primary.first_touch_at, dup.first_touch_at)
    primary.last_touch_at = _merge_date_max(primary.last_touch_at, dup.last_touch_at)
    primary.last_contacted_at = _merge_date_max(primary.last_contacted_at, dup.last_contacted_at)
    primary.last_activity_at = _merge_date_max(primary.last_activity_at, dup.last_activity_at)
    primary.next_follow_up_at = _merge_date_min(primary.next_follow_up_at, dup.next_follow_up_at)

    primary_is_older = (
        (primary_original_marker or datetime.max, primary.id)
        <= (duplicate_original_marker or datetime.max, dup.id)
    )
    original_owner, latest_owner = (primary, dup) if primary_is_older else (dup, primary)
    for field in (
        "original_source", "original_source_detail", "original_source_campaign",
        "original_source_url", "original_referrer",
    ):
        if not getattr(primary, field, None):
            setattr(primary, field, getattr(original_owner, field, None) or getattr(latest_owner, field, None))
    latest_is_dup = (
        (duplicate_latest_marker or datetime.min, dup.id)
        > (primary_latest_marker or datetime.min, primary.id)
    )
    latest_owner = dup if latest_is_dup else primary
    for field in (
        "latest_source", "latest_source_detail", "latest_source_campaign",
        "latest_source_url", "latest_referrer",
    ):
        value = getattr(latest_owner, field, None)
        if value:
            setattr(primary, field, value)

    # Conservative subscription merge: opt-outs/unsubscribes always win.
    sms_opted_out = bool(primary.sms_opted_out or primary.do_not_sms or primary.sms_opt_out_at or dup.sms_opted_out or dup.do_not_sms or dup.sms_opt_out_at)
    if sms_opted_out:
        primary.sms_opted_out = True
        primary.do_not_sms = True
        primary.sms_marketing_opt_in = False
        primary.sms_consent_status = "opted_out"
        primary.sms_opt_out_at = _merge_date_min(primary.sms_opt_out_at, dup.sms_opt_out_at) or datetime.utcnow()
    elif dup.sms_marketing_opt_in and not primary.sms_marketing_opt_in:
        primary.sms_marketing_opt_in = True
        primary.sms_consent_status = dup.sms_consent_status or primary.sms_consent_status
        primary.sms_marketing_opt_in_at = primary.sms_marketing_opt_in_at or dup.sms_marketing_opt_in_at
    primary.sms_marketing_opt_in_at = _merge_date_min(
        primary.sms_marketing_opt_in_at,
        dup.sms_marketing_opt_in_at,
    )
    primary.sms_marketing_opt_in_source = _merge_text(
        primary.sms_marketing_opt_in_source,
        dup.sms_marketing_opt_in_source,
    )
    primary.sms_consent_status = _merge_consent_status(
        primary.sms_consent_status, dup.sms_consent_status
    )

    email_unsubscribed = (
        has_explicit_email_opt_out(primary)
        or has_explicit_email_opt_out(dup)
    )
    if email_unsubscribed:
        apply_email_opt_out(primary)
    elif has_explicit_email_opt_in(primary) or has_explicit_email_opt_in(dup):
        primary.email_opt_in = True
        primary.email_subscribed = True
        primary.is_subscribed = True
    if not email_unsubscribed:
        primary.email_consent_status = _merge_consent_status(
            primary.email_consent_status, dup.email_consent_status
        )

    primary.do_not_market = bool(primary.do_not_market or dup.do_not_market)
    primary.do_not_contact = bool(primary.do_not_contact or dup.do_not_contact)
    primary.marketing_preferences_reason = _merge_text(primary.marketing_preferences_reason, dup.marketing_preferences_reason)
    primary.marketing_preferences_source = _merge_text(primary.marketing_preferences_source, dup.marketing_preferences_source)
    primary.marketing_preferences_updated_by_user_id = (
        primary.marketing_preferences_updated_by_user_id
        or dup.marketing_preferences_updated_by_user_id
    )
    primary.marketing_preferences_updated_at = _merge_date_max(primary.marketing_preferences_updated_at, dup.marketing_preferences_updated_at)
    if dup.marketing_preferences_updated_at and (
        not primary.marketing_preferences_updated_at
        or dup.marketing_preferences_updated_at >= primary.marketing_preferences_updated_at
    ):
        primary.marketing_preferences_updated_by_user_id = (
            dup.marketing_preferences_updated_by_user_id
            or primary.marketing_preferences_updated_by_user_id
        )
    primary.identity_conflict_status = (
        primary.identity_conflict_status
        if primary.identity_conflict_status not in {None, "none"}
        else dup.identity_conflict_status
    )
    if not primary.identity_conflict_details and dup.identity_conflict_details:
        primary.identity_conflict_details = dup.identity_conflict_details
    approval_rank = {"rejected": 4, "review_required": 3, "approved": 2, "pending": 1, None: 0}
    if approval_rank.get(dup.approval_status, 0) > approval_rank.get(primary.approval_status, 0):
        for field in (
            "approval_status", "approved_at", "approved_by_user_id", "approval_source",
            "approval_match_source", "approval_rejected_at",
        ):
            setattr(primary, field, getattr(dup, field, None))


def _merge_mapping_metadata(survivor, duplicate) -> None:
    """Conservatively combine common relationship metadata before deduping."""
    for field in ("created_at", "added_at", "sent_at"):
        if hasattr(survivor, field):
            setattr(survivor, field, _merge_date_min(
                getattr(survivor, field, None), getattr(duplicate, field, None)
            ))
    for field in ("updated_at", "delivered_at", "replied_at", "opted_out_at", "removed_at"):
        if hasattr(survivor, field):
            setattr(survivor, field, _merge_date_max(
                getattr(survivor, field, None), getattr(duplicate, field, None)
            ))
    for field in ("source", "reason", "error_message", "exclusion_reason"):
        if hasattr(survivor, field):
            setattr(survivor, field, _merge_text(
                getattr(survivor, field, None), getattr(duplicate, field, None)
            ))


def _reconcile_contact_points(old_id: int, new_id: int) -> dict[str, int]:
    changed = {"contact_phone_number": 0, "contact_email_address": 0}
    for model, normalizer, table_name in (
        (ContactPhoneNumber, normalize_phone, "contact_phone_number"),
        (ContactEmailAddress, normalize_email, "contact_email_address"),
    ):
        rows = (
            model.query.filter(model.contact_id.in_([old_id, new_id]))
            .order_by(model.contact_id.asc(), model.id.asc())
            .with_for_update()
            .all()
        )
        survivor_rows = [row for row in rows if row.contact_id == new_id]
        duplicate_rows = [row for row in rows if row.contact_id == old_id]
        primary_exists = any(row.is_primary for row in survivor_rows)
        by_value = {
            normalizer(row.normalized_value or row.original_value): row
            for row in survivor_rows
            if normalizer(row.normalized_value or row.original_value)
        }
        for row in duplicate_rows:
            key = normalizer(row.normalized_value or row.original_value)
            existing = by_value.get(key) if key else None
            if existing:
                if _VERIFICATION_RANK.get(row.verification_status, 0) > _VERIFICATION_RANK.get(existing.verification_status, 0):
                    existing.verification_status = row.verification_status
                    existing.source = row.source or existing.source
                existing.original_value = existing.original_value or row.original_value
                existing.created_at = _merge_date_min(existing.created_at, row.created_at)
                existing.updated_at = _merge_date_max(existing.updated_at, row.updated_at)
                existing.is_primary = bool(existing.is_primary or (row.is_primary and not primary_exists))
                db.session.delete(row)
            else:
                if row.is_primary and primary_exists:
                    row.is_primary = False
                row.contact_id = new_id
                survivor_rows.append(row)
                if key:
                    by_value[key] = row
                primary_exists = primary_exists or row.is_primary
            changed[table_name] += 1
    return changed


def _reconcile_segment_members(old_id: int, new_id: int) -> int:
    changed = 0
    rows = (
        SegmentMember.query.filter(SegmentMember.contact_id.in_([old_id, new_id]))
        .order_by(SegmentMember.segment_id.asc(), SegmentMember.contact_id.asc(), SegmentMember.id.asc())
        .with_for_update()
        .all()
    )
    survivors = {row.segment_id: row for row in rows if row.contact_id == new_id}
    for row in (item for item in rows if item.contact_id == old_id):
        existing = survivors.get(row.segment_id)
        if existing:
            _merge_mapping_metadata(existing, row)
            existing.is_excluded = bool(existing.is_excluded or row.is_excluded)
            existing.added_by_user_id = existing.added_by_user_id or row.added_by_user_id
            existing.removed_by_user_id = existing.removed_by_user_id or row.removed_by_user_id
            db.session.delete(row)
        else:
            row.contact_id = new_id
            survivors[row.segment_id] = row
        changed += 1
    return changed


def _reconcile_sms_recipients(old_id: int, new_id: int) -> int:
    changed = 0
    rows = (
        SMSRecipient.query.filter(SMSRecipient.contact_id.in_([old_id, new_id]))
        .order_by(SMSRecipient.campaign_id.asc(), SMSRecipient.contact_id.asc(), SMSRecipient.id.asc())
        .with_for_update()
        .all()
    )
    for row in rows:
        if row.provider_message_sid and not SMSRecipientDeliveryAttempt.query.filter_by(
            provider_message_sid=row.provider_message_sid
        ).first():
            db.session.add(SMSRecipientDeliveryAttempt(
                company_id=row.company_id,
                campaign_id=row.campaign_id,
                contact_id=new_id,
                source_recipient_id=row.id,
                provider_message_sid=row.provider_message_sid,
                status=row.status,
                sent_at=row.sent_at,
                delivered_at=row.delivered_at,
                replied_at=row.replied_at,
                opted_out_at=row.opted_out_at,
                error_code=row.error_code,
                provider_error_code=row.provider_error_code,
                error_message=row.error_message,
                provider_response={
                    "message_sid": row.message_sid,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                },
            ))
    SMSRecipientDeliveryAttempt.query.filter_by(contact_id=old_id).update(
        {"contact_id": new_id}, synchronize_session=False
    )
    survivors = {
        row.campaign_id: row for row in rows
        if row.contact_id == new_id and row.campaign_id is not None
    }
    status_rank = {"pending": 0, "failed": 1, "sent": 2, "delivered": 3, "replied": 4, "opted_out": 5}
    for row in (item for item in rows if item.contact_id == old_id):
        # PostgreSQL UNIQUE constraints are NULLS DISTINCT by default. A NULL
        # campaign therefore represents an independent send, not a collision.
        existing = survivors.get(row.campaign_id) if row.campaign_id is not None else None
        if existing:
            _merge_mapping_metadata(existing, row)
            if status_rank.get(row.status, 0) > status_rank.get(existing.status, 0):
                existing.status = row.status
            existing.phone_number = existing.phone_number or row.phone_number
            existing.message_sid = existing.message_sid or row.message_sid
            provider_sid_to_transfer = row.provider_message_sid if not existing.provider_message_sid else None
            existing.error_code = existing.error_code or row.error_code
            existing.provider_error_code = existing.provider_error_code or row.provider_error_code
            db.session.delete(row)
            if provider_sid_to_transfer:
                db.session.flush()
                existing.provider_message_sid = provider_sid_to_transfer
        else:
            row.contact_id = new_id
            if row.campaign_id is not None:
                survivors[row.campaign_id] = row
        changed += 1
    return changed


def _reconcile_duplicate_exclusions(old_id: int, new_id: int) -> int:
    rows = (
        ContactDuplicateExclusion.query.filter(
            or_(
                ContactDuplicateExclusion.contact_id_a == old_id,
                ContactDuplicateExclusion.contact_id_b == old_id,
            )
        )
        .order_by(ContactDuplicateExclusion.id.asc())
        .with_for_update()
        .all()
    )
    changed = 0
    for row in rows:
        a = new_id if row.contact_id_a == old_id else row.contact_id_a
        b = new_id if row.contact_id_b == old_id else row.contact_id_b
        if a == b:
            db.session.delete(row)
            changed += 1
            continue
        a, b = sorted((a, b))
        existing = ContactDuplicateExclusion.query.filter_by(
            company_id=row.company_id, contact_id_a=a, contact_id_b=b
        ).filter(ContactDuplicateExclusion.id != row.id).first()
        if existing:
            existing.reason = _merge_text(existing.reason, row.reason)
            existing.created_at = _merge_date_min(existing.created_at, row.created_at)
            db.session.delete(row)
        else:
            row.contact_id_a, row.contact_id_b = a, b
        changed += 1
    return changed


def _contact_unique_column_sets(table, contact_column_name: str) -> list[tuple[str, ...]]:
    unique_sets = []
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            names = tuple(column.name for column in constraint.columns)
            if contact_column_name in names:
                unique_sets.append(names)
    for index in table.indexes:
        names = tuple(column.name for column in index.columns)
        if index.unique and contact_column_name in names:
            unique_sets.append(names)
    return unique_sets


def _repoint_generic_relationship(cls, column, old_id: int, new_id: int) -> int:
    rows = cls.query.filter(column == old_id).order_by(cls.__mapper__.primary_key[0].asc()).all()
    unique_sets = _contact_unique_column_sets(cls.__table__, column.name)
    changed = 0
    for row in rows:
        collision = None
        for names in unique_sets:
            # SQL UNIQUE uses NULLS DISTINCT unless explicitly declared
            # otherwise. No mapped constraint in this schema opts into
            # NULLS NOT DISTINCT.
            if any(
                name != column.name and getattr(row, name) is None
                for name in names
            ):
                continue
            filters = []
            for name in names:
                value = new_id if name == column.name else getattr(row, name)
                filters.append(getattr(cls, name) == value)
            collision = cls.query.filter(*filters).first()
            if collision and collision is not row:
                break
        if collision and collision is not row:
            _merge_mapping_metadata(collision, row)
            db.session.delete(row)
        else:
            setattr(row, column.name, new_id)
        changed += 1
    return changed


def _repoint_contact_references(old_id: int, new_id: int) -> dict[str, int]:
    updated = _reconcile_contact_points(old_id, new_id)
    updated["segment_member"] = _reconcile_segment_members(old_id, new_id)
    updated["sms_recipient"] = _reconcile_sms_recipients(old_id, new_id)
    updated["contact_duplicate_exclusion"] = _reconcile_duplicate_exclusions(old_id, new_id)
    special_tables = {
        "contact_phone_number", "contact_email_address", "segment_member",
        "sms_recipient", "contact_duplicate_exclusion",
    }
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_
        table = mapper.local_table
        if table.name == Contact.__tablename__ or table.name in special_tables:
            continue
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == Contact.__tablename__ and fk.column.name == "id":
                    count = _repoint_generic_relationship(cls, column, old_id, new_id)
                    if count:
                        updated[table.name] = updated.get(table.name, 0) + count
    # Contact self-references are intentionally handled last to avoid self-links.
    Contact.query.filter(
        Contact.possible_duplicate_of_id == old_id,
        Contact.id != new_id,
    ).update({"possible_duplicate_of_id": new_id}, synchronize_session=False)
    Contact.query.filter(
        Contact.merged_into_contact_id == old_id,
        Contact.id != new_id,
    ).update({"merged_into_contact_id": new_id}, synchronize_session=False)
    if new_id:
        primary = db.session.get(Contact, new_id)
        if primary and primary.possible_duplicate_of_id == old_id:
            primary.possible_duplicate_of_id = None
    return updated


def _merge_segment_memberships(primary: Contact, duplicate: Contact, audit: dict) -> None:
    """Repoint memberships without violating the canonical pair uniqueness."""
    existing = {row.segment_id: row for row in SegmentMember.query.filter_by(contact_id=primary.id).all()}
    moved = 0
    consolidated = 0
    for duplicate_row in SegmentMember.query.filter_by(contact_id=duplicate.id).all():
        primary_row = existing.get(duplicate_row.segment_id)
        if primary_row:
            if not duplicate_row.removed_at and not duplicate_row.is_excluded:
                primary_row.removed_at = None
                primary_row.removed_by_user_id = None
                primary_row.is_excluded = False
                primary_row.exclusion_reason = None
            db.session.delete(duplicate_row)
            consolidated += 1
        else:
            duplicate_row.contact_id = primary.id
            existing[duplicate_row.segment_id] = duplicate_row
            moved += 1
    if moved or consolidated:
        audit["references_reassigned"]["segment_member"] = (
            audit["references_reassigned"].get("segment_member", 0) + moved + consolidated
        )


def merge_duplicate_contacts(company_id: int | None = None, *, dry_run: bool = False, actor_user_id: int | None = None) -> dict[str, int]:
    """Auto-merge duplicate contacts by normalized phone, email, or full-name+company.

    The oldest contact in each group is kept for this scheduled/batch helper.
    Interactive/admin flows should call merge_contacts(primary_id, duplicate_ids)
    to explicitly choose the survivor.
    """
    groups = find_duplicate_contacts(company_id)
    merged = 0
    references = 0
    for group in groups:
        if ":possible_name_company:" in group.get("match_key", ""):
            continue
        contacts = sorted(group["contacts"], key=lambda c: (c.created_at or datetime.utcnow(), c.id))
        primary = contacts[0]
        duplicates = [c.id for c in contacts[1:]]
        result = merge_contacts(primary.id, duplicates, actor_user_id=actor_user_id, company_id=primary.company_id, dry_run=dry_run)
        merged += len(result["merged_contact_ids"])
        references += sum(result["references_reassigned"].values())
    logger.info("Contact duplicate merge complete: company_id=%s merged=%s references=%s dry_run=%s", company_id, merged, references, dry_run)
    return {"duplicate_groups": len(groups), "contacts_merged": merged, "references_updated": references, "audit_entries": len(groups), "dry_run": dry_run}


def _audit_merge(primary: Contact, audit: dict, *, actor_user_id: int | None) -> None:
    db.session.add(
        IntegrationAuditLog(
            company_id=primary.company_id,
            service_slug="contact_dedupe",
            action="merge",
            user_id=actor_user_id,
            changes=audit,
        )
    )


def related_record_counts(contact_id: int) -> dict[str, int]:
    """Reflectively count every mapped FK that references Contact."""
    counts: dict[str, int] = {}
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_; table = mapper.local_table
        if table.name == Contact.__tablename__:
            continue
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == Contact.__tablename__ and fk.column.name == "id":
                    try:
                        count = cls.query.filter(column == contact_id).count()
                        if count:
                            counts[table.name] = counts.get(table.name, 0) + count
                    except Exception:
                        counts[table.name] = counts.get(table.name, 0)
    return counts
