"""Tenant license and billing management routes/APIs."""
from __future__ import annotations

import os
from datetime import datetime

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from services.license_service import (
    FEATURE_MODULE_SEEDS, PHONE_PWA_FEATURE, auto_suspend_past_due, cancel_license,
    get_company_license, license_status_details, reactivate_license, seed_feature_modules,
    suspend_license,
)

license_bp = Blueprint("license_mgmt", __name__)


def _company():
    from models import Company, UserCompanyAccess
    if not current_user.is_authenticated:
        return None
    if current_user.default_company_id:
        return db.session.get(Company, current_user.default_company_id)
    access = UserCompanyAccess.query.filter_by(user_id=current_user.id).first()
    return db.session.get(Company, access.company_id) if access else None


def _tenant_admin_required():
    from models import UserCompanyAccess
    company = _company()
    if not company:
        abort(403)
    if getattr(current_user, "is_admin", False):
        return company
    access = UserCompanyAccess.query.filter_by(user_id=current_user.id, company_id=company.id).first()
    role = (getattr(access, "role", "") or "").lower()
    if role not in {"owner", "admin"}:
        abort(403)
    return company


def _global_admin_required():
    if not getattr(current_user, "is_admin", False):
        abort(403)


def _license_to_dict(license_row):
    if not license_row:
        return None
    return {
        "id": license_row.id,
        "company_id": license_row.company_id,
        "feature_key": license_row.feature_key,
        "status": license_row.status,
        "seats_included": license_row.seats_included,
        "seats_used": license_row.seats_used,
        "monthly_price": float(license_row.monthly_price or 0),
        "billing_cycle": license_row.billing_cycle,
        "starts_at": license_row.starts_at.isoformat() if license_row.starts_at else None,
        "trial_ends_at": license_row.trial_ends_at.isoformat() if license_row.trial_ends_at else None,
        "renews_at": license_row.renews_at.isoformat() if license_row.renews_at else None,
        "suspended_at": license_row.suspended_at.isoformat() if license_row.suspended_at else None,
        "suspension_reason": license_row.suspension_reason,
        "auto_disable_enabled": bool(license_row.auto_disable_enabled),
        "grace_period_days": license_row.grace_period_days,
    }


def _invoice_to_dict(invoice):
    return {
        "id": invoice.id,
        "company_id": invoice.company_id,
        "invoice_number": invoice.invoice_number,
        "status": invoice.status,
        "amount_due": invoice.amount_due,
        "amount_paid": invoice.amount_paid,
        "currency": invoice.currency,
        "hosted_invoice_url": invoice.hosted_invoice_url,
        "invoice_pdf": invoice.invoice_pdf,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
    }


@license_bp.route("/settings/licenses")
@login_required
def tenant_licenses_page():
    company = _tenant_admin_required()
    seed_feature_modules()
    from models import FeatureModule, TenantLicense
    licenses = {l.feature_key: l for l in TenantLicense.query.filter_by(company_id=company.id).all()}
    modules = FeatureModule.query.filter_by(is_active=True).order_by(FeatureModule.category, FeatureModule.name).all()
    return render_template("licenses/settings_licenses.html", company=company, modules=modules, licenses=licenses)


@license_bp.route("/settings/licenses/<feature_key>")
@login_required
def tenant_license_detail_page(feature_key):
    company = _tenant_admin_required()
    from models import FeatureModule
    module = FeatureModule.query.filter_by(key=feature_key).first_or_404()
    license_row = get_company_license(company.id, feature_key)
    return render_template("licenses/license_detail.html", company=company, module=module, license=license_row)


@license_bp.route("/settings/billing")
@login_required
def tenant_billing_page():
    company = _tenant_admin_required()
    from models import TenantBillingAccount, TenantInvoice
    account = TenantBillingAccount.query.filter_by(company_id=company.id).first()
    invoices = TenantInvoice.query.filter_by(company_id=company.id).order_by(TenantInvoice.created_at.desc()).limit(20).all()
    return render_template("licenses/settings_billing.html", company=company, account=account, invoices=invoices)


