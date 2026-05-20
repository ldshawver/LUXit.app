"""
Google Contacts sync service.

OAuth 2.0 flow (no external library beyond `requests`) + People API fetch.
Tokens are stored per user in the GoogleOAuthToken table.
Sync matches normalized phone numbers against TwilioConversation.from_number
and writes the Google display name into contact_name.
"""
import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

SCOPES = "https://www.googleapis.com/auth/contacts.readonly"
PEOPLE_API = "https://people.googleapis.com/v1/people/me/connections"
TOKEN_URL  = "https://oauth2.googleapis.com/token"
AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------

def _normalize(raw: str) -> str:
    """Return E.164 string for US numbers, or best-effort for others."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else raw


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "")

def _client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")

def _redirect_uri() -> str:
    base = os.environ.get("GOOGLE_REDIRECT_URI", "").rstrip("/")
    if not base:
        base = os.environ.get("LUXIT_PUBLIC_BASE_URL", "https://luxit.app").rstrip("/")
    return base + "/twilio/google-contacts/callback"


def get_auth_url(state: str = "") -> str:
    params = {
        "client_id":     _client_id(),
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         SCOPES,
        "access_type":   "offline",
        "prompt":        "consent",
    }
    if state:
        params["state"] = state
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(user_id: int, code: str) -> dict:
    """Exchange authorization code for access + refresh tokens. Stores them."""
    resp = requests.post(TOKEN_URL, data={
        "code":          code,
        "client_id":     _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri":  _redirect_uri(),
        "grant_type":    "authorization_code",
    }, timeout=15)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Google token exchange failed: {data['error_description']}")

    _store_token(user_id, data)
    return data


def _store_token(user_id: int, data: dict):
    from extensions import db
    from models import GoogleOAuthToken
    tok = GoogleOAuthToken.query.filter_by(user_id=user_id).first()
    if not tok:
        tok = GoogleOAuthToken(user_id=user_id)
        db.session.add(tok)
    tok.access_token  = data["access_token"]
    if "refresh_token" in data:
        tok.refresh_token = data["refresh_token"]
    expires_in = int(data.get("expires_in", 3600))
    tok.token_expiry  = datetime.utcnow() + timedelta(seconds=expires_in - 60)
    db.session.commit()


def _refresh_if_needed(tok) -> str:
    """Return a valid access token, refreshing via refresh_token if needed."""
    from extensions import db
    if tok.token_expiry and datetime.utcnow() < tok.token_expiry:
        return tok.access_token

    resp = requests.post(TOKEN_URL, data={
        "client_id":     _client_id(),
        "client_secret": _client_secret(),
        "refresh_token": tok.refresh_token,
        "grant_type":    "refresh_token",
    }, timeout=15)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Google token refresh failed: {data.get('error_description', data['error'])}")

    tok.access_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    tok.token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)
    db.session.commit()
    return tok.access_token


def get_token(user_id: int):
    """Return GoogleOAuthToken row or None."""
    from models import GoogleOAuthToken
    return GoogleOAuthToken.query.filter_by(user_id=user_id).first()


def is_connected(user_id: int) -> bool:
    tok = get_token(user_id)
    return bool(tok and tok.access_token)


def disconnect(user_id: int):
    """Revoke token and delete from DB."""
    from extensions import db
    from models import GoogleOAuthToken
    tok = GoogleOAuthToken.query.filter_by(user_id=user_id).first()
    if tok:
        if tok.access_token:
            try:
                requests.post(REVOKE_URL,
                              params={"token": tok.access_token}, timeout=5)
            except Exception:
                pass
        db.session.delete(tok)
        db.session.commit()


# ---------------------------------------------------------------------------
# Contact fetch
# ---------------------------------------------------------------------------

def _fetch_all_contacts(access_token: str) -> dict:
    """
    Return dict: normalized_phone -> display_name
    Handles pagination automatically.
    """
    phone_map = {}
    page_token = None
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        params = {
            "personFields": "names,phoneNumbers",
            "pageSize":     1000,
        }
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(PEOPLE_API, headers=headers, params=params, timeout=20)
        if resp.status_code == 401:
            raise RuntimeError("Google token invalid or revoked.")
        resp.raise_for_status()
        body = resp.json()

        for person in body.get("connections", []):
            names = person.get("names", [])
            phones = person.get("phoneNumbers", [])
            if not names or not phones:
                continue

            # Prefer displayName, fall back to given+family
            name = (names[0].get("displayName") or
                    f"{names[0].get('givenName','')} {names[0].get('familyName','')}".strip())
            if not name:
                continue

            for ph in phones:
                raw = ph.get("value", "")
                normalized = _normalize(raw)
                if normalized and normalized not in phone_map:
                    phone_map[normalized] = name

        page_token = body.get("nextPageToken")
        if not page_token:
            break

    return phone_map


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_contacts(user_id: int, company_id: int) -> dict:
    """
    Fetch Google contacts and update TwilioConversation.contact_name
    for all conversations in this company.

    Returns {"synced": int, "matched": int, "error": str|None}
    """
    from extensions import db
    from models import TwilioConversation

    tok = get_token(user_id)
    if not tok:
        return {"synced": 0, "matched": 0, "error": "Not connected to Google."}

    try:
        access_token = _refresh_if_needed(tok)
        phone_map    = _fetch_all_contacts(access_token)
    except Exception as exc:
        logger.error("Google Contacts fetch error for user %s: %s", user_id, exc)
        return {"synced": 0, "matched": 0, "error": str(exc)}

    convs   = TwilioConversation.query.filter_by(company_id=company_id).all()
    matched = 0

    for conv in convs:
        norm = _normalize(conv.from_number)
        name = phone_map.get(norm)
        if name and conv.contact_name != name:
            conv.contact_name = name
            matched += 1

    tok.last_sync_at     = datetime.utcnow()
    tok.contacts_synced  = len(phone_map)
    db.session.commit()

    logger.info(
        "Google Contacts sync: user=%s company=%s total_contacts=%d matched=%d",
        user_id, company_id, len(phone_map), matched
    )
    return {"synced": len(phone_map), "matched": matched, "error": None}
