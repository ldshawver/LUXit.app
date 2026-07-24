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
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

SCOPES = "https://www.googleapis.com/auth/contacts.readonly"
PEOPLE_API = "https://people.googleapis.com/v1/people/me/connections"
TOKEN_URL  = "https://oauth2.googleapis.com/token"
AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class GoogleContactsPaginationError(RuntimeError):
    """Raised when Google rejects a People API pageToken during one sync run."""

    def __init__(self, message: str, provider_error: dict | None = None):
        super().__init__(message)
        self.provider_error = provider_error or {"message": message}


def _sanitize_google_error(body) -> dict:
    if not isinstance(body, dict):
        return {"message": str(body)[:1000]}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    sanitized = {k: err.get(k) for k in ("code", "message", "status") if err.get(k) is not None}
    details = err.get("details")
    if isinstance(details, list):
        sanitized["details"] = details[:5]
    return sanitized or {"message": str(body)[:1000]}


def _google_error_status(provider_error: dict | None) -> str:
    if not provider_error:
        return ""
    return str(provider_error.get("status") or provider_error.get("message") or "").upper()


# ---------------------------------------------------------------------------
# Phone normalization — canonical implementation used across the whole app
# ---------------------------------------------------------------------------

def normalize_phone(raw: str | None) -> str:
    """Compatibility wrapper around the application's sole E.164 parser."""
    from services.phone_normalization import normalize_phone_e164
    return normalize_phone_e164(raw)


# Keep old name as alias for backward compatibility
_normalize = normalize_phone


def normalize_email(raw: str | None) -> str:
    """Normalize email addresses for matching without exposing OAuth secrets."""
    return unicodedata.normalize("NFKC", raw or "").strip().lower()


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


def _token_error_requires_reconnect(message: str | None) -> bool:
    """True when Google says the stored OAuth grant can no longer refresh.

    These errors should not be auto-cleared by retrying sync because the user
    must grant consent again to issue a new refresh token.
    """
    msg = (message or "").lower()
    return any(marker in msg for marker in (
        "invalid_grant",
        "token has been expired or revoked",
        "token expired",
        "refresh token",
        "invalid or revoked",
        "invalid_token",
    ))


def _mark_reconnect_required(tok, message: str):
    from extensions import db
    tok.sync_error = message
    db.session.commit()


def token_needs_reconnect(tok) -> bool:
    """Return true when the stored token state requires a fresh OAuth consent."""
    if not tok:
        return False
    if is_token_expired(tok):
        return True
    return _token_error_requires_reconnect(getattr(tok, "sync_error", None))


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
        detail = data.get("error_description") or data.get("error") or "refresh failed"
        message = f"Google token refresh failed: {detail}"
        if _token_error_requires_reconnect(f"{data.get('error')} {detail}"):
            _mark_reconnect_required(tok, message)
        raise RuntimeError(message)

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

