"""Contact duplicate detection and merge utilities."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

from extensions import db
from models import Contact, IntegrationAuditLog, ContactDuplicateExclusion

logger = logging.getLogger(__name__)


from services.phone_normalization import normalize_phone_e164

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


def _contact_keys(contact: Contact) -> set[str]:
    """Duplicate keys scoped by tenant/company to prevent cross-tenant merges."""
    tenant = contact.company_id or "none"
    keys = set()
    email = normalize_email(contact.email)
    if email:
        keys.add(f"company:{tenant}:email:{email}")
    phone = normalize_phone(contact.normalized_phone or contact.phone or "")
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


def merge_contacts(primary_contact_id: int, duplicate_contact_ids: list[int], *, actor_user_id: int | None = None, company_id: int | None = None, dry_run: bool = False) -> dict:
    """Merge selected duplicate contacts into an admin/user-chosen primary.

    All contacts must belong to the same company/tenant. Historical rows with
    contact_id FKs are reassigned to the primary; duplicate Contact rows are
    deactivated rather than deleted so no history is destroyed.
    """
    duplicate_contact_ids = [int(x) for x in duplicate_contact_ids if int(x) != int(primary_contact_id)]
    primary = db.session.get(Contact, primary_contact_id)
    if not primary or not primary.is_active:
        raise ValueError("primary contact not found")
    expected_company_id = company_id if company_id is not None else primary.company_id
    if primary.company_id != expected_company_id:
        raise ValueError("primary contact is outside the requested company/tenant")
    duplicates = Contact.query.filter(Contact.id.in_(duplicate_contact_ids), Contact.is_active.is_(True)).all()
    if len(duplicates) != len(set(duplicate_contact_ids)):
        raise ValueError("one or more duplicate contacts were not found")
    cross_tenant = [c.id for c in duplicates if c.company_id != expected_company_id]
    if cross_tenant:
        raise ValueError("cannot merge contacts across companies/tenants")

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
    _audit_merge(primary, audit, actor_user_id=actor_user_id)

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()
    return audit


def _set_if_blank(primary: Contact, dup: Contact, field: str, audit: dict):
    if not getattr(primary, field, None) and getattr(dup, field, None):
        setattr(primary, field, getattr(dup, field))
        audit["fields_transferred"].setdefault(field, []).append(dup.id)


def _merge_contact_data(primary: Contact, dup: Contact, audit: dict) -> None:
    for field in ("email", "name", "first_name", "last_name", "company", "phone", "normalized_phone", "segment", "source", "source_channel", "source_phone_number", "source_provider", "source_context", "imported_batch_id", "imported_list"):
        _set_if_blank(primary, dup, field, audit)

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

    email_unsubscribed = bool(primary.email_unsubscribed or primary.do_not_email or dup.email_unsubscribed or dup.do_not_email)
    if email_unsubscribed:
        primary.email_unsubscribed = True
        primary.do_not_email = True
        primary.email_opt_in = False
        primary.email_subscribed = False
        primary.is_subscribed = False
    elif dup.email_opt_in and not primary.email_opt_in:
        primary.email_opt_in = True
        primary.email_subscribed = True
        primary.is_subscribed = True

    primary.do_not_market = bool(primary.do_not_market or dup.do_not_market)
    primary.marketing_preferences_reason = _merge_text(primary.marketing_preferences_reason, dup.marketing_preferences_reason)
    primary.marketing_preferences_source = _merge_text(primary.marketing_preferences_source, dup.marketing_preferences_source)
    primary.marketing_preferences_updated_at = _merge_date_max(primary.marketing_preferences_updated_at, dup.marketing_preferences_updated_at)


def _repoint_contact_references(old_id: int, new_id: int) -> dict[str, int]:
    updated: dict[str, int] = {}
    for mapper in db.Model.registry.mappers:
        cls = mapper.class_
        table = mapper.local_table
        if table.name == Contact.__tablename__:
            continue
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == Contact.__tablename__ and fk.column.name == "id":
                    count = cls.query.filter(column == old_id).update({column.name: new_id}, synchronize_session=False) or 0
                    if count:
                        updated[table.name] = updated.get(table.name, 0) + count
    return updated


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
