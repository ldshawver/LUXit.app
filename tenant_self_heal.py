"""Startup tenant self-heal — authoritative-membership-only repair.

Historically the startup guard in :mod:`app` (and
:meth:`models.User.ensure_default_company_context`) would attach *any* user to
the lowest-ID active :class:`~models.Company` — creating a
:class:`~models.UserCompanyAccess` row, setting ``default_company_id`` and, for
admins, granting ``owner``/``full_app``.  That is a tenant-isolation defect: a
brand-new unbound user could be bound to an unrelated tenant purely because that
tenant happened to have the lowest active id, and platform-admin status implied
ownership of arbitrary tenant data.

This module replaces that behaviour.  It repairs *internally inconsistent*
records **only when the correct tenant relationship is already authoritative**
(an existing active ``UserCompanyAccess`` row, or a legacy ``user_company``
link, to an active company).  It never infers membership from company ordering,
recency, activity, count, or ``first()``.

If a user has no authoritative membership the user is left unbound and a
sanitized warning is logged.  Startup never raises from here.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _deterministic_default_company(user):
    """Return the user's default company chosen *only* from companies the user
    is already authorized to access.

    Deterministic policy, matching the existing product resolver
    (:meth:`models.User.get_default_company`): a ``UserCompanyAccess`` row
    already flagged ``is_default=True`` wins; otherwise the first entry of
    :meth:`models.User.get_all_companies` (active memberships, ordered by name).
    """
    from models import Company, UserCompanyAccess
    from extensions import db

    authorized_ids = user._authorized_company_ids()
    if not authorized_ids:
        return None

    flagged = (
        db.session.query(UserCompanyAccess)
        .filter(
            UserCompanyAccess.user_id == user.id,
            UserCompanyAccess.is_default.is_(True),
            UserCompanyAccess.is_active.is_(True),
            UserCompanyAccess.company_id.in_(authorized_ids),
        )
        .order_by(UserCompanyAccess.company_id.asc())
        .first()
    )
    if flagged is not None:
        found = db.session.get(Company, flagged.company_id)
        if found is not None and found.is_active:
            return found

    companies = [c for c in user.get_all_companies() if c.id in authorized_ids]
    return companies[0] if companies else None


def run_startup_self_heal():
    """Normalize inconsistent user/company records using authoritative state only.

    Returns a dict ``{"repaired": int, "left_unbound": int}`` (plus ``"error"``
    if an exception was swallowed).  Never raises.
    """
    from extensions import db
    from models import Company, User, UserCompanyAccess

    session = db.session
    repaired = 0
    left_unbound = 0

    try:
        users = session.query(User).all()

        for user in users:
            authorized_ids = user._authorized_company_ids()

            if not authorized_ids:
                # No authoritative membership.  Do NOT fabricate one, do NOT
                # grant a role, do NOT attach to a global fallback company.
                if user.default_company_id is not None:
                    logger.warning(
                        "tenant-self-heal: user id=%s has default_company_id set but "
                        "no authoritative membership; leaving unbound (manual repair "
                        "required)",
                        user.id,
                    )
                left_unbound += 1
                continue

            # The user already belongs somewhere — safe to normalize.
            if user.default_company_id in authorized_ids:
                target_id = user.default_company_id
            else:
                target = _deterministic_default_company(user)
                target_id = target.id if target is not None else None

            if target_id is None:
                left_unbound += 1
                continue

            if user.default_company_id != target_id:
                user.default_company_id = target_id
                repaired += 1

            # Ensure the is_default flag among the user's *own* access rows
            # points at the resolved company and nowhere else.
            own_rows = (
                session.query(UserCompanyAccess)
                .filter(
                    UserCompanyAccess.user_id == user.id,
                    UserCompanyAccess.is_active.is_(True),
                    UserCompanyAccess.company_id.in_(authorized_ids),
                )
                .all()
            )
            for row in own_rows:
                want_default = row.company_id == target_id
                if row.is_default != want_default:
                    row.is_default = want_default
                    repaired += 1

        if repaired:
            session.commit()
            logger.warning(
                "tenant-self-heal: normalized %s membership record(s)", repaired
            )
        else:
            # Release the read transaction cleanly; nothing was written.
            session.rollback()

        if left_unbound:
            logger.warning(
                "tenant-self-heal: %s user(s) have no authoritative company "
                "membership and were left unbound",
                left_unbound,
            )

        return {"repaired": repaired, "left_unbound": left_unbound}

    except Exception as exc:  # pragma: no cover - defensive
        try:
            session.rollback()
        except Exception:
            pass
        logger.warning("tenant-self-heal skipped due to error: %s", exc)
        return {"repaired": 0, "left_unbound": 0, "error": str(exc)}
