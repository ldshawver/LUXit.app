"""Self-serve signup: account + organization creation and email verification.

Off unless ``SELF_SERVE_SIGNUP_ENABLED`` is set. A signup creates one Company,
one User (never a platform admin) and an owner UserCompanyAccess row in a
single transaction. The account cannot sign in until the emailed link is used.
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from extensions import db

_SALT = "luxit-email-verify-v1"
VERIFY_MAX_AGE = 48 * 3600
RESEND_COOLDOWN = timedelta(minutes=2)
MIN_PASSWORD = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def signup_enabled() -> bool:
    return (os.environ.get("SELF_SERVE_SIGNUP_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=_SALT)


def make_token(user_id: int, email: str) -> str:
    return _serializer().dumps({"u": int(user_id), "e": email})


def validate(form) -> tuple[dict, list[str]]:
    data = {
        "org_name": (form.get("org_name") or "").strip(),
        "full_name": (form.get("full_name") or "").strip(),
        "email": (form.get("email") or "").strip().lower(),
        "password": form.get("password") or "",
        "confirm": form.get("confirm_password") or "",
        "terms": bool(form.get("accept_terms")),
    }
    errors = []
    if not 2 <= len(data["org_name"]) <= 120:
        errors.append("Enter your organization's name.")
    if len(data["full_name"]) > 128:
        errors.append("Name is too long.")
    if not _EMAIL_RE.match(data["email"]) or len(data["email"]) > 120:
        errors.append("Enter a valid email address.")
    if len(data["password"]) < MIN_PASSWORD:
        errors.append(f"Password must be at least {MIN_PASSWORD} characters.")
    elif data["password"] != data["confirm"]:
        errors.append("Passwords do not match.")
    if not data["terms"]:
        errors.append("You must accept the Terms of Service and Privacy Policy.")
    return data, errors


def _unique_username(email: str) -> str:
    from models import User
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@", 1)[0].lower())[:40] or "user"
    candidate = base
    while User.query.filter_by(username=candidate).first() is not None:
        candidate = f"{base}-{secrets.token_hex(3)}"
    return candidate


def create_account(data: dict, *, ip: str | None = None):
    """Create org + owner. Returns ``(user, status)``.

    status is ``"created"``, ``"unverified_existing"`` (an unverified signup
    already holds this email: resend its link) or ``"exists"`` (a verified or
    pre-signup account holds it). Callers must respond identically in all three
    cases so the form can't be used to discover accounts.
    """
    from models import Company, CompanyPlanPolicy, User, UserCompanyAccess, UserEmailVerification
    from services.entitlements import FREE_PLAN_MAX_SEATS

    existing = User.query.filter(User.email == data["email"]).first()
    if existing is not None:
        pending = db.session.get(UserEmailVerification, existing.id)
        if pending is not None and pending.verified_at is None:
            return existing, "unverified_existing"
        return existing, "exists"

    first, _, last = data["full_name"].partition(" ")
    company = Company(
        name=data["org_name"],
        is_active=True,
        onboarding_status="pending",
        subscription_tier="free",
        billing_tier="free",
        billing_status="none",
        max_team_members=FREE_PLAN_MAX_SEATS,
    )
    db.session.add(company)
    db.session.flush()
    db.session.add(CompanyPlanPolicy(company_id=company.id, plan_source="self_serve"))
    user = User(
        username=_unique_username(data["email"]),
        email=data["email"],
        password_hash=generate_password_hash(data["password"]),
        is_admin=False,
        default_company_id=company.id,
        first_name=first[:64] or None,
        last_name=last[:64] or None,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="owner", is_default=True))
    db.session.add(UserEmailVerification(
        user_id=user.id,
        email=user.email,
        terms_accepted_at=datetime.utcnow(),
        signup_ip=(ip or "")[:64] or None,
    ))
    db.session.commit()
    return user, "created"


def may_send(user) -> bool:
    """True if a verification email may go out now (per-account cooldown)."""
    from models import UserEmailVerification
    row = db.session.get(UserEmailVerification, user.id)
    if row is None or row.verified_at is not None:
        return False
    return row.last_sent_at is None or datetime.utcnow() - row.last_sent_at >= RESEND_COOLDOWN


def mark_sent(user) -> None:
    from models import UserEmailVerification
    row = db.session.get(UserEmailVerification, user.id)
    if row is not None:
        row.last_sent_at = datetime.utcnow()
        db.session.commit()


def verify_token(token: str):
    """Mark the account verified. Returns ``(user, outcome)``.

    outcome: ``"verified"``, ``"already"``, ``"expired"`` or ``"invalid"``. A
    token is bound to the email it was sent to, so it dies if the address
    changes.
    """
    from models import User, UserEmailVerification
    try:
        data = _serializer().loads(token, max_age=VERIFY_MAX_AGE)
    except SignatureExpired:
        return None, "expired"
    except BadSignature:
        return None, "invalid"
    user = db.session.get(User, data.get("u"))
    row = db.session.get(UserEmailVerification, data.get("u"))
    if user is None or row is None or user.email != data.get("e") or row.email != data.get("e"):
        return None, "invalid"
    if row.verified_at is not None:
        return user, "already"
    row.verified_at = datetime.utcnow()
    db.session.commit()
    return user, "verified"


def needs_verification(user) -> bool:
    """True if this account signed up itself and hasn't verified yet."""
    from models import UserEmailVerification
    try:
        row = db.session.get(UserEmailVerification, user.id)
    except SQLAlchemyError:
        # Table not migrated yet → no signup accounts can exist. Don't turn a
        # missed migration into a login outage for every existing user.
        db.session.rollback()
        current_app.logger.error("user_email_verification unavailable; skipping verification gate")
        return False
    return row is not None and row.verified_at is None
