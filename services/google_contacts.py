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
# Phone normalization — canonical implementation used across the whole app
# ---------------------------------------------------------------------------

def normalize_phone(raw: str) -> str:
    """Return E.164 string for US numbers, or best-effort for others.

    Handles all common US formats:
      4155551212          → +14155551212
      14155551212         → +14155551212
      (415) 555-1212      → +14155551212
      1-415-555-1212      → +14155551212
      415.555.1212        → +14155551212
      +14155551212        → +14155551212  (already E.164)
      +447911123456       → +447911123456  (international pass-through)
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else (raw or "")


# Keep old name as alias for backward compatibility
_normalize = normalize_phone


def _all_forms(phone: str) -> list:
    """Return all likely DB representations of a phone number to try."""
    if not phone:
        return []
    norm = normalize_phone(phone)
    forms = [phone, norm]
    digits = re.sub(r"\D", "", norm)
    if digits.startswith("1") and len(digits) == 11:
        forms.append(digits[1:])       # 10-digit without country code
        forms.append(digits)            # 11-digit without +
    forms.append(norm.lstrip("+"))
    return list(dict.fromkeys(forms))   # deduplicate, preserve order


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
    tok.sync_error    = None
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


def is_token_expired(tok) -> bool:
    """True if the token is expired and cannot auto-refresh (no refresh_token)."""
    if not tok:
        return False
    if tok.refresh_token:
        return False
    if tok.token_expiry and datetime.utcnow() >= tok.token_expiry:
        return True
    return False


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

            name = (names[0].get("displayName") or
                    f"{names[0].get('givenName','')} {names[0].get('familyName','')}".strip())
            if not name:
                continue

            for ph in phones:
                raw = ph.get("value", "")
                normalized = normalize_phone(raw)
                if normalized and normalized not in phone_map:
                    phone_map[normalized] = name

        page_token = body.get("nextPageToken")
        if not page_token:
            break

    return phone_map


# ---------------------------------------------------------------------------
# Contact lookup (on-demand, no network call)
# ---------------------------------------------------------------------------

def lookup_contact_name(company_id: int, phone: str) -> tuple:
    """
    Look up a contact name for *phone* within *company_id* using only the
    local DB (no Google API call).  Returns (name_or_None, source_or_None).

    Checks in priority order:
      1. Contact table (CRM) — all normalized forms
      2. TwilioConversation.contact_name (previously synced from Google)
    """
    from models import Contact, TwilioConversation

    forms = _all_forms(phone)
    norm  = normalize_phone(phone)

    logger.debug(
        "lookup_contact_name: company=%s phone=%s norm=%s forms=%s",
        company_id, phone, norm, forms
    )

    # 1. CRM Contact table
    for form in forms:
        contact = Contact.query.filter_by(
            company_id=company_id, phone=form, is_active=True
        ).first()
        if contact:
            name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or None
            if name:
                logger.debug(
                    "lookup_contact_name: CRM match contact_id=%s name=%s form=%s",
                    contact.id, name, form
                )
                return name, "crm"

    # 2. Existing conversation with a synced name
    for form in forms:
        conv = TwilioConversation.query.filter_by(
            company_id=company_id, from_number=form
        ).first()
        if conv and conv.contact_name:
            logger.debug(
                "lookup_contact_name: existing conv match conv_id=%s name=%s",
                conv.id, conv.contact_name
            )
            return conv.contact_name, conv.contact_source or "google"

    logger.debug(
        "lookup_contact_name: no match for phone=%s company=%s — will show phone number",
        phone, company_id
    )
    return None, None


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_contacts(user_id: int, company_id: int) -> dict:
    """
    Fetch Google contacts and:
      1. Update TwilioConversation.contact_name for all conversations in
         this company where the phone number matches.
      2. Upsert matching Contact rows so future inbound messages link by name.

    Returns {"synced": int, "matched": int, "error": str|None}
    """
    from extensions import db
    from models import TwilioConversation, Contact

    tok = get_token(user_id)
    if not tok:
        return {"synced": 0, "matched": 0, "error": "Not connected to Google."}

    try:
        access_token = _refresh_if_needed(tok)
        phone_map    = _fetch_all_contacts(access_token)
    except Exception as exc:
        err_msg = str(exc)
        logger.error("Google Contacts fetch error for user %s: %s", user_id, exc)
        try:
            tok.sync_error = err_msg
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {"synced": 0, "matched": 0, "error": err_msg}

    # Populate the local Contact cache for every Google phone number, not only
    # numbers that already have an SMS conversation. This makes PWA contact
    # search and future inbound-SMS name resolution work immediately after sync.
    for norm, name in phone_map.items():
        _upsert_contact_from_google(db, company_id, norm, name, None)

    convs   = TwilioConversation.query.filter_by(company_id=company_id).all()
    matched = 0

    for conv in convs:
        norm = normalize_phone(conv.from_number)
        name = phone_map.get(norm)

        logger.debug(
            "sync_contacts: conv_id=%s from=%s norm=%s match=%s",
            conv.id, conv.from_number, norm, name
        )

        if name:
            if conv.contact_name != name:
                matched += 1
            conv.contact_name   = name
            conv.contact_source = "google"
            _upsert_contact_from_google(db, company_id, norm, name, conv)

    tok.last_sync_at    = datetime.utcnow()
    tok.contacts_synced = len(phone_map)
    tok.sync_error      = None
    db.session.commit()

    logger.info(
        "Google Contacts sync: user=%s company=%s total_contacts=%d matched=%d",
        user_id, company_id, len(phone_map), matched
    )
    return {"synced": len(phone_map), "matched": matched, "error": None}


def _upsert_contact_from_google(db, company_id: int, norm_phone: str,
                                 name: str, conv) -> None:
    """Create or update a Contact row for a Google-synced number."""
    from models import Contact

    parts      = name.split(" ", 1)
    first_name = parts[0]
    last_name  = parts[1] if len(parts) > 1 else ""

    # Try to find existing contact by any normalized form
    contact = None
    for form in _all_forms(norm_phone):
        contact = Contact.query.filter_by(
            company_id=company_id, phone=form
        ).first()
        if contact:
            break

    if contact:
        if not contact.first_name and not contact.last_name:
            contact.first_name = first_name
            contact.last_name  = last_name
        contact.name       = contact.name or name
        contact.source     = contact.source or "google_contacts"
        contact.is_active  = True
    else:
        contact = Contact(
            company_id = company_id,
            phone      = norm_phone,
            first_name = first_name,
            last_name  = last_name,
            name       = name,
            source     = "google_contacts",
            is_active  = True,
            is_subscribed = False,
        )
        db.session.add(contact)
        db.session.flush()

    # Link conversation to contact if not already linked
    if conv and not conv.contact_id and contact.id:
        conv.contact_id = contact.id
