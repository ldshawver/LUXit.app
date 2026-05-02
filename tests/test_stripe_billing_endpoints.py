"""Tests for the Stripe Checkout / Portal endpoints and the billing
lifecycle fields populated by the webhook.

Covers:
  - POST /api/stripe/create-checkout-session  (lookup_key required, no price IDs)
  - POST /api/stripe/create-portal-session
  - GET  /ready                               (503 when secrets missing)
  - Webhook lifecycle field population:
      checkout.session.completed → tier, max_team_members, status='active'
      invoice.payment_failed     → grace_period_ends_at set
      customer.subscription.updated → seats include add-on quantity
      customer.subscription.deleted → billing_status='suspended'
  - Company.can_add_team_member() seat enforcement
"""
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe

from app import create_app
from extensions import db as _db
from models import Company, SaasAutomationLog, User, UserCompanyAccess


WEBHOOK_SECRET = "whsec_test_secret_for_pytest_only"


@pytest.fixture
def app():
    os.environ["STRIPE_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost",
                    WTF_CSRF_ENABLED=False, LOGIN_DISABLED=False)
    yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def company_user(app):
    """Create a Company + admin User who owns it (default_company_id)."""
    with app.app_context():
        Company.query.filter_by(stripe_customer_id="cus_billing_test").delete()
        _db.session.commit()
        c = Company(name="Billing Test Co", stripe_customer_id="cus_billing_test",
                    billing_status="none", billing_tier="free")
        _db.session.add(c)
        _db.session.flush()
        u = User(username="billtest", email="bill@test.com",
                 password_hash="x", is_admin=True, default_company_id=c.id)
        _db.session.add(u)
        _db.session.commit()
        yield c, u
        try:
            SaasAutomationLog.query.filter_by(company_id=c.id).delete()
            UserCompanyAccess.query.filter_by(company_id=c.id).delete()
            User.query.filter_by(default_company_id=c.id).delete()
            _db.session.delete(c)
            _db.session.commit()
        except Exception:
            _db.session.rollback()


