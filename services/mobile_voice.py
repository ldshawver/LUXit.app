"""Voice routing for LUX Connect native devices.

Inbound: ring_pwa already decides WHO is eligible for a call (Receive Calls ON
and Available). This module adds each eligible user's native handsets to the
same <Dial>, so the PWA and every phone ring together. Twilio rings all
<Client> legs of one <Dial> at once and cancels the others the moment one
answers; the SDK turns that cancel into "stop ringing" on every other device.

A handset is dialed only when it is active, has ringing enabled, has reported
a successful Twilio push registration, still holds a live session (not
revoked, refresh not expired), and its user may take calls on the dialed
number. A signed-out, revoked or lapsed handset is never dialed even if a
stale Twilio binding still exists for it: only an explicit sign-out clears the
registration, so a phone whose sessions expired or were revoked in bulk would
otherwise keep ringing, and answering would connect a signed-out phone.

Outbound: native identities embed the device row id and an HMAC over the
per-install uid, so the TwiML App webhook can verify the exact device.
"""
from __future__ import annotations

import logging

from extensions import db

logger = logging.getLogger(__name__)

# Twilio allows at most 10 <Client> nouns in one <Dial>.
MAX_DIAL_CLIENTS = 10


def user_may_call_on(user, company_id: int, phone_number: str | None) -> bool:
    """Same number rules as the Voice token gate, for one specific number."""
    from models import PhoneNumberUserPermission, TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers

    allowed = accessible_phone_numbers(user, company_id)
    if not allowed or (phone_number and phone_number not in allowed):
        return False
    explicit = PhoneNumberUserPermission.query.filter_by(company_id=company_id, user_id=user.id).count() > 0
    if not explicit:
        return True
    q = (PhoneNumberUserPermission.query
         .join(TwilioPhoneNumber, PhoneNumberUserPermission.phone_number_id == TwilioPhoneNumber.id)
         .filter(PhoneNumberUserPermission.company_id == company_id,
                 PhoneNumberUserPermission.user_id == user.id,
                 PhoneNumberUserPermission.can_access_pwa.is_(True),
                 PhoneNumberUserPermission.can_call.is_(True),
                 TwilioPhoneNumber.is_active.is_(True)))
    if phone_number:
        q = q.filter(TwilioPhoneNumber.phone_number == phone_number)
    return q.count() > 0


def ringable_devices(company_id: int, eligible_user_ids, phone_number: str | None):
    from datetime import datetime

    from models import MobileDevice, MobileSession, User
    from services.phone_identity import mobile_voice_identity

    ids = sorted(u for u in (eligible_user_ids or ()) if u is not None)
    if not ids:
        return []
    rows = (MobileDevice.query
            .filter(MobileDevice.company_id == company_id,
                    MobileDevice.user_id.in_(ids),
                    MobileDevice.status == "active",
                    MobileDevice.ring_enabled.is_(True),
                    MobileDevice.voice_registered_at.isnot(None),
                    MobileSession.query.filter(
                        MobileSession.mobile_device_id == MobileDevice.id,
                        MobileSession.revoked_at.is_(None),
                        MobileSession.refresh_expires_at > datetime.utcnow(),
                    ).exists())
            .order_by(MobileDevice.last_seen_at.desc())
            .all())
    allowed_users: dict[int, bool] = {}
    revoked_since: dict[int, datetime | None] = {}
    out = []
    for device in rows:
        if device.user_id not in allowed_users:
            user = db.session.get(User, device.user_id)
            allowed_users[device.user_id] = bool(
                user and getattr(user, "active", True) and user_may_call_on(user, company_id, phone_number)
            )
            revoked_since[device.user_id] = getattr(user, "session_revoked_at", None)
        # A password reset/change ends every older session without stamping
        # each row; only a session signed in since then keeps the phone live.
        revoked = revoked_since[device.user_id]
        if revoked and not MobileSession.query.filter(
                MobileSession.mobile_device_id == device.id,
                MobileSession.revoked_at.is_(None),
                MobileSession.refresh_expires_at > datetime.utcnow(),
                MobileSession.created_at >= revoked).first():
            continue
        if allowed_users[device.user_id]:
            out.append((device, mobile_voice_identity(company_id, device.user_id, device.id, device.device_uid)))
    return out


def merge_dial_identities(existing: list[str], company_id: int, eligible_user_ids, phone_number: str | None) -> list[str]:
    """Append eligible native identities to the PWA identities, capped for Twilio."""
    merged = list(existing)
    try:
        for _device, identity in ringable_devices(company_id, eligible_user_ids, phone_number):
            if identity not in merged:
                merged.append(identity)
    except Exception:
        logger.exception("Unable to resolve native ring targets", extra={"company_id": company_id})
        return list(existing)
    if len(merged) > MAX_DIAL_CLIENTS:
        logger.warning("Voice inbound: %d ring targets, Twilio limit %d; dropping the least recently seen",
                       len(merged), MAX_DIAL_CLIENTS)
        merged = merged[:MAX_DIAL_CLIENTS]
    return merged


def verify_outbound_identity(identity: str):
    """Resolve a native identity from a TwiML App webhook.

    Returns ``(company_id, user_id, device)`` when the identity is a well-formed
    native identity whose HMAC matches an active device, ``None`` when it is not
    a native identity at all, and raises ``PermissionError`` when it looks like
    one but fails verification.
    """
    from models import MobileDevice
    from services.phone_identity import mobile_voice_identity, parse_mobile_voice_identity

    parsed = parse_mobile_voice_identity(identity)
    if not parsed:
        return None
    company_id, user_id, device_id = parsed
    device = db.session.get(MobileDevice, device_id)
    if (not device or device.company_id != company_id or device.user_id != user_id
            or device.status != "active"
            or mobile_voice_identity(company_id, user_id, device.id, device.device_uid) != identity):
        raise PermissionError("native identity not verified")
    return company_id, user_id, device