def _fetch_all_contacts(access_token: str, sync_token: str | None = None) -> dict:
    """
    Return dict: normalized_phone -> Google contact data.
    pageToken is only a transient cursor for this call; only nextSyncToken is
    returned in metadata for durable persistence after the final page.
    """
    phone_map = {}
    page_token = None
    pages_processed = 0
    contacts_processed = 0
    headers = {"Authorization": f"Bearer {access_token}"}
    base_params = {
        "personFields": "names,phoneNumbers,emailAddresses,organizations,metadata,photos",
        "pageSize": 1000,
        "sortOrder": "LAST_MODIFIED_DESCENDING",
        "sources": "READ_SOURCE_TYPE_CONTACT",
        "requestSyncToken": "true",
    }
    if sync_token:
        base_params["syncToken"] = sync_token

    while True:
        params = dict(base_params)
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(PEOPLE_API, headers=headers, params=params, timeout=20)
        if resp.status_code == 401:
            raise RuntimeError("Google token invalid or revoked. Reconnect Google Contacts to grant consent again.")
        if resp.status_code == 400:
            provider_error = _sanitize_google_error(resp.json() if resp.content else {})
            msg = _google_error_status(provider_error)
            if page_token and "EXPIRED_SYNC_TOKEN" not in msg:
                raise GoogleContactsPaginationError("Google People API rejected pageToken during contacts sync", provider_error)
        resp.raise_for_status()
        body = resp.json()
        pages_processed += 1

        for person in body.get("connections", []):
            names = person.get("names", [])
            phones = person.get("phoneNumbers", [])
            emails = person.get("emailAddresses", [])
            orgs = person.get("organizations", [])
            photos = person.get("photos", [])
            if not phones and not emails:
                continue
            contacts_processed += 1

            primary_name = names[0] if names else {}
            name = (primary_name.get("displayName") or
                    f"{primary_name.get('givenName','')} {primary_name.get('familyName','')}".strip())
            data = {
                "resource_name": person.get("resourceName"),
                "etag": person.get("etag"),
                "name": name or "",
                "first_name": primary_name.get("givenName") or "",
                "last_name": primary_name.get("familyName") or "",
                "email": (emails[0].get("value") if emails else "") or "",
                "company": (orgs[0].get("name") if orgs else "") or "",
                "avatar_url": (photos[0].get("url") if photos else "") or "",
                "phones": [ph.get("value", "") for ph in phones if ph.get("value")],
            }
            if not data["first_name"] and name:
                parts = name.split(" ", 1)
                data["first_name"] = parts[0]
                data["last_name"] = parts[1] if len(parts) > 1 else ""

            added_phone = False
            for ph in phones:
                raw = ph.get("value", "")
                normalized = normalize_phone(raw)
                if normalized:
                    key = normalized
                    if key in phone_map and phone_map[key].get("resource_name") != data.get("resource_name"):
                        key = f"{normalized}|{data.get('resource_name') or contacts_processed}"
                    phone_map[key] = {**data, "phone": raw, "normalized_phone": normalized}
                    added_phone = True
            if not added_phone and data.get("email"):
                key = "email:" + normalize_email(data["email"])
                phone_map[key] = {**data, "phone": "", "normalized_phone": ""}

        page_token = body.get("nextPageToken")
        if not page_token:
            meta = {"pages_processed": pages_processed, "contacts_processed": contacts_processed, "incremental": bool(sync_token)}
            next_sync_token = body.get("nextSyncToken")
            if next_sync_token:
                meta["next_sync_token"] = next_sync_token
            phone_map["__meta__"] = meta
            break

    return phone_map




def _contact_display_name(contact) -> str | None:
    if not contact:
        return None
    from services.contact_resolver import safe_name
    phone = getattr(contact, "normalized_phone", None) or getattr(contact, "phone", None)
    return safe_name(getattr(contact, "display_name", None), phone) or safe_name(getattr(contact, "name", None), phone) or safe_name(f"{contact.first_name or ''} {contact.last_name or ''}", phone)


def _find_contact_by_phone(company_id: int, phone: str):
    from models import Contact
    norm = normalize_phone(phone)
    contact = Contact.query.filter_by(company_id=company_id, normalized_phone=norm, is_active=True).first()
    if contact:
        return contact
    for form in _all_forms(phone):
        contact = Contact.query.filter_by(company_id=company_id, phone=form, is_active=True).first()
        if contact:
            if not getattr(contact, "normalized_phone", None):
                contact.normalized_phone = norm
            return contact
    for contact in Contact.query.filter_by(company_id=company_id, is_active=True).yield_per(200):
        if normalize_phone(getattr(contact, "phone", None)) == norm:
            contact.normalized_phone = norm
            return contact
    return None


def lookup_contact_for_phone(company_id: int, phone: str) -> dict:
    """Return matched contact metadata for a phone number using normalized matching."""
    from services.contact_resolver import resolve_contact_identity
    resolved = resolve_contact_identity(company_id, phone=phone, allow_enrichment=True)
    return {
        "name": None if resolved.safe_display_name == "Name needed" else resolved.safe_display_name,
        "source": resolved.name_source,
        "contact_id": resolved.canonical_contact_id,
        "normalized_phone": normalize_phone(phone),
        "masked_phone": resolved.masked_phone,
        "verification_level": resolved.verification_level,
        "identity_status": resolved.identity_status,
        "conflict_state": resolved.conflict_state,
    }


def _conversation_name_is_replaceable(conv) -> bool:
    current = (getattr(conv, "contact_name", None) or "").strip()
    if not current:
        return True
    if normalize_phone(current) == normalize_phone(getattr(conv, "from_number", None)):
        return True
    if re.sub(r"\D", "", current) and len(re.sub(r"\D", "", current)) >= 7:
        return True
    return (getattr(conv, "contact_source", None) or "").lower() in {"google", "google_contacts", "contacts_cache"}


def _apply_contact_to_conversation(conv, contact_info: dict) -> bool:
    name = (contact_info.get("name") or "").strip()
    if not name:
        return False
    changed = False
    if contact_info.get("contact_id") and conv.contact_id != contact_info["contact_id"]:
        conv.contact_id = contact_info["contact_id"]
        changed = True
    if _conversation_name_is_replaceable(conv) and conv.contact_name != name:
        conv.contact_name = name
        conv.contact_source = contact_info.get("source") or "crm"
        changed = True
    return changed


