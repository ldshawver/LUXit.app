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
from models import Company, CustomerOnboardingProject, CustomerOnboardingTask, SaasAutomationLog, User, UserCompanyAccess


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
        existing = Company.query.filter_by(stripe_customer_id="cus_billing_test").first()
        if existing:
            _cleanup_company_rows(_db, existing.id)
            _delete_company_users(_db, existing.id)
            _db.session.delete(existing)
            _db.session.commit()
        for existing_user in User.query.filter(
            (User.email == "bill@test.com") | (User.username == "billtest")
        ).all():
            UserCompanyAccess.query.filter_by(user_id=existing_user.id).delete(synchronize_session=False)
            _db.session.delete(existing_user)
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
            _cleanup_company_rows(_db, c.id)
            _delete_company_users(_db, c.id)
            _db.session.delete(c)
            _db.session.commit()
        except Exception:
            _db.session.rollback()


def _login(client, user_id):
    """Programmatically mark Flask-Login session as authenticated."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _cleanup_company_rows(db, company_id):
    """Delete all FK-dependent rows for a company in safe order."""
    from models import IntegrationEvent
    CustomerOnboardingTask.query.filter(
        CustomerOnboardingTask.project_id.in_(
            db.session.query(CustomerOnboardingProject.id).filter_by(company_id=company_id)
        )
    ).delete(synchronize_session=False)
    CustomerOnboardingProject.query.filter_by(company_id=company_id).delete(synchronize_session=False)
    SaasAutomationLog.query.filter_by(company_id=company_id).delete(synchronize_session=False)
    IntegrationEvent.query.filter_by(company_id=company_id).delete(synchronize_session=False)


def _delete_company_users(db, company_id):
    """Delete users whose default_company_id matches, cleaning up FK deps first."""
    user_ids = [u.id for u in User.query.filter_by(default_company_id=company_id).all()]
    if user_ids:
        UserCompanyAccess.query.filter(
            UserCompanyAccess.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)


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
            # Remove any leftover rows from a previous failed run
            existing_co = Company.query.filter_by(stripe_customer_id="cus_paid_setup").first()
            if existing_co:
                _cleanup_company_rows(_db, existing_co.id)
                _delete_company_users(_db, existing_co.id)
                _db.session.delete(existing_co)
                _db.session.commit()
            for eu in User.query.filter(
                (User.email == "paid@setup.com") | (User.username == "paidsetup")
            ).all():
                UserCompanyAccess.query.filter_by(user_id=eu.id).delete(synchronize_session=False)
                _db.session.delete(eu)
            _db.session.commit()
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
            try:
                if uid:
                    UserCompanyAccess.query.filter_by(user_id=uid).delete(synchronize_session=False)
                    User.query.filter(User.id == uid).delete(synchronize_session=False)
                if cid:
                    _cleanup_company_rows(_db, cid)
                    Company.query.filter(Company.id == cid).delete(synchronize_session=False)
                _db.session.commit()
            except Exception:
                _db.session.rollback()


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

def test_ready_endpoint_ok(client, monkeypatch):
    """All required envs set in the test fixture → 200 ready, with the
    spec-mandated boolean keys (db, stripe_secret_key, stripe_webhook_secret,
    openai_api_key, stripe_webhook_route, agent_scheduler)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ready"] is True
    for k in ("database", "stripe_secret_key", "stripe_webhook_secret",
              "openai_api_key", "stripe_webhook_route"):
        assert body["checks"][k] is True, f"missing/false check: {k}"
    # agent_scheduler is reported but not fail-gating
    assert "agent_scheduler" in body["checks"]
    # Never echoes the secret values
    assert "sk_test_dummy" not in resp.data.decode()
    assert WEBHOOK_SECRET not in resp.data.decode()
    assert "sk-test-openai" not in resp.data.decode()


