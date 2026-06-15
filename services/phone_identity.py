"""Tenant-scoped, non-guessable Twilio Voice SDK identities."""

from __future__ import annotations

import hashlib
import hmac
import os


def _identity_secret() -> str:
    return (
        os.environ.get("PWA_VOICE_IDENTITY_SECRET")
        or os.environ.get("SESSION_SECRET")
        or os.environ.get("SECRET_KEY")
        or os.environ.get("TWILIO_API_SECRET")
        or "luxit-dev-voice-identity"
    )


def pwa_voice_identity(company_id: int) -> str:
    """Return a stable, tenant-scoped Twilio Client identity.

    Twilio Voice identities are visible to the browser, so the company id alone
    is not used as the identity.  The short HMAC suffix prevents tenants from
    guessing another tenant's registered Client identity while staying under
    Twilio's identity length limits.
    """
    raw = str(int(company_id)).encode("utf-8")
    digest = hmac.new(_identity_secret().encode("utf-8"), raw, hashlib.sha256).hexdigest()[:16]
    return f"luxit_c{int(company_id)}_{digest}"
