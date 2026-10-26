"""Only workspace owners/admins may start checkout, open the portal or report usage.

Regression: membership alone (any role, or just default_company_id) was enough,
so a viewer could open the Stripe Customer Portal and cancel the subscription.
"""
import os
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess


@pytest.fixture
def env():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    with app.app_context():
        tag = uuid.uuid4().hex[:10]
        company = Company(name=f"Billing authz {tag}", stripe_customer_id=f"cus_authz_{tag}")
        db.session.add(company)
        db.session.flush()
        users = {}
        for role in ("owner", "admin", "manager", "viewer", None):
            name = f"authz-{role or 'nomember'}-{tag}"
            u = User(username=name, email=f"{name}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            if role:
                db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role))
            users[role or "nomember"] = u.id
        db.session.commit()
        yield app, company.id, users


def _as(app, user_id):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
    return c


@pytest.mark.parametrize("who,allowed", [
    ("owner", True), ("admin", True), ("manager", False), ("viewer", False), ("nomember", False),
])
def test_portal_and_checkout_need_owner_or_admin(env, who, allowed):
    app, company_id, users = env
    c = _as(app, users[who])
    portal = SimpleNamespace(id="bps_x", url="https://billing.stripe.com/p/x")
    checkout = SimpleNamespace(id="cs_x", url="https://checkout.stripe.com/c/x")
    with patch("services.stripe_billing.create_billing_portal_session", return_value=portal), \
         patch("services.stripe_billing.create_checkout_session", return_value=checkout) as mk:
        p = c.post("/api/stripe/create-portal-session", json={"company_id": company_id})
        k = c.post("/api/stripe/create-checkout-session",
                   json={"lookup_key": "luxit_starter_monthly", "company_id": company_id})
    if allowed:
        assert (p.status_code, k.status_code) == (200, 200)
    else:
        assert (p.status_code, k.status_code) == (403, 403)
        assert mk.call_count == 0


def test_usage_report_needs_owner_or_admin(env):
    app, company_id, users = env
    r = _as(app, users["viewer"]).post("/api/stripe/report-contact-usage", json={"company_id": company_id})
    assert r.status_code == 403