@license_bp.route("/settings/billing/statements")
@login_required
def tenant_statements_page():
    company = _tenant_admin_required()
    from models import TenantInvoice
    invoices = TenantInvoice.query.filter_by(company_id=company.id).order_by(TenantInvoice.created_at.desc()).all()
    return render_template("licenses/billing_statements.html", company=company, invoices=invoices)


@license_bp.route("/settings/billing/payment-method")
@login_required
def tenant_payment_method_page():
    company = _tenant_admin_required()
    return render_template("licenses/payment_method.html", company=company)


@license_bp.route("/settings/billing/autopay")
@login_required
def tenant_autopay_page():
    company = _tenant_admin_required()
    from models import TenantBillingAccount
    account = TenantBillingAccount.query.filter_by(company_id=company.id).first()
    return render_template("licenses/autopay.html", company=company, account=account)


@license_bp.route("/api/licenses")
@login_required
def api_licenses():
    company = _tenant_admin_required()
    from models import TenantLicense
    return jsonify({"success": True, "licenses": [_license_to_dict(l) for l in TenantLicense.query.filter_by(company_id=company.id).all()]})


@license_bp.route("/api/licenses/<feature_key>")
@login_required
def api_license_detail(feature_key):
    company = _tenant_admin_required()
    details = license_status_details(company.id, feature_key)
    return jsonify({"success": True, "license": _license_to_dict(details["license"]), "allowed": details["allowed"], "warning": details["warning"]})


@license_bp.route("/api/billing/account", methods=["GET", "PATCH"])
@login_required
def api_billing_account():
    company = _tenant_admin_required()
    from models import TenantBillingAccount
    account = TenantBillingAccount.query.filter_by(company_id=company.id).first()
    if not account:
        account = TenantBillingAccount(company_id=company.id)
        db.session.add(account)
    if request.method == "PATCH":
        data = request.get_json() or {}
        for field in ("billing_email", "billing_contact_name", "billing_address_json"):
            if field in data:
                setattr(account, field, data[field])
        db.session.commit()
    return jsonify({"success": True, "account": {
        "company_id": company.id,
        "billing_email": account.billing_email,
        "billing_contact_name": account.billing_contact_name,
        "billing_address_json": account.billing_address_json,
        "autopay_enabled": bool(account.autopay_enabled),
        "payment_status": account.payment_status,
        "stripe_customer_id": account.stripe_customer_id,
        "default_payment_method_id": bool(account.default_payment_method_id),
    }})


@license_bp.route("/api/billing/autopay", methods=["POST"])
@login_required
def api_billing_autopay():
    company = _tenant_admin_required()
    from models import TenantBillingAccount
    account = TenantBillingAccount.query.filter_by(company_id=company.id).first() or TenantBillingAccount(company_id=company.id)
    db.session.add(account)
    data = request.get_json() or {}
    account.autopay_enabled = bool(data.get("autopay_enabled", data.get("enabled", True)))
    db.session.commit()
    return jsonify({"success": True, "autopay_enabled": account.autopay_enabled})


@license_bp.route("/api/billing/invoices")
@login_required
def api_billing_invoices():
    company = _tenant_admin_required()
    from models import TenantInvoice
    invoices = TenantInvoice.query.filter_by(company_id=company.id).order_by(TenantInvoice.created_at.desc()).all()
    return jsonify({"success": True, "invoices": [_invoice_to_dict(i) for i in invoices]})


@license_bp.route("/api/billing/invoices/<int:invoice_id>")
@login_required
def api_billing_invoice(invoice_id):
    company = _tenant_admin_required()
    from models import TenantInvoice
    invoice = TenantInvoice.query.filter_by(id=invoice_id, company_id=company.id).first_or_404()
    return jsonify({"success": True, "invoice": _invoice_to_dict(invoice)})