def backfill_conversation_contact_names(company_id: int | None = None, dry_run: bool = False) -> dict:
    """Backfill existing TwilioConversation contact_id/contact_name from Contact rows."""
    from extensions import db
    from models import TwilioConversation
    q = TwilioConversation.query
    if company_id:
        q = q.filter_by(company_id=company_id)
    scanned = matched = updated = 0
    samples = []
    for conv in q.yield_per(200):
        scanned += 1
        info = lookup_contact_for_phone(conv.company_id, conv.from_number)
        if not info.get("name"):
            continue
        matched += 1
        before = {"id": conv.id, "from_number": conv.from_number, "old": conv.contact_name, "new": info["name"]}
        if _apply_contact_to_conversation(conv, info):
            updated += 1
            if len(samples) < 20:
                samples.append(before)
    if not dry_run:
        db.session.commit()
    else:
        db.session.rollback()
    return {"scanned": scanned, "matched": matched, "updated": updated, "dry_run": dry_run, "samples": samples}

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

    # 1. CRM / Google Contacts cache using normalized phone matching.
    contact_info = lookup_contact_for_phone(company_id, phone)
    if contact_info.get("name"):
        logger.debug(
            "lookup_contact_name: contact match contact_id=%s name=%s norm=%s",
            contact_info.get("contact_id"), contact_info.get("name"), norm
        )
        return contact_info["name"], contact_info.get("source") or "crm"

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

def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _tag_union(*tag_values) -> str:
    seen = []
    for tags in tag_values:
        if isinstance(tags, list):
            parts = tags
        else:
            parts = re.split(r"[,;]", tags or "")
        for part in parts:
            tag = str(part).strip()
            if tag and tag.lower() not in {t.lower() for t in seen}:
                seen.append(tag)
    return ",".join(seen)


def _google_data(norm_phone: str, value) -> dict:
    if isinstance(value, dict):
        data = dict(value)
    else:
        data = {"name": value or ""}
    data.setdefault("phone", norm_phone)
    data.setdefault("normalized_phone", norm_phone)
    data.setdefault("resource_name", None)
    data.setdefault("email", "")
    data.setdefault("company", "")
    data.setdefault("avatar_url", "")
    name = (data.get("name") or "").strip()
    data.setdefault("first_name", "")
    data.setdefault("last_name", "")
    if name and not data.get("first_name") and not data.get("last_name"):
        parts = name.split(" ", 1)
        data["first_name"] = parts[0]
        data["last_name"] = parts[1] if len(parts) > 1 else ""
    return data


def _find_contact_by_google_data(company_id: int, data: dict):
    from extensions import db
    from models import Contact
    resource = data.get("resource_name")
    if resource:
        found = Contact.query.filter_by(company_id=company_id, external_google_contact_id=resource, is_active=True).first()
        if found:
            return found
    phone = data.get("normalized_phone") or normalize_phone(data.get("phone"))
    if phone:
        found = _find_contact_by_phone(company_id, phone)
        if found:
            return found
    email = normalize_email(data.get("email"))
    if email:
        found = Contact.query.filter(Contact.company_id == company_id, Contact.is_active == True, db.func.lower(Contact.email) == email).first()
        if found:
            return found
    return None



def _contact_snapshot(contact) -> dict:
    fields = ["id", "email", "phone", "normalized_phone", "name", "first_name", "last_name",
              "company", "tags", "source", "source_detail", "source_added_at",
              "external_google_contact_id", "avatar_url", "created_at", "updated_at",
              "segment", "sms_consent_status"]
    snap = {}
    for field in fields:
        if hasattr(contact, field):
            value = getattr(contact, field)
            snap[field] = value.isoformat() if hasattr(value, "isoformat") else value
    return snap


def _merge_confidence(contact, data: dict) -> dict:
    resource_match = bool(data.get("resource_name") and getattr(contact, "external_google_contact_id", None) == data.get("resource_name"))
    phone = data.get("normalized_phone") or normalize_phone(data.get("phone"))
    contact_phone = getattr(contact, "normalized_phone", None) or normalize_phone(getattr(contact, "phone", None))
    phone_match = bool(phone and contact_phone and phone == contact_phone)
    email = normalize_email(data.get("email"))
    contact_email = normalize_email(getattr(contact, "email", None))
    email_match = bool(email and contact_email and email == contact_email)
    if resource_match:
        score, reason = 100, "google_resource_id"
    elif phone_match and email_match:
        score, reason = 95, "phone_email"
    elif phone_match and getattr(contact, "normalized_phone", None):
        score, reason = 80, "phone"
    elif phone_match:
        score, reason = 70, "phone_normalization"
    elif email_match:
        score, reason = 75, "email"
    else:
        score, reason = 0, "no_match"
    return {"confidence": score, "reason": reason, "phone_match": phone_match, "email_match": email_match, "resource_match": resource_match}


