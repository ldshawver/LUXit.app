"""Receive Calls -- per-(user, company) inbound-call routing preference.

TRUE  = route this tenant's shared LUXit calls to the user's eligible PWA
        devices. An eligible active PWA registers its Twilio.Device
        automatically once calling prerequisites are ready, over any usable
        internet connection (Wi-Fi or cellular data -- the OS/browser chooses).
FALSE = the user's PWA does not register for / present inbound calls and
        server-side inbound routing (twilio_sms.voice_inbound ring_pwa) excludes
        the user. Voicemail / no-answer fallback and inbound persistence are
        unaffected.

Independent of phone_availability:
    receive_calls TRUE  + AVAILABLE -> eligible for inbound call routing
    receive_calls TRUE  + AWAY      -> configured, temporarily excluded from
                                       shared-call attention (availability wins)
    receive_calls FALSE + AVAILABLE -> no PWA calls; still gets SMS/chat attention
    receive_calls FALSE + AWAY      -> no PWA calls, no shared attention

Setting one never mutates the other. Server-authoritative; a change broadcasts
an SSE `receive_calls` event so the user's other active PWA instances converge
(register / unregister) without a reload.

Eligibility for inbound routing is the AND of:
    active tenant membership (user_company_access.is_active, user.active)
  * an approved, active PWADevice for the user
  * receive_calls = TRUE
  * phone_availability = 'available'
  * the existing per-number / security checks
"""
from __future__ import annotations

from datetime import datetime

from extensions import db
from models import User, UserCompanyAccess
from services.comms_permissions import can_manage_users, normalize_role

ON = True
OFF = False
_SOURCES = {"user", "admin", "system"}


class ReceiveCallsError(ValueError):
    pass


def normalize_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value if value is not None else "").strip().lower()
    if v in ("1", "true", "on", "yes", "enabled", "enable"):
        return True
    if v in ("0", "false", "off", "no", "disabled", "disable"):
        return False
    raise ReceiveCallsError("receive_calls must be a boolean")


def _access(user_id: int, company_id: int) -> UserCompanyAccess | None:
    return UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).first()


def _payload(acc: UserCompanyAccess | None, user_id: int) -> dict:
    enabled = bool(getattr(acc, "receive_calls", True)) if acc else True
    changed_at = getattr(acc, "receive_calls_changed_at", None) if acc else None
    return {
        "user_id": user_id,
        "company_id": getattr(acc, "company_id", None) if acc else None,
        "receive_calls": enabled,
        "enabled": enabled,
        "source": getattr(acc, "receive_calls_source", None) if acc else None,
        "changed_by_user_id": getattr(acc, "receive_calls_changed_by_user_id", None) if acc else None,
        "changed_at": changed_at.isoformat() if changed_at else None,
    }


def get_receive_calls(user_id: int, company_id: int) -> dict:
    return _payload(_access(user_id, company_id), user_id)


def receive_calls_enabled(user_id: int, company_id: int) -> bool:
    """A missing membership row or NULL value counts as enabled (compat default)."""
    acc = _access(user_id, company_id)
    if not acc:
        return True
    val = getattr(acc, "receive_calls", True)
    return True if val is None else bool(val)


def receive_calls_user_ids(company_id: int) -> set[int]:
    """Active-membership user ids on this company whose receive_calls is TRUE
    (a missing/NULL value counts as TRUE)."""
    rows = (
        db.session.query(UserCompanyAccess.user_id, UserCompanyAccess.receive_calls)
        .filter(
            UserCompanyAccess.company_id == company_id,
            UserCompanyAccess.is_active.is_(True),
        )
        .all()
    )
    return {uid for uid, val in rows if val is None or val is True}


def set_receive_calls(user_id: int, company_id: int, value, *, actor_user_id: int, source: str) -> dict:
    flag = normalize_flag(value)
    if source not in _SOURCES:
        raise ReceiveCallsError("source must be 'user', 'admin' or 'system'")
    acc = _access(user_id, company_id)
    if not acc:
        raise ReceiveCallsError("user is not a member of this company")
    acc.receive_calls = flag
    acc.receive_calls_changed_at = datetime.utcnow()
    acc.receive_calls_changed_by_user_id = actor_user_id
    acc.receive_calls_source = source
    db.session.flush()
    return _payload(acc, user_id)


def can_admin_manage(actor, company_id: int) -> bool:
    if getattr(actor, "is_admin", False) and _access(actor.id, company_id) is not None:
        return True
    return can_manage_users(actor, company_id)


def team_receive_calls(company_id: int) -> list[dict]:
    rows = (
        UserCompanyAccess.query.filter_by(company_id=company_id)
        .filter(UserCompanyAccess.is_active.is_(True))
        .all()
    )
    out = []
    for acc in rows:
        user = db.session.get(User, acc.user_id)
        if not user or not getattr(user, "active", True):
            continue
        p = _payload(acc, acc.user_id)
        p["name"] = getattr(user, "username", None) or getattr(user, "email", None) or f"User {acc.user_id}"
        p["role"] = normalize_role(getattr(acc, "role", None))
        out.append(p)
    return out