@license_bp.route("/api/billing/create-checkout-session", methods=["POST"])
@login_required
def api_create_checkout_session():
    company = _tenant_admin_required()
    try:
        from services.stripe_billing import create_checkout_session
        body = request.get_json() or {}
        session = create_checkout_session(
            body.get("lookup_key") or "luxit_starter_monthly",
            company_id=company.id,
            user_id=current_user.id,
        )
        return jsonify({"success": True, "url": getattr(session, "url", None), "id": getattr(session, "id", None)})
    except Exception as exc:
        return jsonify({"success": False, "error": "Stripe checkout is not configured.", "detail": str(exc)[:200]}), 503


@license_bp.route("/api/billing/create-portal-session", methods=["POST"])
@login_required
def api_create_portal_session():
    company = _tenant_admin_required()
    try:
        from models import TenantBillingAccount
        from services.stripe_billing import create_billing_portal_session
        account = TenantBillingAccount.query.filter_by(company_id=company.id).first()
        customer_id = (account.stripe_customer_id if account else None) or getattr(company, "stripe_customer_id", None)
        session = create_billing_portal_session(customer_id)
        return jsonify({"success": True, "url": getattr(session, "url", None)})
    except Exception as exc:
        return jsonify({"success": False, "error": "Stripe customer portal is not configured.", "detail": str(exc)[:200]}), 503


@license_bp.route("/global-admin/licenses")
@login_required
def global_licenses_page():
    _global_admin_required()
    from models import TenantLicense
    licenses = TenantLicense.query.order_by(TenantLicense.company_id, TenantLicense.feature_key).all()
    return render_template("licenses/global_licenses.html", licenses=licenses)


@license_bp.route("/global-admin/tenants/<int:company_id>/licenses")
@login_required
def global_tenant_licenses_page(company_id):
    _global_admin_required()
    from models import Company, TenantLicense
    company = Company.query.get_or_404(company_id)
    licenses = TenantLicense.query.filter_by(company_id=company.id).all()
    return render_template("licenses/global_tenant_licenses.html", company=company, licenses=licenses)


@license_bp.route("/global-admin/billing")
@login_required
def global_billing_page():
    _global_admin_required()
    from models import TenantBillingAccount, TenantInvoice
    return render_template("licenses/global_billing.html", accounts=TenantBillingAccount.query.all(), invoices=TenantInvoice.query.order_by(TenantInvoice.created_at.desc()).limit(100).all())


@license_bp.route("/global-admin/billing/automations")
@login_required
def global_billing_automations_page():
    _global_admin_required()
    from models import BillingAutomationRule
    return render_template("licenses/global_automations.html", rules=BillingAutomationRule.query.order_by(BillingAutomationRule.created_at.desc()).all())


@license_bp.route("/global-admin/billing/templates")
@login_required
def global_billing_templates_page():
    _global_admin_required()
    from models import BillingEmailTemplate
    return render_template("licenses/global_templates.html", templates=BillingEmailTemplate.query.all())


@license_bp.route("/global-admin/billing/statements")
@login_required
def global_billing_statements_page():
    _global_admin_required()
    from models import TenantInvoice
    return render_template("licenses/global_statements.html", invoices=TenantInvoice.query.order_by(TenantInvoice.created_at.desc()).all())


@license_bp.route("/global-admin/features")
@login_required
def global_features_page():
    _global_admin_required()
    seed_feature_modules()
    from models import FeatureModule
    return render_template("licenses/global_features.html", modules=FeatureModule.query.order_by(FeatureModule.category, FeatureModule.name).all())


@license_bp.route("/api/global/licenses")
@login_required
def api_global_licenses():
    _global_admin_required()
    from models import TenantLicense
    return jsonify({"success": True, "licenses": [_license_to_dict(l) for l in TenantLicense.query.all()]})


@license_bp.route("/api/global/licenses/<int:license_id>/suspend", methods=["POST"])
@login_required
def api_global_license_suspend(license_id):
    _global_admin_required()
    from models import TenantLicense
    lic = TenantLicense.query.get_or_404(license_id)
    reason = (request.get_json() or {}).get("reason") or "manual_global_admin"
    lic = suspend_license(lic.company_id, lic.feature_key, reason, current_user.id, "global_admin")
    return jsonify({"success": True, "license": _license_to_dict(lic)})


