"""Extra workspaces can't step around the free plan; logos can't carry script."""
import io
import os
import uuid

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import Company, CompanyPlanPolicy, User, UserCompanyAccess
from services.entitlements import is_free_plan


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
        home = Company(name=f"Home {tag}")
        db.session.add(home)
        db.session.flush()
        ids = {"tag": tag}
        for key, admin in (("member", False), ("platform", True)):
            u = User(username=f"ws-{key}-{tag}", email=f"ws-{key}-{tag}@example.com", password_hash="x",
                     is_admin=admin, default_company_id=home.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=home.id, role="owner", is_default=True))
            ids[key] = u.id
        db.session.commit()
        yield app, ids
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def _company(name):
    return Company.query.filter_by(name=name).one()


@pytest.mark.parametrize("path,field", [("/companies/add", "name"), ("/onboarding/submit", "company_name")])
def test_member_created_workspace_starts_on_free_plan(env, path, field):
    app, ids = env
    name = f"Second {ids['tag']} {path}"
    _as(app, ids["member"]).post(path, data={field: name})
    company = _company(name)
    assert db.session.get(CompanyPlanPolicy, company.id).plan_source == "self_serve"
    assert is_free_plan(company.id)
    assert company.max_team_members == 1 and company.billing_status == "none"


def test_platform_admin_creates_managed_workspace(env):
    app, ids = env
    name = f"Managed {ids['tag']}"
    _as(app, ids["platform"]).post("/companies/add", data={"name": name})
    company = _company(name)
    assert db.session.get(CompanyPlanPolicy, company.id) is None
    assert not is_free_plan(company.id)


@pytest.mark.parametrize("filename", ["x.html", "x.svg", "x", "../../app.py"])
def test_logo_refuses_non_raster_files(env, filename):
    app, ids = env
    name = f"Logo {ids['tag']} {uuid.uuid4().hex[:4]}"
    _as(app, ids["member"]).post("/companies/add", data={
        "name": name, "logo": (io.BytesIO(b"<script>alert(1)</script>"), filename)},
        content_type="multipart/form-data")
    assert _company(name).logo_path is None


def test_logo_gets_random_name(env):
    app, ids = env
    name = f"Logo ok {ids['tag']}"
    _as(app, ids["member"]).post("/companies/add", data={
        "name": name, "logo": (io.BytesIO(b"\x89PNG\r\n"), "shared-name.png")}, content_type="multipart/form-data")
    path = _company(name).logo_path
    try:
        assert path.startswith("company_logos/") and path.endswith(".png") and "shared-name" not in path
    finally:
        if path and os.path.exists(f"static/{path}"):
            os.remove(f"static/{path}")


def test_company_settings_page_requires_membership(env):
    app, ids = env
    other = Company(name=f"Other {ids['tag']}", website_url="https://other-tenant.example")
    db.session.add(other)
    db.session.commit()
    r = _as(app, ids["member"]).get(f"/company/{other.id}/settings")
    assert r.status_code == 302 and b"other-tenant.example" not in r.data
    home = User.query.get(ids["member"]).default_company_id
    assert _as(app, ids["member"]).get(f"/company/{home}/settings").status_code == 200
