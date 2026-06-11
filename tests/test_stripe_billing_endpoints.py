"""Tests for the Stripe Checkout / Portal endpoints and the billing
lifecycle fields populated by the webhook.

All test data uses per-test-unique Stripe IDs so tests are isolated from
stale data left by older test runs and from each other.
"""
import json
import os
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import stripe

from app import create_app
from extensions import db as _db
from models import Company, User, UserCompanyAccess


WEBHOOK_SECRET = "whsec_test_secret_for_pytest_only"


def _uniq(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["STRIPE_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost",
                    WTF_CSRF_ENABLED=False, LOGIN_DISABLED=False)
    with a.app_context():
        yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def company_user():
    """Create a Company + admin User per test.  Unique IDs prevent
    conflicts with stale data.  _db_rollback handles cleanup.
    """
    cust_id = _uniq("cus_bill_")
    c = Company(name=f"Bill Test {cust_id}", stripe_customer_id=cust_id,
                billing_status="none", billing_tier="free")
    _db.session.add(c)
    _db.session.flush()
    uname = _uniq("bill_")
    u = User(username=uname, email=f"{uname}@test.com",
             password_hash="x", is_admin=True, default_company_id=c.id)
    _db.session.add(u)
    _db.session.commit()
    yield c, u


def _login(client, user_id):
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


def _make_event(event_type, obj, event_id=None):
    if event_id is None:
        event_id = _uniq("evt_")
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
    resp = client.post("/api/stripe/create-checkout-session",
                       json={"lookup_key": "luxit_starter_monthly"})
    assert resp.status_code in (302, 401)


def test_create_checkout_requires_lookup_key(client, company_user):
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
    company, user = company_user
    _login(client, user.id)
    resp = client.post(
        "/api/stripe/create-checkout-session",
        json={"lookup_key": "luxit_additional_account_monthly",
              "company_id": company.id},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "Customer Portal" in (body.get("error") or "")


def test_checkout_completed_with_addon_lookup_key_does_not_corrupt_tier(client, company_user):
    company, _ = company_user
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.billing_tier = "starter"
    c.max_team_members = 5
    _db.session.commit()

    sub_id = _uniq("sub_")
    event = _make_event("checkout.session.completed", {
        "id": _uniq("cs_"),
        "customer": company.stripe_customer_id,
        "subscription": sub_id,
        "metadata": {"lookup_key": "luxit_additional_account_monthly"},
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.billing_tier == "starter"
    assert c.max_team_members == 5
    assert c.stripe_subscription_id == sub_id
    assert c.billing_status == "active"


def test_create_checkout_rejects_setup_fee_lookup_key(client, company_user):
    company, user = company_user
    _login(client, user.id)
    resp = client.post(
        "/api/stripe/create-checkout-session",
        json={"lookup_key": "luxit_setup_fee_once", "company_id": company.id},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "include_setup_fee" in (body.get("error") or "")


def test_create_checkout_passes_include_setup_fee_when_unpaid(client, company_user):
    company, user = company_user
    _login(client, user.id)
    fake = SimpleNamespace(id=_uniq("cs_"), url="https://stripe.test/cs_with_fee")
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


def test_create_checkout_drops_setup_fee_when_already_paid(client):
    cust_id = _uniq("cus_paid_")
    c = Company(name=f"Paid Setup {cust_id}",
                stripe_customer_id=cust_id,
                billing_status="none", billing_tier="free",
                setup_fee_paid=True)
    _db.session.add(c)
    _db.session.flush()
    uname = _uniq("paidsetup_")
    u = User(username=uname, email=f"{uname}@test.com",
             password_hash="x", is_admin=True, default_company_id=c.id)
    _db.session.add(u)
    _db.session.commit()
    cid, uid = c.id, u.id

    _login(client, uid)
    fake = SimpleNamespace(id=_uniq("cs_"), url="https://stripe.test/cs_no_fee")
    with patch("services.stripe_billing.create_checkout_session", return_value=fake) as mk:
        resp = client.post(
            "/api/stripe/create-checkout-session",
            json={"lookup_key": "luxit_professional_monthly",
                  "company_id": cid,
                  "include_setup_fee": True},
        )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["include_setup_fee"] is False
    assert mk.call_args.kwargs["include_setup_fee"] is False


def test_create_checkout_omits_setup_fee_by_default(client, company_user):
    company, user = company_user
    _login(client, user.id)
    fake = SimpleNamespace(id=_uniq("cs_"), url="https://stripe.test/cs_default")
    with patch("services.stripe_billing.create_checkout_session", return_value=fake) as mk:
        resp = client.post(
            "/api/stripe/create-checkout-session",
            json={"lookup_key": "luxit_starter_monthly",
                  "company_id": company.id},
        )
    assert resp.status_code == 200
    assert mk.call_args.kwargs["include_setup_fee"] is False


def test_checkout_completed_marks_setup_fee_paid(client, company_user):
    company, _ = company_user
    cs_id = _uniq("cs_setup_")
    event = _make_event("checkout.session.completed", {
        "id": cs_id,
        "customer": company.stripe_customer_id,
        "subscription": _uniq("sub_"),
        "metadata": {
            "lookup_key":        "luxit_starter_monthly",
            "include_setup_fee": "true",
        },
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.setup_fee_paid is True
    assert c.setup_fee_paid_at is not None
    assert c.setup_fee_checkout_session_id == cs_id
    assert c.billing_tier == "starter"
    assert c.max_team_members == 5


def test_checkout_completed_without_setup_fee_flag_does_not_mark_paid(client, company_user):
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": _uniq("cs_"),
        "customer": company.stripe_customer_id,
        "subscription": _uniq("sub_"),
        "metadata": {
            "lookup_key":        "luxit_professional_monthly",
            "include_setup_fee": "false",
        },
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.setup_fee_paid is False
    assert c.setup_fee_paid_at is None
    assert c.setup_fee_checkout_session_id is None


def test_invoice_payment_succeeded_does_not_mark_setup_fee_paid(client, company_user):
    company, _ = company_user
    event = _make_event("invoice.payment_succeeded", {
        "id":           _uniq("in_"),
        "customer":     company.stripe_customer_id,
        "subscription": _uniq("sub_"),
        "amount_paid":  19900,
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.setup_fee_paid is False
    assert c.setup_fee_paid_at is None


def test_create_checkout_happy_path(client, company_user):
    company, user = company_user
    _login(client, user.id)

    fake_session = SimpleNamespace(id=_uniq("cs_"),
                                   url="https://checkout.stripe.com/c/test/cs_test")
    with patch("services.stripe_billing.create_checkout_session",
               return_value=fake_session) as mk:
        resp = client.post("/api/stripe/create-checkout-session",
                           json={"lookup_key": "luxit_starter_monthly",
                                 "company_id": company.id})
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["url"] == fake_session.url
    assert body["session_id"] == fake_session.id
    assert body["lookup_key"] == "luxit_starter_monthly"
    assert mk.call_count == 1
    assert mk.call_args.kwargs["company_id"] == company.id


# =============================================================================
# Customer Portal endpoint
# =============================================================================

def test_create_portal_requires_auth(client):
    resp = client.post("/api/stripe/create-portal-session", json={})
    assert resp.status_code in (302, 401)


def test_create_portal_requires_stripe_customer(client):
    c = Company(name=_uniq("No Cust "), billing_status="none")
    _db.session.add(c)
    _db.session.flush()
    uname = _uniq("nocust_")
    u = User(username=uname, email=f"{uname}@x.com", password_hash="x",
             is_admin=True, default_company_id=c.id)
    _db.session.add(u)
    _db.session.commit()
    uid, cid = u.id, c.id

    _login(client, uid)
    resp = client.post("/api/stripe/create-portal-session",
                       json={"company_id": cid})
    assert resp.status_code == 400
    assert b"stripe customer" in resp.data


def test_create_portal_happy_path(client, company_user):
    company, user = company_user
    _login(client, user.id)
    fake = SimpleNamespace(id=_uniq("bps_"), url="https://billing.stripe.com/p/test")
    with patch("services.stripe_billing.create_billing_portal_session",
               return_value=fake):
        resp = client.post("/api/stripe/create-portal-session",
                           json={"company_id": company.id})
    assert resp.status_code == 200
    assert resp.get_json()["url"] == fake.url


# =============================================================================
# Webhook lifecycle: tier mapping + period sync + grace + suspension
# =============================================================================

def test_checkout_completed_applies_starter_tier(client, company_user):
    company, _ = company_user
    sub_id = _uniq("sub_")
    event = _make_event("checkout.session.completed", {
        "id": _uniq("cs_"),
        "customer": company.stripe_customer_id,
        "subscription": sub_id,
        "client_reference_id": str(company.id),
        "metadata": {"lookup_key": "luxit_starter_monthly",
                     "company_id": str(company.id)},
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200, resp.data

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.billing_tier == "starter"
    assert c.billing_status == "active"
    assert c.max_team_members == 5
    assert c.stripe_price_lookup_key == "luxit_starter_monthly"
    assert c.stripe_subscription_id == sub_id


def test_checkout_completed_applies_professional_unlimited(client, company_user):
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": _uniq("cs_"),
        "customer": company.stripe_customer_id,
        "subscription": _uniq("sub_"),
        "metadata": {"lookup_key": "luxit_professional_monthly"},
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.billing_tier == "professional"
    assert c.max_team_members is None
    assert c.can_add_team_member() is True


def test_payment_failed_sets_grace_period(client, company_user):
    company, _ = company_user
    event = _make_event("invoice.payment_failed", {
        "id": _uniq("in_"),
        "customer": company.stripe_customer_id,
        "subscription": _uniq("sub_"),
        "attempt_count": 1,
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.billing_status == "grace_period"
    assert c.grace_period_ends_at is not None
    from datetime import datetime, timedelta
    delta = c.grace_period_ends_at - datetime.utcnow()
    assert timedelta(days=13) < delta < timedelta(days=15)


def test_subscription_updated_includes_addon_quantity(client, company_user):
    company, _ = company_user
    event = _make_event("customer.subscription.updated", {
        "id": _uniq("sub_"),
        "customer": company.stripe_customer_id,
        "status": "active",
        "current_period_start": 1714521600,
        "current_period_end":   1717200000,
        "cancel_at_period_end": False,
        "items": {"data": [
            {"price": {"lookup_key": "luxit_starter_monthly"}, "quantity": 1},
            {"price": {"lookup_key": "luxit_additional_account_monthly"}, "quantity": 3},
        ]},
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.billing_tier == "starter"
    assert c.max_team_members == 5 + 3
    assert c.billing_status == "active"
    assert c.current_period_start is not None
    assert c.current_period_end   is not None
    assert c.cancel_at_period_end is False


def test_subscription_deleted_suspends_company(client, company_user):
    company, _ = company_user
    event = _make_event("customer.subscription.deleted", {
        "id": _uniq("sub_"),
        "customer": company.stripe_customer_id,
    })
    resp = _signed_post(client, event)
    assert resp.status_code == 200

    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.billing_status == "suspended"
    assert c.stripe_subscription_status == "canceled"


# =============================================================================
# Seat enforcement
# =============================================================================

def test_can_add_team_member_unlimited(company_user):
    company, _ = company_user
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.max_team_members = None
    _db.session.commit()
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.can_add_team_member() is True
    assert c.team_seats_available is None


def test_can_add_team_member_limit_reached(company_user):
    company, user = company_user
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.max_team_members = 1
    _db.session.commit()
    _db.session.expire_all()
    c2 = _db.session.get(Company, company.id)
    assert c2.team_member_count >= 1
    assert c2.can_add_team_member() is False
    assert c2.team_seats_available == 0


# =============================================================================
# /ready endpoint
# =============================================================================

def test_ready_endpoint_ok(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ready"] is True
    for k in ("database", "stripe_secret_key", "stripe_webhook_secret",
              "openai_api_key", "stripe_webhook_route"):
        assert body["checks"][k] is True, f"missing/false check: {k}"
    assert "agent_scheduler" in body["checks"]
    assert "sk_test_dummy" not in resp.data.decode()
    assert WEBHOOK_SECRET not in resp.data.decode()
    assert "sk-test-openai" not in resp.data.decode()


def test_ready_endpoint_missing_openai(client, monkeypatch):
    import services.provider_config as pc
    _original_bool = pc.get_provider_config_bool

    def _no_openai(provider, scope, field="api_key", **kw):
        if provider == "openai":
            return False
        return _original_bool(provider, scope, field, **kw)

    monkeypatch.setattr(pc, "get_provider_config_bool", _no_openai)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["checks"]["openai_api_key"] is False
    assert any("OPENAI_API_KEY" in m for m in body["missing"])


def test_ready_endpoint_missing_secret(client, monkeypatch):
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


def test_report_contact_usage_no_subscription_item(company_user):
    from services.stripe_billing import report_contact_usage
    company, _ = company_user
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.contacts_used = 9999
    c.included_contacts = 2500
    _db.session.commit()
    result = report_contact_usage(c)
    _db.session.expire_all()
    c2 = _db.session.get(Company, company.id)
    assert result["reported"] is False
    assert result["skipped_reason"] == "no_usage_subscription_item"
    assert result["quantity"] == 7499
    assert c2.contacts_overage == 7499


def test_report_contact_usage_set_action_with_overage(company_user):
    from services.stripe_billing import report_contact_usage
    company, _ = company_user
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.contacts_used = 3000
    c.included_contacts = 2500
    c.stripe_contact_usage_subscription_item_id = _uniq("si_")
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
    assert result["quantity"] == 500
    assert result["subscription_item"] == c.stripe_contact_usage_subscription_item_id
    kwargs = create_record_mock.call_args.kwargs
    args   = create_record_mock.call_args.args
    assert args[0] == c.stripe_contact_usage_subscription_item_id
    assert kwargs.get("action") == "set"
    assert kwargs.get("quantity") == 500

    _db.session.expire_all()
    c2 = _db.session.get(Company, company.id)
    assert c2.last_reported_contact_usage == 500
    assert c2.last_usage_reported_at is not None
    assert c2.contacts_overage == 500


def test_report_contact_usage_under_limit_reports_zero(company_user):
    from services.stripe_billing import report_contact_usage
    company, _ = company_user
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    c.contacts_used = 1000
    c.included_contacts = 2500
    c.stripe_contact_usage_subscription_item_id = _uniq("si_")
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


def test_report_contact_usage_endpoint_forbidden_for_other_company(client, company_user):
    company, _ = company_user
    cust_id2 = _uniq("cus_other_")
    other = Company(name=f"Other {cust_id2}", stripe_customer_id=cust_id2,
                    billing_status="none", billing_tier="free")
    _db.session.add(other)
    _db.session.flush()
    uname2 = _uniq("outsider_")
    outsider = User(username=uname2, email=f"{uname2}@test.com",
                    password_hash="x", is_admin=False, default_company_id=other.id)
    _db.session.add(outsider)
    _db.session.commit()
    outsider_id = outsider.id
    target_company_id = company.id

    _login(client, outsider_id)
    resp = client.post("/api/stripe/report-contact-usage",
                       json={"company_id": target_company_id})
    assert resp.status_code == 403


def test_subscription_updated_persists_usage_subscription_item(client, company_user):
    company, _ = company_user
    si_meter_id = _uniq("si_meter_")
    sub_obj = {
        "id": _uniq("sub_"),
        "object": "subscription",
        "customer": company.stripe_customer_id,
        "status": "active",
        "current_period_start": int(time.time()),
        "current_period_end":   int(time.time()) + 30 * 86400,
        "cancel_at_period_end": False,
        "items": {"data": [
            {"id": _uniq("si_pro_"),   "quantity": 1,
             "price": {"lookup_key": "luxit_professional_monthly", "recurring": {"interval": "month"}}},
            {"id": si_meter_id, "quantity": 1,
             "price": {"lookup_key": "luxit_contacts_usage_monthly", "recurring": {"interval": "month"}}},
        ]},
    }
    event = _make_event("customer.subscription.updated", sub_obj)
    resp = _signed_post(client, event)
    assert resp.status_code == 200
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.stripe_contact_usage_subscription_item_id == si_meter_id
    assert c.included_contacts == 50_000


def test_report_contact_usage_endpoint_skips_without_meter(client, company_user):
    company, user = company_user
    _login(client, user.id)
    resp = client.post("/api/stripe/report-contact-usage",
                       json={"company_id": company.id})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["reported"] is False
    assert body["skipped_reason"] == "no_usage_subscription_item"


def test_checkout_completed_sets_included_contacts_for_starter(client, company_user):
    company, _ = company_user
    event = _make_event("checkout.session.completed", {
        "id": _uniq("cs_"),
        "object": "checkout.session",
        "customer": company.stripe_customer_id,
        "subscription": _uniq("sub_"),
        "client_reference_id": str(company.id),
        "metadata": {"lookup_key": "luxit_starter_monthly",
                     "company_id": str(company.id),
                     "include_setup_fee": "false"},
    })
    with patch("services.stripe_billing.get_stripe") as mk_get:
        mk_get.return_value = SimpleNamespace(
            Subscription=SimpleNamespace(retrieve=lambda *a, **kw: {"items": {"data": []}})
        )
        resp = _signed_post(client, event)
    assert resp.status_code == 200
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.included_contacts == 2500
    assert c.billing_tier == "starter"


def test_checkout_completed_persists_usage_subscription_item(client, company_user):
    company, _ = company_user
    si_meter_id = _uniq("si_meter_")
    event = _make_event("checkout.session.completed", {
        "id": _uniq("cs_"),
        "object": "checkout.session",
        "customer": company.stripe_customer_id,
        "subscription": _uniq("sub_"),
        "client_reference_id": str(company.id),
        "metadata": {"lookup_key": "luxit_professional_monthly",
                     "company_id": str(company.id),
                     "include_setup_fee": "false"},
    })
    fake_sub = {"items": {"data": [
        {"id": _uniq("si_pro_"), "price": {"lookup_key": "luxit_professional_monthly"}},
        {"id": si_meter_id,      "price": {"lookup_key": "luxit_contacts_usage_monthly"}},
    ]}}
    with patch("services.stripe_billing.get_stripe") as mk_get:
        mk_get.return_value = SimpleNamespace(
            Subscription=SimpleNamespace(retrieve=lambda *a, **kw: fake_sub)
        )
        resp = _signed_post(client, event)
    assert resp.status_code == 200
    _db.session.expire_all()
    c = _db.session.get(Company, company.id)
    assert c.stripe_contact_usage_subscription_item_id == si_meter_id
    assert c.included_contacts == 50_000
