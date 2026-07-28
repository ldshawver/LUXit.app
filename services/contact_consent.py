"""Canonical contact consent semantics shared by merge and delivery paths."""
from __future__ import annotations


CANONICAL_EMAIL_OPTOUT_STATUS = "unsubscribed"
EMAIL_OPTOUT_STATUSES = frozenset({
    "unsubscribed",
    "opted_out",
    "denied",
    "suppressed",
    "revoked",
})
EMAIL_OPTIN_STATUSES = frozenset({"opted_in", "subscribed"})


def normalize_email_consent_status(value: str | None) -> str:
    return str(value or "unknown").strip().lower() or "unknown"


def has_explicit_email_opt_out(contact) -> bool:
    """Treat canonical and legacy opt-out evidence as equally suppressive."""
    return bool(
        getattr(contact, "do_not_email", False)
        or getattr(contact, "email_unsubscribed", False)
        or getattr(contact, "is_subscribed", None) is False
        or getattr(contact, "email_subscribed", None) is False
        or normalize_email_consent_status(
            getattr(contact, "email_consent_status", None)
        )
        in EMAIL_OPTOUT_STATUSES
    )


def has_explicit_email_opt_in(contact) -> bool:
    return bool(
        getattr(contact, "email_opt_in", False)
        or normalize_email_consent_status(
            getattr(contact, "email_consent_status", None)
        )
        in EMAIL_OPTIN_STATUSES
    )


def apply_email_opt_out(contact) -> None:
    """Persist one non-contradictory representation of an email opt-out."""
    contact.email_consent_status = CANONICAL_EMAIL_OPTOUT_STATUS
    contact.email_unsubscribed = True
    contact.do_not_email = True
    contact.email_opt_in = False
    contact.email_subscribed = False
    contact.is_subscribed = False


def is_email_contactable(contact) -> bool:
    """Return whether a contact may pass the repository's email send gate."""
    return bool(
        contact
        and getattr(contact, "is_active", True)
        and getattr(contact, "email", None)
        and not getattr(contact, "do_not_market", False)
        and not has_explicit_email_opt_out(contact)
    )
