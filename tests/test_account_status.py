"""Account status API (M7): one view of plan, limits and features per workspace."""
import uuid

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from services.license_service import PHONE_PWA_FEATURE
from models import Company, CompanyPlanPolicy, Contact, TenantLicense, User, UserCompanyAccess


class _FreshUserClient(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        if db.session.get(Company, 1) is None:
            # Company 1 is the platform tenant and always holds the phone license.
            db.session.add(Company(id=1, name=f"Platform {tag}"))
            db.session.flush()
        if TenantLicense.query.filter_by(company_id=1, feature_key=PHONE_PWA_FEATURE).first() is None:
            # As in production: the platform tenant already holds a phone license,
            # so new workspaces are never seeded one by the fresh-install rule.
            db.session.add(TenantLicense(company_id=1, feature_key=PHONE_PWA_FEATURE, status="active"))
            db.session.flush()
        free = Company(name=f"Free {tag}", billing_status="none", billing_tier="free", max_team_members=1)
        paid = Company(name=f"Paid {tag}", billing_status="active", billing_tier="starter", max_team_members=5,
                       stripe_subscription_id=f"sub_{tag}", included_contacts=2500)
        legacy = Company(name=f"Legacy {tag}")
        db.session.add_all([free, paid, legacy])
        db.session.flush()
        db.session.add_all([CompanyPlanPolicy(company_id=free.id, plan_source="self_serve"),
                            CompanyPlanPolicy(company_id=paid.id, plan_source="self_serve")])
        ids = {"free": free.id, "paid": paid.id, "legacy": legacy.id}
        for key, company, role in (("free_owner", free, "owner"), ("paid_viewer", paid, "viewer"),
                                   ("legacy_admin", legacy, "admin")):
            u = User(username=f"acct-{key}-{tag}", email=f"acct-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            ids[key] = u.id
        nobody = User(username=f"acct-none-{tag}", email=f"acct-none-{tag}@example.com", password_hash="x")
        db.session.add(nobody)
        db.session.add_all([Contact(company_id=free.id, first_name=f"F{i}", is_active=True) for i in range(3)])
        db.session.add(TenantLicense(company_id=legacy.id, feature_key=PHONE_PWA_FEATURE, status="active"))
        db.session.commit()
        ids["nobody"] = nobody.id
        yield app, ids
        db.session.remove()


def _get(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c.get("/api/account/entitlements")


def test_free_workspace(env):
    app, ids = env
    data = _get(app, ids["free_owner"]).get_json()
    assert data["workspace"]["id"] == ids["free"] and data["role"] == "owner"
    assert data["plan"] == {"source": "self_serve", "tier": "free", "billing_status": "none", "free": True,
                            "grace_period_ends_at": None}
    assert data["seats"] == {"members": 1, "pending_invites": 0, "used": 1, "limit": 1}
    assert data["contacts"]["used"] == 3 and data["contacts"]["limit"] == 250
    assert data["features"]["ai"] is False and data["features"]["phone_sms"]["allowed"] is False
    assert data["can_manage_billing"] is True


def test_paid_workspace_viewer(env):
    app, ids = env
    data = _get(app, ids["paid_viewer"]).get_json()
    assert data["plan"]["tier"] == "starter" and data["plan"]["free"] is False
    assert data["contacts"]["limit"] == 2500 and data["features"]["ai"] is True
    assert data["role"] == "viewer" and data["can_manage_billing"] is False


def test_legacy_managed_workspace_is_unlimited(env):
    app, ids = env
    data = _get(app, ids["legacy_admin"]).get_json()
    assert data["plan"]["source"] == "managed" and data["plan"]["free"] is False
    assert data["contacts"]["limit"] is None and data["features"]["phone_sms"]["allowed"] is True


def test_requires_a_workspace(env):
    app, ids = env
    assert _get(app, ids["nobody"]).status_code == 404
    assert app.test_client().get("/api/account/entitlements").status_code in (302, 401)