def _login(client, user_id):
    """Programmatically mark Flask-Login session as authenticated."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _signed_post(client, event):
    body = json.dumps(event)
    ts = int(time.time())
    sig = stripe.WebhookSignature._compute_signature(f"{ts}.{body}", WEBHOOK_SECRET)
    return client.post(
        "/api/stripe/webhook",
        data=body, content_type="application/json",
        headers={"Stripe-Signature": f"t={ts},v1={sig}"},
    )


def _make_event(event_type, obj, event_id):
    return {
        "id": event_id, "object": "event", "type": event_type,
        "livemode": False, "api_version": "2024-06-20",
        "created": int(time.time()), "data": {"object": obj},
        "request": {"id": None, "idempotency_key": None}, "pending_webhooks": 0,
    }


# =============================================================================
# Checkout session endpoint
# =============================================================================

def test_create_checkout_requires_auth(client):
    """Unauthenticated request → redirect to login (not 200)."""
    resp = client.post("/api/stripe/create-checkout-session",
                       json={"lookup_key": "luxit_starter_monthly"})
    assert resp.status_code in (302, 401)


def test_create_checkout_requires_lookup_key(client, company_user):
    """Missing lookup_key → 400."""
    _, user = company_user
    _login(client, user.id)
    resp = client.post("/api/stripe/create-checkout-session", json={})
    assert resp.status_code == 400
    assert b"lookup_key" in resp.data


def test_create_checkout_rejects_unknown_lookup_key(client, company_user):
    _, user = company_user
    _login(client, user.id)
    resp = client.post("/api/stripe/create-checkout-session",
                       json={"lookup_key": "totally_made_up"})
    assert resp.status_code == 400
    assert b"unknown lookup_key" in resp.data


def test_create_checkout_rejects_frontend_price_id(client, company_user):
    """A request that includes price_id / amount must be rejected."""
    _, user = company_user
    _login(client, user.id)
    for forbidden in ("price_id", "price", "amount", "unit_amount"):
        resp = client.post("/api/stripe/create-checkout-session",
                           json={"lookup_key": "luxit_starter_monthly",
                                 forbidden: "price_abc123"})
        assert resp.status_code == 400, f"{forbidden} should be rejected"
        body = resp.get_json()
        assert body and "price IDs" in body["error"]


def test_create_checkout_rejects_addon_lookup_key(client, company_user):
    """The seat-addon lookup_key must NOT start a new subscription —
    callers must use the Customer Portal to bump quantity instead."""
    company, user = company_user
    _login(client, user.id)
    resp = client.post(
        "/api/stripe/create-checkout-session",
        json={"lookup_key": "luxit_additional_account_monthly",
              "company_id": company.id},
    )
    # The lookup_key is in ALL_KNOWN_LOOKUP_KEYS so it passes the allowlist,
    # but the service layer raises ValueError → endpoint returns 400.
    assert resp.status_code == 400
    body = resp.get_json()
    assert "Customer Portal" in (body.get("error") or "")


def test_checkout_completed_with_addon_lookup_key_does_not_corrupt_tier(app, client, company_user):
    """If checkout.session.completed somehow arrives with the add-on
    lookup_key, the webhook must NOT overwrite billing_tier/seats with
    ('custom', None). Tier comes from the subscription item recompute."""
    company, _ = company_user
    # Pre-set the company on a known tier.
    with app.app_context():
        c = Company.query.get(company.id)
        c.billing_tier = "starter"
        c.max_team_members = 5
        _db.session.commit()

    event = _make_event("checkout.session.completed", {
        "id": "cs_addon_001",
        "customer": company.stripe_customer_id,
        "subscription": "sub_addon_001",
        "metadata": {"lookup_key": "luxit_additional_account_monthly"},
    }, event_id="evt_checkout_addon_safe")
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        # Tier and seat cap preserved (NOT overwritten to custom/None).
        assert c.billing_tier == "starter"
        assert c.max_team_members == 5
        # But subscription was still linked + status flipped active.
        assert c.stripe_subscription_id == "sub_addon_001"
        assert c.billing_status == "active"


def test_create_checkout_rejects_setup_fee_lookup_key(client, company_user):
    """The one-time setup fee lookup_key must NOT be the primary lookup_key.
    It is only valid as a secondary line item via include_setup_fee=True."""
    company, user = company_user
    _login(client, user.id)
    resp = client.post(
        "/api/stripe/create-checkout-session",
        json={"lookup_key": "luxit_setup_fee_once", "company_id": company.id},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "include_setup_fee" in (body.get("error") or "")


def test_create_checkout_passes_include_setup_fee_when_unpaid(app, client, company_user):
    """Starter checkout with include_setup_fee=true and unpaid setup fee
    must call the service with include_setup_fee=True."""
    company, user = company_user
    _login(client, user.id)
    fake = SimpleNamespace(id="cs_with_fee", url="https://stripe.test/cs_with_fee")
    with patch("services.stripe_billing.create_checkout_session", return_value=fake) as mk:
        resp = client.post(
            "/api/stripe/create-checkout-session",
            json={"lookup_key": "luxit_starter_monthly",
                  "company_id": company.id,
                  "include_setup_fee": True},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["include_setup_fee"] is True
    assert mk.call_args.kwargs["include_setup_fee"] is True


def test_create_checkout_drops_setup_fee_when_already_paid(app, client):
    """If company.setup_fee_paid is already True, the server must override
    the frontend's request and pass include_setup_fee=False to Stripe.

    We build the fixture inline (with ``setup_fee_paid=True`` from creation)
    rather than using ``company_user`` because the shared fixture commits
    a Company with the default ``False`` first and then mutating it post-
    yield interacts badly with Flask-SQLAlchemy's identity map across
    nested app contexts.
    """
    cid = uid = None
    try:
        with app.app_context():
            c = Company(name="Paid Setup Co",
                        stripe_customer_id="cus_paid_setup",
                        billing_status="none", billing_tier="free",
                        setup_fee_paid=True)
            _db.session.add(c)
            _db.session.flush()
            u = User(username="paidsetup", email="paid@setup.com",
                     password_hash="x", is_admin=True, default_company_id=c.id)
            _db.session.add(u)
            _db.session.commit()
            cid, uid = c.id, u.id

        _login(client, uid)
        fake = SimpleNamespace(id="cs_no_fee", url="https://stripe.test/cs_no_fee")
        with patch("services.stripe_billing.create_checkout_session", return_value=fake) as mk:
            resp = client.post(
                "/api/stripe/create-checkout-session",
                json={"lookup_key": "luxit_professional_monthly",
                      "company_id": cid,
                      "include_setup_fee": True},
            )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        # Server-side guard kicked in: include_setup_fee was forced to False.
        assert body["include_setup_fee"] is False
        assert mk.call_args.kwargs["include_setup_fee"] is False
    finally:
        with app.app_context():
            if uid:
                User.query.filter_by(id=uid).delete()
            if cid:
                Company.query.filter_by(id=cid).delete()
            _db.session.commit()


def test_create_checkout_omits_setup_fee_by_default(client, company_user):
    """If the frontend doesn't ask for the setup fee, it isn't attached."""
    company, user = company_user
    _login(client, user.id)
    fake = SimpleNamespace(id="cs_default", url="https://stripe.test/cs_default")
    with patch("services.stripe_billing.create_checkout_session", return_value=fake) as mk:
        resp = client.post(
            "/api/stripe/create-checkout-session",
            json={"lookup_key": "luxit_starter_monthly",
                  "company_id": company.id},
        )
    assert resp.status_code == 200
    assert mk.call_args.kwargs["include_setup_fee"] is False


def test_checkout_completed_marks_setup_fee_paid(app, client, company_user):
    """checkout.session.completed with metadata.include_setup_fee='true'
    must mark the company setup_fee_paid + record the session id + timestamp."""
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": "cs_setup_fee_paid_001",
        "customer": company.stripe_customer_id,
        "subscription": "sub_with_setup_001",
        "metadata": {
            "lookup_key":        "luxit_starter_monthly",
            "include_setup_fee": "true",
        },
    }, event_id="evt_setup_fee_001")
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.setup_fee_paid is True
        assert c.setup_fee_paid_at is not None
        assert c.setup_fee_checkout_session_id == "cs_setup_fee_paid_001"
        # Tier still applied alongside setup fee.
        assert c.billing_tier == "starter"
        assert c.max_team_members == 5


def test_checkout_completed_without_setup_fee_flag_does_not_mark_paid(app, client, company_user):
    """checkout.session.completed without the setup-fee metadata must NOT
    set setup_fee_paid (e.g. an upgrade after fee was already paid)."""
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": "cs_no_setup_001",
        "customer": company.stripe_customer_id,
        "subscription": "sub_no_setup_001",
        "metadata": {
            "lookup_key":        "luxit_professional_monthly",
            "include_setup_fee": "false",
        },
    }, event_id="evt_no_setup_001")
    resp = _signed_post(client, event)
    assert resp.status_code == 200
    with app.app_context():
        c = Company.query.get(company.id)
        assert c.setup_fee_paid is False
        assert c.setup_fee_paid_at is None
        assert c.setup_fee_checkout_session_id is None


def test_invoice_payment_succeeded_does_not_mark_setup_fee_paid(app, client, company_user):
    """Renewal invoices must never flip setup_fee_paid — only the initial
    checkout.session.completed handler is allowed to do so. This guards
    against accidental re-charging via recurring-invoice logic."""
    company, _ = company_user
    event = _make_event("invoice.payment_succeeded", {
        "id":           "in_renewal_001",
        "customer":     company.stripe_customer_id,
        "subscription": "sub_renewal_001",
        "amount_paid":  19900,
    }, event_id="evt_renewal_001")
    resp = _signed_post(client, event)
    assert resp.status_code == 200
    with app.app_context():
        c = Company.query.get(company.id)
        assert c.setup_fee_paid is False  # unchanged
        assert c.setup_fee_paid_at is None


def test_create_checkout_happy_path(app, client, company_user):
    """Valid lookup_key + auth → 200 with checkout URL."""
    company, user = company_user
    _login(client, user.id)

    fake_session = SimpleNamespace(id="cs_test_123",
                                   url="https://checkout.stripe.com/c/test/cs_test_123")
    with patch("services.stripe_billing.create_checkout_session",
               return_value=fake_session) as mk:
        resp = client.post("/api/stripe/create-checkout-session",
                           json={"lookup_key": "luxit_starter_monthly",
                                 "company_id": company.id})
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["url"] == fake_session.url
    assert body["session_id"] == "cs_test_123"
    assert body["lookup_key"] == "luxit_starter_monthly"
    # Service was called with the right kwargs
    assert mk.call_count == 1
    call_kwargs = mk.call_args.kwargs
    assert call_kwargs["company_id"] == company.id


# =============================================================================
# Customer Portal endpoint
# =============================================================================

def test_create_portal_requires_auth(client):
    resp = client.post("/api/stripe/create-portal-session", json={})
    assert resp.status_code in (302, 401)


def test_create_portal_requires_stripe_customer(app, client):
    """A company without stripe_customer_id → 400."""
    with app.app_context():
        c = Company(name="No Customer Co", billing_status="none")
        _db.session.add(c)
        _db.session.flush()
        u = User(username="nocust", email="nocust@x.com", password_hash="x",
                 is_admin=True, default_company_id=c.id)
        _db.session.add(u)
        _db.session.commit()
        uid, cid = u.id, c.id
    try:
        _login(client, uid)
        resp = client.post("/api/stripe/create-portal-session",
                           json={"company_id": cid})
        assert resp.status_code == 400
        assert b"stripe customer" in resp.data
    finally:
        with app.app_context():
            User.query.filter_by(id=uid).delete()
            Company.query.filter_by(id=cid).delete()
            _db.session.commit()


def test_create_portal_happy_path(client, company_user):
    company, user = company_user
    _login(client, user.id)
    fake = SimpleNamespace(id="bps_test", url="https://billing.stripe.com/p/test")
    with patch("services.stripe_billing.create_billing_portal_session",
               return_value=fake):
        resp = client.post("/api/stripe/create-portal-session",
                           json={"company_id": company.id})
    assert resp.status_code == 200
    assert resp.get_json()["url"] == fake.url


# =============================================================================
# Webhook lifecycle: tier mapping + period sync + grace + suspension
# =============================================================================

def test_checkout_completed_applies_starter_tier(app, client, company_user):
    """checkout.session.completed with starter lookup_key → tier+5 seats."""
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": "cs_starter_001",
        "customer": company.stripe_customer_id,
        "subscription": "sub_starter_001",
        "client_reference_id": str(company.id),
        "metadata": {"lookup_key": "luxit_starter_monthly",
                     "company_id": str(company.id)},
    }, event_id="evt_lifecycle_starter")
    resp = _signed_post(client, event)
    assert resp.status_code == 200, resp.data

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.billing_tier == "starter"
        assert c.billing_status == "active"
        assert c.max_team_members == 5
        assert c.stripe_price_lookup_key == "luxit_starter_monthly"
        assert c.stripe_subscription_id == "sub_starter_001"


def test_checkout_completed_applies_professional_unlimited(app, client, company_user):
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": "cs_pro_001",
        "customer": company.stripe_customer_id,
        "subscription": "sub_pro_001",
        "metadata": {"lookup_key": "luxit_professional_monthly"},
    }, event_id="evt_lifecycle_pro")
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.billing_tier == "professional"
        assert c.max_team_members is None  # unlimited
        assert c.can_add_team_member() is True


def test_payment_failed_sets_grace_period(app, client, company_user):
    """invoice.payment_failed sets grace_period_ends_at ~14 days out."""
    company, _ = company_user
    event = _make_event("invoice.payment_failed", {
        "id": "in_failed_001",
        "customer": company.stripe_customer_id,
        "subscription": "sub_starter_001",
        "attempt_count": 1,
    }, event_id="evt_payment_failed_grace")
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.billing_status == "grace_period"
        assert c.grace_period_ends_at is not None
        # Within ~14 days from now (allow a 1-minute clock skew)
        from datetime import datetime, timedelta
        delta = c.grace_period_ends_at - datetime.utcnow()
        assert timedelta(days=13) < delta < timedelta(days=15)


def test_subscription_updated_includes_addon_quantity(app, client, company_user):
    """Starter base (5 seats) + 3x add-on → max_team_members == 8."""
    company, _ = company_user
    event = _make_event("customer.subscription.updated", {
        "id": "sub_with_addon_001",
        "customer": company.stripe_customer_id,
        "status": "active",
        "current_period_start": 1714521600,  # 2024-05-01
        "current_period_end":   1717200000,  # 2024-06-01
        "cancel_at_period_end": False,
        "items": {"data": [
            {"price": {"lookup_key": "luxit_starter_monthly"}, "quantity": 1},
            {"price": {"lookup_key": "luxit_additional_account_monthly"}, "quantity": 3},
        ]},
    }, event_id="evt_sub_updated_addon")
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.billing_tier == "starter"
        assert c.max_team_members == 5 + 3
        assert c.billing_status == "active"
        assert c.current_period_start is not None
        assert c.current_period_end   is not None
        assert c.cancel_at_period_end is False


def test_subscription_deleted_suspends_company(app, client, company_user):
    company, _ = company_user
    event = _make_event("customer.subscription.deleted", {
        "id": "sub_to_delete_001",
        "customer": company.stripe_customer_id,
    }, event_id="evt_sub_deleted_suspend")
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    with app.app_context():
        c = Company.query.get(company.id)
        assert c.billing_status == "suspended"
        assert c.stripe_subscription_status == "canceled"


# =============================================================================
# Seat enforcement
# =============================================================================

def test_can_add_team_member_unlimited(app, company_user):
    company, _ = company_user
    with app.app_context():
        c = Company.query.get(company.id)
        c.max_team_members = None
        _db.session.commit()
        assert c.can_add_team_member() is True
        assert c.team_seats_available is None


def test_can_add_team_member_limit_reached(app, company_user):
    company, user = company_user
    with app.app_context():
        c = Company.query.get(company.id)
        # Already 1 user (the admin) attached via default_company_id.
        c.max_team_members = 1
        _db.session.commit()
        c2 = Company.query.get(company.id)
        assert c2.team_member_count >= 1
        assert c2.can_add_team_member() is False
        assert c2.team_seats_available == 0


# =============================================================================
# /ready endpoint
# =============================================================================

def test_ready_endpoint_ok(client):
    """Both Stripe envs are set in the test fixture → 200 ready."""
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ready"] is True
    assert body["checks"]["database"] is True
    assert body["checks"]["stripe_secret_key"] is True
    assert body["checks"]["stripe_webhook_secret"] is True
    assert body["checks"]["stripe_webhook_route"] is True
    # Never echoes the secret values
    assert "sk_test_dummy" not in resp.data.decode()
    assert WEBHOOK_SECRET not in resp.data.decode()


def test_ready_endpoint_missing_secret(client, monkeypatch):
    """Missing STRIPE_WEBHOOK_SECRET → 503 with explicit `missing` list."""
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ready"] is False
    assert body["checks"]["stripe_webhook_secret"] is False
    assert any("STRIPE_WEBHOOK_SECRET" in m for m in body["missing"])