def _merge_threshold() -> int:
    try:
        return int(os.environ.get("GOOGLE_CONTACTS_AUTO_MERGE_THRESHOLD", "95"))
    except ValueError:
        return 95


def _preview_limit() -> int:
    try:
        return int(os.environ.get("GOOGLE_CONTACTS_PREVIEW_LIMIT", "100"))
    except ValueError:
        return 100


def _append_preview(preview: dict, section: str, item: dict, omitted: dict, limit: int):
    bucket = preview.setdefault(section, [])
    if len(bucket) < limit:
        bucket.append(item)
    else:
        omitted[section] = omitted.get(section, 0) + 1


def _merge_duplicate_contacts(db, survivor, duplicate, *, data: dict, user_id: int | None,
                              sync_job_id: int | None, confidence: dict) -> tuple[bool, dict]:
    if not survivor or not duplicate or survivor.id == duplicate.id or survivor.company_id != duplicate.company_id:
        return False, {}
    before_survivor = _contact_snapshot(survivor)
    before_duplicate = _contact_snapshot(duplicate)
    reference_mappings = []
    # Repoint every mapped table with a contact_id column, including Twilio,
    # SMS/MMS, campaigns, calls, CRM activities, notes, tasks, deals, feedback,
    # attachments, AI/marketing entities, and future CRM tables.
    for mapper in db.Model.registry.mappers:
        model = mapper.class_
        table_name = getattr(model, "__tablename__", None)
        if table_name == "contact" or not hasattr(model, "contact_id"):
            continue
        query = model.query.filter_by(contact_id=duplicate.id)
        if hasattr(model, "company_id"):
            query = query.filter_by(company_id=survivor.company_id)
        count = query.update({"contact_id": survivor.id}, synchronize_session=False)
        if count:
            reference_mappings.append({"table": table_name, "from_contact_id": duplicate.id, "to_contact_id": survivor.id, "rows": count})
    survivor.tags = _tag_union(survivor.tags, duplicate.tags)
    updated_fields, preserved_fields, skipped_fields = [], [], []
    for field in ["email", "phone", "normalized_phone", "name", "first_name", "last_name", "company", "source", "source_detail", "source_added_at", "external_google_contact_id", "avatar_url"]:
        source_value = getattr(duplicate, field, None) if hasattr(duplicate, field) else None
        dest_value = getattr(survivor, field, None) if hasattr(survivor, field) else None
        if hasattr(survivor, field) and _blank(dest_value) and not _blank(source_value):
            setattr(survivor, field, source_value)
            updated_fields.append(field)
        elif not _blank(dest_value):
            preserved_fields.append(field)
        else:
            skipped_fields.append(field)
    duplicate.is_active = False
    duplicate.tags = _tag_union(duplicate.tags, "Merged Duplicate")
    duplicate.source_detail = (duplicate.source_detail or "")[:200]
    after_survivor = _contact_snapshot(survivor)
    from models import ContactMergeAudit
    audit = ContactMergeAudit(
        company_id=survivor.company_id,
        source_contact_id=duplicate.id,
        destination_contact_id=survivor.id,
        merge_reason=confidence.get("reason"),
        google_resource_id=data.get("resource_name"),
        phone_match=bool(confidence.get("phone_match")),
        email_match=bool(confidence.get("email_match")),
        match_confidence=int(confidence.get("confidence") or 0),
        user_id=user_id,
        sync_job_id=sync_job_id,
        updated_fields=updated_fields,
        preserved_fields=preserved_fields,
        skipped_fields=skipped_fields,
        fields_before={"survivor": before_survivor, "duplicate": before_duplicate},
        fields_after={"survivor": after_survivor, "duplicate": _contact_snapshot(duplicate)},
        reference_mappings=reference_mappings,
    )
    db.session.add(audit)
    return True, {"audit_id": None, "reference_mappings": reference_mappings, "updated_fields": updated_fields,
                  "preserved_fields": preserved_fields, "skipped_fields": skipped_fields,
                  "fields_before": audit.fields_before, "fields_after": audit.fields_after}


