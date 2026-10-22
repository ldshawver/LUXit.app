"""Tenant-scoped SMS identity collection and confirmation state machine."""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import text
from extensions import db
from models import Contact, ContactEmailAddress, ContactSourceEvent
from services.contact_resolver import (
    ConfirmationResolverOutcome,
    classify_confirmation_evidence,
    discover_confirmation_candidate_ids,
    load_confirmation_evidence,
    resolve_contact_identity,
    safe_name,
    validate_person_name,
)
from services.phone_normalization import normalize_phone_e164

IDENTITY_PROMPT = (
    "Welcome to MyOrder.fun! To ensure your new customer onboarding is "
    "successful, please reply with your first and last name, and a good "
    "email. We will save your contact info and once confirmed we will be "
    "happy to help you place your order."
)
_YES = {"yes", "y", "confirm", "confirmed"}
_NO = {"no", "n", "incorrect", "change"}
APPROVED_CONTACT_TAG = "Approved Contact"
CONFIRMED_REPLY = "Thank you. Your identity has been confirmed."
logger = logging.getLogger(__name__)
_TRUSTED_NAME_SOURCES = {"customer_confirmed", "pwa_verified", "manual", "google_contacts"}


def _valid_email(value: str) -> str | None:
    try:
        return validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def validate_name_candidate(value: str | None) -> tuple[str, str] | None:
    """Validate the deliberately narrow person-name grammar used by SMS collection."""
    return validate_person_name(value)


def extract_identity(body: str) -> tuple[str | None, str | None, str | None, list[str]]:
    """Compatibility parser; stateful callers must gate name and email separately."""
    email_match = re.search(r"[^\s,;<>]+@[^\s,;<>]+", body or "")
    email = _valid_email(email_match.group(0).rstrip(".!?")) if email_match else None
    remainder = (body or "")
    if email_match:
        remainder = remainder[:email_match.start()] + " " + remainder[email_match.end():]
    name = validate_name_candidate(remainder.strip(" ,;"))
    first, last = name if name else (None, None)
    missing = []
    if not first:
        missing.append("first and last name")
    if not email:
        missing.append("valid email address")
    return first, last, email, missing


def apply_cached_google_match(contact: Contact) -> str:
    result = resolve_contact_identity(contact.company_id, contact_id=contact.id, allow_enrichment=True)
    return "ambiguous" if result.conflict_state != "none" else ("matched" if result.name_source in {"google_contacts", "ios_contacts"} else "unresolved")


def _add_tag(contact: Contact, tag: str) -> None:
    from services.contact_audience import add_contact_tag
    add_contact_tag(contact, tag)


def _identity_missing(contact: Contact) -> list[str]:
    missing = []
    if not validate_name_candidate(
        f"{contact.pending_first_name or contact.first_name or ''} "
        f"{contact.pending_last_name or contact.last_name or ''}"
    ):
        missing.append("first and last name")
    if not _valid_email(contact.pending_email or contact.normalized_email or contact.primary_email or contact.email or ""):
        missing.append("valid email address")
    return missing


_UNSAFE_IDENTITY_STATES = {"declined", "ambiguous"}


def _trusted_stored_name(contact: Contact) -> tuple[str, str] | None:
    """Return a stored person name only when its provenance is trustworthy."""
    trusted = (
        contact.identity_status not in _UNSAFE_IDENTITY_STATES
        and contact.name_verification_level in {"verified", "trusted"}
        and contact.name_source in _TRUSTED_NAME_SOURCES
        and (contact.name_provenance or {}).get("source") in {
            "customer_confirmed", "customer_confirmed_sms", "pwa_verified",
            "manual", "google_contacts",
        }
    )
    if not trusted:
        return None
    return validate_name_candidate(
        f"{contact.first_name or ''} {contact.last_name or ''}"
    )


