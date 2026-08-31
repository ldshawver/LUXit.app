"""Communication Availability — shared-line real-time participation.

'available' | 'away'. AWAY is a *communication availability* state, scoped to
(user, company). It pauses the user's live participation in the tenant's shared
communications — inbound-call ringing / call UI / Voice token, AND new
SMS/chat notifications, sounds, badges and realtime conversation attention
events. It is NOT an account disable: it never touches is_active / role /
membership / SMS consent / the business number, and never affects other users
on the line, and it never suppresses inbound persistence, canonical contact
resolution, MessageSid idempotency, STOP/compliance or automation.

The user-facing label may still read "Phone Availability" on the compact phone
UI; the persisted column names (`phone_availability*`) are retained — the state
is one authoritative per-(user, company) value regardless of channel.

Server-authoritative. Effective state = the single user_company_access row;
provenance (changed_at / changed_by / source) is recorded so a stale browser
cannot silently override a newer admin decision (last write wins by timestamp,
and clients re-fetch on the SSE event). A user going Away applies to ALL of
that user's devices (the state is not device-scoped).
"""
from __future__ import annotations

from datetime import datetime

from extensions import db
from models import User, UserCompanyAccess
from services.comms_permissions import can_manage_users, normalize_role

AVAILABLE = "available"
AWAY = "away"
_STATES = {AVAILABLE, AWAY}


class AvailabilityError(ValueError):
    pass


def normalize_state(value) -> str:
    v = str(value or "").strip().lower()
    if v in _STATES:
        return v
    raise AvailabilityError(f"phone availability must be one of {sorted(_STATES)}")


def _access(user_id: int, company_id: int) -> UserCompanyAccess | None:
    return UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).first()


def _payload(acc: UserCompanyAccess, user_id: int) -> dict:
    state = getattr(acc, "phone_availability", None) or AVAILABLE if acc else AVAILABLE
    changed_at = getattr(acc, "phone_availability_changed_at", None) if acc else None
    return {
        "user_id": user_id,
        "company_id": getattr(acc, "company_id", None) if acc else None,
        "state": state,
        "available": state == AVAILABLE,
        "source": getattr(acc, "phone_availability_source", None) if acc else None,
        "changed_by_user_id": getattr(acc, "phone_availability_changed_by_user_id", None) if acc else None,
        "changed_at": changed_at.isoformat() if changed_at else None,
    }


def get_availability(user_id: int, company_id: int) -> dict:
    return _payload(_access(user_id, company_id), user_id)


def is_available(user_id: int, company_id: int) -> bool:
    acc = _access(user_id, company_id)
    # No membership row, or unset -> treated as available (default, safe for
    # existing users and never a lock-out).
    return (getattr(acc, "phone_availability", AVAILABLE) or AVAILABLE) == AVAILABLE if acc else True


def available_user_ids(company_id: int) -> set[int]:
    """User ids on this company whose phone availability is 'available'
    (a missing/NULL value counts as available)."""
    rows = (
        db.session.query(UserCompanyAccess.user_id, UserCompanyAccess.phone_availability)
        .filter(UserCompanyAccess.company_id == company_id,
                UserCompanyAccess.is_active.is_(True))
        .all()
    )
    return {uid for uid, state in rows if (state or AVAILABLE) == AVAILABLE}


def set_availability(user_id: int, company_id: int, state, *, actor_user_id: int, source: str) -> dict:
    state = normalize_state(state)
    if source not in ("user", "admin"):
        raise AvailabilityError("source must be 'user' or 'admin'")
    acc = _access(user_id, company_id)
    if not acc:
        raise AvailabilityError("user is not a member of this company")
    acc.phone_availability = state
    acc.phone_availability_changed_at = datetime.utcnow()
    acc.phone_availability_changed_by_user_id = actor_user_id
    acc.phone_availability_source = source
    db.session.flush()
    return _payload(acc, user_id)


def set_all_availability(company_id: int, state, *, actor_user_id: int,
                         source: str = "admin") -> list[dict]:
    """Bulk-set every active member of a company to `state`.

    Used by the admin 'Set All Available' / 'Set All Away' controls. Returns the
    updated payload for each affected user (so the caller can broadcast one SSE
    event per user for multi-device convergence). Never touches membership /
    role / consent — same invariants as set_availability().
    """
    state = normalize_state(state)
    if source not in ("user", "admin"):
        raise AvailabilityError("source must be 'user' or 'admin'")
    rows = (
        UserCompanyAccess.query
        .filter_by(company_id=company_id)
        .filter(UserCompanyAccess.is_active.is_(True))
        .all()
    )
    now = datetime.utcnow()
    out = []
    for acc in rows:
        acc.phone_availability = state
        acc.phone_availability_changed_at = now
        acc.phone_availability_changed_by_user_id = actor_user_id
        acc.phone_availability_source = source
        out.append(_payload(acc, acc.user_id))
    db.session.flush()
    return out


def can_admin_manage(actor, company_id: int) -> bool:
    """An actor may set another user's availability iff they are a same-tenant
    owner/admin or have manage-users for that company. Platform admins qualify;
    cross-tenant actors never do (the company_id gate is server-side)."""
    if getattr(actor, "is_admin", False):
        acc = _access(actor.id, company_id)
        if acc is not None:
            return True
    return can_manage_users(actor, company_id)


def team_availability(company_id: int) -> list[dict]:
    rows = (
        UserCompanyAccess.query
        .filter_by(company_id=company_id)
        .filter(UserCompanyAccess.is_active.is_(True))
        .all()
    )
    out = []
    for acc in rows:
        user = db.session.get(User, acc.user_id)
        if not user or not getattr(user, "active", True):
            continue
        p = _payload(acc, acc.user_id)
        p["name"] = (getattr(user, "username", None) or getattr(user, "email", None) or f"User {acc.user_id}")
        p["role"] = normalize_role(getattr(acc, "role", None))
        changed_by = p.get("changed_by_user_id")
        if changed_by:
            actor = db.session.get(User, changed_by)
            p["changed_by_name"] = (
                (getattr(actor, "username", None) or getattr(actor, "email", None)
                 or f"User {changed_by}") if actor else f"User {changed_by}"
            )
        else:
            p["changed_by_name"] = None
        out.append(p)
    out.sort(key=lambda r: (r["state"] != AVAILABLE, r["name"].lower()))
    return out
