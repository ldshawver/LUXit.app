"""Tenant-scoped user archive/restore helpers."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import union_all

from extensions import db
from models import Company, IntegrationAuditLog, PhoneNumberUserPermission, PushSubscription, User, UserCompanyAccess

OWNER_ROLES = {UserCompanyAccess.ROLE_OWNER, UserCompanyAccess.ROLE_ADMIN}


def _active_user_ids_for_company(company_id: int):
    via_default = db.session.query(User.id).filter(User.default_company_id == company_id, User.active == True)
    via_access = (
        db.session.query(UserCompanyAccess.user_id.label("id"))
        .join(User, User.id == UserCompanyAccess.user_id)
        .filter(UserCompanyAccess.company_id == company_id, UserCompanyAccess.is_active == True, User.active == True)
    )
    return {r[0] for r in via_default.all()} | {r[0] for r in via_access.all()}


def active_team_member_count(company_id: int) -> int:
    return len(_active_user_ids_for_company(company_id))


def active_owner_user_ids(company_id: int) -> set[int]:
    ids = {
        r[0]
        for r in db.session.query(User.id)
        .filter(User.default_company_id == company_id, User.active == True, User.is_admin == True)
        .all()
    }
    ids |= {
        r[0]
        for r in db.session.query(UserCompanyAccess.user_id)
        .join(User, User.id == UserCompanyAccess.user_id)
        .filter(
            UserCompanyAccess.company_id == company_id,
            UserCompanyAccess.is_active == True,
            User.active == True,
            UserCompanyAccess.role.in_(OWNER_ROLES),
        )
        .all()
    }
    return ids


def restoration_eligible(user: User, company_id: int) -> tuple[bool, str]:
    company = db.session.get(Company, company_id)
    if not company or not company.is_active:
        return False, "Company is inactive or missing"
    if not active_owner_user_ids(company_id):
        return False, "Company must have an active owner/admin before restoration"
    existing = User.query.filter(User.email == user.email, User.id != user.id, User.active == True).first()
    if existing:
        return False, "Another active user already has this email"
    max_members = getattr(company, "max_team_members", None)
    if max_members is not None and active_team_member_count(company_id) >= max_members:
        return False, "Company seat limit is reached"
    return True, "Eligible"


def archive_user_for_company(user: User, company_id: int, actor: User) -> None:
    if user.id == actor.id:
        raise ValueError("Cannot archive your own account")
    if getattr(user, "is_admin", False):
        raise ValueError("Platform administrators cannot be archived from tenant management")
    belongs_to_company = user.default_company_id == company_id or UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company_id).first() is not None
    if not belongs_to_company:
        raise ValueError("User is not a member of this company")
    owners_after = active_owner_user_ids(company_id) - {user.id}
    if not owners_after:
        raise ValueError("Cannot archive the final active owner/admin for this company")
    now = datetime.utcnow()
    accesses = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company_id, is_active=True).all()
    had_default = user.default_company_id == company_id
    for acc in accesses:
        acc.previous_role = acc.role
        acc.is_active = False
        acc.archived_at = now
        acc.archived_by_user_id = actor.id
        acc.can_access_mobile_inbox = False
        acc.can_access_full_app = False
        acc.comms_hub_enabled = False
        acc.pwa_access_enabled = False
        acc.manage_users_enabled = False
        acc.communications_license = False
    PhoneNumberUserPermission.query.filter_by(company_id=company_id, user_id=user.id).update({
        "can_access_pwa": False,
        "can_view_sms": False,
        "can_send_sms": False,
        "can_view_calls": False,
        "can_call": False,
        "can_view_voicemail": False,
        "can_manage_number": False,
        "can_send_campaigns": False,
    })
    PushSubscription.query.filter_by(company_id=company_id, user_id=user.id, is_active=True).update({"is_active": False})
    user.active = False
    user.archived_at = now
    user.archived_by_user_id = actor.id
    user.archived_company_id = company_id
    user.session_revoked_at = now
    if had_default:
        user.default_company_id = None
    db.session.add(IntegrationAuditLog(company_id=company_id, service_slug="users", action="archive", user_id=actor.id, changes={"target_user_id": user.id, "access_rows": len(accesses)}))


def restore_user_for_company(user: User, company_id: int, actor: User) -> None:
    ok, reason = restoration_eligible(user, company_id)
    if not ok:
        raise ValueError(reason)
    access = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company_id).first()
    if not access:
        raise ValueError("User has no archived access row for this company")
    access.is_active = True
    access.archived_at = None
    access.archived_by_user_id = None
    access.role = access.previous_role or access.role or UserCompanyAccess.ROLE_VIEWER
    access.previous_role = None
    access.can_access_full_app = True
    user.active = True
    user.archived_at = None
    user.archived_by_user_id = None
    user.archived_company_id = None
    user.default_company_id = company_id
    db.session.add(IntegrationAuditLog(company_id=company_id, service_slug="users", action="restore", user_id=actor.id, changes={"target_user_id": user.id, "company_id": company_id}))
