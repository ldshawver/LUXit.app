"""Stripe test-mode lifecycle, end to end through the signed webhook.

Two self-serve tenants. Events are signed exactly as Stripe signs them
(HMAC-SHA256 over "<t>.<body>" with the endpoint secret), so this exercises
the real verification path, not a mock. Covers activation, entitlements,
payment failure + grace, recovery, plan change, cancellation, replay,
out-of-order delivery, events for a superseded subscription, and that one
tenant's events never touch the other.
"""
import json
import os
import time
import uuid
from datetime import datetime, timedelta

import pytest
import stripe

from app import create_app
from extensions import db as _db
from models import Company, CompanyPlanPolicy, SaasAutomationLog
from services import entitlements

SECRET = "whsec_lifecycle_sandbox_only"


def _id(prefix):
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["STRIPE_WEBHOOK_SECRET"] = SECRET
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    with a.app_context():
        yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def _tenant(name):
    c = Company(name=f"{name} {_id('')}", stripe_customer_id=_id("cus_"))
    _db.session.add(c)
    _db.session.flush()
    entitlements.apply_new_workspace_policy(c, creator=None)
    _db.session.commit()
    return c


@pytest.fixture
def tenants(app, monkeypatch):
    # checkout.session.completed looks up the metered usage item; no network.
    monkeypatch.setattr("services.stripe_billing.get_stripe", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    return _tenant("Tenant A"), _tenant("Tenant B")


def _post(client, ev, secret=SECRET, ts=None, body=None):
    body = body if body is not None else json.dumps(ev)
    ts = ts or int(time.time())
    sig = stripe.WebhookSignature._compute_signature(f"{ts}.{json.dumps(ev)}", secret)
    return client.post("/api/stripe/webhook", data=body, content_type="application/json",
                       headers={"Stripe-Signature": f"t={ts},v1={sig}"})


_clock = [int(time.time()) - 1000]


def _ev(etype, obj, created=None, eid=None):
    _clock[0] += 1
    return {"id": eid or _id("evt_"), "object": "event", "type": etype, "livemode": False,
            "created": created or _clock[0], "data": {"object": obj}}


def _sub(cus, sub_id, status, lookup="luxit_starter_monthly", **extra):
    return dict({"id": sub_id, "object": "subscription", "customer": cus, "status": status,
                 "items": {"data": [{"price": {"lookup_key": lookup}, "quantity": 1}]}}, **extra)


def _checkout(c, sub_id, lookup="luxit_starter_monthly"):
    return _ev("checkout.session.completed", {
        "id": _id("cs_test_"), "object": "checkout.session", "customer": c.stripe_customer_id,
        "subscription": sub_id, "client_reference_id": str(c.id), "mode": "subscription",
        "metadata": {"company_id": str(c.id), "lookup_key": lookup}})


def _invoice(c, sub_id, etype):
    return _ev(etype, {"id": _id("in_"), "object": "invoice", "customer": c.stripe_customer_id,
                       "subscription": sub_id, "attempt_count": 1, "amount_paid": 19900})


def _fresh(c):
    _db.session.expire_all()
    return _db.session.get(Company, c.id)


def _snapshot(c):
    c = _fresh(c)
    return (c.billing_status, c.stripe_subscription_status, c.stripe_subscription_id,
            c.billing_tier, c.max_team_members, c.grace_period_ends_at)


# ── signature verification ────────────────────────────────────────────────

def test_signature_rejects_tampering_wrong_secret_and_stale_timestamp(client, tenants):
    a, _ = tenants
    ev = _checkout(a, _id("sub_"))
    before = _snapshot(a)
    tampered = json.dumps(ev).replace(str(a.id), "999999")
    assert _post(client, ev, body=tampered).status_code == 400
    assert _post(client, ev, secret="whsec_attacker").status_code == 400
    assert _post(client, ev, ts=int(time.time()) - 3600).status_code == 400
    assert _snapshot(a) == before
    assert SaasAutomationLog.query.filter_by(stripe_event_id=ev["id"]).first() is None


# ── full lifecycle, two tenants ───────────────────────────────────────────

def test_lifecycle_activation_grace_recovery_upgrade_cancel_isolated(client, tenants):
    a, b = tenants
    b_before = _snapshot(b)
    assert entitlements.is_free_plan(a.id) and entitlements.is_free_plan(b.id)

    sub = _id("sub_")
    activate = _checkout(a, sub)
    assert _post(client, activate).status_code == 200
    ca = _fresh(a)
    assert (ca.billing_status, ca.billing_tier, ca.max_team_members) == ("active", "starter", 5)
    assert not entitlements.is_free_plan(a.id)

    # Stripe redelivers the same event: acknowledged, nothing re-applied.
    r = _post(client, activate)
    assert r.status_code == 200 and r.get_json().get("duplicate") is True

    # Card declined at renewal → 14-day grace, still entitled.
    assert _post(client, _invoice(a, sub, "invoice.payment_failed")).status_code == 200
    ca = _fresh(a)
    assert ca.billing_status == "grace_period"
    assert ca.grace_period_ends_at > datetime.utcnow() + timedelta(days=13)
    assert not entitlements.is_free_plan(a.id), "grace period must keep paid entitlements"

    # Retry succeeds → active, grace cleared.
    assert _post(client, _invoice(a, sub, "invoice.payment_succeeded")).status_code == 200
    ca = _fresh(a)
    assert ca.billing_status == "active" and ca.grace_period_ends_at is None

    # Upgrade in the portal + schedule cancellation.
    up = _sub(a.stripe_customer_id, sub, "active", "luxit_professional_monthly", cancel_at_period_end=True)
    assert _post(client, _ev("customer.subscription.updated", up)).status_code == 200
    ca = _fresh(a)
    assert (ca.billing_tier, ca.max_team_members, ca.cancel_at_period_end) == ("professional", None, True)
    assert not entitlements.is_free_plan(a.id)

    # Period ends → subscription deleted → suspended, back to free-plan limits.
    gone = _sub(a.stripe_customer_id, sub, "canceled", "luxit_professional_monthly")
    assert _post(client, _ev("customer.subscription.deleted", gone)).status_code == 200
    ca = _fresh(a)
    assert ca.billing_status == "suspended" and ca.cancel_at_period_end is False
    assert entitlements.is_free_plan(a.id)

    assert _snapshot(b) == b_before, "tenant B changed by tenant A's billing events"
    assert entitlements.is_free_plan(b.id)


def test_grace_expiry_drops_entitlements(client, tenants):
    a, _ = tenants
    sub = _id("sub_")
    _post(client, _checkout(a, sub))
    _post(client, _invoice(a, sub, "invoice.payment_failed"))
    ca = _fresh(a)
    ca.grace_period_ends_at = datetime.utcnow() - timedelta(minutes=1)
    _db.session.commit()
    assert entitlements.is_free_plan(a.id)


def test_event_for_a_superseded_subscription_does_not_suspend_the_new_one(client, tenants):
    a, _ = tenants
    old, new = _id("sub_old_"), _id("sub_new_")
    _post(client, _checkout(a, old))
    _post(client, _ev("customer.subscription.deleted", _sub(a.stripe_customer_id, old, "canceled")))
    assert _fresh(a).billing_status == "suspended"

    _post(client, _checkout(a, new))            # tenant resubscribes
    assert _fresh(a).stripe_subscription_id == new

    # Late / retried events about the old subscription arrive afterwards.
    for late in (_ev("customer.subscription.deleted", _sub(a.stripe_customer_id, old, "canceled")),
                 _ev("customer.subscription.updated", _sub(a.stripe_customer_id, old, "canceled")),
                 _invoice(a, old, "invoice.payment_failed")):
        assert _post(client, late).status_code == 200
    ca = _fresh(a)
    assert (ca.billing_status, ca.stripe_subscription_id) == ("active", new)
    log = SaasAutomationLog.query.filter_by(stripe_event_id=late["id"]).first()
    assert log.status == "skipped"


def test_out_of_order_failed_payment_does_not_override_newer_success(client, tenants):
    a, _ = tenants
    sub = _id("sub_")
    _post(client, _checkout(a, sub))
    failed = _invoice(a, sub, "invoice.payment_failed")
    succeeded = _invoice(a, sub, "invoice.payment_succeeded")
    # Stripe retries `failed` (our endpoint 500'd) after `succeeded` was delivered.
    assert _post(client, succeeded).status_code == 200
    assert _post(client, failed).status_code == 200
    ca = _fresh(a)
    assert ca.billing_status == "active" and ca.grace_period_ends_at is None


def test_customer_of_tenant_a_cannot_be_redirected_to_tenant_b(client, tenants):
    a, b = tenants
    b_before = _snapshot(b)
    ev = _checkout(a, _id("sub_"))
    ev["data"]["object"]["client_reference_id"] = str(b.id)
    ev["data"]["object"]["metadata"]["company_id"] = str(b.id)
    assert _post(client, ev).status_code == 200
    assert _fresh(a).billing_status == "active"
    assert _snapshot(b) == b_before
