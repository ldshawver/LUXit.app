"""Bearer-token sessions for the LUX Connect native app.

The web app authenticates with a Flask session cookie plus a CSRF token. A
native app has no cookie jar worth trusting and no page to render a CSRF token
into, so it gets opaque bearer tokens instead:

  access token   ``lxa_…``  1 hour, sent as ``Authorization: Bearer``
  refresh token  ``lxr_…``  60 days, sliding; rotated on every use

Only SHA-256 hashes are stored. Every authenticated request re-checks the user,
the tenant membership, Mobile Inbox access and the device, so archiving a user,
revoking a device or a global ``session_revoked_at`` takes effect on the next
request rather than when the token happens to expire.

Refresh rotation uses reuse detection: presenting an already-rotated refresh
token revokes every session of that device (a stolen token was used). The one
exception is a lost response: if the rotated-to session has never been used and
the retry arrives within ``REFRESH_GRACE``, the old token may rotate again and
the unused successor is retired. That is what a phone on a flaky network does
when the response to its refresh never arrives.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from extensions import db

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=60)
REFRESH_GRACE = timedelta(seconds=60)

THROTTLE_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_IDENTIFIER = 10
MAX_FAILURES_PER_IP = 30

ACCESS_PREFIX = "lxa_"
REFRESH_PREFIX = "lxr_"

USABLE_DEVICE_STATUSES = ("active", "pending_approval")


class MobileAuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class IssuedTokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    session_id: int

    def as_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TTL.total_seconds()),
            "access_expires_at": self.access_expires_at.isoformat() + "Z",
            "refresh_expires_at": self.refresh_expires_at.isoformat() + "Z",
        }


@dataclass
class MobileContext:
    session: object
    device: object
    user: object
    company: object


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def identifier_hash(identifier: str) -> str:
    return hashlib.sha256((identifier or "").strip().lower().encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.utcnow()


# ── sign-in throttling ────────────────────────────────────────────────────── #

def is_throttled(identifier: str, ip: str | None) -> bool:
    from models import MobileAuthAttempt
    since = _now() - THROTTLE_WINDOW
    by_ident = MobileAuthAttempt.query.filter(
        MobileAuthAttempt.identifier_hash == identifier_hash(identifier),
        MobileAuthAttempt.created_at >= since,
    ).count()
    if by_ident >= MAX_FAILURES_PER_IDENTIFIER:
        return True
    if ip:
        by_ip = MobileAuthAttempt.query.filter(
            MobileAuthAttempt.ip == ip, MobileAuthAttempt.created_at >= since,
        ).count()
        if by_ip >= MAX_FAILURES_PER_IP:
            return True
    return False


def record_failure(identifier: str, ip: str | None) -> None:
    from models import MobileAuthAttempt
    db.session.add(MobileAuthAttempt(identifier_hash=identifier_hash(identifier), ip=(ip or "")[:64] or None))
    db.session.commit()


def clear_failures(identifier: str) -> None:
    from models import MobileAuthAttempt
    MobileAuthAttempt.query.filter_by(identifier_hash=identifier_hash(identifier)).delete()


# ── credentials ───────────────────────────────────────────────────────────── #

_DUMMY_PASSWORD_HASH: str | None = None


def _burn_password_check(password: str) -> None:
    """Spend one password hash on an unknown account, so response time does
    not reveal whether the email or username exists."""
    global _DUMMY_PASSWORD_HASH
    from werkzeug.security import check_password_hash, generate_password_hash
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(16))
    check_password_hash(_DUMMY_PASSWORD_HASH, password)


def verify_credentials(identifier: str, password: str):
    """Same lookup and checks as the web login (auth.login), without the session."""
    from sqlalchemy import or_
    from werkzeug.security import check_password_hash
    from models import User

    identifier = (identifier or "").strip()
    if not identifier or not password:
        raise MobileAuthError("invalid_credentials", "Email and password are required.", 400)
    user = User.query.filter(or_(User.username == identifier, User.email == identifier.lower())).first()
    if not user or not user.password_hash:
        _burn_password_check(password)
        raise MobileAuthError("invalid_credentials", "Incorrect email or password.", 401)
    if not check_password_hash(user.password_hash, password):
        raise MobileAuthError("invalid_credentials", "Incorrect email or password.", 401)
    if not bool(getattr(user, "is_active", True)):
        raise MobileAuthError("account_archived", "This account has been archived. Ask an administrator to restore access.", 403)
    from services.signup import needs_verification
    if needs_verification(user):
        raise MobileAuthError("email_unverified", "Confirm your email address first, then sign in again.", 403)
    return user


def resolve_company_for(user):
    """The company the web PWA would use, plus the Mobile Inbox access gate."""
    from inbox_pwa import _check_mobile_inbox_access, _get_company
    company = _get_company(user)
    if not company:
        raise MobileAuthError("no_company", "This account is not attached to a company.", 409)
    if not bool(getattr(company, "is_active", True)):
        raise MobileAuthError("company_inactive", "This company account is not active.", 403)
    if not _check_mobile_inbox_access(user, company):
        raise MobileAuthError("mobile_access_disabled", "LUX Connect access is not enabled for this account. Ask your administrator.", 403)
    return company


# ── devices ───────────────────────────────────────────────────────────────── #

def _clean(value, limit: int) -> str | None:
    value = (str(value) if value is not None else "").strip()
    return value[:limit] or None


def upsert_device(user, company, payload: dict):
    from models import MobileDevice
    uid = _clean(payload.get("device_uid"), 64)
    platform = (payload.get("platform") or "").strip().lower()
    if not uid or len(uid) < 16:
        raise MobileAuthError("invalid_device", "A device id is required.", 400)
    if platform not in ("ios", "android"):
        raise MobileAuthError("invalid_device", "Platform must be ios or android.", 400)
    device = MobileDevice.query.filter_by(company_id=company.id, user_id=user.id, device_uid=uid).first()
    requires_approval = bool(getattr(company, "require_approved_pwa_devices", False))
    if not device:
        device = MobileDevice(company_id=company.id, user_id=user.id, device_uid=uid, platform=platform,
                              status="pending_approval" if requires_approval else "active")
        db.session.add(device)
    elif device.status in ("signed_out", "revoked"):
        # Signing in again re-activates the install. A tenant that requires
        # device approval gets a fresh approval request instead.
        device.status = "pending_approval" if requires_approval else "active"
        device.revoked_at = None
        device.revoked_by_user_id = None
    device.platform = platform
    for field, limit in (("device_name", 120), ("model", 120), ("os_version", 40), ("app_version", 40)):
        if field in payload:
            setattr(device, field, _clean(payload.get(field), limit))
    device.last_seen_at = _now()
    db.session.flush()
    return device


# ── sessions ──────────────────────────────────────────────────────────────── #

def issue_session(user, company, device, *, ip: str | None = None) -> IssuedTokens:
    from models import MobileSession
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(48)
    now = _now()
    row = MobileSession(
        mobile_device_id=device.id, user_id=user.id, company_id=company.id,
        access_token_hash=token_hash(access), access_expires_at=now + ACCESS_TTL,
        refresh_token_hash=token_hash(refresh), refresh_expires_at=now + REFRESH_TTL,
        created_ip=(ip or "")[:64] or None,
    )
    db.session.add(row)
    db.session.flush()
    return IssuedTokens(access, refresh, row.access_expires_at, row.refresh_expires_at, row.id)


def revoke_device_sessions(device, reason: str) -> int:
    from models import MobileSession
    now = _now()
    rows = MobileSession.query.filter(
        MobileSession.mobile_device_id == device.id, MobileSession.revoked_at.is_(None),
    ).all()
    for row in rows:
        row.revoked_at = now
        row.revoke_reason = reason[:40]
    return len(rows)


def _validate_principal(session_row):
    """Re-check everything a session depends on. Returns (device, user, company)."""
    from models import Company, MobileDevice, User, UserCompanyAccess
    device = db.session.get(MobileDevice, session_row.mobile_device_id)
    if not device or device.status not in USABLE_DEVICE_STATUSES:
        raise MobileAuthError("device_revoked", "This device has been signed out.", 401)
    user = db.session.get(User, session_row.user_id)
    if not user or not bool(getattr(user, "is_active", True)):
        raise MobileAuthError("account_archived", "This account is no longer active.", 401)
    revoked_all = getattr(user, "session_revoked_at", None)
    if revoked_all and session_row.created_at and session_row.created_at < revoked_all:
        raise MobileAuthError("session_revoked", "Please sign in again.", 401)
    company = db.session.get(Company, session_row.company_id)
    if not company or not bool(getattr(company, "is_active", True)):
        raise MobileAuthError("company_inactive", "This company account is not active.", 401)
    if not getattr(user, "is_admin", False):
        acc = UserCompanyAccess.query.filter_by(user_id=user.id, company_id=company.id).first()
        if not acc or not bool(getattr(acc, "is_active", True)) or not acc.has_mobile_inbox_access():
            raise MobileAuthError("mobile_access_disabled", "LUX Connect access was removed for this account.", 401)
    return device, user, company


def authenticate_access_token(raw_token: str) -> MobileContext:
    from models import MobileSession
    if not raw_token or not raw_token.startswith(ACCESS_PREFIX):
        raise MobileAuthError("invalid_token", "Sign in required.", 401)
    row = MobileSession.query.filter_by(access_token_hash=token_hash(raw_token)).first()
    if not row or row.revoked_at or row.rotated_at:
        raise MobileAuthError("invalid_token", "Sign in required.", 401)
    if row.access_expires_at <= _now():
        raise MobileAuthError("token_expired", "Access token expired.", 401)
    device, user, company = _validate_principal(row)
    now = _now()
    # Throttle last-seen writes to once a minute per session/device.
    if not row.last_used_at or (now - row.last_used_at) > timedelta(minutes=1):
        row.last_used_at = now
        device.last_seen_at = now
        db.session.commit()
    return MobileContext(row, device, user, company)


def rotate_refresh_token(raw_refresh: str, *, ip: str | None = None):
    """Exchange a refresh token for a new token pair. Returns (IssuedTokens, MobileContext)."""
    from models import MobileSession
    if not raw_refresh or not raw_refresh.startswith(REFRESH_PREFIX):
        raise MobileAuthError("invalid_refresh", "Please sign in again.", 401)
    row = MobileSession.query.filter_by(refresh_token_hash=token_hash(raw_refresh)).first()
    if not row or row.revoked_at:
        raise MobileAuthError("invalid_refresh", "Please sign in again.", 401)
    now = _now()
    if row.refresh_expires_at <= now:
        raise MobileAuthError("refresh_expired", "Please sign in again.", 401)
    if row.rotated_at:
        successor = db.session.get(MobileSession, row.rotated_to_id) if row.rotated_to_id else None
        lost_response = (
            successor is not None
            and successor.last_used_at is None
            and successor.revoked_at is None
            and successor.rotated_at is None
            and (now - row.rotated_at) <= REFRESH_GRACE
        )
        if not lost_response:
            from models import MobileDevice
            device = db.session.get(MobileDevice, row.mobile_device_id)
            if device:
                revoke_device_sessions(device, "refresh_reuse")
            db.session.commit()
            raise MobileAuthError("session_revoked", "Please sign in again.", 401)
        successor.revoked_at = now
        successor.revoke_reason = "superseded_retry"
    device, user, company = _validate_principal(row)
    issued = issue_session(user, company, device, ip=ip)
    row.rotated_at = now
    row.rotated_to_id = issued.session_id
    device.last_seen_at = now
    db.session.commit()
    new_row = db.session.get(MobileSession, issued.session_id)
    return issued, MobileContext(new_row, device, user, company)
