"""
Airtable integration service.

LUXit Postgres remains the source of truth. Airtable is used for
lightweight external tracking and sync only. Never blocks core features.

Credentials: AIRTABLE_API_KEY or AIRTABLE_TOKEN (Replit secret).
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.airtable.com/v0"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def health_check() -> dict:
    token = _token()
    if not token:
        return {"status": "missing_config", "detail": "Airtable credentials not configured"}
    try:
        r = requests.get(
            "https://api.airtable.com/v0/meta/bases",
            headers=_headers(token),
            timeout=10,
        )
        if r.status_code == 200:
            return {"status": "connected"}
        if r.status_code == 401:
            return {"status": "error", "detail": "Invalid API key"}
        return {"status": "error", "detail": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_records(base_id: str, table_name: str,
                 filter_formula: str | None = None,
                 max_records: int = 100) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config", "records": []}
    params: dict = {"maxRecords": max_records}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    try:
        r = requests.get(
            f"{_BASE_URL}/{base_id}/{table_name}",
            headers=_headers(token),
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return {"ok": True, "records": r.json().get("records", [])}
    except Exception as exc:
        _log_error("list_records", exc)
        return {"ok": False, "reason": str(exc)[:200], "records": []}


def get_record(base_id: str, table_name: str, record_id: str) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = requests.get(
            f"{_BASE_URL}/{base_id}/{table_name}/{record_id}",
            headers=_headers(token),
            timeout=15,
        )
        r.raise_for_status()
        return {"ok": True, "record": r.json()}
    except Exception as exc:
        _log_error("get_record", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def create_record(base_id: str, table_name: str, fields: dict,
                  company_id: int | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = requests.post(
            f"{_BASE_URL}/{base_id}/{table_name}",
            headers=_headers(token),
            json={"fields": fields},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        _log_sync(company_id, "airtable_create", data.get("id"))
        return {"ok": True, "record": data}
    except Exception as exc:
        _log_error("create_record", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def update_record(base_id: str, table_name: str, record_id: str,
                  fields: dict, company_id: int | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = requests.patch(
            f"{_BASE_URL}/{base_id}/{table_name}/{record_id}",
            headers=_headers(token),
            json={"fields": fields},
            timeout=15,
        )
        r.raise_for_status()
        _log_sync(company_id, "airtable_update", record_id)
        return {"ok": True, "record": r.json()}
    except Exception as exc:
        _log_error("update_record", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def delete_record(base_id: str, table_name: str, record_id: str,
                  company_id: int | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = requests.delete(
            f"{_BASE_URL}/{base_id}/{table_name}/{record_id}",
            headers=_headers(token),
            timeout=15,
        )
        r.raise_for_status()
        _log_sync(company_id, "airtable_delete", record_id)
        return {"ok": True, "deleted": record_id}
    except Exception as exc:
        _log_error("delete_record", exc)
        return {"ok": False, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _token():
    return (os.environ.get("AIRTABLE_API_KEY")
            or os.environ.get("AIRTABLE_TOKEN"))


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _log_error(endpoint: str, exc: Exception):
    logger.error("Airtable %s: %s", endpoint, exc)
    try:
        from extensions import db
        from models import IntegrationErrorLog
        el = IntegrationErrorLog(
            provider="airtable",
            endpoint=endpoint,
            error_message=str(exc)[:500],
        )
        db.session.add(el)
        db.session.commit()
    except Exception:
        pass


def _log_sync(company_id, event_type, external_id):
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
