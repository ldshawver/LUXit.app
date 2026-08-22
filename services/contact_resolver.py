"""One tenant-scoped canonical contact and safe display-name resolver."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from sqlalchemy import func, or_

from extensions import db
from models import Contact, ContactEmailAddress, ContactPhoneNumber, GoogleContactLookup
from services.phone_normalization import normalize_phone_e164

PLACEHOLDERS = {"pending identity", "unknown", "unknown contact", "n/a", "na", "none", "caller", "new contact", "new caller", "name needed"}
SOURCE_RANK = {"customer_confirmed": 700, "pwa_verified": 700,
               "canonical": 600, "manual": 600, "user": 600,
               "google_contacts": 500, "google": 500,
               "ios_contacts": 400, "icloud_import": 400,
               "trusted_import": 300}
# Identity states in which stored name data must never be treated as trustworthy,
# regardless of source ranking (e.g. a name flagged ambiguous by conflicting
# Google/iOS evidence, or a contact that explicitly declined confirmation).
UNSAFE_IDENTITY_STATES = {"declined", "ambiguous"}
IDENTITY_KEYWORDS = {
    "yes", "y", "confirm", "confirmed", "no", "n", "incorrect", "change",
    "stop", "start", "help",
}
GENERIC_ACKNOWLEDGEMENTS = {
    "ok", "okay", "k", "thanks", "thank you", "sounds good", "got it",
    "sure", "fine", "great", "hello", "hi", "hey",
}
TRUSTED_NAME_SOURCES = {
    "customer_confirmed", "pwa_verified", "manual", "google_contacts",
}
TRUSTED_NAME_PROVENANCE_SOURCES = {
    "customer_confirmed", "customer_confirmed_sms", "pwa_verified",
    "manual", "google_contacts",
}
TRUSTED_VERIFICATION_LEVELS = {"verified", "trusted"}
TRUSTED_POINT_SOURCES = {
    "customer_confirmed", "pwa_verified", "manual", "twilio",
    "google_contacts", "trusted_import",
}
ROLE_EMAIL_LOCAL_PARTS = {
    "admin", "billing", "contact", "hello", "help", "info", "office",
    "orders", "sales", "service", "support", "team",
}
PLACEHOLDER_EMAILS = {
    "unknown@example.com", "test@example.com", "none@example.com",
    "noreply@example.com", "no-reply@example.com",
}
DISPOSABLE_EMAIL_DOMAINS = {
    "10minutemail.com", "guerrillamail.com", "mailinator.com", "tempmail.com",
}
MESSAGE_SENTENCE_RE = re.compile(
    r"^(?:"
    r"(?:can|could|would)\s+you\s+[^\W\d_]+|"
    r"(?:where|when|why|how)\s+(?:are|is|do|does|can|could|would|will)\s+|"
    r"please\s+(?:send|call|text|help|tell|schedule|book|cancel|change|provide)\s+|"
    r"i\s+(?:need|want|would\s+like|am|have)\s+|"
    r"need\s+(?:more\s+)?(?:help|information|details|service|support)|"
    r"(?:send|call|text|tell)\s+(?:me|us)\s+"
    r")",
    re.IGNORECASE,
)


class ConfirmationResolverOutcome(str, Enum):
    NO_MATCH = "NO_MATCH"
    SELF_MATCH = "SELF_MATCH"
    SINGLE_COMPATIBLE_MATCH = "SINGLE_COMPATIBLE_MATCH"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    TRUE_AMBIGUITY = "TRUE_AMBIGUITY"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    CROSS_TENANT_ONLY = "CROSS_TENANT_ONLY"


@dataclass(frozen=True)
class ConfirmationResolution:
    outcome: ConfirmationResolverOutcome
    canonical_contact_id: int | None
    candidate_contact_ids: tuple[int, ...]
    candidate_count: int
    failure_reason: str | None = None


def safe_name(value: str | None, phone: str | None = None) -> str | None:
    value = " ".join(str(value or "").split()).strip()
    if not value or value.casefold() in PLACEHOLDERS or "@" in value:
        return None
    if not any(char.isalpha() for char in value):
        return None
    digits = re.sub(r"\D", "", value)
    phone_digits = re.sub(r"\D", "", phone or "")
    if phone_digits and digits == phone_digits:
        return None
    return value


def validate_person_name(value: str | None) -> tuple[str, str] | None:
    """Parse an explicitly prompted legal/person name without rewriting it.

    This is deliberately a structured protocol, not an attempt to infer identity
    from ordinary prose.  The caller must separately enforce ``awaiting_name``.
    """
    candidate = " ".join((value or "").strip().split())
    folded = candidate.casefold()
    if (
        not candidate
        or folded in IDENTITY_KEYWORDS
        or folded in GENERIC_ACKNOWLEDGEMENTS
        or "@" in candidate
        or re.search(r"(?:https?://|www\.)", candidate, re.IGNORECASE)
        or any(char.isdigit() for char in candidate)
        or not any(char.isalpha() for char in candidate)
        or MESSAGE_SENTENCE_RE.search(candidate)
    ):
        return None
    if len(candidate) > 240 or candidate.count(",") > 1:
        return None

    comma_form = "," in candidate
    if comma_form:
        family, given = (part.strip() for part in candidate.split(",", 1))
        if not family or not given:
            return None
        ordered = f"{given} {family}"
    else:
        ordered = candidate
    tokens = ordered.split()
    if not 1 <= len(tokens) <= 8:
        return None

    suffixes = {"jr.", "sr.", "ii", "iii", "iv", "v"}
    for token in tokens:
        if token.casefold() in suffixes:
            continue
        if len(token) > 80 or token[0] in "'’-" or token[-1] in "'’-":
            return None
        for char in token:
            if char in "'’‐‑‒–—-":
                continue
            if unicodedata.category(char).startswith("L"):
                continue
            return None

    if comma_form:
        return given, family
    return tokens[0], " ".join(tokens[1:])


def mask_phone(phone: str | None) -> str | None:
    normalized = normalize_phone_e164(phone)
    return f"••• ••• {normalized[-4:]}" if normalized else None


@dataclass(frozen=True)
class ContactResolution:
    canonical_contact_id: int | None
    first_name: str | None
    last_name: str | None
    safe_display_name: str
    masked_phone: str | None
    name_source: str | None
    verification_level: str
    identity_status: str
    conflict_state: str

    def as_dict(self):
        return self.__dict__.copy()


def _trusted_name_provenance(contact: Contact) -> bool:
    if contact.identity_status in UNSAFE_IDENTITY_STATES:
        return False
    provenance = contact.name_provenance if isinstance(contact.name_provenance, dict) else {}
    provenance_source = str(provenance.get("source") or "").casefold()
    if (
        contact.name_verification_level in TRUSTED_VERIFICATION_LEVELS
        and contact.name_source in TRUSTED_NAME_SOURCES
        and provenance_source in TRUSTED_NAME_PROVENANCE_SOURCES
    ):
        return True
    # Canonical first-party contact data: contact.first_name/last_name are only ever
    # written by trusted paths (contact creation/import, the SMS-confirmed flow, or
    # this resolver's own Google/iOS promotion below) -- unconfirmed SMS replies are
    # staged in pending_first_name/pending_last_name and never reach here. So an
    # existing first_name with a non-unsafe identity_status is inherently trustworthy
    # even before it has been explicitly "confirmed" or matched against Google/iOS.
    return bool(contact.first_name)


def _contact_name(contact: Contact) -> str | None:
    """Return a name only when both value and durable provenance are trusted."""
    if not _trusted_name_provenance(contact):
        return None
    phone = contact.normalized_phone or contact.phone
    for value in (
        f"{contact.first_name or ''} {contact.last_name or ''}",
        contact.display_name,
        contact.name,
    ):
        validated = validate_person_name(value)
        if validated:
            return safe_name(" ".join(part for part in validated if part), phone)
    return None


def is_placeholder_or_shared_email(value: str | None) -> bool:
    normalized = (value or "").strip().casefold()
    if not normalized or normalized in PLACEHOLDER_EMAILS or "@" not in normalized:
        return True
    local, domain = normalized.rsplit("@", 1)
    return (
        local in ROLE_EMAIL_LOCAL_PARTS
        or domain in DISPOSABLE_EMAIL_DOMAINS
        or domain in {"example.com", "example.org", "example.net"}
    )


def _normalized_contact_email(contact: Contact) -> str:
    return (contact.normalized_email or contact.primary_email or contact.email or "").strip().casefold()


@dataclass(frozen=True)
class ConfirmationEvidence:
    contacts: tuple[Contact, ...]
    phone_points: tuple[ContactPhoneNumber, ...]
    email_points: tuple[ContactEmailAddress, ...]


def discover_confirmation_candidate_ids(
    company_id: int,
    *,
    normalized_phone: str | None,
    normalized_email: str | None,
    limit: int = 50,
) -> tuple[int, ...]:
    """Return a bounded tenant-scoped candidate set using indexed predicates."""
    candidate_ids: set[int] = set()
    contact_filters = []
    if normalized_phone:
        contact_filters.append(Contact.normalized_phone == normalized_phone)
    if normalized_email:
        contact_filters.extend((
            func.lower(func.trim(Contact.normalized_email)) == normalized_email,
            func.lower(Contact.primary_email) == normalized_email,
            func.lower(Contact.email) == normalized_email,
        ))
    if contact_filters:
        rows = (
            db.session.query(Contact.id)
            .filter(
                Contact.company_id == company_id,
                Contact.is_active.is_(True),
                Contact.merged_into_contact_id.is_(None),
                or_(*contact_filters),
            )
            .order_by(Contact.id.asc())
            .limit(limit + 1)
            .all()
        )
        candidate_ids.update(row[0] for row in rows)
    if normalized_phone and len(candidate_ids) <= limit:
        rows = (
            db.session.query(ContactPhoneNumber.contact_id)
            .join(Contact, Contact.id == ContactPhoneNumber.contact_id)
            .filter(
                ContactPhoneNumber.company_id == company_id,
                ContactPhoneNumber.normalized_value == normalized_phone,
                Contact.is_active.is_(True),
                Contact.merged_into_contact_id.is_(None),
            )
            .order_by(ContactPhoneNumber.contact_id.asc())
            .limit(limit + 1)
            .all()
        )
        candidate_ids.update(row[0] for row in rows)
    if normalized_email and len(candidate_ids) <= limit:
        rows = (
            db.session.query(ContactEmailAddress.contact_id)
            .join(Contact, Contact.id == ContactEmailAddress.contact_id)
            .filter(
                ContactEmailAddress.company_id == company_id,
                func.lower(func.trim(ContactEmailAddress.normalized_value)) == normalized_email,
                Contact.is_active.is_(True),
                Contact.merged_into_contact_id.is_(None),
            )
            .order_by(ContactEmailAddress.contact_id.asc())
            .limit(limit + 1)
            .all()
        )
        candidate_ids.update(row[0] for row in rows)
    return tuple(sorted(candidate_ids)[: limit + 1])


def load_confirmation_evidence(
    company_id: int,
    candidate_ids: tuple[int, ...],
    *,
    lock: bool = False,
) -> ConfirmationEvidence:
    """Batch-load contacts and all their identity points in deterministic order."""
    if not candidate_ids:
        return ConfirmationEvidence((), (), ())
    contacts_q = Contact.query.filter(
        Contact.company_id == company_id,
        Contact.id.in_(candidate_ids),
    ).order_by(Contact.id.asc())
    phones_q = ContactPhoneNumber.query.filter(
        ContactPhoneNumber.company_id == company_id,
        ContactPhoneNumber.contact_id.in_(candidate_ids),
    ).order_by(ContactPhoneNumber.contact_id.asc(), ContactPhoneNumber.id.asc())
    emails_q = ContactEmailAddress.query.filter(
        ContactEmailAddress.company_id == company_id,
        ContactEmailAddress.contact_id.in_(candidate_ids),
    ).order_by(ContactEmailAddress.contact_id.asc(), ContactEmailAddress.id.asc())
    if lock:
        contacts_q = contacts_q.with_for_update()
        phones_q = phones_q.with_for_update()
        emails_q = emails_q.with_for_update()
    return ConfirmationEvidence(
        tuple(contacts_q.populate_existing().all()),
        tuple(phones_q.populate_existing().all()),
        tuple(emails_q.populate_existing().all()),
    )


def _trusted_point(row) -> bool:
    return (
        row.verification_status in {"confirmed", "verified", "trusted"}
        or row.source in TRUSTED_POINT_SOURCES
    )


def _contact_identity_values(contact, phone_points, email_points):
    phones = {
        normalized
        for normalized in (
            normalize_phone_e164(contact.normalized_phone or contact.primary_phone or contact.phone),
            *(normalize_phone_e164(row.normalized_value or row.original_value) for row in phone_points),
        )
        if normalized
    }
    emails = {
        normalized
        for normalized in (
            _normalized_contact_email(contact),
            *((row.normalized_value or row.original_value or "").strip().casefold() for row in email_points),
        )
        if normalized
    }
    trusted_phones = {
        normalize_phone_e164(row.normalized_value or row.original_value)
        for row in phone_points if _trusted_point(row)
    }
    trusted_emails = {
        (row.normalized_value or row.original_value or "").strip().casefold()
        for row in email_points if _trusted_point(row)
    }
    identity_source = (contact.identity_verification_source or contact.source_provider or "").casefold()
    if (
        contact.identity_status == "confirmed"
        and identity_source in TRUSTED_POINT_SOURCES
    ) or contact.source_provider == "twilio":
        trusted_phones.add(normalize_phone_e164(contact.normalized_phone or contact.primary_phone or contact.phone))
    if (
        contact.identity_status == "confirmed"
        and identity_source in TRUSTED_POINT_SOURCES
    ):
        trusted_emails.add(_normalized_contact_email(contact))
    return (
        {value for value in phones if value},
        {value for value in emails if value},
        {value for value in trusted_phones if value},
        {value for value in trusted_emails if value},
    )


def classify_confirmation_evidence(
    company_id: int,
    *,
    current_contact_id: int,
    normalized_phone: str | None,
    normalized_email: str | None,
    first_name: str,
    last_name: str,
    evidence: ConfirmationEvidence,
) -> ConfirmationResolution:
    """Classify only the supplied, freshly loaded tenant-scoped evidence."""
    contacts = [
        row for row in evidence.contacts
        if row.company_id == company_id and row.is_active and row.merged_into_contact_id is None
    ]
    phone_by_contact: dict[int, list[ContactPhoneNumber]] = {}
    email_by_contact: dict[int, list[ContactEmailAddress]] = {}
    for row in evidence.phone_points:
        phone_by_contact.setdefault(row.contact_id, []).append(row)
    for row in evidence.email_points:
        email_by_contact.setdefault(row.contact_id, []).append(row)

    matching = []
    details = {}
    proposed_name = " ".join(part for part in (first_name, last_name) if part).strip()
    conflicts = []
    strong = []
    for candidate in contacts:
        phones, emails, trusted_phones, trusted_emails = _contact_identity_values(
            candidate,
            phone_by_contact.get(candidate.id, ()),
            email_by_contact.get(candidate.id, ()),
        )
        phone_match = bool(normalized_phone and normalized_phone in phones)
        email_match = bool(normalized_email and normalized_email in emails)
        if not phone_match and not email_match:
            continue
        matching.append(candidate)
        verified_phone_conflict = bool(
            normalized_phone and trusted_phones and normalized_phone not in trusted_phones
        )
        verified_email_conflict = bool(
            normalized_email and trusted_emails and normalized_email not in trusted_emails
        )
        trusted_name = _contact_name(candidate)
        name_conflict = bool(
            trusted_name and proposed_name
            and trusted_name.casefold() != proposed_name.casefold()
        )
        if candidate.id != current_contact_id and (
            verified_phone_conflict or verified_email_conflict or name_conflict
        ):
            conflicts.append(candidate)
        high_confidence = (
            candidate.id != current_contact_id
            and phone_match
            and email_match
            and normalized_phone in trusted_phones
            and normalized_email in trusted_emails
            and not is_placeholder_or_shared_email(normalized_email)
            and not (verified_phone_conflict or verified_email_conflict or name_conflict)
        )
        if high_confidence:
            strong.append(candidate)
        details[candidate.id] = (phone_match, email_match, high_confidence)

    matching_ids = tuple(sorted(row.id for row in matching))
    self_match = any(row.id == current_contact_id for row in matching)
    others = [row for row in matching if row.id != current_contact_id]
    if conflicts:
        return ConfirmationResolution(
            ConfirmationResolverOutcome.IDENTITY_CONFLICT, None, matching_ids,
            len(matching), "identity_confirm_verified_conflict",
        )
    if len(strong) > 1 or len(others) > 1:
        return ConfirmationResolution(
            ConfirmationResolverOutcome.TRUE_AMBIGUITY, None, matching_ids,
            len(matching), "identity_confirm_true_ambiguity",
        )
    if len(strong) == 1 and len(others) == 1:
        return ConfirmationResolution(
            ConfirmationResolverOutcome.SINGLE_COMPATIBLE_MATCH, strong[0].id,
            matching_ids, len(matching),
        )
    if others:
        return ConfirmationResolution(
            ConfirmationResolverOutcome.INSUFFICIENT_EVIDENCE, None, matching_ids,
            len(matching), "identity_confirm_insufficient_evidence",
        )
    if self_match:
        return ConfirmationResolution(
            ConfirmationResolverOutcome.SELF_MATCH, current_contact_id,
            matching_ids, len(matching),
        )
    return ConfirmationResolution(
        ConfirmationResolverOutcome.NO_MATCH, current_contact_id, (), 0,
    )


def resolve_confirmation_identity(
    company_id: int,
    *,
    current_contact_id: int,
    phone: str | None,
    email: str | None,
    first_name: str,
    last_name: str,
) -> ConfirmationResolution:
    """Discover and classify candidates; mutation callers must lock and rerun."""
    normalized_phone = normalize_phone_e164(phone)
    normalized_email = (email or "").strip().casefold()
    candidate_ids = discover_confirmation_candidate_ids(
        company_id,
        normalized_phone=normalized_phone,
        normalized_email=normalized_email,
    )
    evidence = load_confirmation_evidence(company_id, candidate_ids)
    result = classify_confirmation_evidence(
        company_id,
        current_contact_id=current_contact_id,
        normalized_phone=normalized_phone,
        normalized_email=normalized_email,
        first_name=first_name,
        last_name=last_name,
        evidence=evidence,
    )
    if result.outcome != ConfirmationResolverOutcome.NO_MATCH:
        return result

    # Cross-tenant detection returns only a boolean and never exposes rows.
    cross_tenant = False
    if normalized_phone:
        cross_tenant = db.session.query(Contact.id).filter(
            Contact.company_id != company_id,
            Contact.is_active.is_(True),
            Contact.normalized_phone == normalized_phone,
        ).first() is not None
    if not cross_tenant and normalized_email:
        cross_tenant = db.session.query(Contact.id).filter(
            Contact.company_id != company_id,
            Contact.is_active.is_(True),
            or_(
                func.lower(func.trim(Contact.normalized_email)) == normalized_email,
                func.lower(Contact.primary_email) == normalized_email,
                func.lower(Contact.email) == normalized_email,
            ),
        ).first() is not None
    if not cross_tenant and normalized_phone:
        cross_tenant = db.session.query(ContactPhoneNumber.id).filter(
            ContactPhoneNumber.company_id != company_id,
            ContactPhoneNumber.normalized_value == normalized_phone,
        ).first() is not None
    if not cross_tenant and normalized_email:
        cross_tenant = db.session.query(ContactEmailAddress.id).filter(
            ContactEmailAddress.company_id != company_id,
            func.lower(func.trim(ContactEmailAddress.normalized_value)) == normalized_email,
        ).first() is not None
    if cross_tenant:
        return ConfirmationResolution(
            ConfirmationResolverOutcome.CROSS_TENANT_ONLY,
            current_contact_id,
            (),
            0,
            "identity_confirm_cross_tenant_only",
        )
    return ConfirmationResolution(
        ConfirmationResolverOutcome.NO_MATCH,
        current_contact_id,
        (),
        0,
    )


def resolve_contact_identity(company_id: int, *, phone=None, email=None, contact_id=None, allow_enrichment=False) -> ContactResolution:
    q = Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True))
    candidates = []
    if contact_id is not None:
        candidates = q.filter(Contact.id == contact_id).limit(1).all()
    norm_phone = normalize_phone_e164(phone)
    if not candidates and norm_phone:
        candidates = q.filter(Contact.normalized_phone == norm_phone).order_by(Contact.id.asc()).limit(3).all()
    norm_email = (email or "").strip().casefold()
    if not candidates and norm_email:
        candidates = q.filter(or_(Contact.normalized_email == norm_email, func.lower(func.trim(Contact.email)) == norm_email)).order_by(Contact.id.asc()).limit(3).all()
        linked_ids = [
            r.contact_id for r in ContactEmailAddress.query.filter(
                ContactEmailAddress.company_id == company_id,
                func.lower(func.trim(ContactEmailAddress.normalized_value)) == norm_email,
            ).order_by(ContactEmailAddress.contact_id.asc()).limit(3).all()
        ]
        if linked_ids:
            candidates += [c for c in q.filter(Contact.id.in_(linked_ids)).limit(3).all() if c not in candidates]
    if len(candidates) > 1:
        return ContactResolution(None, None, None, "Name needed", mask_phone(norm_phone or phone), None, "unverified", "conflict", "multiple_contacts")
    contact = candidates[0] if candidates else None
    if not contact:
        return ContactResolution(None, None, None, "Name needed", mask_phone(phone), None, "unverified", "name_needed", "none")

    current = _contact_name(contact)
    source = contact.name_source or "canonical"
    if source == "user" and not contact.name_verified_at and contact.source in {"google", "google_contacts"}:
        source = "google_contacts"
    evidence = []
    if current:
        evidence.append((SOURCE_RANK.get(source, 100), current, source))
    lookups = GoogleContactLookup.query.filter_by(company_id=company_id, normalized_phone=contact.normalized_phone).all() if contact.normalized_phone else []
    google_names = {
        " ".join(part for part in parsed if part)
        for r in lookups if not r.is_ambiguous
        for parsed in [validate_person_name(r.display_name)]
        if parsed
    }
    google_names.discard(None)
    if any(r.is_ambiguous for r in lookups) or len(google_names) > 1:
        contact.google_match_status = "ambiguous"
        contact.identity_conflict_status = "ambiguous"
        return ContactResolution(contact.id, contact.first_name, contact.last_name, current or "Name needed", mask_phone(contact.normalized_phone or contact.phone), source if current else None, contact.name_verification_level or "unverified", contact.identity_status, "google_ambiguous")
    if google_names:
        evidence.append((400, next(iter(google_names)), "google_contacts"))
    ios = q.filter(Contact.normalized_phone == contact.normalized_phone, Contact.source_provider.in_(["ios_contacts", "icloud_import"])).all() if contact.normalized_phone else []
    ios_names = {_contact_name(row) for row in ios}; ios_names.discard(None)
    if len(ios_names) > 1:
        contact.identity_conflict_status = "ambiguous"
        return ContactResolution(contact.id, contact.first_name, contact.last_name, current or "Name needed", mask_phone(contact.normalized_phone or contact.phone), source if current else None, contact.name_verification_level or "unverified", contact.identity_status, "ios_ambiguous")
    if ios_names:
        evidence.append((300, next(iter(ios_names)), "ios_contacts"))
    evidence.sort(key=lambda row: row[0], reverse=True)
    best = evidence[0] if evidence else None
    if best and allow_enrichment and (not current or best[0] > SOURCE_RANK.get(source, 100)):
        contact.display_name = contact.name = best[1]
        parts = best[1].split(None, 1)
        contact.first_name, contact.last_name = parts[0], parts[1] if len(parts) > 1 else contact.last_name
        contact.name_source = best[2]
        contact.name_verification_level = "trusted"
        contact.name_provenance = {
            "source": best[2],
            "confidence": 80,
            "provider_verified": True,
        }
        if contact.identity_status == "pending_identity":
            contact.identity_status = "confirmed"
        current, source = best[1], best[2]
    elif current and contact.identity_status == "pending_identity" and (contact.normalized_phone or contact.phone):
        # A trusted first-party (canonical) name is already on file alongside a phone
        # number, with no stronger/conflicting evidence to promote over it. That is
        # enough to skip SMS identity collection, but it is not the same guarantee as
        # a completed SMS confirmation or Google/iOS match, so it gets its own status
        # rather than being folded into "confirmed".
        contact.identity_status = "minimum_established"
    return ContactResolution(contact.id, contact.first_name, contact.last_name, current or "Name needed", mask_phone(contact.normalized_phone or contact.phone), source if current else None, contact.name_verification_level or "unverified", contact.identity_status, contact.identity_conflict_status or "none")


def resolve_pending_contacts(company_id: int) -> dict[str, int]:
    """Safe tenant batch used after sync/import and by an external scheduled worker."""
    counts = {"examined": 0, "updated": 0, "conflicted": 0, "name_needed": 0}
    rows = Contact.query.filter(Contact.company_id == company_id, Contact.is_active.is_(True)).all()
    for contact in rows:
        counts["examined"] += 1
        before = _contact_name(contact)
        result = resolve_contact_identity(company_id, contact_id=contact.id, allow_enrichment=True)
        if result.conflict_state != "none": counts["conflicted"] += 1
        elif result.safe_display_name == "Name needed": counts["name_needed"] += 1
        elif not before: counts["updated"] += 1
    db.session.flush()
    return counts