def begin_identity_collection(contact: Contact, message_sid: str) -> str:
    """Create an explicit server-side collection state without parsing the SMS."""
    now = datetime.utcnow()
    contact.identity_requested_at = contact.identity_fields_requested_at = now
    contact.identity_request_count = (contact.identity_request_count or 0) + 1
    contact.identity_last_request_sid = message_sid
    contact.identity_request_state = {
        "phase": "collection",
        "company_id": contact.company_id,
        "contact_id": contact.id,
        "request_sid": message_sid,
        "requested_at": now.isoformat(),
    }
    stored_name = _trusted_stored_name(contact)
    if stored_name:
        contact.pending_first_name, contact.pending_last_name = stored_name
        contact.identity_status = "awaiting_email"
        contact.identity_requested_fields = ["email"]
        return "Thanks for contacting us. Please reply with your email address."
    contact.pending_first_name = contact.pending_last_name = contact.pending_email = None
    contact.identity_status = "awaiting_name"
    contact.identity_requested_fields = ["first_name", "last_name", "email"]
    return IDENTITY_PROMPT


def _reset_unsafe_confirmation(contact: Contact) -> None:
    contact.pending_first_name = contact.pending_last_name = contact.pending_email = None
    contact.identity_status = "awaiting_name"
    contact.identity_requested_fields = ["first_name", "last_name", "email"]
    contact.identity_request_state = {}
    contact.identity_last_request_sid = None