def test_ready_endpoint_missing_openai(client, monkeypatch):
    """Missing OPENAI_API_KEY → 503 with explicit `missing` list."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["checks"]["openai_api_key"] is False
    assert any("OPENAI_API_KEY" in m for m in body["missing"])


def test_ready_endpoint_missing_secret(client, monkeypatch):
    """Missing STRIPE_WEBHOOK_SECRET → 503 with explicit `missing` list."""
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ready"] is False
    assert body["checks"]["stripe_webhook_secret"] is False
    assert any("STRIPE_WEBHOOK_SECRET" in m for m in body["missing"])


# =============================================================================
# Contact-usage / metered billing
# =============================================================================

def test_compute_contacts_overage_under_limit():
    from services.stripe_billing import compute_contacts_overage
    assert compute_contacts_overage(0, 2500) == 0
    assert compute_contacts_overage(2500, 2500) == 0
    assert compute_contacts_overage(2499, 2500) == 0


def test_compute_contacts_overage_over_limit():
    from services.stripe_billing import compute_contacts_overage
    assert compute_contacts_overage(2501, 2500) == 1
    assert compute_contacts_overage(60_000, 50_000) == 10_000


def test_compute_contacts_overage_never_negative():
    """Negative inputs and a None allowance must clamp to zero — we never
    push a negative usage record to Stripe."""
    from services.stripe_billing import compute_contacts_overage
    assert compute_contacts_overage(-100, 2500) == 0
    assert compute_contacts_overage(10_000, None) == 0
    assert compute_contacts_overage(None, 2500) == 0


def test_included_contacts_for_tier_table():
    from services.stripe_billing import included_contacts_for_tier
    assert included_contacts_for_tier("starter") == 2_500
    assert included_contacts_for_tier("professional") == 50_000
    assert included_contacts_for_tier("free") is None
    assert included_contacts_for_tier(None) is None


def test_find_contacts_usage_item_id_present():
    from services.stripe_billing import find_contacts_usage_item_id
    sub = {"items": {"data": [
        {"id": "si_tier",  "price": {"lookup_key": "luxit_starter_monthly"}},
        {"id": "si_usage", "price": {"lookup_key": "luxit_contacts_usage_monthly"}},
    ]}}
    assert find_contacts_usage_item_id(sub) == "si_usage"


def test_find_contacts_usage_item_id_absent():
    from services.stripe_billing import find_contacts_usage_item_id
    sub = {"items": {"data": [
        {"id": "si_tier", "price": {"lookup_key": "luxit_starter_monthly"}},
    ]}}
    assert find_contacts_usage_item_id(sub) is None
    assert find_contacts_usage_item_id({}) is None


def test_report_contact_usage_no_subscription_item(app, company_user):
    """If the company has no metered usage subscription item, the helper
    must return reported=False and skip the Stripe call (never raises),
    while still refreshing the locally-cached overage for the UI."""
    from services.stripe_billing import report_contact_usage
    company, _ = company_user
    with app.app_context():
        c = Company.query.get(company.id)
        c.contacts_used = 9999
        c.included_contacts = 2500
        _db.session.commit()
        # No stripe_contact_usage_subscription_item_id set → must skip
        result = report_contact_usage(c)
        c2 = Company.query.get(company.id)
    assert result["reported"] is False
    assert result["skipped_reason"] == "no_usage_subscription_item"
    # Locally-computed overage echoed back & persisted so UI matches.
    assert result["quantity"] == 7499  # 9999 - 2500
    assert c2.contacts_overage == 7499


def test_report_contact_usage_set_action_with_overage(app, company_user):
    """With usage item + overage, helper calls Stripe with action=set and
    persists last_reported_contact_usage / last_usage_reported_at."""
    from services.stripe_billing import report_contact_usage
    company, _ = company_user
    with app.app_context():
        c = Company.query.get(company.id)
        c.contacts_used = 3000
        c.included_contacts = 2500
        c.stripe_contact_usage_subscription_item_id = "si_test_meter"
        _db.session.commit()

        with patch("services.stripe_billing.get_stripe") as mk_get:
            fake_stripe = SimpleNamespace(
                SubscriptionItem=SimpleNamespace(create_usage_record=lambda *a, **kw: None)
            )
            create_record_mock = patch.object(
                fake_stripe.SubscriptionItem, "create_usage_record",
            ).start()
            mk_get.return_value = fake_stripe
            try:
                result = report_contact_usage(c)
            finally:
                patch.stopall()

        assert result["reported"] is True
        assert result["quantity"] == 500   # 3000 - 2500
        assert result["subscription_item"] == "si_test_meter"
        # Verify Stripe was called with action="set" and quantity=500
        kwargs = create_record_mock.call_args.kwargs
        args   = create_record_mock.call_args.args
        assert args[0] == "si_test_meter"
        assert kwargs.get("action") == "set"
        assert kwargs.get("quantity") == 500

        c2 = Company.query.get(company.id)
        assert c2.last_reported_contact_usage == 500
        assert c2.last_usage_reported_at is not None
        assert c2.contacts_overage == 500


def test_report_contact_usage_under_limit_reports_zero(app, company_user):
    """Under-limit usage must still send action=set with quantity=0 so
    Stripe's recorded period total is correct."""
    from services.stripe_billing import report_contact_usage
    company, _ = company_user
    with app.app_context():
        c = Company.query.get(company.id)
        c.contacts_used = 1000
        c.included_contacts = 2500
        c.stripe_contact_usage_subscription_item_id = "si_test_meter"
        _db.session.commit()

        with patch("services.stripe_billing.get_stripe") as mk_get:
            create_record_mock = SimpleNamespace(call_args=None)
            def _create(*a, **kw):
                create_record_mock.call_args = SimpleNamespace(args=a, kwargs=kw)
            mk_get.return_value = SimpleNamespace(
                SubscriptionItem=SimpleNamespace(create_usage_record=_create)
            )
            result = report_contact_usage(c)

        assert result["reported"] is True
        assert result["quantity"] == 0
        assert create_record_mock.call_args.kwargs["action"] == "set"
        assert create_record_mock.call_args.kwargs["quantity"] == 0


