"""
SaaS Command Center — blueprint for LUXit.app SaaS management.

Routes (all require login + admin):
  GET  /saas                          — dashboard
  GET  /saas/accounts                 — account list
  GET  /saas/accounts/<id>            — account detail
  POST /saas/accounts/<id>/edit       — update SaaS fields
  POST /saas/licenses/create          — create license
  POST /saas/licenses/<id>/edit       — update license
  POST /saas/licenses/<id>/delete     — delete license
  GET  /saas/onboarding               — onboarding project list
  POST /saas/onboarding/create        — create project (+ seed default tasks)
  POST /saas/onboarding/<id>/task     — add task to project
  POST /saas/onboarding/task/<tid>/toggle  — mark task complete / pending
  POST /saas/deals/create             — create deal
  POST /saas/deals/<id>/stage         — update deal stage

Webhook / API:
  POST /api/stripe/webhook            — Stripe event handler
  POST /api/saas/n8n/trigger          — fire n8n webhook for an event
  GET  /api/saas/automation-log       — recent log (JSON)
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import requests
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db

logger = logging.getLogger(__name__)

saas_bp = Blueprint("saas", __name__, url_prefix="/saas")

# ---------------------------------------------------------------------------
# Default onboarding checklist applied to every new project
# ---------------------------------------------------------------------------
DEFAULT_ONBOARDING_TASKS = [
    "Confirm business details",
    "Configure brand kit (logo, colors)",
    "Create admin user",
    "Provision tenant / sub-domain",
    "Connect billing (Stripe / MyPayLink)",
    "Configure SMS / email settings",
    "Test login and core features",
    "Go-live approval",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_company():
    from models import Company, UserCompanyAccess
    if current_user.default_company_id:
        return Company.query.get(current_user.default_company_id)
    access = UserCompanyAccess.query.filter_by(user_id=current_user.id).first()
    return Company.query.get(access.company_id) if access else None


def _log(event_type, source, company_id=None, payload=None, status="success", error=None):
    from models import SaasAutomationLog
    try:
        entry = SaasAutomationLog(
            company_id=company_id,
            event_type=event_type,
            source=source,
            payload=payload,
            status=status,
            error=error,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        logger.warning("SaasAutomationLog write failed: %s", exc)


def _fire_n8n(event_type: str, company_id: int, payload: dict):
    """POST event to n8n webhook URL — delegates to shared n8n_service."""
    from services.n8n_service import fire_n8n
    fire_n8n(event_type, company_id, payload)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@saas_bp.route("/")
@login_required
def dashboard():
    from models import Company, SaasLicense, CustomerOnboardingProject, Deal, SaasAutomationLog
    company = _get_company()

    accounts   = Company.query.filter_by(is_active=True).order_by(Company.created_at.desc()).all()
    licenses   = SaasLicense.query.order_by(SaasLicense.created_at.desc()).limit(20).all()
    projects   = CustomerOnboardingProject.query.order_by(CustomerOnboardingProject.created_at.desc()).limit(20).all()
    deals      = Deal.query.filter_by(company_id=company.id if company else None).order_by(Deal.created_at.desc()).all() if company else []
    recent_log = SaasAutomationLog.query.order_by(SaasAutomationLog.created_at.desc()).limit(50).all()

    stats = {
        "total_accounts": len(accounts),
        "active_licenses": SaasLicense.query.filter_by(status="active").count(),
        "pending_onboarding": CustomerOnboardingProject.query.filter(
            CustomerOnboardingProject.status.in_(["pending", "in_progress"])
        ).count(),
        "total_deals": len(deals),
    }

    return render_template(
        "saas/dashboard.html",
        company=company,
        accounts=accounts,
        licenses=licenses,
        projects=projects,
        deals=deals,
        recent_log=recent_log,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@saas_bp.route("/accounts/<int:account_id>/edit", methods=["POST"])
@login_required
def edit_account(account_id):
    from models import Company
    acct = Company.query.get_or_404(account_id)
    fields = [
        "stripe_customer_id", "stripe_subscription_id", "stripe_subscription_status",
        "supabase_tenant_id", "mypaylink_id", "n8n_contact_id",
        "subscription_tier", "onboarding_status", "implementation_status", "saas_notes",
    ]
    for f in fields:
        val = request.form.get(f, "").strip()
        if val != "":
            setattr(acct, f, val)
    db.session.commit()
    flash(f"Account '{acct.name}' updated.", "success")
    _log("account.updated", "manual", company_id=acct.id, payload={"account_id": acct.id})
    return redirect(url_for("saas.dashboard") + "#accounts")


# ---------------------------------------------------------------------------
# Licenses
# ---------------------------------------------------------------------------

@saas_bp.route("/licenses/create", methods=["POST"])
@login_required
def create_license():
    from models import SaasLicense
    lic = SaasLicense(
        company_id       = int(request.form["company_id"]),
        app_name         = request.form.get("app_name", "LUXit").strip(),
        plan             = request.form.get("plan", "starter").strip(),
        status           = request.form.get("status", "trial").strip(),
        tenant_url       = request.form.get("tenant_url", "").strip() or None,
        stripe_product_id= request.form.get("stripe_product_id", "").strip() or None,
        stripe_price_id  = request.form.get("stripe_price_id", "").strip() or None,
        notes            = request.form.get("notes", "").strip() or None,
    )
    start = request.form.get("start_date")
    renew = request.form.get("renewal_date")
    if start:
        try:
            lic.start_date = datetime.fromisoformat(start)
        except ValueError:
            pass
    if renew:
        try:
            lic.renewal_date = datetime.fromisoformat(renew)
        except ValueError:
            pass

    db.session.add(lic)
    db.session.commit()
    _log("license.created", "manual", company_id=lic.company_id,
         payload={"license_id": lic.id, "app": lic.app_name})
    flash("License created.", "success")
    return redirect(url_for("saas.dashboard") + "#licenses")


@saas_bp.route("/licenses/<int:lic_id>/edit", methods=["POST"])
@login_required
def edit_license(lic_id):
    from models import SaasLicense
    lic = SaasLicense.query.get_or_404(lic_id)
    for f in ["app_name", "plan", "status", "tenant_url", "stripe_product_id",
              "stripe_price_id", "notes"]:
        val = request.form.get(f, "").strip()
        if val != "":
            setattr(lic, f, val)
    for df in ["start_date", "renewal_date"]:
        val = request.form.get(df, "").strip()
        if val:
            try:
                setattr(lic, df, datetime.fromisoformat(val))
            except ValueError:
                pass
    db.session.commit()
    flash("License updated.", "success")
    return redirect(url_for("saas.dashboard") + "#licenses")


@saas_bp.route("/licenses/<int:lic_id>/delete", methods=["POST"])
@login_required
def delete_license(lic_id):
    from models import SaasLicense
    lic = SaasLicense.query.get_or_404(lic_id)
    db.session.delete(lic)
    db.session.commit()
    flash("License deleted.", "success")
    return redirect(url_for("saas.dashboard") + "#licenses")


# ---------------------------------------------------------------------------
# Onboarding Projects
# ---------------------------------------------------------------------------

@saas_bp.route("/onboarding/create", methods=["POST"])
@login_required
def create_onboarding_project():
    from models import CustomerOnboardingProject, CustomerOnboardingTask
    company_id = int(request.form["company_id"])
    proj = CustomerOnboardingProject(
        company_id = company_id,
        contact_id = int(request.form["contact_id"]) if request.form.get("contact_id") else None,
        deal_id    = int(request.form["deal_id"])    if request.form.get("deal_id")    else None,
        title      = request.form.get("title", "Onboarding Project").strip(),
        status     = "pending",
        notes      = request.form.get("notes", "").strip() or None,
    )
    db.session.add(proj)
    db.session.flush()

    for i, task_title in enumerate(DEFAULT_ONBOARDING_TASKS):
        db.session.add(CustomerOnboardingTask(
            project_id = proj.id,
            company_id = company_id,
            title      = task_title,
            sort_order = i,
        ))

    db.session.commit()
    _log("onboarding.created", "manual", company_id=company_id,
         payload={"project_id": proj.id, "title": proj.title})
    _fire_n8n("onboarding_started", company_id, {"project_id": proj.id})
    flash(f"Onboarding project '{proj.title}' created with {len(DEFAULT_ONBOARDING_TASKS)} tasks.", "success")
    return redirect(url_for("saas.dashboard") + "#onboarding")


@saas_bp.route("/onboarding/<int:proj_id>/task", methods=["POST"])
@login_required
def add_task(proj_id):
    from models import CustomerOnboardingProject, CustomerOnboardingTask
    proj = CustomerOnboardingProject.query.get_or_404(proj_id)
    task = CustomerOnboardingTask(
        project_id = proj.id,
        company_id = proj.company_id,
        title      = request.form.get("title", "").strip(),
        description= request.form.get("description", "").strip() or None,
        assigned_to= request.form.get("assigned_to", "").strip() or None,
        sort_order = len(proj.tasks),
    )
    db.session.add(task)
    db.session.commit()
    flash("Task added.", "success")
    return redirect(url_for("saas.dashboard") + "#onboarding")


@saas_bp.route("/onboarding/task/<int:task_id>/toggle", methods=["POST"])
@login_required
def toggle_task(task_id):
    from models import CustomerOnboardingTask, CustomerOnboardingProject
    task = CustomerOnboardingTask.query.get_or_404(task_id)
    if task.status == "completed":
        task.status       = "pending"
        task.completed_at = None
    else:
        task.status       = "completed"
        task.completed_at = datetime.utcnow()

    proj = CustomerOnboardingProject.query.get(task.project_id)
    if proj:
        all_tasks = proj.tasks
        if all_tasks and all(t.status == "completed" for t in all_tasks):
            proj.status       = "completed"
            proj.completed_at = datetime.utcnow()
            _fire_n8n("onboarding_completed", proj.company_id, {"project_id": proj.id})
        elif any(t.status == "completed" for t in all_tasks):
            proj.status = "in_progress"

    db.session.commit()
    return jsonify({"status": task.status, "ok": True})


@saas_bp.route("/onboarding/<int:proj_id>/status", methods=["POST"])
@login_required
def update_project_status(proj_id):
    from models import CustomerOnboardingProject
    proj = CustomerOnboardingProject.query.get_or_404(proj_id)
    proj.status = request.form.get("status", proj.status)
    if proj.status == "completed" and not proj.completed_at:
        proj.completed_at = datetime.utcnow()
    db.session.commit()
    flash("Project status updated.", "success")
    return redirect(url_for("saas.dashboard") + "#onboarding")


# ---------------------------------------------------------------------------
# Deals (SaaS pipeline)
# ---------------------------------------------------------------------------

SAAS_STAGES = [
    "Lead", "Demo Requested", "Proposal Sent",
    "Contract Signed", "Paid", "Implementation Started", "Active Customer",
]

@saas_bp.route("/deals/create", methods=["POST"])
@login_required
def create_deal():
    from models import Deal
    company = _get_company()
    deal = Deal(
        company_id = int(request.form.get("company_id") or (company.id if company else 0)),
        contact_id = int(request.form["contact_id"]) if request.form.get("contact_id") else None,
        name       = request.form.get("name", "New Deal").strip(),
        stage      = request.form.get("stage", "Lead").strip(),
        value      = float(request.form.get("value") or 0),
        notes      = request.form.get("notes", "").strip() or None,
    )
    db.session.add(deal)
    db.session.commit()
    _log("deal.created", "manual", company_id=deal.company_id,
         payload={"deal_id": deal.id, "stage": deal.stage})
    if deal.stage in ("Paid", "Active Customer"):
        _fire_n8n("subscription_activated", deal.company_id, {"deal_id": deal.id})
    flash("Deal created.", "success")
    return redirect(url_for("saas.dashboard") + "#pipeline")


@saas_bp.route("/deals/<int:deal_id>/stage", methods=["POST"])
@login_required
def update_deal_stage(deal_id):
    from models import Deal
    deal = Deal.query.get_or_404(deal_id)
    old_stage  = deal.stage
    deal.stage = request.form.get("stage", deal.stage)
    db.session.commit()
    _log("deal.stage_changed", "manual", company_id=deal.company_id,
         payload={"deal_id": deal.id, "from": old_stage, "to": deal.stage})
    if deal.stage == "Paid" and old_stage != "Paid":
        _fire_n8n("subscription_activated", deal.company_id, {"deal_id": deal.id})
    flash(f"Deal moved to '{deal.stage}'.", "success")
    return redirect(url_for("saas.dashboard") + "#pipeline")


# ---------------------------------------------------------------------------
# n8n manual trigger
# ---------------------------------------------------------------------------

@saas_bp.route("/n8n/trigger", methods=["POST"])
@login_required
def manual_n8n_trigger():
    data       = request.get_json(force=True, silent=True) or {}
    event_type = data.get("event_type", "manual_trigger")
    company_id = data.get("company_id") or (c.id if (c := _get_company()) else None)
    _fire_n8n(event_type, company_id, data.get("payload", {}))
    return jsonify({"ok": True, "event": event_type})


# ---------------------------------------------------------------------------
# Automation log API
# ---------------------------------------------------------------------------

@saas_bp.route("/automation-log")
@login_required
def automation_log_api():
    from models import SaasAutomationLog
    logs = SaasAutomationLog.query.order_by(SaasAutomationLog.created_at.desc()).limit(100).all()
    return jsonify([{
        "id": l.id, "event_type": l.event_type, "source": l.source,
        "company_id": l.company_id, "status": l.status,
        "error": l.error, "created_at": l.created_at.isoformat(),
    } for l in logs])


# ---------------------------------------------------------------------------
# Stripe webhook
# ---------------------------------------------------------------------------

stripe_webhook_bp = Blueprint("stripe_webhook", __name__)


@stripe_webhook_bp.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    from models import Company, SaasLicense, CustomerOnboardingProject, CustomerOnboardingTask

    payload   = request.get_data()
    sig       = request.headers.get("Stripe-Signature", "")
    secret    = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if secret:
        try:
            import stripe as stripe_lib
            event = stripe_lib.Webhook.construct_event(payload, sig, secret)
        except Exception as exc:
            logger.warning("Stripe webhook signature failed: %s", exc)
            abort(400)
    else:
        try:
            event = json.loads(payload)
        except Exception:
            abort(400)

    ev_type = event.get("type", "")
    obj     = event.get("data", {}).get("object", {})
    logger.info("Stripe webhook: %s", ev_type)

    company    = None
    company_id = None

    stripe_cid = obj.get("customer")
    if stripe_cid:
        company = Company.query.filter_by(stripe_customer_id=stripe_cid).first()
        if company:
            company_id = company.id

    try:
        if ev_type == "customer.created":
            if company:
                pass  # already linked
            _log(ev_type, "stripe", company_id=company_id, payload=obj)

        elif ev_type == "checkout.session.completed":
            sub_id = obj.get("subscription")
            if company and sub_id:
                company.stripe_subscription_id     = sub_id
                company.stripe_subscription_status = "active"
                db.session.commit()
                # Create onboarding project if none exists
                existing = CustomerOnboardingProject.query.filter_by(company_id=company.id).first()
                if not existing:
                    proj = CustomerOnboardingProject(
                        company_id=company.id,
                        title=f"Onboarding — {company.name}",
                        status="pending",
                    )
                    db.session.add(proj)
                    db.session.flush()
                    for i, t in enumerate(DEFAULT_ONBOARDING_TASKS):
                        db.session.add(CustomerOnboardingTask(
                            project_id=proj.id, company_id=company.id,
                            title=t, sort_order=i,
                        ))
                    db.session.commit()
                    _fire_n8n("onboarding_started", company.id, {"project_id": proj.id})
                _fire_n8n("subscription_activated", company.id, {"stripe_event": ev_type})
            _log(ev_type, "stripe", company_id=company_id, payload=obj)

        elif ev_type == "invoice.payment_succeeded":
            if company:
                company.stripe_subscription_status = "active"
                db.session.commit()
                _fire_n8n("invoice_paid", company.id, {"invoice_id": obj.get("id")})
            _log(ev_type, "stripe", company_id=company_id, payload=obj)

        elif ev_type == "invoice.payment_failed":
            if company:
                company.stripe_subscription_status = "past_due"
                db.session.commit()
                _fire_n8n("payment_failed", company.id, {"invoice_id": obj.get("id")})
            _log(ev_type, "stripe", company_id=company_id, payload=obj)

        elif ev_type == "customer.subscription.updated":
            status = obj.get("status", "")
            if company:
                company.stripe_subscription_status = status
                company.stripe_subscription_id     = obj.get("id", company.stripe_subscription_id)
                db.session.commit()
                if status == "canceled":
                    _fire_n8n("customer_canceled", company.id, {"subscription_id": obj.get("id")})
            _log(ev_type, "stripe", company_id=company_id, payload=obj)

        elif ev_type == "customer.subscription.deleted":
            if company:
                company.stripe_subscription_status = "canceled"
                db.session.commit()
                _fire_n8n("customer_canceled", company.id, {"subscription_id": obj.get("id")})
            _log(ev_type, "stripe", company_id=company_id, payload=obj)

        else:
            _log(ev_type, "stripe", company_id=company_id, payload={"event_type": ev_type}, status="skipped")

    except Exception as exc:
        logger.exception("Stripe webhook processing error: %s", exc)
        _log(ev_type, "stripe", company_id=company_id, payload=obj, status="failed", error=str(exc))

    return jsonify({"received": True}), 200
