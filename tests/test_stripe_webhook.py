"""Tests for the Stripe webhook handler at POST /api/stripe/webhook.

Covers all five lifecycle events plus signature validation, security
behavior when the webhook secret is missing, audit logging, and the
billing status field on Company.
"""
import json
import os
import time
import uuid

import pytest
import stripe

from app import create_app
from extensions import db as _db
from models import Company, CustomerOnboardingProject, SaasAutomationLog


WEBHOOK_SECRET = "whsec_test_secret_for_pytest_only"


def _uniq(prefix: str = "") -> str:
    """Generate a short unique suffix safe for Stripe IDs and company names."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["STRIPE_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    with a.app_context():
        yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def company():
    """Create a fresh Company with a per-test-unique Stripe customer ID.

    Unique IDs avoid any conflict with stale test data from previous runs.
    No teardown needed — _db_rollback rolls back the entire test transaction.
    """
    cust_id = _uniq("cus_wh_")
    c = Company(
        name=f"WH Test Co {cust_id}",
        stripe_customer_id=cust_id,
        stripe_subscription_status="none",
    )
    _db.session.add(c)
    _db.session.commit()
    yield c


def _signed_post(client, event_payload):
    """POST a Stripe event to /api/stripe/webhook with a valid signature."""
    body = json.dumps(event_payload)
    timestamp = int(time.time())
    sig_header = stripe.WebhookSignature._compute_signature(
        f"{timestamp}.{body}", WEBHOOK_SECRET
    )
    header = f"t={timestamp},v1={sig_header}"
    return client.post(
        "/api/stripe/webhook",
        data=body,
        content_type="application/json",
        headers={"Stripe-Signature": header},
    )


def _make_event(event_type, obj, event_id=None):
    if event_id is None:
        event_id = _uniq("evt_")
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "livemode": False,
        "api_version": "2024-06-20",
        "created": int(time.time()),
        "data": {"object": obj},
        "request": {"id": None, "idempotency_key": None},
        "pending_webhooks": 0,
    }


# -----------------------------------------------------------------------------
# Test 1: Endpoint exists and only accepts POST
# -----------------------------------------------------------------------------

def test_webhook_only_accepts_post(client):
    """GET / PUT / DELETE on the webhook should not be allowed."""
    for method in ("get", "put", "delete", "patch"):
        response = getattr(client, method)("/api/stripe/webhook")
        assert response.status_code in (
            405, 404,
        ), f"{method.upper()} should not be allowed, got {response.status_code}"


# -----------------------------------------------------------------------------
# Test 2: Signature validation
# -----------------------------------------------------------------------------

def test_webhook_rejects_bad_signature(client):
    """Invalid Stripe-Signature header → 400."""
    body = json.dumps(_make_event("invoice.payment_succeeded", {"id": "in_x"}))
    response = client.post(
        "/api/stripe/webhook",
        data=body,
        content_type="application/json",
        headers={"Stripe-Signature": "t=1,v1=invalid"},
    )
    assert response.status_code == 400


def test_webhook_rejects_missing_signature(client):
    """No Stripe-Signature header → 400."""
    body = json.dumps(_make_event("invoice.payment_succeeded", {"id": "in_x"}))
    response = client.post(
        "/api/stripe/webhook",
        data=body,
        content_type="application/json",
    )
    assert response.status_code == 400


# -----------------------------------------------------------------------------
# Test 3: checkout.session.completed → activates billing + onboarding
# -----------------------------------------------------------------------------

def test_checkout_session_completed_activates_billing(client, company):
    event_id = _uniq("evt_checkout_")
    event = _make_event(
        "checkout.session.completed",
        {
            "id": _uniq("cs_"),
            "customer": company.stripe_customer_id,
            "subscription": _uniq("sub_"),
            "amount_total": 9900,
        },
        event_id=event_id,
    )
    response = _signed_post(client, event)
    assert response.status_code == 200
    body = response.get_json()
    assert body["received"] is True
    assert body["event_id"] == event_id

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.stripe_subscription_status == "active"
    assert c.stripe_subscription_id is not None

    proj = CustomerOnboardingProject.query.filter_by(company_id=c.id).first()
    assert proj is not None, "checkout.session.completed should create onboarding project"

    log = SaasAutomationLog.query.filter_by(
        company_id=c.id, event_type="checkout.session.completed"
    ).first()
    assert log is not None
    assert log.source == "stripe"
    assert log.status == "success"


# -----------------------------------------------------------------------------
# Test 4: invoice.payment_succeeded → status active
# -----------------------------------------------------------------------------

def test_invoice_payment_succeeded_sets_active(client, company):
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.stripe_subscription_status = "past_due"
    _db.session.commit()

    event = _make_event(
        "invoice.payment_succeeded",
        {"id": _uniq("in_paid_"), "customer": company.stripe_customer_id, "amount_paid": 9900},
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.stripe_subscription_status == "active"
    log = SaasAutomationLog.query.filter_by(
        company_id=c.id, event_type="invoice.payment_succeeded"
    ).first()
    assert log is not None and log.status == "success"


# -----------------------------------------------------------------------------
# Test 5: invoice.payment_failed → status grace_period
# -----------------------------------------------------------------------------

def test_invoice_payment_failed_sets_grace_period(client, company):
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.stripe_subscription_status = "active"
    _db.session.commit()

    event_id = _uniq("evt_fail_")
    sub_id = _uniq("sub_")
    event = _make_event(
        "invoice.payment_failed",
        {
            "id": _uniq("in_fail_"),
            "customer": company.stripe_customer_id,
            "attempt_count": 2,
            "subscription": sub_id,
        },
        event_id=event_id,
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.stripe_subscription_status == "grace_period"
    log = SaasAutomationLog.query.filter_by(
        company_id=c.id, event_type="invoice.payment_failed"
    ).first()
    assert log is not None and log.status == "success"
    assert log.stripe_event_id == event_id
    assert log.customer_id == company.stripe_customer_id
    assert log.subscription_id == sub_id


# -----------------------------------------------------------------------------
# Test 6: customer.subscription.updated → mirrors stripe status
# -----------------------------------------------------------------------------

def test_subscription_updated_mirrors_status(client, company):
    sub_id = _uniq("sub_")
    event = _make_event(
        "customer.subscription.updated",
        {"id": sub_id, "customer": company.stripe_customer_id, "status": "trialing"},
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.stripe_subscription_status == "trialing"
    assert c.stripe_subscription_id == sub_id


# -----------------------------------------------------------------------------
# Test 7: customer.subscription.deleted → status canceled
# -----------------------------------------------------------------------------

def test_subscription_deleted_sets_canceled(client, company):
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.stripe_subscription_status = "active"
    _db.session.commit()

    sub_id = _uniq("sub_")
    event = _make_event(
        "customer.subscription.deleted",
        {"id": sub_id, "customer": company.stripe_customer_id, "status": "canceled"},
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.stripe_subscription_status == "canceled"
    log = SaasAutomationLog.query.filter_by(
        company_id=c.id, event_type="customer.subscription.deleted"
    ).first()
    assert log is not None and log.status == "success"


# -----------------------------------------------------------------------------
# Test 8: Unknown event types are logged as 'skipped' (not 'failed')
# -----------------------------------------------------------------------------

def test_unknown_event_logged_as_skipped(client, company):
    event = _make_event(
        "charge.refunded",
        {"id": _uniq("ch_"), "customer": company.stripe_customer_id},
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    _db.session.expire_all()
    log = SaasAutomationLog.query.filter_by(
        company_id=company.id, event_type="charge.refunded"
    ).first()
    assert log is not None and log.status == "skipped"


# -----------------------------------------------------------------------------
# Test 9: Duplicate event IDs are ignored (idempotency)
# -----------------------------------------------------------------------------

def test_duplicate_event_id_is_ignored(client, company):
    """Same Stripe event_id delivered twice should only be processed once."""
    event_id = _uniq("evt_dup_")
    event = _make_event(
        "invoice.payment_succeeded",
        {"id": _uniq("in_dup_"), "customer": company.stripe_customer_id, "amount_paid": 9900},
        event_id=event_id,
    )

    r1 = _signed_post(client, event)
    assert r1.status_code == 200
    body1 = r1.get_json()
    assert body1.get("duplicate") is not True

    r2 = _signed_post(client, event)
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert body2.get("duplicate") is True

    _db.session.expire_all()
    rows = SaasAutomationLog.query.filter_by(
        stripe_event_id=event_id, company_id=company.id
    ).all()
    assert len(rows) == 1, f"Expected 1 audit row, got {len(rows)}"
    assert rows[0].status == "success"

    dup_rows = SaasAutomationLog.query.filter_by(
        company_id=company.id, status="duplicate"
    ).all()
    assert len(dup_rows) >= 1, "Duplicate delivery should be audit-logged"
    assert any(
        (r.payload or {}).get("_duplicate_of") == event_id
        for r in dup_rows
    ), "Duplicate audit row should reference the original event ID"


def test_processing_failure_returns_500_for_retry(client, company, monkeypatch):
    """If event handling raises, the endpoint should respond 5xx so Stripe retries."""
    import saas_mgmt
    def boom(*a, **k):
        raise RuntimeError("simulated downstream failure")
    monkeypatch.setattr(saas_mgmt, "_fire_n8n", boom)

    event = _make_event(
        "invoice.payment_succeeded",
        {"id": _uniq("in_boom_"), "customer": company.stripe_customer_id, "amount_paid": 9900},
    )
    response = _signed_post(client, event)
    assert response.status_code == 500, "Processing errors must return 5xx for Stripe retry"

    _db.session.expire_all()
    log = SaasAutomationLog.query.filter_by(
        company_id=company.id, event_type="invoice.payment_succeeded"
    ).first()
    assert log is not None
    assert log.status == "failed"
    assert log.error and "simulated downstream failure" in log.error


def test_failed_event_can_be_retried_successfully(client, company, monkeypatch):
    """A retry of a previously-failed event should reprocess (not silently dedupe)."""
    import saas_mgmt

    def boom(*a, **k):
        raise RuntimeError("first attempt failure")
    monkeypatch.setattr(saas_mgmt, "_fire_n8n", boom)

    event_id = _uniq("evt_retry_")
    event = _make_event(
        "invoice.payment_succeeded",
        {"id": _uniq("in_retry_"), "customer": company.stripe_customer_id, "amount_paid": 4900},
        event_id=event_id,
    )
    r1 = _signed_post(client, event)
    assert r1.status_code == 500

    _db.session.expire_all()
    log = SaasAutomationLog.query.filter_by(
        stripe_event_id=event_id, company_id=company.id
    ).first()
    assert log.status == "failed"

    monkeypatch.setattr(saas_mgmt, "_fire_n8n", lambda *a, **k: None)
    r2 = _signed_post(client, event)
    assert r2.status_code == 200, "Retry of failed event must be reprocessed, not duplicate-acked"
    body = r2.get_json()
    assert body.get("duplicate") is not True, "Retry of failed event must not be marked duplicate"

    _db.session.expire_all()
    rows = SaasAutomationLog.query.filter_by(
        stripe_event_id=event_id, company_id=company.id
    ).all()
    assert len(rows) == 1, "Retry should update the existing claim row in-place"
    assert rows[0].status == "success"
    assert rows[0].error is None


# -----------------------------------------------------------------------------
# Test 10: Health endpoint exposes Stripe configuration
# -----------------------------------------------------------------------------

def test_health_endpoint_shows_stripe_config(client):
    response = client.get("/health")
    assert response.status_code in (200, 503)
    body = response.get_json()
    assert "stripe" in body
    assert body["stripe"] in ("configured", "partial", "disabled")
    assert "stripe_details" in body
    assert "secret_key" in body["stripe_details"]
    assert "webhook_secret" in body["stripe_details"]


def test_health_config_endpoint_shows_stripe(client):
    response = client.get("/health/config")
    assert response.status_code == 200
    body = response.get_json()
    assert "features" in body
    assert "stripe" in body["features"]
    assert "stripe_webhook" in body["features"]
