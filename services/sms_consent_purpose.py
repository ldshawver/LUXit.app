"""Minimum-viable purpose-specific SMS eligibility for LUXit.

Five concepts, never interchangeable:

  customer_relationship      - is a customer (segment membership) - the caller's concern
  conversational_sms_evidence - a verifiable prior *inbound* SMS from this
                                person to this tenant (Twilio MessageSid +
                                timestamp; metadata only, message body is
                                never read)
  transactional_eligibility  - may receive order / status / service messages
  promotional_eligibility    - may receive marketing (requires an affirmative
                               opt-in: sms_marketing_opt_in and
                               sms_consent_status in {opted_in, subscribed})
  suppressed                 - STOP / opt-out / DNC / archived - overrides ALL

No schema change. Everything is derived from existing contact columns and
twilio_message metadata. This module NEVER writes consent, opt-in/out, STOP,
suppression or DNC state.
"""
from __future__ import annotations

import re

from extensions import db
from models import Contact, TwilioMessage

CAMPAIGN_PURPOSES = ("conversational", "transactional", "promotional")
DEFAULT_CAMPAIGN_PURPOSE = "promotional"  # strictest => zero behaviour change for existing campaigns


def normalize_campaign_purpose(value) -> str:
    v = str(value or "").strip().lower().replace("/", "_").replace("-", "_").replace(" ", "_")
    v = {"conversational_follow_up": "conversational", "follow_up": "conversational",
         "informational": "transactional", "transactional_informational": "transactional",
         "marketing": "promotional", "promotional_marketing": "promotional"}.get(v, v)
    return v if v in CAMPAIGN_PURPOSES else DEFAULT_CAMPAIGN_PURPOSE


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def is_suppressed(contact: Contact) -> bool:
    tags = {t.strip().lower() for t in re.split(r"[,;|]", str(contact.tags or "")) if t.strip()}
    return bool(
        contact.sms_opted_out or contact.do_not_sms or contact.sms_opt_out_at
        or contact.do_not_market or getattr(contact, "do_not_contact", False)
        or not contact.is_active or contact.archived_at
        or getattr(contact, "merged_into_contact_id", None)
        or contact.status in {"archived", "suppressed", "merged"}
        or "sms_opt_out" in tags or "no_sms" in tags or "blocked" in tags
    )


def has_promotional_optin(contact: Contact) -> bool:
    return bool(contact.sms_marketing_opt_in and contact.sms_consent_status in {"opted_in", "subscribed"})


def conversational_evidence_phone_digits(company_id: int) -> set[str]:
    """One query: the digit-only from-numbers of every inbound SMS with a
    reserved Twilio SID for this tenant. Callers intersect against a contact's
    number to decide conversational_sms_evidence without an N+1."""
    rows = (
        db.session.query(TwilioMessage.from_number)
        .filter(
            TwilioMessage.company_id == company_id,
            TwilioMessage.direction == "inbound",
            TwilioMessage.twilio_sid.isnot(None),
            TwilioMessage.twilio_sid != "",
        )
        .distinct()
        .all()
    )
    out = set()
    for (from_number,) in rows:
        d = _digits(from_number)
        if len(d) >= 10:
            out.add(d[-10:])
    return out


def has_conversational_evidence(company_id: int, contact: Contact, *, evidence_digits: set[str] | None = None) -> bool:
    d = _digits(contact.normalized_phone or contact.phone)
    if len(d) < 10:
        return False
    key = d[-10:]
    if evidence_digits is not None:
        return key in evidence_digits
    return key in conversational_evidence_phone_digits(company_id)


def classify(company_id: int, contact: Contact, *, evidence_digits: set[str] | None = None,
            customer_relationship: bool | None = None) -> dict:
    supp = is_suppressed(contact)
    conv = (not supp) and has_conversational_evidence(company_id, contact, evidence_digits=evidence_digits)
    promo = (not supp) and has_promotional_optin(contact)
    # MVP: LUXit has no separate transactional consent record yet, so a
    # verified conversational relationship is the transactional basis.
    trans = conv
    return {
        "customer_relationship": customer_relationship,
        "conversational_sms_evidence": conv,
        "transactional_eligibility": trans,
        "promotional_eligibility": promo,
        "suppressed": supp,
    }


def is_eligible_for_purpose(purpose: str, classification: dict) -> bool:
    if classification["suppressed"]:
        return False
    return {
        "promotional": classification["promotional_eligibility"],
        "transactional": classification["transactional_eligibility"],
        "conversational": classification["conversational_sms_evidence"],
    }.get(purpose, False)
