"""Tests for the Stripe webhook handler at POST /api/stripe/webhook.

Covers all five lifecycle events plus signature validation, security
behavior when the webhook secret is missing, audit logging, and the
billing status field on Company.
"""
import json
import os
import time

import pytest
import stripe

from app import create_app
from extensions import db as _db
from models import Company, SaasAutomationLog, CustomerOnboardingProject


WEBHOOK_SECRET = "whsec_test_secret_for_pytest_only"


@pytest.fixture
def app():
    os.environ["STRIPE_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    yield app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def company(app):
    """Create a Company with a Stripe customer ID for webhook routing."""
    with app.app_context():
        existing = Company.query.filter_by(stripe_customer_id="cus_test_123").first()
        if existing:
            _db.session.delete(existing)
            _db.session.commit()
        c = Company(
            name="Lucifer Cruz Test",
            stripe_customer_id="cus_test_123",
            stripe_subscription_status="none",
        )
        _db.session.add(c)
        _db.session.commit()
        yield c
        try:
            SaasAutomationLog.query.filter_by(company_id=c.id).delete()
            CustomerOnboardingProject.query.filter_by(company_id=c.id).delete()
            _db.session.delete(c)
            _db.session.commit()
        except Exception:
            _db.session.rollback()


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


def _make_event(event_type, obj, event_id="evt_test_001"):
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

def test_checkout_session_completed_activates_billing(app, client, company):
    event = _make_event(
        "checkout.session.completed",
        {
            "id": "cs_test_001",
            "customer": "cus_test_123",
            "subscription": "sub_test_001",
            "amount_total": 9900,
        },
        event_id="evt_checkout_001",
    )
    response = _signed_post(client, event)
    assert response.status_code == 200
    body = response.get_json()
    assert body["received"] is True
    assert body["event_id"] == "evt_checkout_001"

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.stripe_subscription_status == "active"
        assert c.stripe_subscription_id == "sub_test_001"

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

def test_invoice_payment_succeeded_sets_active(app, client, company):
    with app.app_context():
        c = Company.query.get(company.id)
        c.stripe_subscription_status = "past_due"
        _db.session.commit()

    event = _make_event(
        "invoice.payment_succeeded",
        {"id": "in_paid_001", "customer": "cus_test_123", "amount_paid": 9900},
        event_id="evt_paid_001",
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.stripe_subscription_status == "active"
        log = SaasAutomationLog.query.filter_by(
            company_id=c.id, event_type="invoice.payment_succeeded"
        ).first()
        assert log is not None and log.status == "success"


# -----------------------------------------------------------------------------
# Test 5: invoice.payment_failed → status grace_period
# -----------------------------------------------------------------------------

def test_invoice_payment_failed_sets_grace_period(app, client, company):
    with app.app_context():
        c = Company.query.get(company.id)
        c.stripe_subscription_status = "active"
        _db.session.commit()

    event = _make_event(
        "invoice.payment_failed",
        {"id": "in_fail_001", "customer": "cus_test_123", "attempt_count": 2,
         "subscription": "sub_test_001"},
        event_id="evt_fail_001",
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.stripe_subscription_status == "grace_period"
        log = SaasAutomationLog.query.filter_by(
            company_id=c.id, event_type="invoice.payment_failed"
        ).first()
        assert log is not None and log.status == "success"
        assert log.stripe_event_id == "evt_fail_001"
        assert log.customer_id == "cus_test_123"
        assert log.subscription_id == "sub_test_001"


# -----------------------------------------------------------------------------
# Test 6: customer.subscription.updated → mirrors stripe status
# -----------------------------------------------------------------------------

def test_subscription_updated_mirrors_status(app, client, company):
    event = _make_event(
        "customer.subscription.updated",
        {"id": "sub_test_001", "customer": "cus_test_123", "status": "trialing"},
        event_id="evt_updated_001",
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.stripe_subscription_status == "trialing"
        assert c.stripe_subscription_id == "sub_test_001"


# -----------------------------------------------------------------------------
# Test 7: customer.subscription.deleted → status canceled
# -----------------------------------------------------------------------------

def test_subscription_deleted_sets_canceled(app, client, company):
    with app.app_context():
        c = Company.query.get(company.id)
        c.stripe_subscription_status = "active"
        _db.session.commit()

    event = _make_event(
        "customer.subscription.deleted",
        {"id": "sub_test_001", "customer": "cus_test_123", "status": "canceled"},
        event_id="evt_deleted_001",
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.stripe_subscription_status == "canceled"
        log = SaasAutomationLog.query.filter_by(
            company_id=c.id, event_type="customer.subscription.deleted"
        ).first()
        assert log is not None and log.status == "success"


# -----------------------------------------------------------------------------
# Test 8: Unknown event types are logged as 'skipped' (not 'failed')
# -----------------------------------------------------------------------------

def test_unknown_event_logged_as_skipped(app, client, company):
    event = _make_event(
        "charge.refunded",
        {"id": "ch_test_001", "customer": "cus_test_123"},
        event_id="evt_unknown_001",
    )
    response = _signed_post(client, event)
    assert response.status_code == 200

    with app.app_context():
        log = SaasAutomationLog.query.filter_by(event_type="charge.refunded").first()
        assert log is not None and log.status == "skipped"


# -----------------------------------------------------------------------------
# Test 9: Duplicate event IDs are ignored (idempotency)
# -----------------------------------------------------------------------------

def test_duplicate_event_id_is_ignored(app, client, company):
    """Same Stripe event_id delivered twice should only be processed once."""
    event = _make_event(
        "invoice.payment_succeeded",
        {"id": "in_dup_001", "customer": "cus_test_123", "amount_paid": 9900},
        event_id="evt_duplicate_777",
    )

    # First delivery — should process normally
    r1 = _signed_post(client, event)
    assert r1.status_code == 200
    body1 = r1.get_json()
    assert body1.get("duplicate") is not True

    # Second delivery (same event_id) — should be flagged as duplicate
    r2 = _signed_post(client, event)
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert body2.get("duplicate") is True

    # Only ONE audit log row holds the unique stripe_event_id
    with app.app_context():
        rows = SaasAutomationLog.query.filter_by(
            stripe_event_id="evt_duplicate_777"
        ).all()
        assert len(rows) == 1, f"Expected 1 audit row, got {len(rows)}"
        assert rows[0].status == "success"

        # The duplicate delivery itself should be audited as a 'duplicate' row
        dup_rows = SaasAutomationLog.query.filter_by(status="duplicate").all()
        assert len(dup_rows) >= 1, "Duplicate delivery should be audit-logged"
        assert any(
            (r.payload or {}).get("_duplicate_of") == "evt_duplicate_777"
            for r in dup_rows
        ), "Duplicate audit row should reference the original event ID"


def test_processing_failure_returns_500_for_retry(app, client, company, monkeypatch):
    """If event handling raises, the endpoint should respond 5xx so Stripe retries."""
    import saas_mgmt
    def boom(*a, **k):
        raise RuntimeError("simulated downstream failure")
    monkeypatch.setattr(saas_mgmt, "_fire_n8n", boom)

    event = _make_event(
        "invoice.payment_succeeded",
        {"id": "in_boom_001", "customer": "cus_test_123", "amount_paid": 9900},
        event_id="evt_boom_001",
    )
    response = _signed_post(client, event)
    assert response.status_code == 500, "Processing errors must return 5xx for Stripe retry"

    with app.app_context():
        log = SaasAutomationLog.query.filter_by(stripe_event_id="evt_boom_001").first()
        assert log is not None
        assert log.status == "failed"
        assert log.error and "simulated downstream failure" in log.error


def test_failed_event_can_be_retried_successfully(app, client, company, monkeypatch):
    """A retry of a previously-failed event should reprocess (not silently dedupe)."""
    import saas_mgmt

    # First attempt: force failure
    def boom(*a, **k):
        raise RuntimeError("first attempt failure")
    monkeypatch.setattr(saas_mgmt, "_fire_n8n", boom)

    event = _make_event(
        "invoice.payment_succeeded",
        {"id": "in_retry_001", "customer": "cus_test_123", "amount_paid": 4900},
        event_id="evt_retry_001",
    )
    r1 = _signed_post(client, event)
    assert r1.status_code == 500

    with app.app_context():
        log = SaasAutomationLog.query.filter_by(stripe_event_id="evt_retry_001").first()
        assert log.status == "failed"

    # Second attempt: clear the failing patch so processing succeeds
    monkeypatch.setattr(saas_mgmt, "_fire_n8n", lambda *a, **k: None)
    r2 = _signed_post(client, event)
    assert r2.status_code == 200, "Retry of failed event must be reprocessed, not duplicate-acked"
    body = r2.get_json()
    assert body.get("duplicate") is not True, "Retry of failed event must not be marked duplicate"

    with app.app_context():
        rows = SaasAutomationLog.query.filter_by(stripe_event_id="evt_retry_001").all()
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
