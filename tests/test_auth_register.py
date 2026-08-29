"""Regression coverage for the admin bootstrap route ``/auth/register``.

Guards the defect where the route created the tenant company with a raw
``INSERT INTO company (name, is_active)`` that skipped the SQLAlchemy model's
column defaults and tripped the ``NOT NULL`` columns which only carry a
Python-side default (e.g. ``require_approved_pwa_devices``, ``contacts_used``).
The fix creates the company through the ORM so the canonical model defaults
apply; these tests pin that behaviour without hard-coding the defaults here.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import app, db
from models import Company, User, UserCompanyAccess


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"

    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _register(client, follow_redirects=False, **overrides):
    form = {
        "username": "admin",
        "email": "admin@example.com",
        "password": "supersecret1",
        "confirm_password": "supersecret1",
        "company_name": "",
    }
    form.update(overrides)
    return client.post("/auth/register", data=form, follow_redirects=follow_redirects)


# A. Company Name left blank -> admin created, no new company, no crash.
def test_register_blank_company_name_creates_admin_without_company(client):
    companies_before = Company.query.count()

    resp = _register(client, company_name="")
    assert resp.status_code == 302

    user = User.query.filter_by(email="admin@example.com").one()
    assert user.is_admin is True
    assert user.password_hash and user.password_hash != "supersecret1"
    assert user.default_company_id is None
    # Registration itself creates no company when the field is blank.
    assert Company.query.count() == companies_before


# B / C. Non-empty synthetic company name -> company row is created using the
# real model schema (the raw-INSERT path would raise NOT NULL here).
def test_register_with_company_name_creates_company_with_model_defaults(client):
    resp = _register(client, company_name="ZZ-Synthetic Register Co")
    assert resp.status_code == 302

    company = Company.query.filter_by(name="ZZ-Synthetic Register Co").one()
    assert company.is_active is True
    # Canonical model defaults were applied (not asserting exact values here —
    # only that the ORM populated the NOT NULL / Python-default columns).
    assert company.require_approved_pwa_devices is not None
    assert company.contacts_used is not None
    assert company.setup_fee_paid is not None

    user = User.query.filter_by(email="admin@example.com").one()
    assert user.default_company_id == company.id


# D. Admin/owner association: the first login self-heals the owner access row.
def test_first_login_attaches_registered_admin_as_owner(client):
    _register(client, company_name="ZZ-Synthetic Register Co")
    client.get("/auth/logout", follow_redirects=False)

    resp = client.post(
        "/auth/login",
        data={"username": "admin@example.com", "password": "supersecret1"},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    user = User.query.filter_by(email="admin@example.com").one()
    acc = UserCompanyAccess.query.filter_by(user_id=user.id).one()
    assert acc.role == UserCompanyAccess.ROLE_OWNER
    assert acc.is_default is True


# E. Once an admin exists, further registration is refused.
def test_register_closed_after_first_admin(client):
    first = _register(client, username="admin", email="admin@example.com")
    assert first.status_code == 302

    second = client.post(
        "/auth/register",
        data={
            "username": "second",
            "email": "second@example.com",
            "password": "supersecret1",
            "confirm_password": "supersecret1",
            "company_name": "",
        },
        follow_redirects=True,
    )
    assert second.status_code == 200
    assert b"Admin registration is closed" in second.data
    assert User.query.filter_by(email="second@example.com").first() is None


# F. A failure during registration must leave no partial user/company/access rows.
def test_register_failure_rolls_back_all_rows(client):
    users_before = User.query.count()
    companies_before = Company.query.count()
    access_before = UserCompanyAccess.query.count()

    with patch.object(db.session, "commit", side_effect=SQLAlchemyError("boom")):
        resp = _register(client, company_name="ZZ-Synthetic Register Co", follow_redirects=True)

    assert resp.status_code == 200
    assert b"Registration failed" in resp.data
    assert User.query.filter_by(email="admin@example.com").first() is None
    assert Company.query.filter_by(name="ZZ-Synthetic Register Co").first() is None
    assert User.query.count() == users_before
    assert Company.query.count() == companies_before
    assert UserCompanyAccess.query.count() == access_before
