"""Communications/phone-number permission helpers.

These helpers are intentionally additive: owners/admins keep existing access,
while explicit per-number grants narrow standard-user PWA history to the lines
assigned to them.
"""

from __future__ import annotations

from typing import Iterable

from extensions import db


def normalize_role(role: str | None) -> str:
    value = (role or "viewer").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "superadmin": "owner",
        "super_admin": "owner",
        "super": "owner",
        "administrator": "admin",
        "tenant_admin": "admin",
        "company_admin": "admin",
        "supervisor": "manager",
        "member": "staff",
        "user": "viewer",
        "inboxonly": "inbox_only",
        "inbox_only_user": "inbox_only",
    }
    return aliases.get(value, value)


def user_access_for_company(user, company_id: int):
    from models import UserCompanyAccess
    if not user or not company_id:
        return None
    return UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company_id).first()


def can_manage_users(user, company_id: int) -> bool:
    if not user or not company_id:
        return False
    if getattr(user, "is_admin", False):
        return True
    acc = user_access_for_company(user, company_id)
    return bool(acc and acc.can_manage_users())


def accessible_phone_numbers(user, company_id: int) -> list[str]:
    """Return E.164/business numbers a user may access, or all tenant numbers.

    Backward compatibility rule: users with no explicit per-number grants and no
    legacy assigned_number keep company-wide access if their role grants PWA/hub
    access. Once explicit grants or assigned_number are present, history is
    filtered to those numbers.
    """
    from models import PhoneNumberUserPermission, TwilioAccount, TwilioPhoneNumber

    if not user or not company_id:
        return []
    acc = user_access_for_company(user, company_id)
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"

    all_numbers = [
        pn.phone_number for pn in TwilioPhoneNumber.query.filter_by(company_id=company_id, is_active=True).all()
        if pn.phone_number
    ]
    for ta in TwilioAccount.query.filter_by(company_id=company_id).all():
        if ta.from_phone and ta.from_phone not in all_numbers:
            all_numbers.append(ta.from_phone)

    if getattr(user, "is_admin", False) or role in {"owner", "admin"}:
        return all_numbers

    explicit = [
        p.phone_number.phone_number for p in PhoneNumberUserPermission.query
        .join(TwilioPhoneNumber, PhoneNumberUserPermission.phone_number_id == TwilioPhoneNumber.id)
        .filter(
            PhoneNumberUserPermission.company_id == company_id,
            PhoneNumberUserPermission.user_id == user.id,
            PhoneNumberUserPermission.can_access_pwa.is_(True),
            TwilioPhoneNumber.is_active.is_(True),
        ).all()
        if p.phone_number and p.phone_number.phone_number
    ]
    if explicit:
        return list(dict.fromkeys(explicit))

    assigned = getattr(acc, "assigned_number", None) if acc else None
    if assigned:
        return [assigned]

    # Mobile-inbox-only staff must be assigned an explicit line; otherwise the
    # PWA can render a clear no-number state instead of silently exposing every
    # tenant conversation or looping on empty API results. Full Communications
    # Hub users keep the legacy company-wide fallback.
    if acc and acc.has_comms_hub_access():
        return all_numbers
    return []


def filter_conversations_for_user(query, user, company_id: int):
    from models import TwilioConversation
    numbers = accessible_phone_numbers(user, company_id)
    if not numbers:
        return query.filter(db.text("1=0"))
    return query.filter(TwilioConversation.to_number.in_(numbers))


def filter_calls_for_user(query, user, company_id: int):
    from models import TwilioCallLog
    acc = user_access_for_company(user, company_id)
    role = normalize_role(getattr(acc, "role", None)) if acc else "viewer"
    if getattr(user, "is_admin", False) or role in {"owner", "admin"}:
        return query
    numbers = accessible_phone_numbers(user, company_id)
    if not numbers:
        return query.filter(db.text("1=0"))
    return query.filter(db.or_(TwilioCallLog.to_number.in_(numbers), TwilioCallLog.from_number.in_(numbers)))