def _confirmation_metadata_valid(contact: Contact, now: datetime) -> bool:
    state = contact.identity_request_state if isinstance(contact.identity_request_state, dict) else {}
    requested_at = contact.identity_fields_requested_at
    sid = contact.identity_last_request_sid
    if not requested_at or not sid:
        return False
    if state.get("phase") != "awaiting_confirmation":
        return False
    if state.get("company_id") != contact.company_id or state.get("contact_id") != contact.id:
        return False
    if state.get("confirmation_nonce") != sid:
        return False
    try:
        state_requested_at = datetime.fromisoformat(state["requested_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if abs((state_requested_at - requested_at).total_seconds()) > 2:
        return False
    ttl = timedelta(hours=int(os.getenv("IDENTITY_CONFIRMATION_TTL_HOURS", "24")))
    return requested_at >= now - ttl and requested_at <= now + timedelta(minutes=5)


def _advisory_identity_locks(company_id: int, values: tuple[str, ...]) -> None:
    """Serialize confirmations sharing identity keys before discovering rows."""
    if db.session.get_bind().dialect.name != "postgresql":
        return
    keys = []
    for value in values:
        digest = hashlib.sha256(f"{company_id}:{value}".encode()).digest()[:8]
        keys.append(int.from_bytes(digest, byteorder="big", signed=True))
    for key in sorted(set(keys)):
        db.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _identity_log(
    *,
    contact: Contact,
    message_sid: str,
    outcome: str,
    reason_code: str,
    candidate_ids=(),
    pending_state_id: str | int | None = None,
) -> None:
    phone = (contact.normalized_phone or contact.phone or "").encode()
    email = (contact.pending_email or contact.normalized_email or contact.email or "").strip().casefold().encode()
    logger.info(
        "identity_confirmation_decision company_id=%s current_contact_id=%s "
        "phone_hash=%s email_hash=%s pending_state_id=%s resolver_outcome=%s "
        "candidate_count=%s candidate_contact_ids=%s failure_reason_code=%s message_sid=%s",
        contact.company_id,
        contact.id,
        hashlib.sha256(phone).hexdigest()[:12] if phone else None,
        hashlib.sha256(email).hexdigest()[:12] if email else None,
        pending_state_id,
        outcome,
        len(candidate_ids),
        list(candidate_ids),
        reason_code,
        message_sid,
    )


def confirm_pending_identity(contact: Contact, conversation, message_sid: str) -> dict:
    """Confirm from fresh, deterministically locked server-side identity state."""
    current_contact_id = contact.id
    company_id = contact.company_id
    snapshot = (
        Contact.query.filter(
            Contact.id == current_contact_id,
            Contact.company_id == company_id,
            Contact.is_active.is_(True),
        )
        .populate_existing()
        .one_or_none()
    )
    if not snapshot or snapshot.identity_status not in {"awaiting_confirmation", "confirmed"}:
        if snapshot:
            _identity_log(
                contact=snapshot,
                message_sid=message_sid,
                outcome="MISSING_PENDING_STATE",
                reason_code="identity_confirm_missing_pending_state",
                pending_state_id=snapshot.identity_last_request_sid or snapshot.id,
            )
        return {
            "reply": "I could not find an active identity confirmation. Please reply with your first and last name.",
            "review": True,
            "reason": "identity_confirm_missing_pending_state",
        }
    if snapshot.identity_status == "confirmed":
        return {"reply": None, "confirmed": True, "idempotent": True}

    first = (snapshot.pending_first_name or "").strip()
    last = (snapshot.pending_last_name or "").strip()
    email = _valid_email(snapshot.pending_email or "")
    phone = normalize_phone_e164(snapshot.normalized_phone or snapshot.phone)
    lock_values = tuple(value for value in (phone, email) if value)
    _advisory_identity_locks(company_id, lock_values)

    discovered_ids = discover_confirmation_candidate_ids(
        company_id,
        normalized_phone=phone,
        normalized_email=email,
    )
    locked_ids = tuple(sorted(set(discovered_ids) | {current_contact_id}))
    evidence = load_confirmation_evidence(company_id, locked_ids, lock=True)
    contact = next((row for row in evidence.contacts if row.id == current_contact_id), None)
    if not contact or contact.identity_status not in {"awaiting_confirmation", "confirmed"}:
        if contact:
            _identity_log(
                contact=contact,
                message_sid=message_sid,
                outcome="MISSING_PENDING_STATE",
                reason_code="identity_confirm_missing_pending_state",
                pending_state_id=contact.identity_last_request_sid or contact.id,
            )
        return {
            "reply": "I could not find an active identity confirmation. Please reply with your first and last name.",
            "review": True,
            "reason": "identity_confirm_missing_pending_state",
        }
    if contact.identity_status == "confirmed":
        return {"reply": None, "confirmed": True, "idempotent": True}

    now = datetime.utcnow()
    if not _confirmation_metadata_valid(contact, now):
        pending_state_id = contact.identity_last_request_sid or contact.id
        _reset_unsafe_confirmation(contact)
        _identity_log(
            contact=contact,
            message_sid=message_sid,
            outcome="MISSING_PENDING_STATE",
            reason_code="identity_confirm_missing_pending_state",
            pending_state_id=pending_state_id,
        )
        return {
            "reply": "That identity confirmation is no longer valid. Please reply with your first and last name.",
            "review": True,
            "reason": "identity_confirm_missing_pending_state",
        }

    # Confirmation trusts only the values captured in the explicit server-side
    # pending state. Neither the YES body nor browser/client values participate.
    first = (contact.pending_first_name or "").strip()
    last = (contact.pending_last_name or "").strip()
    email = _valid_email(contact.pending_email or "")
    validated_name = validate_name_candidate(f"{first} {last}")
    if validated_name:
        first, last = validated_name
    display = safe_name(f"{first} {last}", contact.normalized_phone or contact.phone) if validated_name else None
    if not validated_name or not email or not display:
        contact.identity_conflict_status = "invalid"
        contact.identity_requested_fields = _identity_missing(contact)
        return {"reply": "I could not verify the saved information. Please reply with your first and last name and a valid email address.", "review": True}

    # Detect a candidate inserted/changed during discovery.  The advisory keys
    # serialize this flow; a non-cooperating writer causes a safe retry.
    rediscovered_ids = discover_confirmation_candidate_ids(
        company_id,
        normalized_phone=normalize_phone_e164(contact.normalized_phone or contact.phone),
        normalized_email=email,
    )
    if set(rediscovered_ids) != set(discovered_ids):
        raise RuntimeError("identity_candidates_changed_during_lock")
    resolution = classify_confirmation_evidence(
        company_id,
        current_contact_id=contact.id,
        normalized_phone=normalize_phone_e164(contact.normalized_phone or contact.phone),
        normalized_email=email,
        first_name=first,
        last_name=last,
        evidence=evidence,
    )
    reason_by_outcome = {
        ConfirmationResolverOutcome.NO_MATCH: "identity_confirm_no_match",
        ConfirmationResolverOutcome.SELF_MATCH: "identity_confirm_self_match",
        ConfirmationResolverOutcome.SINGLE_COMPATIBLE_MATCH: "identity_confirm_single_compatible_match",
        ConfirmationResolverOutcome.INSUFFICIENT_EVIDENCE: "identity_confirm_insufficient_evidence",
        ConfirmationResolverOutcome.TRUE_AMBIGUITY: "identity_confirm_true_ambiguity",
        ConfirmationResolverOutcome.IDENTITY_CONFLICT: "identity_confirm_verified_conflict",
        ConfirmationResolverOutcome.CROSS_TENANT_ONLY: "identity_confirm_cross_tenant_only",
    }
    reason_code = resolution.failure_reason or reason_by_outcome[resolution.outcome]
    _identity_log(
        contact=contact,
        message_sid=message_sid,
        outcome=resolution.outcome.value,
        reason_code=reason_code,
        candidate_ids=resolution.candidate_contact_ids,
        pending_state_id=contact.identity_last_request_sid or contact.id,
    )
    if resolution.outcome in {
        ConfirmationResolverOutcome.TRUE_AMBIGUITY,
        ConfirmationResolverOutcome.IDENTITY_CONFLICT,
        ConfirmationResolverOutcome.INSUFFICIENT_EVIDENCE,
    }:
        contact.identity_conflict_status = (
            "verified_conflict"
            if resolution.outcome == ConfirmationResolverOutcome.IDENTITY_CONFLICT
            else (
                "insufficient_evidence"
                if resolution.outcome == ConfirmationResolverOutcome.INSUFFICIENT_EVIDENCE
                else "ambiguous"
            )
        )
        contact.approval_status = "review_required"
        return {
            "reply": "We could not safely confirm this identity automatically. Our team will review it.",
            "review": True,
            "resolver_outcome": resolution.outcome.value,
            "reason": reason_code,
        }
    if resolution.outcome == ConfirmationResolverOutcome.SINGLE_COMPATIBLE_MATCH:
        from services.contact_dedupe import merge_contacts
        canonical = next(
            row for row in evidence.contacts
            if row.id == resolution.canonical_contact_id
        )
        merge_contacts(
            canonical.id,
            [contact.id],
            company_id=company_id,
            commit=False,
        )
        contact = canonical
        conversation.contact_id = canonical.id
    consent_snapshot = (
        contact.sms_marketing_opt_in,
        contact.sms_consent_status,
        contact.sms_opted_out,
        contact.sms_opt_out_at,
        contact.do_not_sms,
        contact.do_not_market,
        contact.do_not_contact,
    )
    contact.first_name, contact.last_name = first, last
    contact.display_name = contact.name = display
    contact.primary_email = contact.email = contact.normalized_email = email
    contact.identity_status = "confirmed"
    contact.identity_confirmed_at = contact.identity_verified_at = now
    contact.identity_confirmation_sid = contact.identity_last_response_sid = message_sid
    contact.identity_verification_source = contact.name_source = "customer_confirmed"
    contact.name_verification_level = "verified"
    contact.name_verified_at = now
    contact.name_provenance = {"source": "customer_confirmed_sms", "confidence": 100}
    contact.identity_conflict_status = "none"
    contact.identity_conflict_details = {}
    contact.approval_status, contact.approved_at = "approved", now
    contact.approval_source = "customer_confirmation"
    contact.approval_match_source = "customer_confirmed_sms"
    _add_tag(contact, APPROVED_CONTACT_TAG)
    existing = ContactEmailAddress.query.filter_by(company_id=contact.company_id, contact_id=contact.id, normalized_value=email).first()
    if not existing:
        db.session.add(ContactEmailAddress(company_id=contact.company_id, contact_id=contact.id, original_value=email,
            normalized_value=email, is_primary=True, verification_status="confirmed", source="customer_confirmed"))
    db.session.add(ContactSourceEvent(company_id=contact.company_id, contact_id=contact.id, source="customer_confirmed",
        source_detail="SMS identity confirmation", event_type="identity_confirmed", event_metadata={"provider": "twilio", "message_sid": message_sid}))
    conversation.contact_name, conversation.contact_source = display, "customer_confirmed"
    contact.pending_first_name = contact.pending_last_name = contact.pending_email = None
    contact.identity_requested_fields, contact.identity_request_state = [], {}
    assert consent_snapshot == (
        contact.sms_marketing_opt_in,
        contact.sms_consent_status,
        contact.sms_opted_out,
        contact.sms_opt_out_at,
        contact.do_not_sms,
        contact.do_not_market,
        contact.do_not_contact,
    )
    return {
        "reply": CONFIRMED_REPLY,
        "confirmed": True,
        "canonical_contact_id": contact.id,
        "resolver_outcome": resolution.outcome.value,
    }


def process_identity_message(contact: Contact, conversation, body: str, message_sid: str) -> dict:
    """Advance identity state; returns a reply body or indicates no identity reply."""
    keyword = (body or "").strip().casefold()
    if contact.identity_status == "awaiting_confirmation":
        if keyword in _YES:
            return confirm_pending_identity(contact, conversation, message_sid)
        if keyword in _NO:
            _reset_unsafe_confirmation(contact)
            return {"reply": "No problem. Please reply with your first and last name (or just your first name)."}
        return {"reply": "Please reply YES to confirm or NO to correct your contact information."}

    if contact.identity_status == "awaiting_name":
        stored_name = _trusted_stored_name(contact)
        if stored_name:
            contact.pending_first_name, contact.pending_last_name = stored_name
            contact.identity_status = "awaiting_email"
            contact.identity_requested_fields = ["email"]
            return {"reply": "Please reply with your email address."}
        name = validate_name_candidate(body)
        if not name:
            _identity_log(
                contact=contact,
                message_sid=message_sid,
                outcome="NAME_REJECTED",
                reason_code="identity_name_candidate_rejected",
                pending_state_id=contact.identity_last_request_sid or contact.id,
            )
            return {"reply": "Please reply with your first and last name only."}
        contact.pending_first_name, contact.pending_last_name = name
        contact.identity_status = "awaiting_email"
        contact.identity_requested_fields = ["email"]
        return {"reply": "Thank you. Now reply with your email address."}

    if contact.identity_status == "awaiting_email":
        if not validate_name_candidate(
            f"{contact.pending_first_name or ''} {contact.pending_last_name or ''}"
        ):
            contact.pending_first_name = contact.pending_last_name = None
            contact.identity_status = "awaiting_name"
            contact.identity_requested_fields = ["first_name", "last_name", "email"]
            return {"reply": "Please reply with your first and last name."}
        email = _valid_email((body or "").strip())
        if not email:
            return {"reply": "Please reply with a valid email address."}
        contact.pending_email = email
        contact.identity_status = "awaiting_confirmation"
        contact.identity_requested_fields = []
        now = datetime.utcnow()
        contact.identity_fields_requested_at = now
        contact.identity_last_request_sid = message_sid
        contact.identity_request_state = {
            "phase": "awaiting_confirmation",
            "company_id": contact.company_id,
            "contact_id": contact.id,
            "confirmation_nonce": message_sid,
            "requested_at": now.isoformat(),
        }
        return {"reply": f"Is this correct? {contact.pending_first_name} {contact.pending_last_name}, {contact.pending_email}. Reply YES to confirm or NO if incorrect."}

    if contact.identity_status in {"confirmed", "minimum_established"} or contact.google_match_status == "matched":
        return {"reply": None}
    _identity_log(
        contact=contact,
        message_sid=message_sid,
        outcome="NOT_IN_COLLECTION_STATE",
        reason_code="identity_message_not_in_collection_state",
        pending_state_id=contact.identity_last_request_sid or contact.id,
    )
    return {"reply": None, "not_in_collection_state": True}


def should_request_identity(contact: Contact, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    cooldown = timedelta(hours=int(os.getenv("IDENTITY_REQUEST_COOLDOWN_HOURS", "24")))
    limit = int(os.getenv("IDENTITY_REQUEST_ATTEMPT_LIMIT", "3"))
    last_request = getattr(contact, "identity_fields_requested_at", None) or contact.identity_requested_at
    return (
        contact.identity_status not in {
            "confirmed", "minimum_established",
            "awaiting_name", "awaiting_email", "awaiting_confirmation", "declined",
        }
        and contact.google_match_status != "matched"
        and (contact.identity_request_count or 0) < limit
        and (not last_request or last_request <= now - cooldown)
        and not contact.sms_opted_out and not contact.do_not_sms and not contact.do_not_contact
    )
