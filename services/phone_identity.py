"""Tenant-scoped, non-guessable Twilio Voice SDK identities."""

from __future__ import annotations

import hashlib
import hmac
import re
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


# Native app (LUX Connect for iOS/Android). One identity per installed device so
# a revoked or signed-out handset can be dropped from inbound <Dial> without
# touching the user's other devices, and so outbound calls can be traced back
# to the exact device. The device row id is embedded for lookup; the HMAC
# covers the per-install uid, which never leaves the server/app pair.
_MOBILE_IDENTITY_RE = re.compile(r"^luxit_c(\d+)_u(\d+)_d(\d+)_([0-9a-f]{16})$")


def mobile_voice_identity(company_id: int, user_id: int, device_id: int, device_uid: str) -> str:
    scope = f"{int(company_id)}:{int(user_id)}:mobile:{device_uid}"
    digest = hmac.new(_identity_secret().encode("utf-8"), scope.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"luxit_c{int(company_id)}_u{int(user_id)}_d{int(device_id)}_{digest}"


def parse_mobile_voice_identity(identity: str):
    """Return (company_id, user_id, device_id) for a well-formed native identity, else None.

    Shape only — callers must still verify the HMAC against the device row with
    mobile_voice_identity().
    """
    m = _MOBILE_IDENTITY_RE.match(identity or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))
