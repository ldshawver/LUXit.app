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


def pwa_voice_identity(company_id: int, user_id: int | None = None, device_key: str | None = None) -> str:
    """Return a stable, tenant-scoped Twilio Client identity.

    Twilio Voice identities are visible to the browser, so the company id alone
    is not used as the identity.  The short HMAC suffix prevents tenants from
    guessing another tenant's registered Client identity while staying under
    Twilio's identity length limits.

    Three forms, one algorithm:
      * (company, user, device_key) -> device-scoped identity. Used when the
        tenant requires approved PWA devices: one identity per approved device.
      * (company, user)             -> per-user, non-device identity. Used when
        device approval is NOT required: the user's Twilio.Device and the
        inbound <Client> both target this, independent of any device_key.
      * (company,)                  -> company-scoped identity. Legacy shared
        fallback for tenants with no eligible per-user identity.
    """
    if user_id is not None and device_key:
        scope = f"{int(company_id)}:{int(user_id)}:{device_key}"
    elif user_id is not None:
        scope = f"{int(company_id)}:{int(user_id)}"
    else:
        scope = str(int(company_id))
    digest = hmac.new(_identity_secret().encode("utf-8"), scope.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    if user_id is not None:
        return f"luxit_c{int(company_id)}_u{int(user_id)}_{digest}"
    return f"luxit_c{int(company_id)}_{digest}"
