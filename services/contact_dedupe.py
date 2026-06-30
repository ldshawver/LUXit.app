"""Contact duplicate detection and merge utilities."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from extensions import db
from models import Contact, IntegrationAuditLog


def normalize_phone(raw: str | None) -> str:
    raw = (raw or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+") and raw[1:].isdigit():
        return raw
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return raw

logger = logging.getLogger(__name__)


def _split_tags(value: str | None) -> list[str]:
    seen = set()
    tags = []
    for raw in (value or "").split(","):
        tag = raw.strip()
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def _merge_text(existing: str | None, incoming: str | None) -> str | None:
    parts = []
    for value in (existing, incoming):
        value = (value or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " | ".join(parts) if parts else None


def normalize_email(raw: str | None) -> str:
    return (raw or "").strip().lower()


def _contact_keys(contact: Contact) -> set[str]:
    # Include company_id in every match key so a global weekly run can never
    # merge contacts across tenants, even when phone/email values match.
    tenant = contact.company_id or "none"
    keys = set()
    email = normalize_email(contact.email)
    if email:
        keys.add(f"company:{tenant}:email:{email}")
    phone = normalize_phone(contact.normalized_phone or contact.phone or "")
    if phone:
        keys.add(f"company:{tenant}:phone:{phone}")
    return keys


def merge_duplicate_contacts(company_id: int | None = None, *, dry_run: bool = False, actor_user_id: int | None = None) -> dict[str, int]:
    """Merge duplicate contacts by normalized phone or email.

    The oldest contact is kept. Empty fields are filled from duplicates, tags are
    unioned, source details are concatenated, and rows with FKs to duplicate
    contacts are repointed to the kept contact before duplicates are deactivated.
    """
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

    duplicate_sets = []
    seen_sets = set()
    for group in buckets.values():
        if len(group) < 2:
            continue
        ids = tuple(sorted(c.id for c in group))
        if ids not in seen_sets:
            seen_sets.add(ids)
            duplicate_sets.append(group)

    merged = 0
    touched = 0
    audit_entries = 0
    for group in duplicate_sets:
        keeper = sorted(group, key=lambda c: (c.created_at or datetime.utcnow(), c.id))[0]
        duplicates = [c for c in group if c.id != keeper.id]
        for dup in duplicates:
            before_tags = _split_tags(keeper.tags)
            duplicate_tags = _split_tags(dup.tags)
            for field in ("email", "name", "first_name", "last_name", "company", "phone", "normalized_phone", "segment"):
                if not getattr(keeper, field, None) and getattr(dup, field, None):
                    setattr(keeper, field, getattr(dup, field))
            if hasattr(keeper, "notes") and hasattr(dup, "notes"):
                keeper.notes = _merge_text(getattr(keeper, "notes", None), getattr(dup, "notes", None))
            keeper.tags = ", ".join(before_tags + [t for t in duplicate_tags if t.lower() not in {x.lower() for x in before_tags}])
            keeper.source = keeper.source or dup.source
            keeper.source_detail = _merge_text(keeper.source_detail, dup.source_detail or dup.source)
            keeper.source_added_at = keeper.source_added_at or dup.source_added_at or dup.created_at
            keeper.source_added_by_user_id = keeper.source_added_by_user_id or dup.source_added_by_user_id
            keeper.is_subscribed = bool(keeper.is_subscribed or dup.is_subscribed)
            keeper.sms_marketing_opt_in = bool(keeper.sms_marketing_opt_in or dup.sms_marketing_opt_in)
            dup.is_active = False
            touched += _repoint_contact_references(dup.id, keeper.id)
            _audit_merge(keeper, dup, actor_user_id=actor_user_id, dry_run=dry_run)
            audit_entries += 1
            merged += 1

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()
    logger.info("Contact duplicate merge complete: company_id=%s merged=%s references=%s audits=%s dry_run=%s", company_id, merged, touched, audit_entries, dry_run)
    return {"duplicate_groups": len(duplicate_sets), "contacts_merged": merged, "references_updated": touched, "audit_entries": audit_entries, "dry_run": dry_run}


def _repoint_contact_references(old_id: int, new_id: int) -> int:
    updated = 0
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_
        table = mapper.local_table
        if table.name == Contact.__tablename__:
            continue
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == Contact.__tablename__ and fk.column.name == "id":
                    count = cls.query.filter(column == old_id).update({column.name: new_id}, synchronize_session=False)
                    updated += count or 0
    return updated


def preview_duplicate_contacts(company_id: int | None = None) -> dict[str, int]:
    """Return duplicate counts without changing contact data."""
    return merge_duplicate_contacts(company_id=company_id, dry_run=True)


def _audit_merge(keeper: Contact, duplicate: Contact, *, actor_user_id: int | None, dry_run: bool) -> None:
    db.session.add(
        IntegrationAuditLog(
            company_id=keeper.company_id,
            service_slug="contact_dedupe",
            action="preview_merge" if dry_run else "merge",
            user_id=actor_user_id,
            changes={
                "kept_contact_id": keeper.id,
                "merged_contact_id": duplicate.id,
                "matched_email": normalize_email(keeper.email) if keeper.email and duplicate.email and normalize_email(keeper.email) == normalize_email(duplicate.email) else None,
                "matched_phone": normalize_phone(keeper.normalized_phone or keeper.phone or "") if normalize_phone(keeper.normalized_phone or keeper.phone or "") and normalize_phone(keeper.normalized_phone or keeper.phone or "") == normalize_phone(duplicate.normalized_phone or duplicate.phone or "") else None,
                "duplicate_source": duplicate.source,
                "duplicate_source_detail": duplicate.source_detail,
                "dry_run": dry_run,
            },
        )
    )
