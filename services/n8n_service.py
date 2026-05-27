"""
n8n Webhook Service — shared trigger utility.
Fires lifecycle events to the company's configured n8n webhook URL.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)


def fire_n8n(event_type: str, company_id: int, payload: dict):
    """POST an event to the n8n webhook URL stored in company secrets.

    Falls back to the N8N_WEBHOOK_URL env var if no per-company URL is set.
    Safe to call anywhere — logs and swallows errors so caller is never blocked.
    """
    from models import Company, SaasAutomationLog
    from extensions import db

    n8n_url = None
    try:
        company = Company.query.get(company_id)
        if company:
            n8n_url = company.get_secret("n8n_webhook_url")
    except Exception as exc:
        logger.warning("n8n_service: company lookup failed: %s", exc)

    n8n_url = n8n_url or os.environ.get("N8N_WEBHOOK_URL")

    if not n8n_url:
        logger.debug("n8n webhook URL not configured — skipping trigger for %s", event_type)
        return

    body = {"event": event_type, "company_id": company_id, **payload}

    status = "success"
    error = None
    try:
        resp = requests.post(n8n_url, json=body, timeout=10)
        if not resp.ok:
            status = "failed"
            error = resp.text[:500]
        logger.info("n8n trigger %s → HTTP %s", event_type, resp.status_code)
    except Exception as exc:
        status = "failed"
        error = str(exc)
        logger.warning("n8n trigger %s failed: %s", event_type, exc)

    try:
        entry = SaasAutomationLog(
            company_id=company_id,
            event_type=event_type,
            source="n8n",
            payload=payload,
            status=status,
            error=error,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        logger.warning("SaasAutomationLog write failed: %s", exc)