def _apply_google_to_contact(contact, data: dict, *, dry_run: bool = False) -> list[str]:
    changes = []
    mapping = {
        "first_name": data.get("first_name"), "last_name": data.get("last_name"),
        "name": data.get("name"), "email": normalize_email(data.get("email")) if data.get("email") else "", "phone": data.get("normalized_phone") or data.get("phone"),
        "normalized_phone": data.get("normalized_phone") or normalize_phone(data.get("phone")),
        "company": data.get("company"), "avatar_url": data.get("avatar_url"),
        "external_google_contact_id": data.get("resource_name"),
    }
    for field, value in mapping.items():
        if hasattr(contact, field) and not _blank(value) and _blank(getattr(contact, field, None)):
            if not dry_run: setattr(contact, field, value)
            changes.append(field)
    new_tags = _tag_union(getattr(contact, "tags", None), "Google Contact")
    if new_tags != (contact.tags or ""):
        if not dry_run: contact.tags = new_tags
        changes.append("tags")
    now = datetime.utcnow()
    if not dry_run:
        contact.source = "google_contacts"
        contact.source_detail = "Google Contacts sync"
        if not contact.source_added_at:
            contact.source_added_at = now
        if hasattr(contact, "updated_at"):
            contact.updated_at = now
        contact.is_active = True
    return changes


def preview_sync_contacts(user_id: int, company_id: int) -> dict:
    return sync_contacts(user_id, company_id, dry_run=True)


def _preview_contact(contact) -> dict | None:
    if not contact:
        return None
    return {"id": contact.id, "name": _contact_display_name(contact), "email": getattr(contact, "email", None), "phone": getattr(contact, "phone", None), "tags": getattr(contact, "tags", None)}


def _finish_sync_job(db, job, status: str, payload: dict, error: str | None = None):
    job.status = status
    job.completed_at = datetime.utcnow()
    job.duration_ms = int((job.completed_at - job.started_at).total_seconds() * 1000)
    job.contacts_retrieved = payload.get("synced", 0)
    job.current_page_count = payload.get("pages_processed", getattr(job, "current_page_count", 0) or 0)
    job.contacts_processed = payload.get("contacts_processed", payload.get("synced", 0))
    if payload.get("failure_stage"):
        job.failure_stage = payload.get("failure_stage")
    if payload.get("sanitized_provider_error"):
        job.sanitized_provider_error = payload.get("sanitized_provider_error")
    job.contacts_created = payload.get("created", 0)
    job.contacts_updated = payload.get("updated", 0)
    job.contacts_merged = payload.get("merged", 0)
    job.contacts_skipped = payload.get("skipped", 0)
    job.preview_payload = payload.get("preview", {})
    if error:
        job.errors = (job.errors or []) + [{"message": error, "at": job.completed_at.isoformat()}]
    db.session.flush()


def _cache_google_lookup(company_id: int, user_id: int, phone_map: dict, *, dry_run: bool = False) -> dict:
    """Persist a local, token-free Google phone/name cache for async matching."""
    from extensions import db
    from models import GoogleContactLookup, GoogleContactConnection
    if dry_run or not isinstance(phone_map, dict):
        return {"cached": 0, "ambiguous": 0}
    connection = GoogleContactConnection.query.filter_by(company_id=company_id, user_id=user_id).first()
    grouped = {}
    for norm, raw in phone_map.items():
        if str(norm).startswith("__") or str(norm).startswith("email:"):
            continue
        data = _google_data(norm, raw)
        normalized = data.get("normalized_phone") or normalize_phone(data.get("phone"))
        if not normalized:
            continue
        grouped.setdefault(normalized, []).append(data)
    cached = ambiguous = 0
    for normalized, candidates in grouped.items():
        lookup = GoogleContactLookup.query.filter_by(company_id=company_id, user_id=user_id, normalized_phone=normalized).first()
        if not lookup:
            lookup = GoogleContactLookup(company_id=company_id, user_id=user_id, normalized_phone=normalized)
            db.session.add(lookup)
        lookup.connection_id = connection.id if connection else None
        lookup.candidate_count = len(candidates)
        lookup.is_ambiguous = len({c.get("resource_name") for c in candidates}) > 1
        lookup.display_name = None if lookup.is_ambiguous else (candidates[0].get("name") or "")
        lookup.resource_id = None if lookup.is_ambiguous else candidates[0].get("resource_name")
        lookup.etag = None if lookup.is_ambiguous else candidates[0].get("etag")
        lookup.candidates = [{"normalized_phone": normalized, "name": c.get("name"), "resource_id": c.get("resource_name"), "etag": c.get("etag")} for c in candidates]
        lookup.last_seen_at = datetime.utcnow()
        cached += 1
        if lookup.is_ambiguous: ambiguous += 1
    return {"cached": cached, "ambiguous": ambiguous}


