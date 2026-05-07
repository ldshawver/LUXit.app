"""
Airtable integration service.

LUXit Postgres is the source of truth. Airtable is used for lightweight
external tracking only. All sync functions are safe — they never crash the app
on Airtable failure, include 3-attempt retry logic, enforce tenant isolation,
and write sync metadata to the ExternalSyncRecord table.

Env vars used:
  AIRTABLE_API_KEY          — PAT or legacy API key
  AIRTABLE_BASE_ID          — Airtable base identifier
  AIRTABLE_LEADS_TABLE      — e.g. "Leads"
  AIRTABLE_ONBOARDING_TABLE — e.g. "Onboarding Pipeline"
  AIRTABLE_IMPLEMENTATION_TABLE
  AIRTABLE_LICENSES_TABLE
  AIRTABLE_SUPPORT_TABLE    — e.g. "Support / Success Notes"
  AIRTABLE_SYNC_TABLE       — e.g. "Sync Metadata"
  AIRTABLE_SYNC_ENABLED     — "true" / "false"
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.airtable.com/v0"
_RETRY_DELAYS = [1, 2, 4]   # seconds between attempts


# ============================================================
# Config helpers
# ============================================================

def _token() -> str | None:
    return os.environ.get("AIRTABLE_API_KEY") or os.environ.get("AIRTABLE_TOKEN")


def _base_id() -> str:
    return os.environ.get("AIRTABLE_BASE_ID", "")


def _sync_enabled() -> bool:
    return os.environ.get("AIRTABLE_SYNC_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def _table(env_key: str) -> str:
    return os.environ.get(env_key, "")


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _is_configured() -> bool:
    return bool(_token() and _base_id())


# ============================================================
# Health
# ============================================================

def health_check() -> dict:
    token = _token()
    if not token:
        return {"status": "missing_config", "detail": "Airtable credentials not configured"}
    if not _base_id():
        return {"status": "missing_config", "detail": "AIRTABLE_BASE_ID not set"}
    try:
        r = requests.get(
            f"{_BASE_URL}/meta/bases/{_base_id()}/tables",
            headers=_headers(token),
            timeout=10,
        )
        if r.status_code == 200:
            tables = [t["name"] for t in r.json().get("tables", [])]
            return {
                "status": "connected",
                "sync_enabled": _sync_enabled(),
                "tables_found": len(tables),
            }
        if r.status_code == 401:
            return {"status": "error", "detail": "Invalid API key"}
        if r.status_code == 403:
            return {"status": "error", "detail": "Access denied — check base permissions"}
        return {"status": "error", "detail": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


# ============================================================
# Low-level CRUD (with retry)
# ============================================================

def list_records(base_id: str, table_name: str,
                 filter_formula: str | None = None,
                 max_records: int = 100) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config", "records": []}
    params: dict = {"maxRecords": max_records}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    result = _request_with_retry(
        "GET", f"{_BASE_URL}/{base_id}/{table_name}", token, params=params
    )
    if result["ok"]:
        return {"ok": True, "records": result["data"].get("records", [])}
    _log_error("list_records", result["error"])
    return {"ok": False, "reason": result["error"], "records": []}


def get_record(base_id: str, table_name: str, record_id: str) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    result = _request_with_retry(
        "GET", f"{_BASE_URL}/{base_id}/{table_name}/{record_id}", token
    )
    if result["ok"]:
        return {"ok": True, "record": result["data"]}
    _log_error("get_record", result["error"])
    return {"ok": False, "reason": result["error"]}


def create_record(base_id: str, table_name: str, fields: dict,
                  company_id: int | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    result = _request_with_retry(
        "POST", f"{_BASE_URL}/{base_id}/{table_name}", token,
        body={"fields": fields}
    )
    if result["ok"]:
        _log_integration_event(company_id, "airtable_create", result["data"].get("id"))
        return {"ok": True, "record": result["data"]}
    _log_error("create_record", result["error"])
    return {"ok": False, "reason": result["error"]}


def update_record(base_id: str, table_name: str, record_id: str,
                  fields: dict, company_id: int | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    result = _request_with_retry(
        "PATCH", f"{_BASE_URL}/{base_id}/{table_name}/{record_id}", token,
        body={"fields": fields}
    )
    if result["ok"]:
        _log_integration_event(company_id, "airtable_update", record_id)
        return {"ok": True, "record": result["data"]}
    _log_error("update_record", result["error"])
    return {"ok": False, "reason": result["error"]}


def delete_record(base_id: str, table_name: str, record_id: str,
                  company_id: int | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    result = _request_with_retry(
        "DELETE", f"{_BASE_URL}/{base_id}/{table_name}/{record_id}", token
    )
    if result["ok"]:
        _log_integration_event(company_id, "airtable_delete", record_id)
        return {"ok": True, "deleted": record_id}
    _log_error("delete_record", result["error"])
    return {"ok": False, "reason": result["error"]}


# ============================================================
# High-level sync functions
# ============================================================

def sync_lead_to_airtable(contact_id: int, company_id: int) -> dict:
    """
    Sync a Contact (lead) to the Airtable Leads table.
    Postgres is authoritative — this is a one-way push.
    Tenant isolation: verifies contact.company_id == company_id.
    """
    if not _sync_enabled():
        return {"ok": False, "reason": "Airtable sync is disabled (AIRTABLE_SYNC_ENABLED=false)"}
    if not _is_configured():
        return {"ok": False, "reason": "Airtable not configured"}

    table = _table("AIRTABLE_LEADS_TABLE")
    if not table:
        return {"ok": False, "reason": "AIRTABLE_LEADS_TABLE not set"}

    try:
        from models import Contact
        contact = Contact.query.get(contact_id)
        if not contact:
            return {"ok": False, "reason": f"Contact {contact_id} not found"}
        if contact.company_id != company_id:
            logger.warning("Tenant isolation: contact %s belongs to company %s not %s",
                           contact_id, contact.company_id, company_id)
            return {"ok": False, "reason": "Tenant isolation violation — contact belongs to another company"}
    except Exception as exc:
        return {"ok": False, "reason": f"DB error: {exc}"}

    fields = {
        "Name":     f"{contact.first_name or ''} {contact.last_name or ''}".strip() or contact.email or "",
        "Email":    contact.email or "",
        "Phone":    contact.phone or "",
        "Source":   contact.source or "",
        "Tags":     contact.tags or "",
        "Status":   "Active" if contact.is_active else "Inactive",
        "Company":  contact.company or "",
        "Segment":  contact.segment or "",
        "LUXit ID": str(contact.id),
        "Synced At": datetime.now(timezone.utc).isoformat(),
    }

    return _upsert_to_airtable(
        local_entity_type="contact",
        local_entity_id=str(contact_id),
        company_id=company_id,
        table=table,
        fields=fields,
        operation="sync_lead",
    )


def sync_onboarding_to_airtable(project_id: int, company_id: int) -> dict:
    """
    Sync a CustomerOnboardingProject to the Airtable Onboarding Pipeline table.
    Tenant isolation enforced.
    """
    if not _sync_enabled():
        return {"ok": False, "reason": "Airtable sync is disabled"}
    if not _is_configured():
        return {"ok": False, "reason": "Airtable not configured"}

    table = _table("AIRTABLE_ONBOARDING_TABLE")
    if not table:
        return {"ok": False, "reason": "AIRTABLE_ONBOARDING_TABLE not set"}

    try:
        from models import CustomerOnboardingProject
        project = CustomerOnboardingProject.query.get(project_id)
        if not project:
            return {"ok": False, "reason": f"OnboardingProject {project_id} not found"}
        if project.company_id != company_id:
            return {"ok": False, "reason": "Tenant isolation violation"}
    except Exception as exc:
        return {"ok": False, "reason": f"DB error: {exc}"}

    fields = {
        "Title":        project.title or "",
        "Status":       (project.status or "").replace("_", " ").title(),
        "Due Date":     project.due_date.date().isoformat() if project.due_date else "",
        "Completed At": project.completed_at.isoformat() if project.completed_at else "",
        "Notes":        (project.notes or "")[:500],
        "LUXit ID":     str(project.id),
        "Synced At":    datetime.now(timezone.utc).isoformat(),
    }

    # Include task summary
    try:
        total = len(project.tasks)
        done  = sum(1 for t in project.tasks if t.status == "completed")
        fields["Tasks Total"]     = total
        fields["Tasks Completed"] = done
    except Exception:
        pass

    return _upsert_to_airtable(
        local_entity_type="onboarding_project",
        local_entity_id=str(project_id),
        company_id=company_id,
        table=table,
        fields=fields,
        operation="sync_onboarding",
    )


def sync_support_note_to_airtable(ticket_id: int, company_id: int) -> dict:
    """
    Sync a FeedbackTicket (support/success note) to the Airtable Support table.
    Tenant isolation enforced.
    """
    if not _sync_enabled():
        return {"ok": False, "reason": "Airtable sync is disabled"}
    if not _is_configured():
        return {"ok": False, "reason": "Airtable not configured"}

    table = _table("AIRTABLE_SUPPORT_TABLE")
    if not table:
        return {"ok": False, "reason": "AIRTABLE_SUPPORT_TABLE not set"}

    try:
        from models import FeedbackTicket
        ticket = FeedbackTicket.query.get(ticket_id)
        if not ticket:
            return {"ok": False, "reason": f"FeedbackTicket {ticket_id} not found"}
        if ticket.company_id != company_id:
            return {"ok": False, "reason": "Tenant isolation violation"}
    except Exception as exc:
        return {"ok": False, "reason": f"DB error: {exc}"}

    fields = {
        "Title":       ticket.title or "",
        "Type":        ticket.ticket_type or "",
        "Status":      ticket.status or "",
        "Severity":    ticket.severity or "",
        "Description": (ticket.description or "")[:500],
        "LUXit ID":    str(ticket.id),
        "Created At":  ticket.created_at.isoformat() if ticket.created_at else "",
        "Synced At":   datetime.now(timezone.utc).isoformat(),
    }

    return _upsert_to_airtable(
        local_entity_type="feedback_ticket",
        local_entity_id=str(ticket_id),
        company_id=company_id,
        table=table,
        fields=fields,
        operation="sync_support",
    )


# ============================================================
# Upsert logic — create or update based on ExternalSyncRecord
# ============================================================

def _upsert_to_airtable(local_entity_type: str, local_entity_id: str,
                         company_id: int, table: str, fields: dict,
                         operation: str) -> dict:
    """
    Look up whether this entity has already been synced (has an external_entity_id).
    If yes → PATCH; if no → POST. Either way, persist result in ExternalSyncRecord.
    """
    token   = _token()
    base    = _base_id()
    now     = datetime.now(timezone.utc)

    # Load existing sync record
    sync_row = _get_sync_row(local_entity_type, local_entity_id, company_id)
    external_id = sync_row.external_entity_id if sync_row else None

    if external_id:
        result = _request_with_retry(
            "PATCH", f"{_BASE_URL}/{base}/{table}/{external_id}", token,
            body={"fields": fields}
        )
    else:
        result = _request_with_retry(
            "POST", f"{_BASE_URL}/{base}/{table}", token,
            body={"fields": fields}
        )

    if result["ok"]:
        new_external_id = result["data"].get("id", external_id)
        _save_sync_row(
            local_entity_type=local_entity_type,
            local_entity_id=local_entity_id,
            company_id=company_id,
            external_entity_id=new_external_id,
            sync_status="synced",
            error_notes=None,
            last_synced_at=now,
        )
        # Also push a row to the Airtable Sync Metadata table (best-effort)
        _write_sync_metadata_row(operation, local_entity_type, local_entity_id,
                                 "synced", "", token, base)
        logger.info("Airtable %s: %s/%s → %s", operation, local_entity_type,
                    local_entity_id, new_external_id)
        return {"ok": True, "external_id": new_external_id, "action": "updated" if external_id else "created"}
    else:
        error_msg = result["error"][:500]
        _save_sync_row(
            local_entity_type=local_entity_type,
            local_entity_id=local_entity_id,
            company_id=company_id,
            external_entity_id=external_id,
            sync_status="failed",
            error_notes=error_msg,
            last_synced_at=now,
        )
        _write_sync_metadata_row(operation, local_entity_type, local_entity_id,
                                 "failed", error_msg, token, base)
        _log_error(operation, error_msg)
        return {"ok": False, "reason": error_msg}


def _write_sync_metadata_row(operation, entity_type, entity_id,
                              status, error_notes, token, base):
    """Best-effort write to the Airtable Sync Metadata table."""
    sync_table = _table("AIRTABLE_SYNC_TABLE")
    if not sync_table or not token or not base:
        return
    try:
        fields = {
            "Operation":   operation,
            "Entity Type": entity_type,
            "Entity ID":   str(entity_id),
            "Status":      status,
            "Error Notes": error_notes or "",
            "Synced At":   datetime.now(timezone.utc).isoformat(),
        }
        requests.post(
            f"{_BASE_URL}/{base}/{sync_table}",
            headers=_headers(token),
            json={"fields": fields},
            timeout=8,
        )
    except Exception:
        pass  # never block


# ============================================================
# Sync log queries (for admin UI)
# ============================================================

def get_sync_logs(company_id: int | None = None,
                  entity_type: str | None = None,
                  limit: int = 50) -> list:
    """Return recent ExternalSyncRecord rows for the admin UI."""
    try:
        from models import ExternalSyncRecord
        q = ExternalSyncRecord.query.filter_by(provider="airtable")
        if company_id is not None:
            q = q.filter_by(company_id=company_id)
        if entity_type:
            q = q.filter_by(local_entity_type=entity_type)
        rows = q.order_by(ExternalSyncRecord.last_synced_at.desc()).limit(limit).all()
        return [
            {
                "id":               r.id,
                "entity_type":      r.local_entity_type,
                "entity_id":        r.local_entity_id,
                "external_id":      r.external_entity_id,
                "sync_status":      r.sync_status,
                "last_synced_at":   r.last_synced_at.isoformat() if r.last_synced_at else None,
                "error":            (json.loads(r.metadata_json) or {}).get("error") if r.metadata_json else None,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error("get_sync_logs: %s", exc)
        return []


def get_sync_stats(company_id: int | None = None) -> dict:
    """Return counts of synced/failed/pending for the admin UI."""
    try:
        from models import ExternalSyncRecord
        q = ExternalSyncRecord.query.filter_by(provider="airtable")
        if company_id is not None:
            q = q.filter_by(company_id=company_id)
        rows = q.all()
        stats = {"synced": 0, "failed": 0, "pending": 0, "total": len(rows)}
        for r in rows:
            stats[r.sync_status] = stats.get(r.sync_status, 0) + 1
        return stats
    except Exception as exc:
        logger.error("get_sync_stats: %s", exc)
        return {"synced": 0, "failed": 0, "pending": 0, "total": 0}


# ============================================================
# ExternalSyncRecord helpers
# ============================================================

def _get_sync_row(entity_type: str, entity_id: str, company_id: int):
    try:
        from models import ExternalSyncRecord
        return ExternalSyncRecord.query.filter_by(
            provider="airtable",
            local_entity_type=entity_type,
            local_entity_id=str(entity_id),
            company_id=company_id,
        ).first()
    except Exception as exc:
        logger.warning("_get_sync_row error: %s", exc)
        return None


def _save_sync_row(local_entity_type, local_entity_id, company_id,
                   external_entity_id, sync_status, error_notes, last_synced_at):
    try:
        from extensions import db
        from models import ExternalSyncRecord
        row = _get_sync_row(local_entity_type, local_entity_id, company_id)
        meta = json.dumps({"error": error_notes}) if error_notes else None
        if row:
            row.external_entity_id = external_entity_id or row.external_entity_id
            row.sync_status        = sync_status
            row.last_synced_at     = last_synced_at
            row.metadata_json      = meta
        else:
            row = ExternalSyncRecord(
                company_id=company_id,
                provider="airtable",
                local_entity_type=local_entity_type,
                local_entity_id=str(local_entity_id),
                external_entity_id=external_entity_id,
                sync_status=sync_status,
                last_synced_at=last_synced_at,
                metadata_json=meta,
            )
            db.session.add(row)
        db.session.commit()
    except Exception as exc:
        logger.error("_save_sync_row error: %s", exc)
        try:
            from extensions import db
            db.session.rollback()
        except Exception:
            pass


# ============================================================
# HTTP with retry
# ============================================================

def _request_with_retry(method: str, url: str, token: str,
                         params: dict | None = None,
                         body: dict | None = None) -> dict:
    """
    Make an HTTP request with up to 3 attempts.
    Returns {"ok": True, "data": {...}} or {"ok": False, "error": "..."}.
    Never raises.
    """
    last_error = "unknown error"
    for attempt, delay in enumerate([0] + _RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.request(
                method,
                url,
                headers=_headers(token),
                params=params,
                json=body,
                timeout=15,
            )
            if resp.status_code < 300:
                return {"ok": True, "data": resp.json() if resp.content else {}}
            # 429 rate limit — always retry
            if resp.status_code == 429:
                last_error = "Rate limited (429)"
                logger.warning("Airtable rate limited on attempt %d — retrying in %ds",
                               attempt, _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)])
                continue
            # 5xx server error — retry
            if resp.status_code >= 500:
                last_error = f"Server error HTTP {resp.status_code}"
                continue
            # 4xx client errors — do not retry
            try:
                body_json = resp.json()
                last_error = body_json.get("error", {}).get("message", resp.text[:200])
            except Exception:
                last_error = resp.text[:200]
            return {"ok": False, "error": last_error}
        except requests.exceptions.Timeout:
            last_error = f"Request timed out (attempt {attempt})"
            logger.warning("Airtable timeout on attempt %d to %s", attempt, url)
        except requests.exceptions.ConnectionError as exc:
            last_error = f"Connection error: {exc}"
            logger.warning("Airtable connection error on attempt %d: %s", attempt, exc)
        except Exception as exc:
            last_error = str(exc)[:200]
            logger.error("Airtable unexpected error on attempt %d: %s", attempt, exc)

    logger.error("Airtable %s %s failed after %d attempts: %s",
                 method, url, len(_RETRY_DELAYS) + 1, last_error)
    return {"ok": False, "error": last_error}


# ============================================================
# Error / event logging
# ============================================================

def _log_error(endpoint: str, error: str | Exception):
    msg = str(error)[:500]
    logger.error("Airtable %s: %s", endpoint, msg)
    try:
        from extensions import db
        from models import IntegrationErrorLog
        el = IntegrationErrorLog(
            provider="airtable",
            endpoint=endpoint,
            error_message=msg,
        )
        db.session.add(el)
        db.session.commit()
    except Exception:
        pass


def _log_integration_event(company_id, event_type, external_id):
    try:
        from extensions import db
        from models import IntegrationEvent
        ev = IntegrationEvent(
            company_id=company_id, provider="airtable",
            event_type=event_type,
            external_id=str(external_id) if external_id else None,
            status="processed",
        )
        db.session.add(ev)
        db.session.commit()
    except Exception:
        pass
