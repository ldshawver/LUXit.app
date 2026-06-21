"""Tenant license and feature-gating service."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from extensions import db

logger = logging.getLogger(__name__)

PHONE_PWA_FEATURE = "phone_pwa_communications"
ACTIVE_STATUSES = {"active", "trialing"}
BLOCKED_STATUSES = {"suspended", "expired"}

FEATURE_MODULE_SEEDS = [
    ("phone_pwa_communications", "Phone/PWA Communications", "communications", Decimal("99.00")),
    ("sms_campaigns", "SMS Campaigns", "marketing", Decimal("49.00")),
    ("crm_contacts", "CRM Contacts", "crm", Decimal("29.00")),
    ("ai_agents", "AI Agents", "ai", Decimal("79.00")),
    ("marketing_calendar", "Marketing Calendar", "marketing", Decimal("19.00")),
    ("analytics_reports", "Analytics & Reports", "analytics", Decimal("39.00")),
    ("pos_myorder", "POS / MyOrder", "pos", Decimal("99.00")),
    ("document_hub", "Document Hub", "operations", Decimal("29.00")),
    ("contractor_hub", "Contractor Hub", "operations", Decimal("39.00")),
]


class LicenseAccessError(PermissionError):
    def __init__(self, feature_key: str, status: str = "missing", message: str | None = None):
        self.feature_key = feature_key
        self.status = status
        super().__init__(message or f"Feature {feature_key} is not licensed ({status}).")


def seed_feature_modules(commit: bool = True):
    from models import FeatureModule
    changed = False
    for key, name, category, price in FEATURE_MODULE_SEEDS:
        module = FeatureModule.query.filter_by(key=key).first()
        if not module:
            module = FeatureModule(key=key)
            db.session.add(module)
            changed = True
        module.name = name
        module.category = category
        module.description = module.description or f"Licensable LUXit module: {name}."
        module.default_monthly_price = price
        module.is_active = True
    if commit and changed:
        db.session.commit()
    return True


def ensure_company_default_license(company_id: int):
    """Preserve existing deployment access by seeding phone/PWA active during rollout.

    Production migration explicitly seeds company 1. Test/fresh deployments may
    create a first tenant with a different id after startup self-heal, so if no
    phone/PWA license exists anywhere yet we seed the requesting tenant too.
    """
    from models import TenantLicense
    any_phone_license = TenantLicense.query.filter_by(feature_key=PHONE_PWA_FEATURE).first()
    if int(company_id or 0) != 1 and any_phone_license:
        return None
    license_row = TenantLicense.query.filter_by(company_id=company_id, feature_key=PHONE_PWA_FEATURE).first()
    if license_row:
        return license_row
    seed_feature_modules(commit=False)
    license_row = TenantLicense(
        company_id=company_id,
        feature_key=PHONE_PWA_FEATURE,
        status="active",
        seats_included=999,
        seats_used=0,
        monthly_price=0,
        billing_cycle="monthly",
        starts_at=datetime.utcnow(),
        auto_disable_enabled=True,
        grace_period_days=7,
    )
    db.session.add(license_row)
    db.session.commit()
    log_license_event(company_id, license_row, "license_seeded", None, "active", {"reason": "existing deployment default"})
    return license_row


def get_company_license(company_id, feature_key):
    from models import TenantLicense
    if not company_id or not feature_key:
        return None
    license_row = TenantLicense.query.filter_by(company_id=company_id, feature_key=feature_key).first()
    if not license_row and feature_key == PHONE_PWA_FEATURE:
        license_row = ensure_company_default_license(int(company_id))
    return license_row


def _past_due_allowed(license_row, now=None):
    now = now or datetime.utcnow()
    grace_days = license_row.grace_period_days if license_row.grace_period_days is not None else 7
    base = license_row.renews_at or license_row.updated_at or license_row.created_at or now
    return now <= base + timedelta(days=grace_days)


def license_status_details(company_id, feature_key, now=None):
    license_row = get_company_license(company_id, feature_key)
    if not license_row:
        return {"allowed": False, "status": "missing", "license": None, "warning": None}
    status = license_row.status or "missing"
    allowed = False
    warning = None
    if status in ACTIVE_STATUSES:
        allowed = True
    elif status == "past_due":
        allowed = _past_due_allowed(license_row, now=now)
        warning = "Payment is past due. Update billing before the grace period ends." if allowed else None
    elif status == "canceled":
        allowed = bool(license_row.renews_at and (now or datetime.utcnow()) < license_row.renews_at)
        warning = "License is canceled and will disable at period end." if allowed else None
    elif status in BLOCKED_STATUSES:
        allowed = False
    return {"allowed": allowed, "status": status, "license": license_row, "warning": warning}


def has_feature(company_id, feature_key):
    return license_status_details(company_id, feature_key).get("allowed", False)


def require_feature(company_id, feature_key):
    details = license_status_details(company_id, feature_key)
    if not details["allowed"]:
        raise LicenseAccessError(feature_key, details["status"])
    return details["license"]


def can_user_access_feature(user, company, feature_key):
    if not user or not company:
        return False
    return has_feature(company.id, feature_key)


def log_license_event(company_id, license_row, event_type, old_status, new_status, details=None, actor_user_id=None, actor_role=None):
    from models import LicenseEventLog
    entry = LicenseEventLog(
        company_id=company_id,
        license_id=getattr(license_row, "id", None),
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        event_type=event_type,
        old_status=old_status,
        new_status=new_status,
        details_json=details or {},
    )
    db.session.add(entry)
    return entry


def suspend_license(company_id, feature_key, reason, actor_user_id=None, actor_role="system"):
    license_row = get_company_license(company_id, feature_key)
    if not license_row:
        from models import TenantLicense
        license_row = TenantLicense(company_id=company_id, feature_key=feature_key, status="suspended")
        db.session.add(license_row)
        db.session.flush()
    old = license_row.status
    license_row.status = "suspended"
    license_row.suspended_at = datetime.utcnow()
    license_row.suspension_reason = reason
    log_license_event(company_id, license_row, "license_suspended", old, "suspended", {"reason": reason}, actor_user_id, actor_role)
    db.session.commit()
    return license_row


def reactivate_license(company_id, feature_key, actor_user_id=None, actor_role="system"):
    license_row = get_company_license(company_id, feature_key)
    if not license_row:
        from models import TenantLicense
        license_row = TenantLicense(company_id=company_id, feature_key=feature_key)
        db.session.add(license_row)
        db.session.flush()
    old = license_row.status
    license_row.status = "active"
    license_row.suspended_at = None
    license_row.suspension_reason = None
    log_license_event(company_id, license_row, "license_reactivated", old, "active", {}, actor_user_id, actor_role)
    db.session.commit()
    return license_row


def cancel_license(company_id, feature_key, actor_user_id=None, actor_role="system"):
    license_row = get_company_license(company_id, feature_key)
    if not license_row:
        return None
    old = license_row.status
    license_row.status = "canceled"
    license_row.canceled_at = datetime.utcnow()
    log_license_event(company_id, license_row, "license_canceled", old, "canceled", {}, actor_user_id, actor_role)
    db.session.commit()
    return license_row


def auto_suspend_past_due(now=None):
    from models import TenantLicense
    now = now or datetime.utcnow()
    changed = []
    for lic in TenantLicense.query.filter_by(status="past_due", auto_disable_enabled=True).all():
        if not _past_due_allowed(lic, now=now):
            old = lic.status
            lic.status = "suspended"
            lic.suspended_at = now
            lic.suspension_reason = "past_due_grace_period_expired"
            log_license_event(lic.company_id, lic, "license_auto_suspended", old, "suspended", {"reason": lic.suspension_reason})
            changed.append(lic.id)
    db.session.commit()
    return changed


def _stripe_ts(value):
    if not value:
        return None
    try:
        return datetime.utcfromtimestamp(int(value))
    except Exception:
        return None


def _company_for_stripe(customer_id=None, subscription_id=None):
    from models import Company, TenantBillingAccount, TenantSubscription
    if customer_id:
        acct = TenantBillingAccount.query.filter_by(stripe_customer_id=customer_id).first()
        if acct:
            return acct.company
        company = Company.query.filter_by(stripe_customer_id=customer_id).first()
        if company:
            return company
    if subscription_id:
        sub = TenantSubscription.query.filter_by(stripe_subscription_id=subscription_id).first()
        if sub:
            return sub.company
        company = Company.query.filter_by(stripe_subscription_id=subscription_id).first()
        if company:
            return company
    return None


def _ensure_billing_account(company):
    from models import TenantBillingAccount
    acct = TenantBillingAccount.query.filter_by(company_id=company.id).first()
    if not acct:
        acct = TenantBillingAccount(company_id=company.id, billing_email=getattr(company, "email", None), payment_status="none")
        db.session.add(acct)
    return acct


def _ensure_license(company_id, feature_key=PHONE_PWA_FEATURE):
    from models import TenantLicense
    lic = get_company_license(company_id, feature_key)
    if not lic:
        lic = TenantLicense(company_id=company_id, feature_key=feature_key, status="active", starts_at=datetime.utcnow())
        db.session.add(lic)
    return lic


def _invoice_upsert(company, obj):
    from models import TenantInvoice
    inv_id = obj.get("id")
    invoice = TenantInvoice.query.filter_by(stripe_invoice_id=inv_id).first() if inv_id else None
    if not invoice:
        invoice = TenantInvoice(company_id=company.id, stripe_invoice_id=inv_id)
        db.session.add(invoice)
    invoice.invoice_number = obj.get("number") or obj.get("invoice_number")
    invoice.status = obj.get("status")
    invoice.amount_due = obj.get("amount_due") or 0
    invoice.amount_paid = obj.get("amount_paid") or 0
    invoice.currency = obj.get("currency") or "usd"
    invoice.hosted_invoice_url = obj.get("hosted_invoice_url")
    invoice.invoice_pdf = obj.get("invoice_pdf")
    invoice.due_date = _stripe_ts(obj.get("due_date"))
    invoice.paid_at = _stripe_ts(obj.get("status_transitions", {}).get("paid_at"))
    return invoice


def _subscription_upsert(company, obj):
    from models import TenantSubscription
    sub_id = obj.get("id") or obj.get("subscription")
    sub = TenantSubscription.query.filter_by(stripe_subscription_id=sub_id).first() if sub_id else None
    if not sub:
        sub = TenantSubscription(company_id=company.id, stripe_subscription_id=sub_id)
        db.session.add(sub)
    sub.status = obj.get("status") or sub.status
    sub.current_period_start = _stripe_ts(obj.get("current_period_start")) or sub.current_period_start
    sub.current_period_end = _stripe_ts(obj.get("current_period_end")) or sub.current_period_end
    sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end", sub.cancel_at_period_end))
    sub.amount_due = obj.get("amount_due") or sub.amount_due or 0
    sub.currency = obj.get("currency") or sub.currency or "usd"
    return sub


def create_billing_automation_event(company_id, event_type, details=None):
    from models import BillingAutomationRule, LicenseEventLog
    rules = BillingAutomationRule.query.filter_by(event_type=event_type, enabled=True).all()
    created = []
    for rule in rules:
        if rule.company_id not in (None, company_id):
            continue
        entry = LicenseEventLog(company_id=company_id, event_type=f"billing_automation:{rule.action}", details_json={"trigger": event_type, "rule_id": rule.id, **(details or {})})
        db.session.add(entry)
        created.append(entry)
    if not created:
        db.session.add(LicenseEventLog(company_id=company_id, event_type=f"billing_automation:{event_type}", details_json=details or {}))
    return created


def sync_license_from_stripe_event(event):
    """Synchronize tenant billing/license tables from a verified Stripe event dict."""
    ev_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {}) or {}
    customer_id = obj.get("customer")
    subscription_id = obj.get("subscription") or obj.get("id") if "subscription" in ev_type else obj.get("subscription")
    company = _company_for_stripe(customer_id=customer_id, subscription_id=subscription_id)

    # Recover company from checkout metadata/client_reference_id.
    if not company and ev_type == "checkout.session.completed":
        from models import Company
        cid = (obj.get("metadata") or {}).get("company_id") or obj.get("client_reference_id")
        try:
            company = db.session.get(Company, int(cid)) if cid else None
        except Exception:
            company = None
    if not company:
        return {"success": False, "skipped": True, "reason": "company_not_found"}

    acct = _ensure_billing_account(company)
    if customer_id:
        acct.stripe_customer_id = customer_id
        company.stripe_customer_id = customer_id
    lic = _ensure_license(company.id, PHONE_PWA_FEATURE)
    old_status = lic.status

    if ev_type in {"checkout.session.completed", "customer.subscription.created", "customer.subscription.updated"}:
        _subscription_upsert(company, obj)
        status = obj.get("status") or "active"
        lic.status = "active" if status in {"active", "trialing"} else status
        lic.renews_at = _stripe_ts(obj.get("current_period_end")) or lic.renews_at
        acct.payment_status = lic.status
        if old_status != lic.status:
            log_license_event(company.id, lic, "stripe_subscription_synced", old_status, lic.status, {"stripe_event_type": ev_type})
    elif ev_type == "customer.subscription.deleted":
        _subscription_upsert(company, obj)
        lic.status = "canceled"
        lic.canceled_at = datetime.utcnow()
        acct.payment_status = "canceled"
        log_license_event(company.id, lic, "stripe_subscription_deleted", old_status, "canceled", {"stripe_event_type": ev_type})
    elif ev_type in {"invoice.created", "invoice.finalized"}:
        _invoice_upsert(company, obj)
        create_billing_automation_event(company.id, "invoice_due", {"invoice_id": obj.get("id")})
    elif ev_type == "invoice.payment_failed":
        _invoice_upsert(company, obj)
        lic.status = "past_due"
        lic.renews_at = lic.renews_at or datetime.utcnow()
        acct.payment_status = "past_due"
        acct.last_payment_failed_at = datetime.utcnow()
        create_billing_automation_event(company.id, "payment_failed", {"invoice_id": obj.get("id")})
        log_license_event(company.id, lic, "stripe_payment_failed", old_status, "past_due", {"invoice_id": obj.get("id")})
    elif ev_type == "invoice.payment_succeeded":
        _invoice_upsert(company, obj)
        lic.status = "active"
        lic.suspended_at = None
        lic.suspension_reason = None
        acct.payment_status = "active"
        acct.last_payment_at = datetime.utcnow()
        create_billing_automation_event(company.id, "license_reactivated", {"invoice_id": obj.get("id")})
        log_license_event(company.id, lic, "stripe_payment_succeeded", old_status, "active", {"invoice_id": obj.get("id")})
    elif ev_type == "payment_method.attached":
        acct.default_payment_method_id = obj.get("id")
    elif ev_type == "customer.updated":
        acct.billing_email = obj.get("email") or acct.billing_email
        acct.billing_contact_name = obj.get("name") or acct.billing_contact_name
        acct.billing_address_json = obj.get("address") or acct.billing_address_json

    db.session.commit()
    return {"success": True, "company_id": company.id, "feature_key": PHONE_PWA_FEATURE, "status": lic.status}