def upsert_connection_from_token(user_id: int, company_id: int):
    """Mirror legacy GoogleOAuthToken metadata into the CRM connection table without exposing tokens."""
    from extensions import db
    from models import GoogleContactConnection
    tok = get_token(user_id)
    conn = GoogleContactConnection.query.filter_by(company_id=company_id, user_id=user_id).first()
    if not conn:
        conn = GoogleContactConnection(company_id=company_id, user_id=user_id)
        db.session.add(conn)
    conn.google_account_email = getattr(tok, "google_account_email", None) if tok else conn.google_account_email
    conn.scopes = [SCOPES]
    conn.sync_status = "connected" if tok else "disconnected"
    conn.last_successful_sync_at = getattr(tok, "last_successful_sync_at", None) if tok else conn.last_successful_sync_at
    return conn


def sync_contacts(user_id: int, company_id: int, dry_run: bool = False) -> dict:
    """Fetch Google contacts and enrich/merge Audience contacts. Supports dry-run previews."""
    from extensions import db
    from models import TwilioConversation, Contact, GoogleContactsSyncJob

    job = GoogleContactsSyncJob(company_id=company_id, user_id=user_id, dry_run=dry_run, status="running")
    db.session.add(job)
    db.session.commit()
    logger.info("Google Contacts sync job %s started user=%s company=%s dry_run=%s", job.id, user_id, company_id, dry_run)

    tok = get_token(user_id)
    if not tok:
        payload = {"sync_job_id": job.id, "synced": 0, "matched": 0, "updated": 0, "merged": 0, "created": 0, "skipped": 0, "error": "Not connected to Google.", "dry_run": dry_run}
        _finish_sync_job(db, job, "failed", payload, payload["error"])
        db.session.commit()
        return payload
    job.google_account_email = getattr(tok, "google_account_email", None)
    if hasattr(tok, "google_page_token") and tok.google_page_token:
        # Clear any legacy saved pagination cursor. pageToken must not survive a sync run.
        tok.google_page_token = None
    try:
        access_token = _refresh_if_needed(tok)
        try:
            try:
                phone_map = _fetch_all_contacts(access_token, sync_token=getattr(tok, "google_sync_token", None))
            except TypeError:
                # Backward-compatible for tests/extensions monkeypatching the old one-arg fetcher.
                phone_map = _fetch_all_contacts(access_token)
        except GoogleContactsPaginationError as exc:
            # A pageToken is valid only inside one active paging chain. If Google
            # rejects it, do not disconnect OAuth or delete contacts; restart once
            # from the first page with the same durable syncToken.
            job.failure_stage = "page_token"
            job.sanitized_provider_error = exc.provider_error
            job.api_failures = (job.api_failures or []) + [{"stage": "page_token", "provider_error": exc.provider_error, "fallback": "restart_without_page_token"}]
            if hasattr(tok, "google_page_token"):
                tok.google_page_token = None
            try:
                phone_map = _fetch_all_contacts(access_token, sync_token=getattr(tok, "google_sync_token", None))
            except TypeError:
                phone_map = _fetch_all_contacts(access_token)
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            provider_error = {}
            try:
                provider_error = _sanitize_google_error(exc.response.json())
            except Exception:
                provider_error = {"message": str(exc)}
            if status in {400, 410} and "EXPIRED_SYNC_TOKEN" in _google_error_status(provider_error) and getattr(tok, "google_sync_token", None):
                job.failure_stage = "sync_token"
                job.sanitized_provider_error = provider_error
                job.api_failures = (job.api_failures or []) + [{"stage": "sync_token", "provider_error": provider_error, "fallback": "full_sync"}]
                tok.google_sync_token = None
                try:
                    phone_map = _fetch_all_contacts(access_token, sync_token=None)
                except TypeError:
                    phone_map = _fetch_all_contacts(access_token)
            elif status in {400, 410} and getattr(tok, "google_sync_token", None):
                job.api_failures = (job.api_failures or []) + [{"message": str(exc), "provider_error": provider_error, "fallback": "full_sync"}]
                tok.google_sync_token = None
                try:
                    phone_map = _fetch_all_contacts(access_token, sync_token=None)
                except TypeError:
                    phone_map = _fetch_all_contacts(access_token)
            else:
                raise
    except Exception as exc:
        err_msg = str(exc); reconnect_required = _token_error_requires_reconnect(err_msg)
        logger.error("Google Contacts sync job %s failed for user %s: %s", job.id, user_id, exc)
        if reconnect_required:
            job.oauth_failures = (job.oauth_failures or []) + [{"message": err_msg}]
        else:
            job.api_failures = (job.api_failures or []) + [{"message": err_msg}]
        tok.sync_error = err_msg
        payload = {"sync_job_id": job.id, "synced": 0, "matched": 0, "updated": 0, "merged": 0, "created": 0, "skipped": 0, "error": err_msg, "reconnect_required": reconnect_required, "dry_run": dry_run}
        _finish_sync_job(db, job, "failed", payload, err_msg)
        db.session.commit()
        return payload

    meta = phone_map.pop("__meta__", {}) if isinstance(phone_map, dict) else {}
    cache_stats = _cache_google_lookup(company_id, user_id, phone_map, dry_run=dry_run)
    job.current_page_count = int(meta.get("pages_processed") or 0)
    job.contacts_processed = int(meta.get("contacts_processed") or len(phone_map))
    updated_ids, created, merged, matched, skipped = set(), 0, 0, 0, 0
    preview = {"will_create": [], "will_update": [], "will_merge": [], "will_skip": [], "possible_merge_requires_review": []}
    preview_omitted = {}
    preview_limit = _preview_limit()
    samples = []
    threshold = _merge_threshold()
    resources_by_phone = {}
    for key, raw in phone_map.items():
        item = _google_data(key, raw)
        phone = item.get("normalized_phone")
        if phone:
            resources_by_phone.setdefault(phone, set()).add(item.get("resource_name") or key)
    ambiguous_phones = {phone for phone, resources in resources_by_phone.items() if len(resources) > 1}

    try:
        for index, (norm, raw_data) in enumerate(phone_map.items(), start=1):
            data = _google_data(norm, raw_data)
            if data.get("normalized_phone") in ambiguous_phones:
                contact = _find_contact_by_phone(company_id, data["normalized_phone"])
                if contact and not dry_run:
                    contact.google_match_status = "ambiguous"
                    contact.identity_status = "ambiguous"
                skipped += 1
                _append_preview(preview, "possible_merge_requires_review", {
                    "requires_review": True, "reason": "duplicate_google_phone",
                }, preview_omitted, preview_limit)
                continue
            contact = _find_contact_by_google_data(company_id, data)
            if contact:
                matched += 1
                changes = _apply_google_to_contact(contact, data, dry_run=dry_run)
                if changes:
                    updated_ids.add(contact.id)
                    _append_preview(preview, "will_update", {"existing_contact": _preview_contact(contact), "incoming_google_contact": data, "fields_to_update": changes}, preview_omitted, preview_limit)
                candidates = []
                phone_key = data.get("normalized_phone") or (norm if not str(norm).startswith("email:") else "")
                if phone_key:
                    for form in _all_forms(phone_key):
                        candidates += Contact.query.filter_by(company_id=company_id, phone=form, is_active=True).all()
                if data.get("email"):
                    email = normalize_email(data["email"])
                    candidates += Contact.query.filter(Contact.company_id == company_id, Contact.is_active == True, db.func.lower(Contact.email) == email).all()
                for dup in {c.id: c for c in candidates}.values():
                    if dup.id == contact.id:
                        continue
                    confidence = _merge_confidence(dup, data)
                    merge_item = {"source_contact": _preview_contact(dup), "destination_contact": _preview_contact(contact),
                                  "match_reason": confidence["reason"], "confidence": confidence["confidence"],
                                  "fields_changing": changes, "incoming_google_contact": data}
                    if confidence["confidence"] >= threshold:
                        _append_preview(preview, "will_merge", merge_item, preview_omitted, preview_limit)
                        if not dry_run:
                            did_merge, audit = _merge_duplicate_contacts(db, contact, dup, data=data, user_id=user_id, sync_job_id=job.id, confidence=confidence)
                            if did_merge:
                                merged += 1
                    else:
                        skipped += 1
                        _append_preview(preview, "possible_merge_requires_review", {**merge_item, "requires_review": True}, preview_omitted, preview_limit)
            else:
                created += 1
                _append_preview(preview, "will_create", {"contact_name": data.get("name"), "incoming_google_contact": data}, preview_omitted, preview_limit)
                if not dry_run:
                    from services.contact_intelligence import resolve_contact
                    contact = resolve_contact(company_id, phone=data.get("normalized_phone") or data.get("phone"), email=data.get("email"), proposed_name=data.get("name"), first_name=data.get("first_name"), last_name=data.get("last_name"), business_name=data.get("company"), source="google_contacts", detail="Google Contacts sync", user_id=user_id)
                    contact.is_subscribed = False
                    _apply_google_to_contact(contact, data, dry_run=False)
            if contact and len(samples) < 10:
                samples.append({"contact_id": contact.id, "name": data.get("name"), "phone": data.get("normalized_phone") or norm})
            if not dry_run and index % int(os.environ.get("GOOGLE_CONTACTS_BATCH_SIZE", "500")) == 0:
                db.session.flush()

        if not dry_run:
            for conv in TwilioConversation.query.filter_by(company_id=company_id).yield_per(500):
                norm = normalize_phone(conv.from_number)
                raw_data = phone_map.get(norm)
                if raw_data:
                    data = _google_data(norm, raw_data)
                    contact = _find_contact_by_google_data(company_id, data)
                    info = {"name": data.get("name"), "source": "google_contacts", "contact_id": contact.id if contact else None, "normalized_phone": norm}
                    if _apply_contact_to_conversation(conv, info):
                        matched += 1

        if preview_omitted:
            preview["omitted_counts"] = preview_omitted
        payload = {"sync_job_id": job.id, "synced": len(phone_map), "matched": matched, "updated": len(updated_ids),
                   "merged": merged if not dry_run else len(preview["will_merge"]), "created": created, "skipped": skipped,
                   "error": None, "dry_run": dry_run, "samples": samples, "preview": preview,
                   "pages_processed": int(meta.get("pages_processed") or 0),
                   "contacts_processed": int(meta.get("contacts_processed") or len(phone_map)),
                   "new_next_sync_token_persisted": bool((not dry_run) and meta.get("next_sync_token")),
                   "status": "completed", "incremental": bool(meta.get("incremental")),
                   "review_required": bool(preview["possible_merge_requires_review"] or preview_omitted.get("possible_merge_requires_review")),
                   "lookup_cache": cache_stats}
        if dry_run:
            job_id = job.id
            db.session.rollback()
            job = db.session.get(GoogleContactsSyncJob, job_id)
            _finish_sync_job(db, job, "completed", payload)
            db.session.commit()
        else:
            from services.contact_resolver import resolve_contact_identity
            resolution_updated = resolution_conflicts = still_needing_names = 0
            for pending_contact in Contact.query.filter_by(company_id=company_id, is_active=True).yield_per(200):
                before = _contact_display_name(pending_contact)
                resolved = resolve_contact_identity(company_id, contact_id=pending_contact.id, allow_enrichment=True)
                if resolved.conflict_state != "none": resolution_conflicts += 1
                elif resolved.safe_display_name == "Name needed": still_needing_names += 1
                elif not before: resolution_updated += 1
            payload.update(resolution_updated=resolution_updated, conflicted=resolution_conflicts,
                           still_needing_names=still_needing_names)
            tok.last_sync_at = datetime.utcnow(); tok.last_successful_sync_at = tok.last_sync_at
            tok.contacts_synced = len(phone_map); tok.contacts_created = created; tok.contacts_updated = len(updated_ids)
            tok.contacts_merged = merged; tok.contacts_skipped = skipped
            tok.last_sync_status = "completed"; tok.sync_error = None
            if meta.get("next_sync_token"):
                tok.google_sync_token = meta["next_sync_token"]
            _finish_sync_job(db, job, "completed", payload)
            tok.last_sync_duration_ms = job.duration_ms
            db.session.commit()
        logger.info("Google Contacts sync job %s completed: %s", job.id, payload)
        return payload
    except Exception as exc:
        db.session.rollback()
        # Restore the job failure record after rolling back any partial contact work.
        job = db.session.get(GoogleContactsSyncJob, job.id) or GoogleContactsSyncJob(id=job.id, company_id=company_id, user_id=user_id, dry_run=dry_run)
        db.session.add(job)
        err_msg = str(exc)
        payload = {"sync_job_id": job.id, "synced": len(phone_map), "matched": matched, "updated": len(updated_ids), "merged": merged,
                   "created": created, "skipped": skipped, "error": err_msg, "dry_run": dry_run, "status": "failed", "preview": preview}
        _finish_sync_job(db, job, "failed", payload, err_msg)
        db.session.commit()
        logger.exception("Google Contacts sync job %s failed during processing", job.id)
        return payload
