"""Plan entitlements for self-serve workspaces (T-8).

A workspace created through self-serve signup (a ``company_plan_policy`` row)
is on the free plan until it holds a paid subscription. Workspaces without a
row predate self-serve signup and keep unrestricted access.

Free plan: 1 seat (set at signup), FREE_PLAN_MAX_CONTACTS active contacts,
no AI features, no phone/SMS (the phone/PWA license is never granted by a
plan; see license_service.sync_license_from_stripe_event).
"""
from __future__ import annotations

import logging
from datetime import datetime

from extensions import db

logger = logging.getLogger(__name__)

FREE_PLAN_MAX_SEATS = 1
FREE_PLAN_MAX_CONTACTS = 250
PAID_BILLING_STATUSES = {"active", "trialing"}


class PlanLimitReached(PermissionError):
    """Raised when a free-plan workspace hits a paid-plan feature or limit."""

    def __init__(self, feature: str, message: str, limit: int | None = None):
        self.feature = feature
        self.limit = limit
        super().__init__(message)


def is_free_plan(company_id) -> bool:
    """True when the workspace is a self-serve signup without a paid subscription."""
    if not company_id:
        return False
    from sqlalchemy.exc import SQLAlchemyError
    from models import Company, CompanyPlanPolicy
    try:
        # Savepoint: this runs inside callers' transactions (contact creation).
        with db.session.begin_nested():
            policy = db.session.get(CompanyPlanPolicy, int(company_id))
    except SQLAlchemyError:
        # Table not migrated yet → no self-serve workspaces can exist. Don't
        # turn a missed migration into a contact-creation outage for everyone.
        logger.error("company_plan_policy unavailable; skipping plan entitlements")
        return False
    if not policy or policy.plan_source != "self_serve":
        return False
    company = db.session.get(Company, int(company_id))
    if not company:
        return True
    status = (company.billing_status or "").lower()
    if status in PAID_BILLING_STATUSES:
        return False
    if status == "past_due" and company.grace_period_ends_at and datetime.utcnow() < company.grace_period_ends_at:
        return False
    return True


def apply_new_workspace_policy(company, creator) -> bool:
    """Put a workspace created after self-serve launch on the free plan.

    Only platform admins create managed (unrestricted) workspaces. Anyone
    else adding a workspace gets the same terms as a signup, so a second
    workspace can't be used to step around the first one's plan. Call after
    the company has an id. Returns True when the free plan was applied.
    """
    if creator is not None and getattr(creator, "is_admin", False):
        return False
    from models import CompanyPlanPolicy
    company.subscription_tier = "free"
    company.billing_tier = "free"
    company.billing_status = "none"
    company.max_team_members = FREE_PLAN_MAX_SEATS
    if db.session.get(CompanyPlanPolicy, company.id) is None:
        db.session.add(CompanyPlanPolicy(company_id=company.id, plan_source="self_serve"))
    return True


def check_contact_capacity(company_id, adding: int = 1):
    if not is_free_plan(company_id):
        return
    from models import Contact
    used = (
        Contact.query
        .filter(Contact.company_id == company_id, Contact.is_active.is_(True), Contact.merged_into_contact_id.is_(None))
        .count()
    )
    if used + adding > FREE_PLAN_MAX_CONTACTS:
        raise PlanLimitReached(
            "contacts",
            f"The free plan includes up to {FREE_PLAN_MAX_CONTACTS} contacts. Choose a plan to add more.",
            limit=FREE_PLAN_MAX_CONTACTS,
        )


def _request_company_id():
    from flask import has_request_context
    if not has_request_context():
        return None
    from flask_login import current_user
    if not getattr(current_user, "is_authenticated", False):
        return None
    try:
        company = current_user.get_default_company()
    except Exception:
        company = None
    return getattr(company, "id", None) or getattr(current_user, "default_company_id", None)


def require_ai_entitlement(company_id=None):
    """Block AI features for free-plan workspaces.

    With no ``company_id`` the signed-in user's workspace is used; background
    jobs without a request context are not gated here.
    """
    company_id = company_id or _request_company_id()
    if company_id and is_free_plan(company_id):
        raise PlanLimitReached("ai", "AI features are included in paid plans. Choose a plan to use them.")


def plan_limit_response(exc: PlanLimitReached):
    """Error handler body: JSON 402 for API callers, a redirect to billing otherwise."""
    from flask import flash, jsonify, redirect, request
    wants_json = request.path.startswith("/api/") or (
        request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html
    )
    db.session.rollback()
    if wants_json:
        return jsonify({
            "success": False, "error": str(exc), "code": "PLAN_LIMIT",
            "feature": exc.feature, "limit": exc.limit, "billing_url": "/app/billing",
        }), 402
    flash(str(exc), "warning")
    return redirect("/app/billing")
