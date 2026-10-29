"""Second-number management for LUX Connect.

A LUX Connect number is an existing ``TwilioPhoneNumber`` owned by the tenant.
A user gets it through ``PhoneNumberUserPermission`` exactly as on the web
Communications hub (``/twilio/comms/numbers/<id>/permissions``); this module is
the JSON equivalent for the app. It never buys, releases or ports a number:
those stay manual, out-of-band operations (see docs/lux-connect-native).
"""
from __future__ import annotations

from extensions import db

PERMISSION_FIELDS = ("can_access_pwa", "can_view_sms", "can_send_sms", "can_view_calls",
                     "can_call", "can_view_voicemail")

# Statuses that mean application-originated SMS must not be attempted from the
# number yet. "unknown" (or no row) never blocks: existing behaviour is kept
# until a registration check has actually run.
BLOCKING_MESSAGING_STATUSES = {"not_registered", "toll_free_pending", "pending"}


class NumberError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def messaging_status(phone_number_id: int | None) -> dict:
    from models import PhoneNumberMessagingRegistration
    if not phone_number_id:
        return {"status": "unknown", "checked_at": None, "detail": None}
    row = PhoneNumberMessagingRegistration.query.filter_by(phone_number_id=phone_number_id).first()
    if not row:
        return {"status": "unknown", "checked_at": None, "detail": None}
    return {"status": row.status, "checked_at": row.checked_at.isoformat() if row.checked_at else None,
            "detail": row.detail}


def messaging_blocked(phone_number_id: int | None) -> dict | None:
    st = messaging_status(phone_number_id)
    if st["status"] in BLOCKING_MESSAGING_STATUSES:
        return st
    return None


def numbers_for_user(user, company_id: int) -> list[dict]:
    """The user's LUX Connect numbers with what they may do on each."""
    from models import PhoneNumberUserPermission, TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers

    allowed = set(accessible_phone_numbers(user, company_id))
    rows = []
    for pn in (TwilioPhoneNumber.query
               .filter(TwilioPhoneNumber.company_id == company_id, TwilioPhoneNumber.is_active.is_(True))
               .order_by(TwilioPhoneNumber.id).all()):
        if pn.phone_number not in allowed:
            continue
        perm = PhoneNumberUserPermission.query.filter_by(phone_number_id=pn.id, user_id=user.id).first()
        rows.append({
            "id": pn.id,
            "phone_number": pn.phone_number,
            "friendly_name": pn.friendly_name or pn.phone_number,
            "voice_enabled": bool(pn.voice_enabled),
            "can_call": bool(perm.can_call) if perm else True,
            "can_send_sms": bool(perm.can_send_sms) if perm else True,
            "messaging": messaging_status(pn.id),
        })
    return rows


def company_numbers(company_id: int) -> list[dict]:
    from models import PhoneNumberUserPermission, TwilioPhoneNumber, User
    out = []
    for pn in (TwilioPhoneNumber.query.filter_by(company_id=company_id)
               .order_by(TwilioPhoneNumber.id).all()):
        assignments = []
        for perm in PhoneNumberUserPermission.query.filter_by(company_id=company_id, phone_number_id=pn.id).all():
            u = db.session.get(User, perm.user_id)
            assignments.append({
                "user_id": perm.user_id,
                "name": (getattr(u, "full_name", None) or getattr(u, "username", None) or getattr(u, "email", None)) if u else None,
                **{f: bool(getattr(perm, f, False)) for f in PERMISSION_FIELDS},
            })
        out.append({
            "id": pn.id, "phone_number": pn.phone_number,
            "friendly_name": pn.friendly_name or pn.phone_number,
            "is_active": bool(pn.is_active), "voice_enabled": bool(pn.voice_enabled),
            "messaging": messaging_status(pn.id),
            "assignments": assignments,
        })
    return out


def _require_admin(actor, company_id: int):
    from services.comms_permissions import can_manage_users, normalize_role, user_access_for_company
    acc = user_access_for_company(actor, company_id)
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    if not (can_manage_users(actor, company_id) or role in {"owner", "admin"}):
        raise NumberError("forbidden", "Only an administrator can manage numbers.", 403)


def assign_number(actor, company_id: int, number_id: int, user_id: int, permissions: dict) -> dict:
    """Give a tenant user access to one of the tenant's existing numbers."""
    from models import PhoneNumberUserPermission, TwilioPhoneNumber, UserCompanyAccess
    _require_admin(actor, company_id)
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company_id).first()
    if not pn:
        raise NumberError("number_not_found", "That number does not belong to this company.", 404)
    if not pn.is_active:
        raise NumberError("number_inactive", "That number is not active.", 409)
    target = UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).first()
    if not target or not bool(getattr(target, "is_active", True)):
        raise NumberError("user_not_found", "That person is not an active member of this company.", 404)
    perm = PhoneNumberUserPermission.query.filter_by(phone_number_id=pn.id, user_id=user_id).first()
    if not perm:
        perm = PhoneNumberUserPermission(company_id=company_id, phone_number_id=pn.id, user_id=user_id)
        db.session.add(perm)
    defaults = {f: True for f in PERMISSION_FIELDS}
    for field in PERMISSION_FIELDS:
        value = permissions.get(field, getattr(perm, field, None))
        setattr(perm, field, bool(defaults[field] if value is None else value))
    if perm.can_access_pwa:
        # Same side effects as the web hub so both surfaces agree.
        target.assigned_number = pn.phone_number
        target.pwa_access_enabled = True
        target.comms_hub_enabled = True
    db.session.commit()
    return {"phone_number_id": pn.id, "user_id": user_id, **{f: bool(getattr(perm, f)) for f in PERMISSION_FIELDS}}


def unassign_number(actor, company_id: int, number_id: int, user_id: int) -> None:
    """Remove a user's access. The number itself is never released."""
    from models import PhoneNumberUserPermission, TwilioPhoneNumber
    _require_admin(actor, company_id)
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=company_id).first()
    if not pn:
        raise NumberError("number_not_found", "That number does not belong to this company.", 404)
    perm = PhoneNumberUserPermission.query.filter_by(phone_number_id=pn.id, user_id=user_id, company_id=company_id).first()
    if not perm:
        raise NumberError("not_assigned", "That person is not assigned to this number.", 404)
    db.session.delete(perm)
    db.session.commit()