@license_bp.route("/api/global/licenses/<int:license_id>/reactivate", methods=["POST"])
@login_required
def api_global_license_reactivate(license_id):
    _global_admin_required()
    from models import TenantLicense
    lic = TenantLicense.query.get_or_404(license_id)
    lic = reactivate_license(lic.company_id, lic.feature_key, current_user.id, "global_admin")
    return jsonify({"success": True, "license": _license_to_dict(lic)})


@license_bp.route("/api/global/licenses/<int:license_id>/cancel", methods=["POST"])
@login_required
def api_global_license_cancel(license_id):
    _global_admin_required()
    from models import TenantLicense
    lic = TenantLicense.query.get_or_404(license_id)
    lic = cancel_license(lic.company_id, lic.feature_key, current_user.id, "global_admin")
    return jsonify({"success": True, "license": _license_to_dict(lic)})


@license_bp.route("/api/global/licenses/grant", methods=["POST"])
@login_required
def api_global_license_grant():
    _global_admin_required()
    from models import TenantLicense
    data = request.get_json() or {}
    lic = get_company_license(int(data["company_id"]), data.get("feature_key") or PHONE_PWA_FEATURE)
    if not lic:
        lic = TenantLicense(company_id=int(data["company_id"]), feature_key=data.get("feature_key") or PHONE_PWA_FEATURE)
        db.session.add(lic)
    lic.status = data.get("status") or "active"
    lic.monthly_price = data.get("monthly_price", lic.monthly_price or 0)
    db.session.commit()
    return jsonify({"success": True, "license": _license_to_dict(lic)})


@license_bp.route("/api/global/features/<feature_key>", methods=["PATCH"])
@login_required
def api_global_feature_update(feature_key):
    _global_admin_required()
    from models import FeatureModule
    module = FeatureModule.query.filter_by(key=feature_key).first_or_404()
    data = request.get_json() or {}
    for field in ("name", "description", "category", "is_active", "default_monthly_price", "stripe_product_id"):
        if field in data:
            setattr(module, field, data[field])
    db.session.commit()
    return jsonify({"success": True, "feature": {"key": module.key, "name": module.name, "is_active": module.is_active}})


@license_bp.route("/api/global/billing/failed-payments")
@login_required
def api_global_failed_payments():
    _global_admin_required()
    from models import TenantInvoice
    rows = TenantInvoice.query.filter(TenantInvoice.status.in_(["open", "uncollectible", "past_due", "failed"])).all()
    return jsonify({"success": True, "invoices": [_invoice_to_dict(i) for i in rows]})


@license_bp.route("/api/global/billing/automations", methods=["GET", "POST"])
@login_required
def api_global_automations():
    _global_admin_required()
    from models import BillingAutomationRule
    if request.method == "POST":
        data = request.get_json() or {}
        rule = BillingAutomationRule(scope=data.get("scope") or "global", event_type=data["event_type"], action=data["action"], enabled=bool(data.get("enabled", True)), delay_days=int(data.get("delay_days") or 0), company_id=data.get("company_id"), created_by_user_id=current_user.id)
        db.session.add(rule); db.session.commit()
    rules = BillingAutomationRule.query.all()
    return jsonify({"success": True, "rules": [{"id": r.id, "event_type": r.event_type, "action": r.action, "enabled": r.enabled, "delay_days": r.delay_days} for r in rules]})


@license_bp.route("/api/global/billing/automations/<int:rule_id>", methods=["PATCH", "DELETE"])
@login_required
def api_global_automation_detail(rule_id):
    _global_admin_required()
    from models import BillingAutomationRule
    rule = BillingAutomationRule.query.get_or_404(rule_id)
    if request.method == "DELETE":
        db.session.delete(rule); db.session.commit(); return jsonify({"success": True})
    data = request.get_json() or {}
    for field in ("scope", "event_type", "action", "enabled", "delay_days", "email_template_id"):
        if field in data:
            setattr(rule, field, data[field])
    db.session.commit()
    return jsonify({"success": True, "rule": {"id": rule.id, "enabled": rule.enabled}})
