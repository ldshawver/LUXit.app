"""What the current workspace is entitled to, in one read model (M7).

Shared by the web app and LUXit Phone so both show the same plan, limits and
feature switches. It reads the existing sources of truth and decides
nothing on its own: plan (``entitlements``), seats (``invitations``),
contact allowance (``stripe_billing``), phone/SMS module (``license_service``).
"""
from __future__ import annotations

import os

from extensions import db


def _contacts_used(company_id):
    from models import Contact
    return (Contact.query
            .filter(Contact.company_id == company_id, Contact.is_active.is_(True),
                    Contact.merged_into_contact_id.is_(None))
            .count())


def _plan_source(company_id):
    from sqlalchemy.exc import SQLAlchemyError
    from models import CompanyPlanPolicy
    try:
        with db.session.begin_nested():
            policy = db.session.get(CompanyPlanPolicy, company_id)
    except SQLAlchemyError:
        return "managed"
    return policy.plan_source if policy else "managed"


def account_status(company, user):
    from saas_mgmt import _user_can_manage_billing
    from services import entitlements, invitations
    from services.license_service import PHONE_PWA_FEATURE, license_status_details
    from services.stripe_billing import compute_contacts_overage, included_contacts_for_tier

    free = entitlements.is_free_plan(company.id)
    source = _plan_source(company.id)
    tier = company.billing_tier or company.subscription_tier

    pending = len(invitations.pending_invitations(company.id))
    members = company.team_member_count
    seat_limit = company.max_team_members

    used = _contacts_used(company.id)
    if free:
        contact_limit, overage = entitlements.FREE_PLAN_MAX_CONTACTS, 0
    elif source == "managed" and not company.stripe_subscription_id:
        contact_limit, overage = None, 0
    else:
        contact_limit = getattr(company, "included_contacts", None) or included_contacts_for_tier(tier)
        overage = compute_contacts_overage(used, contact_limit)

    phone = license_status_details(company.id, PHONE_PWA_FEATURE)
    access = user.get_company_access(company.id) if user is not None else None
    return {
        "workspace": {"id": company.id, "name": company.name},
        "role": "platform_admin" if getattr(user, "is_admin", False) and access is None else getattr(access, "role", None),
        "plan": {
            "source": source,
            "tier": "free" if free else tier,
            "billing_status": company.billing_status,
            "free": free,
            "grace_period_ends_at": company.grace_period_ends_at.isoformat() if company.grace_period_ends_at else None,
        },
        "seats": {"members": members, "pending_invites": pending, "used": members + pending, "limit": seat_limit},
        "contacts": {"used": used, "limit": contact_limit, "overage": overage,
                     "metered": bool(company.stripe_contact_usage_subscription_item_id)},
        "features": {
            "crm": True,
            "ask_luxit": True,
            "automations": True,
            "ai": not free,
            "phone_sms": {"allowed": phone["allowed"], "status": phone["status"], "warning": phone["warning"]},
            "tenant_email_send": os.environ.get("LUXIT_TENANT_EMAIL_SEND_ENABLED", "").lower() in ("1", "true", "yes"),
        },
        "can_manage_billing": _user_can_manage_billing(user, company),
        "billing_url": "/app/billing",
    }
