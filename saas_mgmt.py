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
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

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


_REDACT_KEYS = {
    "client_secret", "setup_secret", "secret", "api_key",
    "card", "cvc", "cvv", "number", "iban", "account_number",
}


def _sanitize_payload(value, depth=0):
    """Recursively redact secret-bearing fields from a Stripe payload.

    Stripe events typically do NOT contain raw card numbers (only last4),
    but we still strip any field name in `_REDACT_KEYS` as defense in depth
    so secrets never land in the audit log or application logs.
    """
    if depth > 10:
        return "<truncated>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _REDACT_KEYS:
                out[k] = "<redacted>"
            else:
                out[k] = _sanitize_payload(v, depth + 1)
        return out
    if isinstance(value, list):
        return [_sanitize_payload(v, depth + 1) for v in value]
    return value


def _log(event_type, source, company_id=None, payload=None, status="success",
         error=None, stripe_event_id=None, customer_id=None, subscription_id=None,
         processed_at=None):
    """Write an audit row to SaasAutomationLog.

    For Stripe events, dedicated columns (stripe_event_id, customer_id,
    subscription_id, received_at, processed_at) are populated so they're
    queryable without scanning the JSON payload.
    """
    from models import SaasAutomationLog

    sanitized = _sanitize_payload(payload) if payload is not None else None
    if source == "stripe" and stripe_event_id and isinstance(sanitized, dict):
        sanitized = dict(sanitized)
        sanitized["_stripe_event_id"] = stripe_event_id

    try:
        entry = SaasAutomationLog(
            company_id=company_id,
            event_type=event_type,
            source=source,
            stripe_event_id=stripe_event_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            payload=sanitized,
            status=status,
            error=error,
            received_at=datetime.utcnow(),
            processed_at=processed_at or (datetime.utcnow() if status != "failed" else None),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
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


# ---------------------------------------------------------------------------
# Stripe Checkout & Customer Portal — public-facing API
# ---------------------------------------------------------------------------

def _user_can_access_company(user, company):
    """Authorization check: does the current user have access to a company?"""
    if not user or not user.is_authenticated or not company:
        return False
    if getattr(user, "is_admin", False):
        return True
    if user.default_company_id == company.id:
        return True
    from models import UserCompanyAccess
    return UserCompanyAccess.query.filter_by(
        user_id=user.id, company_id=company.id
    ).first() is not None


@stripe_webhook_bp.route("/api/stripe/create-checkout-session", methods=["POST"])
@login_required
def create_checkout_session():
    """Create a Stripe Checkout Session for a subscription.

    Request JSON: ``{"lookup_key": "...", "company_id": "...", "user_id": "..."}``

    Security:
      - Auth required.
      - Frontend supplies ``lookup_key`` only — never a price ID or amount.
      - Caller must have access to ``company_id`` (admin or via membership).
    """
    from models import Company
    from services.stripe_billing import (
        create_checkout_session as _mk_session,
        ALL_KNOWN_LOOKUP_KEYS,
    )

    body = request.get_json(silent=True) or {}
    lookup_key       = (body.get("lookup_key") or "").strip()
    company_id       = body.get("company_id")
    user_id          = body.get("user_id") or current_user.id
    # Optional: include the one-time setup fee as a second line item. The
    # frontend may request it but the server is the final authority — if the
    # company already paid the setup fee we silently drop the request.
    include_setup_fee = bool(body.get("include_setup_fee", False))

    # Reject any attempt to bypass lookup_key with a raw price/amount.
    for forbidden in ("price_id", "price", "amount", "unit_amount"):
        if forbidden in body:
            return jsonify({
                "error": "frontend-supplied price IDs/amounts are not allowed; use lookup_key",
                "field": forbidden,
            }), 400

    if not lookup_key:
        return jsonify({"error": "lookup_key is required"}), 400
    if lookup_key not in ALL_KNOWN_LOOKUP_KEYS:
        return jsonify({"error": f"unknown lookup_key: {lookup_key}"}), 400

    # Resolve company: explicit company_id wins, else fall back to user's default.
    company = None
    if company_id:
        try:
            company = Company.query.get(int(company_id))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid company_id"}), 400
    if not company and current_user.default_company_id:
        company = Company.query.get(current_user.default_company_id)
    if not company:
        return jsonify({"error": "company not found"}), 404
    if not _user_can_access_company(current_user, company):
        return jsonify({"error": "forbidden"}), 403

    # Server-side guard: if this company has already paid the one-time setup
    # fee, never attach it again, no matter what the frontend asks for.
    apply_setup_fee = include_setup_fee and not bool(getattr(company, "setup_fee_paid", False))

    try:
        session = _mk_session(
            lookup_key,
            company_id=company.id,
            user_id=user_id,
            customer_id=company.stripe_customer_id or None,
            customer_email=getattr(current_user, "email", None) if not company.stripe_customer_id else None,
            include_setup_fee=apply_setup_fee,
        )
    except RuntimeError as exc:
        logger.error("Stripe checkout failed (config): %s", exc)
        return jsonify({"error": str(exc)}), 503
    except ValueError as exc:
        logger.warning("Stripe checkout rejected: %s", exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Stripe checkout session creation failed")
        return jsonify({"error": "checkout session creation failed"}), 502

    _log("checkout.session.created", "stripe",
         company_id=company.id,
         payload={
             "lookup_key": lookup_key,
             "session_id": getattr(session, "id", None),
             "include_setup_fee": apply_setup_fee,
         },
         customer_id=company.stripe_customer_id)
    return jsonify({
        "url":               getattr(session, "url", None),
        "session_id":        getattr(session, "id", None),
        "lookup_key":        lookup_key,
        "include_setup_fee": apply_setup_fee,
    }), 200


@stripe_webhook_bp.route("/api/stripe/create-portal-session", methods=["POST"])
@login_required
def create_portal_session():
    """Create a Stripe Customer Portal session for the company's customer.

    Request JSON: ``{"company_id": "..."}``
    """
    from models import Company
    from services.stripe_billing import create_billing_portal_session

    body = request.get_json(silent=True) or {}
    company_id = body.get("company_id")

    company = None
    if company_id:
        try:
            company = Company.query.get(int(company_id))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid company_id"}), 400
    if not company and current_user.default_company_id:
        company = Company.query.get(current_user.default_company_id)
    if not company:
        return jsonify({"error": "company not found"}), 404
    if not _user_can_access_company(current_user, company):
        return jsonify({"error": "forbidden"}), 403
    if not company.stripe_customer_id:
        return jsonify({"error": "no stripe customer on file for this company"}), 400

    try:
        session = create_billing_portal_session(company.stripe_customer_id)
    except RuntimeError as exc:
        logger.error("Stripe portal failed (config): %s", exc)
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        logger.exception("Stripe portal session creation failed")
        return jsonify({"error": "portal session creation failed"}), 502

    _log("portal.session.created", "stripe",
         company_id=company.id,
         payload={"session_id": getattr(session, "id", None)},
         customer_id=company.stripe_customer_id)
    return jsonify({
        "url":        getattr(session, "url", None),
        "session_id": getattr(session, "id", None),
    }), 200


@stripe_webhook_bp.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe webhook receiver.

    Security:
      - Only accepts POST (enforced by route declaration).
      - When `STRIPE_WEBHOOK_SECRET` is set, verifies `Stripe-Signature`
        via the Stripe SDK. Rejects with 400 if invalid.
      - When the secret is NOT set, only allows unsigned webhooks in
        development mode (FLASK_ENV=development or DEBUG=true). Production
        with no secret returns 503 to prevent silent acceptance of forged
        events.

    Audit:
      - Every received event (success / skipped / failed) is written to
        `SaasAutomationLog` with event_type, company_id, payload, and the
        Stripe event_id.

    Lifecycle wiring (5 events):
      - checkout.session.completed   → status=active, create onboarding
      - invoice.payment_succeeded    → status=active
      - invoice.payment_failed       → status=past_due
      - customer.subscription.updated→ status=<stripe status>
      - customer.subscription.deleted→ status=canceled
    """
    from models import Company, SaasLicense, CustomerOnboardingProject, CustomerOnboardingTask, SaasAutomationLog

    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    secret  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # Strict by default: only allow unsigned webhooks when the operator has
    # explicitly opted into dev mode. We do NOT key off REPLIT_DEPLOYMENT
    # because LUXit also runs on a VPS where that variable is never set.
    is_dev = (
        os.environ.get("FLASK_ENV") == "development"
        or os.environ.get("DEBUG", "").lower() == "true"
    )

    if secret:
        try:
            import stripe as stripe_lib
            # Stripe SDK v15's verify_header expects a *string* payload — passing
            # raw bytes silently produces a non-matching HMAC. Decode once here
            # (the raw bytes are still what we use for json parsing).
            payload_str = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
            stripe_lib.WebhookSignature.verify_header(payload_str, sig, secret, tolerance=300)
        except Exception as exc:
            logger.warning("Stripe webhook signature verification failed: %s", exc)
            _log("signature_invalid", "stripe", payload={"error": str(exc)[:300]},
                 status="failed", error="signature verification failed")
            abort(400)
        try:
            event = json.loads(payload)
        except Exception:
            logger.warning("Stripe webhook payload signed but not valid JSON")
            abort(400)
    else:
        if not is_dev:
            logger.error("Stripe webhook received but STRIPE_WEBHOOK_SECRET is not set in production — rejecting")
            _log("missing_secret", "stripe", payload=None, status="failed",
                 error="STRIPE_WEBHOOK_SECRET not configured")
            return jsonify({"error": "webhook secret not configured"}), 503
        try:
            event = json.loads(payload)
        except Exception:
            logger.warning("Stripe webhook payload is not valid JSON")
            abort(400)

    ev_id   = event.get("id", "")
    ev_type = event.get("type", "")
    livemode = event.get("livemode", False)
    obj     = event.get("data", {}).get("object", {}) or {}

    company    = None
    company_id = None
    stripe_cid = obj.get("customer")
    if stripe_cid:
        company = Company.query.filter_by(stripe_customer_id=stripe_cid).first()
        if company:
            company_id = company.id

    logger.info(
        "Stripe webhook received: type=%s event_id=%s livemode=%s customer=%s company_id=%s",
        ev_type, ev_id, livemode, stripe_cid, company_id,
    )

    # Atomic idempotency claim: insert a placeholder audit row keyed on the
    # unique stripe_event_id BEFORE any side effects. Two concurrent deliveries
    # cannot both pass this gate — the second one hits IntegrityError and is
    # treated as a duplicate. This avoids the race-window of a SELECT-then-INSERT
    # pattern.
    claim_row = None
    if ev_id:
        sanitized_initial = _sanitize_payload(obj)
        if isinstance(sanitized_initial, dict):
            sanitized_initial = dict(sanitized_initial)
            sanitized_initial["_stripe_event_id"] = ev_id
        try:
            claim_row = SaasAutomationLog(
                company_id=company_id,
                event_type=ev_type,
                source="stripe",
                stripe_event_id=ev_id,
                customer_id=stripe_cid,
                payload=sanitized_initial,
                status="processing",
                received_at=datetime.utcnow(),
            )
            db.session.add(claim_row)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # Look up the existing claim. If it represents a prior FAILED attempt
            # — or a 'processing' row that's gone stale (worker crashed mid-event)
            # — we should let Stripe's retry actually reprocess instead of
            # short-circuiting. Only completed states (success/skipped/duplicate)
            # are treated as true duplicates.
            existing = SaasAutomationLog.query.filter_by(stripe_event_id=ev_id).first()
            STALE_THRESHOLD = timedelta(minutes=5)
            now = datetime.utcnow()
            is_retryable = False
            if existing is not None:
                if existing.status == "failed":
                    is_retryable = True
                elif existing.status == "processing":
                    started = existing.received_at or existing.created_at
                    if started and (now - started) > STALE_THRESHOLD:
                        is_retryable = True

            if is_retryable:
                logger.info(
                    "Stripe[%s] previous attempt status=%s — taking over for reprocessing",
                    ev_id, existing.status,
                )
                try:
                    existing.status = "processing"
                    existing.error = None
                    existing.received_at = now
                    existing.processed_at = None
                    db.session.commit()
                    claim_row = existing
                except Exception as exc:
                    db.session.rollback()
                    logger.warning("Failed to reclaim row for retry, continuing without claim: %s", exc)
                    claim_row = None
                # fall through to event processing below
            else:
                logger.info(
                    "Stripe[%s] duplicate event (prior status=%s) — acknowledging without reprocessing",
                    ev_id, existing.status if existing else "unknown",
                )
                # Audit the duplicate delivery itself with a non-conflicting row.
                try:
                    dup = SaasAutomationLog(
                        company_id=company_id,
                        event_type=ev_type,
                        source="stripe",
                        stripe_event_id=None,  # avoid unique conflict on retries
                        customer_id=stripe_cid,
                        payload={"_duplicate_of": ev_id, "event_type": ev_type},
                        status="duplicate",
                        received_at=now,
                        processed_at=now,
                    )
                    db.session.add(dup)
                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    logger.warning("Failed to write duplicate audit row: %s", exc)
                return jsonify({"received": True, "event_id": ev_id, "duplicate": True}), 200
        except Exception as exc:
            db.session.rollback()
            logger.warning("Idempotency claim insert failed (continuing without claim): %s", exc)
            claim_row = None

    def _finalize(status, error=None, subscription_id=None):
        """Update the claim row in-place to reflect final outcome.

        Falls back to a fresh _log() write if no claim row was created
        (e.g. when stripe_event_id is missing or the claim insert itself
        crashed for non-uniqueness reasons).
        """
        if claim_row is not None:
            try:
                claim_row.status = status
                claim_row.error = (error or "")[:500] if error else None
                claim_row.processed_at = datetime.utcnow()
                if subscription_id:
                    claim_row.subscription_id = subscription_id
                db.session.commit()
                return
            except Exception as exc:
                db.session.rollback()
                logger.warning("Failed to update claim row, falling back to new audit row: %s", exc)
        _log(ev_type, "stripe", company_id=company_id, payload=obj,
             status=status, error=error, stripe_event_id=ev_id,
             customer_id=stripe_cid, subscription_id=subscription_id)

    if not company and stripe_cid:
        logger.warning("Stripe webhook %s for unknown customer %s — logging only", ev_type, stripe_cid)

    try:
        if ev_type == "customer.created":
            logger.info("Stripe[%s] customer.created cid=%s", ev_id, stripe_cid)
            _finalize("success")

        elif ev_type == "checkout.session.completed":
            sub_id = obj.get("subscription")
            prev_status = company.stripe_subscription_status if company else None
            metadata = obj.get("metadata") or {}
            lookup_key = metadata.get("lookup_key")
            client_ref = obj.get("client_reference_id")
            # If we couldn't route by stripe_customer_id but client_reference_id
            # carries a company_id, recover the company here.
            if not company and client_ref:
                try:
                    cid_int = int(client_ref)
                    company = Company.query.get(cid_int)
                    if company:
                        company_id = company.id
                        if stripe_cid and not company.stripe_customer_id:
                            company.stripe_customer_id = stripe_cid
                except (TypeError, ValueError):
                    pass
            logger.info(
                "Stripe[%s] checkout.session.completed cid=%s subscription=%s lookup_key=%s prev_status=%s",
                ev_id, stripe_cid, sub_id, lookup_key, prev_status,
            )
            if company and sub_id:
                from services.stripe_billing import (
                    tier_for_lookup_key, TIER_BY_LOOKUP_KEY,
                )
                company.stripe_subscription_id     = sub_id
                company.stripe_subscription_status = "active"
                company.billing_status             = "active"
                company.grace_period_ends_at       = None
                # Only apply tier+seats when the lookup_key is a real tier.
                # Add-on lookup keys (luxit_additional_account_monthly) are
                # not tiers and would corrupt billing_tier/max_team_members
                # if applied. The authoritative source for seats including
                # add-ons is the customer.subscription.updated handler.
                if lookup_key and lookup_key in TIER_BY_LOOKUP_KEY:
                    tier, seats = tier_for_lookup_key(lookup_key)
                    company.stripe_price_lookup_key = lookup_key
                    company.billing_tier            = tier
                    company.subscription_tier       = tier
                    company.max_team_members        = seats  # None = unlimited

                # ── One-time setup fee fulfillment ─────────────────────────
                # The session metadata (set server-side at create time) is
                # the authoritative record of whether the setup fee line
                # item was attached. We mark setup_fee_paid exactly once;
                # idempotency on stripe_event_id (claim_row above) prevents
                # double-processing if Stripe retries this event.
                if (
                    metadata.get("include_setup_fee") == "true"
                    and not getattr(company, "setup_fee_paid", False)
                ):
                    from datetime import datetime as _dt
                    company.setup_fee_paid                = True
                    company.setup_fee_paid_at             = _dt.utcnow()
                    company.setup_fee_checkout_session_id = obj.get("id")
                    logger.info(
                        "Stripe[%s] company %s setup_fee_paid=True session=%s",
                        ev_id, company.id, obj.get("id"),
                    )
                db.session.commit()
                logger.info(
                    "Stripe[%s] company %s billing_status: %s → active",
                    ev_id, company.id, prev_status,
                )
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
                    logger.info("Stripe[%s] onboarding project %s created for company %s", ev_id, proj.id, company.id)
                    _fire_n8n("onboarding_started", company.id, {"project_id": proj.id})
                _fire_n8n("subscription_activated", company.id, {"stripe_event": ev_type, "subscription_id": sub_id})
            _finalize("success", subscription_id=sub_id)

        elif ev_type == "invoice.payment_succeeded":
            inv_id = obj.get("id")
            amount = obj.get("amount_paid")
            prev_status = company.stripe_subscription_status if company else None
            logger.info(
                "Stripe[%s] invoice.payment_succeeded invoice=%s amount=%s cid=%s prev_status=%s",
                ev_id, inv_id, amount, stripe_cid, prev_status,
            )
            sub_id_inv = obj.get("subscription")
            if company:
                company.stripe_subscription_status = "active"
                company.billing_status             = "active"
                company.grace_period_ends_at       = None  # clear any prior dunning window
                db.session.commit()
                logger.info("Stripe[%s] company %s billing_status: %s → active", ev_id, company.id, prev_status)
                _fire_n8n("invoice_paid", company.id, {"invoice_id": inv_id, "amount": amount})
            _finalize("success", subscription_id=sub_id_inv)

        elif ev_type == "invoice.payment_failed":
            inv_id = obj.get("id")
            attempt = obj.get("attempt_count")
            sub_id_inv = obj.get("subscription")
            prev_status = company.stripe_subscription_status if company else None
            logger.warning(
                "Stripe[%s] invoice.payment_failed invoice=%s attempt=%s cid=%s prev_status=%s",
                ev_id, inv_id, attempt, stripe_cid, prev_status,
            )
            if company:
                # Failed payment moves the tenant into the dunning/grace window
                # rather than instant suspension. Stripe Smart Retries plus
                # subscription.deleted will eventually finalize cancellation.
                from services.stripe_billing import grace_period_end
                company.stripe_subscription_status = "grace_period"
                company.billing_status             = "grace_period"
                # Only set grace_period_ends_at on the first failed attempt
                # so retries don't keep extending the deadline.
                if not company.grace_period_ends_at:
                    company.grace_period_ends_at = grace_period_end()
                db.session.commit()
                logger.info("Stripe[%s] company %s billing_status: %s → grace_period (until %s)",
                            ev_id, company.id, prev_status, company.grace_period_ends_at)
                _fire_n8n("payment_failed", company.id, {"invoice_id": inv_id, "attempt": attempt})
            _finalize("success", subscription_id=sub_id_inv)

        elif ev_type == "customer.subscription.updated":
            sub_id = obj.get("id")
            new_status = obj.get("status", "")
            prev_status = company.stripe_subscription_status if company else None
            logger.info(
                "Stripe[%s] customer.subscription.updated subscription=%s new_status=%s cid=%s prev_status=%s",
                ev_id, sub_id, new_status, stripe_cid, prev_status,
            )
            if company:
                from services.stripe_billing import compute_seats_from_subscription, stripe_ts_to_dt
                company.stripe_subscription_status = new_status
                if sub_id:
                    company.stripe_subscription_id = sub_id
                # Sync billing period + cancel-at-period-end so the UI can
                # show renewal date and "scheduled to cancel" notices.
                company.current_period_start = stripe_ts_to_dt(obj.get("current_period_start"))
                company.current_period_end   = stripe_ts_to_dt(obj.get("current_period_end"))
                company.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
                # Recompute tier + seat allowance from the current set of
                # subscription items (handles add-on quantity changes).
                tier, seats, primary_lookup = compute_seats_from_subscription(obj)
                if primary_lookup:
                    company.stripe_price_lookup_key = primary_lookup
                    company.billing_tier            = tier
                    company.subscription_tier       = tier
                    company.max_team_members        = seats
                # Map Stripe sub status → our billing_status.
                if new_status in ("active", "trialing"):
                    company.billing_status = "active"
                    company.grace_period_ends_at = None
                elif new_status == "past_due":
                    company.billing_status = "grace_period"
                elif new_status in ("canceled", "unpaid", "incomplete_expired"):
                    company.billing_status = "canceled"
                elif new_status == "incomplete":
                    company.billing_status = "incomplete"
                db.session.commit()
                logger.info("Stripe[%s] company %s status: %s → %s (tier=%s, seats=%s, periods=%s..%s, cancel_at_end=%s)",
                            ev_id, company.id, prev_status, new_status, tier, seats,
                            company.current_period_start, company.current_period_end,
                            company.cancel_at_period_end)
                if new_status == "canceled":
                    _fire_n8n("customer_canceled", company.id, {"subscription_id": sub_id})
                elif new_status == "past_due":
                    _fire_n8n("subscription_past_due", company.id, {"subscription_id": sub_id})
            _finalize("success", subscription_id=sub_id)

        elif ev_type == "customer.subscription.deleted":
            sub_id = obj.get("id")
            prev_status = company.stripe_subscription_status if company else None
            logger.info(
                "Stripe[%s] customer.subscription.deleted subscription=%s cid=%s prev_status=%s",
                ev_id, sub_id, stripe_cid, prev_status,
            )
            if company:
                # Set "suspended" so the UI can distinguish a deleted
                # subscription from a status='canceled' that's still in its
                # paid-through window. Data is preserved.
                company.stripe_subscription_status = "canceled"
                company.billing_status             = "suspended"
                company.cancel_at_period_end       = False
                db.session.commit()
                logger.info("Stripe[%s] company %s billing_status: %s → suspended", ev_id, company.id, prev_status)
                _fire_n8n("customer_canceled", company.id, {"subscription_id": sub_id})
            _finalize("success", subscription_id=sub_id)

        else:
            logger.info("Stripe[%s] unhandled event type=%s — logging as skipped", ev_id, ev_type)
            _finalize("skipped")

    except Exception as exc:
        logger.exception("Stripe webhook processing error for event %s (%s): %s", ev_id, ev_type, exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        _finalize("failed", error=str(exc))
        # Return 5xx so Stripe will retry the delivery on transient errors.
        return jsonify({
            "received": False,
            "event_id": ev_id,
            "type": ev_type,
            "error": "processing failed",
        }), 500

    return jsonify({"received": True, "event_id": ev_id, "type": ev_type}), 200
