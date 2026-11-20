"""Team invitations (T-5): a workspace manager invites by email, the invitee
sets their own password (or signs in) and joins with the invited role.

Tokens are single-use, expire after INVITE_TTL, and only their SHA-256 is
stored. Seats are checked when inviting (members + pending invites) and again
when accepting.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from extensions import db

INVITE_TTL = timedelta(days=7)
INVITABLE_ROLES = ("admin", "manager", "editor", "viewer", "staff")
DEFAULT_ROLE = "viewer"
_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvitationError(ValueError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_email(email) -> str:
    return (email or "").strip().lower()


def pending_invitations(company_id):
    from models import TeamInvitation
    now = datetime.utcnow()
    return (
        TeamInvitation.query
        .filter(TeamInvitation.company_id == company_id, TeamInvitation.accepted_at.is_(None),
                TeamInvitation.revoked_at.is_(None), TeamInvitation.expires_at > now)
        .order_by(TeamInvitation.created_at.desc())
        .all()
    )


def _seats_left(company) -> int | None:
    if company.max_team_members is None:
        return None
    return company.max_team_members - company.team_member_count - len(pending_invitations(company.id))


def _role_level(role) -> int:
    from models import UserCompanyAccess
    return UserCompanyAccess.ROLE_HIERARCHY.get(UserCompanyAccess.normalize_role(role), 0)


def can_invite(user, company_id) -> bool:
    from services.comms_permissions import can_manage_users
    return can_manage_users(user, company_id)


def create_invitation(company, inviter, email, role=DEFAULT_ROLE):
    """Create a pending invitation. Returns ``(invitation, raw_token)``."""
    from models import TeamInvitation, User, UserCompanyAccess
    from services.comms_permissions import user_access_for_company

    email = _normalize_email(email)
    role = (role or DEFAULT_ROLE).strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 120:
        raise InvitationError("Enter a valid email address.")
    if role not in INVITABLE_ROLES:
        raise InvitationError("Choose a valid role.")
    if not getattr(inviter, "is_admin", False):
        access = user_access_for_company(inviter, company.id)
        if not access or _role_level(role) > _role_level(access.role):
            raise InvitationError("You can't invite someone with a higher role than your own.")

    member = (
        db.session.query(UserCompanyAccess.id)
        .join(User, User.id == UserCompanyAccess.user_id)
        .filter(UserCompanyAccess.company_id == company.id, UserCompanyAccess.is_active.is_(True),
                db.func.lower(User.email) == email)
        .first()
    )
    if member:
        raise InvitationError("That person is already a member of this workspace.")

    # Re-inviting replaces the earlier pending invite for the same address.
    for old in pending_invitations(company.id):
        if old.email == email:
            old.revoked_at = datetime.utcnow()
    db.session.flush()

    seats = _seats_left(company)
    if seats is not None and seats <= 0:
        raise InvitationError(
            f"Team seat limit reached ({company.max_team_members}). Upgrade your plan to invite more people."
        )

    token = secrets.token_urlsafe(32)
    invitation = TeamInvitation(
        company_id=company.id, email=email, role=role, token_hash=_hash(token),
        invited_by_user_id=getattr(inviter, "id", None),
        expires_at=datetime.utcnow() + INVITE_TTL,
    )
    db.session.add(invitation)
    db.session.flush()
    return invitation, token


def find_by_token(token):
    """Return ``(invitation, state)``; state is pending | expired | used | invalid."""
    from models import TeamInvitation
    if not token or len(token) > 200:
        return None, "invalid"
    invitation = TeamInvitation.query.filter_by(token_hash=_hash(token)).first()
    if not invitation or invitation.revoked_at:
        return None, "invalid"
    if invitation.accepted_at:
        return invitation, "used"
    if invitation.expires_at <= datetime.utcnow():
        return invitation, "expired"
    return invitation, "pending"


def accept(invitation, user):
    """Join ``user`` to the invitation's workspace with the invited role."""
    from models import Company, UserCompanyAccess

    if _normalize_email(user.email) != invitation.email:
        raise InvitationError("This invitation was sent to a different email address.")
    company = db.session.get(Company, invitation.company_id)
    if not company or not company.is_active:
        raise InvitationError("This workspace is no longer available.")

    access = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company.id).first()
    already_member = bool(access and access.is_active)
    if not already_member and company.max_team_members is not None \
            and company.team_member_count >= company.max_team_members:
        raise InvitationError("This workspace has no free seats. Ask its owner to upgrade the plan.")

    if access is None:
        access = UserCompanyAccess(user_id=user.id, company_id=company.id)
        db.session.add(access)
    if not already_member:
        access.role = invitation.role
        access.is_active = True
        access.archived_at = None
        access.can_access_full_app = True
        access.can_access_mobile_inbox = False
    if not user.default_company_id:
        user.default_company_id = company.id
    invitation.accepted_at = datetime.utcnow()
    invitation.accepted_user_id = user.id
    db.session.flush()
    return access


def revoke(company_id, invitation_id):
    from models import TeamInvitation
    invitation = TeamInvitation.query.filter_by(id=invitation_id, company_id=company_id).first()
    if not invitation or invitation.accepted_at:
        return False
    invitation.revoked_at = invitation.revoked_at or datetime.utcnow()
    return True
