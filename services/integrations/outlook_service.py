"""
Microsoft Outlook / Graph integration service.

Extends the existing EmailService (email_service.py) with calendar
and mailbox listing. Credentials: MS_CLIENT_ID, MS_CLIENT_SECRET,
MS_TENANT_ID, MS_FROM_EMAIL — all set in Replit secrets.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def health_check() -> dict:
    cid    = os.environ.get("MS_CLIENT_ID", "")
    secret = os.environ.get("MS_CLIENT_SECRET", "")
    tid    = os.environ.get("MS_TENANT_ID", "")
    if not (cid and secret and tid):
        return {"status": "missing_config",
                "detail": "Microsoft credentials not configured"}
    token = _get_token()
    if not token:
        return {"status": "error", "detail": "Failed to acquire MS Graph access token"}
    return {"status": "connected"}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(to: str, subject: str, html_body: str,
               text_body: str | None = None,
               company_id: int | None = None) -> dict:
    """Send email via Microsoft Graph. Never expose the token."""
    from_email = os.environ.get("MS_FROM_EMAIL", "")
    if not from_email:
        return {"ok": False, "reason": "MS_FROM_EMAIL not set"}

    token = _get_token()
    if not token:
        return {"ok": False, "reason": "Could not acquire MS Graph token"}

    body_content = html_body
    body_type    = "HTML"
    if text_body and not html_body:
        body_content = text_body
        body_type    = "Text"

    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": body_type, "content": body_content},
            "toRecipients": [{"emailAddress": {"address": to}}],
            "from": {"emailAddress": {"address": from_email}},
        }
    }

    try:
        r = requests.post(
            f"{_GRAPH}/users/{from_email}/sendMail",
            headers=_auth_headers(token),
            json=message,
            timeout=30,
        )
        if r.status_code == 202:
            _log_event(company_id, "email_sent", {"to": to, "subject": subject[:100]})
            return {"ok": True}
        _log_error(company_id, "send_email", f"HTTP {r.status_code}: {r.text[:200]}")
        return {"ok": False, "reason": f"Graph returned HTTP {r.status_code}"}
    except Exception as exc:
        _log_error(company_id, "send_email", str(exc))
        return {"ok": False, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Mailbox listing
# ---------------------------------------------------------------------------

def list_recent_emails(limit: int = 20) -> dict:
    from_email = os.environ.get("MS_FROM_EMAIL", "")
    if not from_email:
        return {"ok": False, "reason": "MS_FROM_EMAIL not set"}

    token = _get_token()
    if not token:
        return {"ok": False, "reason": "Could not acquire MS Graph token"}

    try:
        r = requests.get(
            f"{_GRAPH}/users/{from_email}/mailFolders/inbox/messages"
            f"?$top={limit}&$select=id,subject,from,receivedDateTime,isRead",
            headers=_auth_headers(token),
            timeout=20,
        )
        r.raise_for_status()
        msgs = [
            {
                "id": m["id"],
                "subject": m.get("subject"),
                "from": m.get("from", {}).get("emailAddress", {}).get("address"),
                "received": m.get("receivedDateTime"),
                "is_read": m.get("isRead"),
            }
            for m in r.json().get("value", [])
        ]
        return {"ok": True, "messages": msgs}
    except Exception as exc:
        _log_error(None, "list_recent_emails", str(exc))
        return {"ok": False, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def list_calendar_events(limit: int = 20) -> dict:
    from_email = os.environ.get("MS_FROM_EMAIL", "")
    if not from_email:
        return {"ok": False, "reason": "MS_FROM_EMAIL not set"}

    token = _get_token()
    if not token:
        return {"ok": False, "reason": "Could not acquire MS Graph token"}

    try:
        r = requests.get(
            f"{_GRAPH}/users/{from_email}/events"
            f"?$top={limit}&$select=id,subject,start,end,location,webLink"
            "&$orderby=start/dateTime",
            headers=_auth_headers(token),
            timeout=20,
        )
        r.raise_for_status()
        events = [
            {
                "id": e["id"],
                "subject": e.get("subject"),
                "start": e.get("start", {}).get("dateTime"),
                "end":   e.get("end",   {}).get("dateTime"),
                "location": e.get("location", {}).get("displayName"),
                "web_link": e.get("webLink"),
            }
            for e in r.json().get("value", [])
        ]
        return {"ok": True, "events": events}
    except Exception as exc:
        _log_error(None, "list_calendar_events", str(exc))
        return {"ok": False, "reason": str(exc)[:200]}


def create_calendar_event(subject: str, start_dt: str, end_dt: str,
                          body_html: str = "", attendees: list | None = None,
                          location: str | None = None,
                          company_id: int | None = None) -> dict:
    """
    start_dt / end_dt: ISO 8601 strings, e.g. "2026-06-01T10:00:00"
    attendees: list of email strings
    """
    from_email = os.environ.get("MS_FROM_EMAIL", "")
    if not from_email:
        return {"ok": False, "reason": "MS_FROM_EMAIL not set"}

    token = _get_token()
    if not token:
        return {"ok": False, "reason": "Could not acquire MS Graph token"}

    payload: dict = {
        "subject": subject,
        "start": {"dateTime": start_dt, "timeZone": "UTC"},
        "end":   {"dateTime": end_dt,   "timeZone": "UTC"},
        "body":  {"contentType": "HTML", "content": body_html},
    }
    if location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"}
            for a in attendees[:50]
        ]

    try:
        r = requests.post(
            f"{_GRAPH}/users/{from_email}/events",
            headers=_auth_headers(token),
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        _log_event(company_id, "calendar_event_created", {"subject": subject[:100], "id": data.get("id")})
        return {"ok": True, "id": data.get("id"), "web_link": data.get("webLink")}
    except Exception as exc:
        _log_error(company_id, "create_calendar_event", str(exc))
        return {"ok": False, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _get_token() -> str | None:
    try:
        import msal
        cid    = os.environ.get("MS_CLIENT_ID", "")
        secret = os.environ.get("MS_CLIENT_SECRET", "")
        tid    = os.environ.get("MS_TENANT_ID", "")
        if not (cid and secret and tid):
            return None
        authority = f"https://login.microsoftonline.com/{tid}"
        app = msal.ConfidentialClientApplication(
            cid, authority=authority, client_credential=secret
        )
        result = app.acquire_token_silent(["https://graph.microsoft.com/.default"], account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if result and "access_token" in result:
            return result["access_token"]
        logger.error("MS Graph token error: %s", result.get("error_description") if result else "empty result")
        return None
    except Exception as exc:
        logger.error("_get_token exception: %s", exc)
        return None


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _log_event(company_id, event_type, payload):
    try:
        import json
        from extensions import db
        from models import IntegrationEvent
        ev = IntegrationEvent(
            company_id=company_id, provider="outlook",
            event_type=event_type, payload_json=json.dumps(payload), status="processed",
        )
        db.session.add(ev)
        db.session.commit()
    except Exception:
        pass


def _log_error(company_id, endpoint, error_msg):
    logger.error("Outlook %s: %s", endpoint, error_msg)
    try:
        from extensions import db
        from models import IntegrationErrorLog
        el = IntegrationErrorLog(
            company_id=company_id, provider="outlook",
            endpoint=endpoint, error_message=error_msg[:500],
        )
        db.session.add(el)
        db.session.commit()
    except Exception:
        pass
