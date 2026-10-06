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
    """Return True only for affirmative email opt-out evidence.

    ``is_subscribed`` is deliberately NOT consulted. It is the legacy
    newsletter/SMS-keyword flag, not email consent: Google Contacts import
    sets it False on every new contact ("not on the newsletter yet") and SMS
    STOP sets it False too. Reading it here made a missing opt-in look like
    an email unsubscribe, and contact_dedupe then promoted that into a full
    apply_email_opt_out() on the merge survivor. Operator unsubscribes go
    through apply_email_opt_out(), so they are still caught below.
    """
    return bool(
        getattr(contact, "do_not_email", False)
        or getattr(contact, "email_unsubscribed", False)
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


def has_sms_opt_out(contact) -> bool:
    return bool(
        getattr(contact, "sms_opted_out", False)
        or getattr(contact, "sms_opt_out_at", None)
    )


def suppression_badges(contact) -> list[str]:
    """UI badges, derived from the same evidence the send gates enforce.

    Email and SMS are reported per channel; neither channel's opt-out
    produces the other's badge.
    """
    badges = []
    if getattr(contact, "do_not_market", False): badges.append("Do Not Market")
    if getattr(contact, "do_not_email", False): badges.append("Do Not Email")
    if getattr(contact, "do_not_sms", False): badges.append("Do Not SMS")
    if has_sms_opt_out(contact): badges.append("SMS Opted Out")
    if (
        getattr(contact, "email_unsubscribed", False)
        or getattr(contact, "email_subscribed", None) is False
        or normalize_email_consent_status(getattr(contact, "email_consent_status", None)) in EMAIL_OPTOUT_STATUSES
    ):
        badges.append("Email Unsubscribed")
    return badges


def is_suppressed(contact) -> bool:
    return bool(suppression_badges(contact))


def is_email_contactable(contact) -> bool:
    """Return whether a contact may pass the repository's email send gate."""
    return bool(
        contact
        and getattr(contact, "is_active", True)
        and getattr(contact, "email", None)
        and not getattr(contact, "do_not_market", False)
        and not has_explicit_email_opt_out(contact)
    )
