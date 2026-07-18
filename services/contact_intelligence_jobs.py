"""Persistent, resumable contact-intelligence jobs using the app database."""
from __future__ import annotations

from datetime import datetime

from extensions import db
from models import Contact, ContactIntelligenceJob, GoogleContactLookup
from services.contact_intelligence import apply_source_attribution, apply_google_name_match, meaningful_name, normalize_email, sync_contact_points
from services.contact_dedupe import find_duplicate_contacts
from services.phone_normalization import normalize_phone

JOB_TYPES = {"phone_backfill", "attribution_backfill", "duplicate_scan", "google_sync", "unnamed_match"}


def create_job(company_id: int, job_type: str, *, user_id: int | None = None, batch_size: int = 100, dry_run: bool = True) -> ContactIntelligenceJob:
    if job_type not in JOB_TYPES:
        raise ValueError("unsupported contact intelligence job type")
    job = ContactIntelligenceJob(company_id=company_id, user_id=user_id, job_type=job_type, batch_size=max(1, min(int(batch_size or 100), 1000)), dry_run=bool(dry_run), status="queued")
    db.session.add(job); db.session.commit(); return job


def run_job(job_id: int, *, max_batches: int = 1) -> ContactIntelligenceJob:
    job = db.session.get(ContactIntelligenceJob, int(job_id))
    if not job:
        raise ValueError("job not found")
    job.status = "running"; job.started_at = job.started_at or datetime.utcnow(); db.session.commit()
    try:
        for _ in range(max(1, int(max_batches or 1))):
            processed = _run_batch(job)
            db.session.commit()
            if processed < job.batch_size:
                job.status = "completed"; job.completed_at = datetime.utcnow(); db.session.commit(); break
        if job.status == "running":
            job.status = "queued"; db.session.commit()
    except Exception as exc:
        db.session.rollback()
        job = db.session.get(ContactIntelligenceJob, int(job_id))
        job.status = "failed"; job.failed += 1; job.sanitized_last_error = str(exc)[:500]; job.completed_at = datetime.utcnow()
        db.session.commit()
    return job


def _base_contacts(job):
    last_id = int((job.checkpoint or {}).get("last_contact_id") or 0)
    return (Contact.query.filter(Contact.company_id == job.company_id, Contact.id > last_id)
            .order_by(Contact.id.asc()).limit(job.batch_size).all())


def _checkpoint(job, contact_id):
    cp = dict(job.checkpoint or {}); cp["last_contact_id"] = contact_id; job.checkpoint = cp; job.cursor = str(contact_id)


def _run_batch(job: ContactIntelligenceJob) -> int:
    if job.job_type == "duplicate_scan":
        groups = find_duplicate_contacts(job.company_id)
        job.total_found = len(groups); job.processed = len(groups); job.updated = len(groups); return 0
    rows = _base_contacts(job); job.total_found = max(job.total_found, job.processed + len(rows))
    for contact in rows:
        try:
            changed = False
            if job.job_type == "phone_backfill":
                before = contact.normalized_phone
                sync_contact_points(contact, contact.phone or contact.primary_phone, contact.email or contact.primary_email, "legacy")
                changed = before != contact.normalized_phone
            elif job.job_type == "attribution_backfill":
                if not contact.original_source:
                    source = contact.source or "legacy"
                    if not job.dry_run:
                        apply_source_attribution(contact, source, detail=contact.source_detail, at=contact.created_at)
                    changed = True
                else:
                    job.skipped += 1
            elif job.job_type == "unnamed_match":
                if meaningful_name(contact) or not contact.normalized_phone:
                    job.skipped += 1
                else:
                    lookups = GoogleContactLookup.query.filter_by(company_id=job.company_id, normalized_phone=contact.normalized_phone).all()
                    candidates = []
                    for lookup in lookups:
                        if lookup.is_ambiguous:
                            candidates.extend(lookup.candidates or [])
                        else:
                            candidates.append({"normalized_phone": lookup.normalized_phone, "name": lookup.display_name, "resource_id": lookup.resource_id, "etag": lookup.etag})
                    status = apply_google_name_match(contact, candidates) if candidates and not job.dry_run else "skipped"
                    if status == "ambiguous": job.ambiguous += 1
                    elif status == "matched": changed = True
                    else: job.skipped += 1
            elif job.job_type == "google_sync":
                # API fetch is triggered through Google routes; this job type stores progress/status for resumable runs.
                job.skipped += 1
            if changed and not job.dry_run:
                job.updated += 1
            elif changed:
                job.skipped += 1
            job.processed += 1; _checkpoint(job, contact.id)
        except Exception as exc:
            job.failed += 1
            failures = list(job.failures or [])
            failures.append({"contact_id": contact.id, "error": str(exc)[:300]})
            job.failures = failures[-100:]
            job.sanitized_last_error = str(exc)[:500]
            _checkpoint(job, contact.id)
    return len(rows)