def test_report_contact_usage_endpoint_requires_auth(client):
    resp = client.post("/api/stripe/report-contact-usage", json={})
    assert resp.status_code in (302, 401)


def test_report_contact_usage_endpoint_forbidden_for_other_company(app, client, company_user):
    """A logged-in user who doesn't own the requested company must get 403."""
    company, _ = company_user
    with app.app_context():
        other = Company(name="Other Co", stripe_customer_id="cus_other_test",
                        billing_status="none", billing_tier="free")
        _db.session.add(other)
        _db.session.flush()
        outsider = User(username="outsider", email="out@test.com",
                        password_hash="x", is_admin=False, default_company_id=other.id)
        _db.session.add(outsider)
        _db.session.commit()
        outsider_id = outsider.id
        target_company_id = company.id
    try:
        _login(client, outsider_id)
        resp = client.post("/api/stripe/report-contact-usage",
                           json={"company_id": target_company_id})
        assert resp.status_code == 403
    finally:
        with app.app_context():
            User.query.filter_by(username="outsider").delete()
            Company.query.filter_by(stripe_customer_id="cus_other_test").delete()
            _db.session.commit()


def test_subscription_updated_persists_usage_subscription_item(client, company_user):
    """customer.subscription.updated must re-sync the metered usage item id
    from the subscription's items list (no Stripe round-trip needed)."""
    company, _ = company_user
    sub_obj = {
        "id": "sub_upd_meter",
        "object": "subscription",
        "customer": "cus_billing_test",
        "status": "active",
        "current_period_start": int(time.time()),
        "current_period_end":   int(time.time()) + 30 * 86400,
        "cancel_at_period_end": False,
        "items": {"data": [
            {"id": "si_pro2",   "quantity": 1,
             "price": {"lookup_key": "luxit_professional_monthly", "recurring": {"interval": "month"}}},
            {"id": "si_meter2", "quantity": 1,
             "price": {"lookup_key": "luxit_contacts_usage_monthly", "recurring": {"interval": "month"}}},
        ]},
    }
    event = _make_event("customer.subscription.updated", sub_obj, "evt_sub_upd_meter")
    resp = _signed_post(client, event)
    assert resp.status_code == 200
    c = Company.query.get(company.id)
    assert c.stripe_contact_usage_subscription_item_id == "si_meter2"
    assert c.included_contacts == 50_000


def test_report_contact_usage_endpoint_skips_without_meter(client, company_user):
    """Endpoint round-trips the helper's skipped_reason payload."""
    company, user = company_user
    _login(client, user.id)
    resp = client.post("/api/stripe/report-contact-usage",
                       json={"company_id": company.id})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["reported"] is False
    assert body["skipped_reason"] == "no_usage_subscription_item"


def test_checkout_completed_sets_included_contacts_for_starter(client, company_user):
    """checkout.session.completed for Starter must populate
    company.included_contacts = 2500."""
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": "cs_set_inc_starter",
        "object": "checkout.session",
        "customer": "cus_billing_test",
        "subscription": "sub_inc_starter",
        "client_reference_id": str(company.id),
        "metadata": {"lookup_key": "luxit_starter_monthly",
                     "company_id": str(company.id),
                     "include_setup_fee": "false"},
    }, "evt_inc_starter")
    # Stub Subscription.retrieve so the usage-item lookup doesn't fail.
    with patch("services.stripe_billing.get_stripe") as mk_get:
        mk_get.return_value = SimpleNamespace(
            Subscription=SimpleNamespace(retrieve=lambda *a, **kw: {"items": {"data": []}})
        )
        resp = _signed_post(client, event)
    assert resp.status_code == 200
    from app import create_app  # noqa: F401  (just to be explicit)
    c = Company.query.get(company.id)
    assert c.included_contacts == 2500
    assert c.billing_tier == "starter"


def test_checkout_completed_persists_usage_subscription_item(client, company_user):
    """When the Stripe subscription has a metered usage item, the webhook
    persists its si_... on the company so later report_contact_usage calls
    don't need a Stripe round-trip."""
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": "cs_persist_meter",
        "object": "checkout.session",
        "customer": "cus_billing_test",
        "subscription": "sub_with_meter",
        "client_reference_id": str(company.id),
        "metadata": {"lookup_key": "luxit_professional_monthly",
                     "company_id": str(company.id),
                     "include_setup_fee": "false"},
    }, "evt_persist_meter")
    fake_sub = {"items": {"data": [
        {"id": "si_pro",   "price": {"lookup_key": "luxit_professional_monthly"}},
        {"id": "si_meter", "price": {"lookup_key": "luxit_contacts_usage_monthly"}},
    ]}}
    with patch("services.stripe_billing.get_stripe") as mk_get:
        mk_get.return_value = SimpleNamespace(
            Subscription=SimpleNamespace(retrieve=lambda *a, **kw: fake_sub)
        )
        resp = _signed_post(client, event)
    assert resp.status_code == 200
    c = Company.query.get(company.id)
    assert c.stripe_contact_usage_subscription_item_id == "si_meter"
    assert c.included_contacts == 50_000
