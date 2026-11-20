"""Workspace data export, closure (account cancellation), reopen and purge.

Tenant A has an owner, an admin, a viewer (who also belongs to tenant C) and
data in a company-scoped table (contact, deal), a child table reached only
through a foreign key (deal_activity), and a credential table
(company_secret). Tenant B has its own contact and must never change.
"""
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta

import pytest

from app import create_app
from services.license_service import PHONE_PWA_FEATURE
from extensions import db
from models import (Company, CompanySecret, Contact, Deal, DealActivity, MobileDevice, MobileSession,
                    SecurityAuditEvent, TeamInvitation, TenantLicense, User, UserCompanyAccess,
                    WorkspaceClosure)


@pytest.fixture
def world():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    with app.app_context():
        tag = uuid.uuid4().hex[:10]
        a = Company(name=f"Acme {tag}")
        b = Company(name=f"Bystander {tag}")
        c = Company(name=f"Other {tag}")
        db.session.add_all([a, b, c])
        db.session.flush()
        contact_a = Contact(company_id=a.id, email=f"alice-{tag}@a.example", first_name="Alice")
        contact_b = Contact(company_id=b.id, email=f"bob-{tag}@b.example", first_name="Bob")
        db.session.add_all([contact_a, contact_b])
        db.session.flush()
        deal = Deal(company_id=a.id, name=f"Deal {tag}", contact_id=contact_a.id)
        db.session.add(deal)
        db.session.flush()
        db.session.add(DealActivity(deal_id=deal.id, description=f"called {tag}"))
        db.session.add(CompanySecret(company_id=a.id, key="TWILIO_AUTH_TOKEN", value=f"supersecret-{tag}"))
        db.session.add(TenantLicense(company_id=a.id, feature_key=PHONE_PWA_FEATURE, status="active"))

        users = {}
        for key, company, role in (("owner", a, "owner"), ("admin", a, "admin"), ("viewer", a, "viewer"),
                                   ("b_owner", b, "owner")):
            u = User(username=f"wl-{key}-{tag}", email=f"wl-{key}-{tag}@example.com",
                     password_hash=f"pbkdf2:sha256:hash-{tag}", default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            users[key] = u.id
        db.session.add(UserCompanyAccess(user_id=users["viewer"], company_id=c.id, role="editor"))
        padmin = User(username=f"wl-padmin-{tag}", email=f"wl-padmin-{tag}@example.com", password_hash="x",
                      is_admin=True, default_company_id=b.id)
        db.session.add(padmin)
        db.session.flush()
        users["padmin"] = padmin.id

        db.session.add(TeamInvitation(company_id=a.id, email=f"new-{tag}@example.com", role="viewer",
                                      token_hash=uuid.uuid4().hex + uuid.uuid4().hex[:32],
                                      expires_at=datetime.utcnow() + timedelta(days=7)))
        device = MobileDevice(company_id=a.id, user_id=users["owner"], device_uid=f"dev-{tag}", platform="ios")
        db.session.add(device)
        db.session.flush()
        db.session.add(MobileSession(mobile_device_id=device.id, user_id=users["owner"], company_id=a.id,
                                     access_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                                     refresh_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                                     access_expires_at=datetime.utcnow() + timedelta(hours=1),
                                     refresh_expires_at=datetime.utcnow() + timedelta(days=30)))
        db.session.commit()
        yield app, tag, {"a": a.id, "b": b.id, "c": c.id}, users


class _Client:
    """Test client that drops Flask-Login's cached user before every request:
    the fixture keeps one app context open, so ``g._login_user`` would
    otherwise leak from the previous request's user."""

    def __init__(self, app, uid):
        self._c = app.test_client()
        with self._c.session_transaction() as s:
            s["_user_id"] = str(uid)
            s["_fresh"] = True

    def __getattr__(self, method):
        def call(*args, **kwargs):
            from flask import g
            g.pop("_login_user", None)
            return getattr(self._c, method)(*args, **kwargs)
        return call


def _as(app, uid):
    return _Client(app, uid)


def _close(app, uid, name, **extra):
    return _as(app, uid).post("/api/account/close", json={"confirm_name": name, **extra})


# ── export ─────────────────────────────────────────────────────────────── #

def _read_zip(data):
    zf = zipfile.ZipFile(io.BytesIO(data))
    return zf, {n: zf.read(n).decode() for n in zf.namelist()}


def test_owner_export_has_own_rows_children_and_no_secrets(world):
    app, tag, co, users = world
    r = _as(app, users["owner"]).get("/api/account/export")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    _, files = _read_zip(r.data)
    manifest = json.loads(files["manifest.json"])
    blob = "\n".join(files.values())
    assert f"alice-{tag}" in blob and f"Deal {tag}" in blob
    assert f"called {tag}" in files["tables/deal_activity.jsonl"]          # child reached through deal
    assert f"bob-{tag}" not in blob                                          # other tenant
    assert f"supersecret-{tag}" not in blob and f"hash-{tag}" not in blob     # credentials / password hashes
    assert "company_secret" in manifest["excluded_tables"]
    assert "tables/company_secret.jsonl" not in files
    assert manifest["tables"]["contact"]["rows"] == 1
    members = json.loads(files["members.json"])
    assert {m["role"] for m in members} == {"owner", "admin", "viewer"}
    assert all("password_hash" not in m for m in members)
    assert SecurityAuditEvent.query.filter_by(action="account.data_export", company_id=co["a"],
                                              outcome="success").count() == 1


def test_admin_may_export_viewer_may_not(world):
    app, tag, co, users = world
    assert _as(app, users["admin"]).get("/api/account/export").status_code == 200
    r = _as(app, users["viewer"]).get("/api/account/export")
    assert r.status_code == 403 and b"alice" not in r.data
    assert SecurityAuditEvent.query.filter_by(action="account.data_export", outcome="denied").count() >= 1


# ── closure ────────────────────────────────────────────────────────────── #

def test_only_owner_can_close_and_name_must_match(world):
    app, tag, co, users = world
    name = f"Acme {tag}"
    assert _close(app, users["admin"], name).status_code == 403
    assert _close(app, users["viewer"], name).status_code == 403
    r = _close(app, users["owner"], "acme")
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_mismatch"
    assert db.session.get(Company, co["a"]).is_active is not False


def test_close_refused_while_subscription_still_bills(world):
    app, tag, co, users = world
    company = db.session.get(Company, co["a"])
    company.stripe_subscription_id, company.stripe_subscription_status = f"sub_{tag}", "active"
    company.cancel_at_period_end = False
    db.session.commit()
    r = _close(app, users["owner"], f"Acme {tag}")
    assert r.status_code == 409 and r.get_json()["code"] == "subscription_active"
    company.cancel_at_period_end = True          # cancelled in the portal: closing is allowed
    db.session.commit()
    assert _close(app, users["owner"], f"Acme {tag}").status_code == 200


def test_close_revokes_everything_and_blocks_members(world):
    app, tag, co, users = world
    r = _close(app, users["owner"], f"Acme {tag}", reason="switching tools")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["mobile_sessions_revoked"] == 1

    company = db.session.get(Company, co["a"])
    closure = db.session.get(WorkspaceClosure, co["a"])
    assert company.is_active is False and closure.reopened_at is None
    assert 29 <= (closure.purge_after - closure.closed_at).days <= 30
    assert TeamInvitation.query.filter_by(company_id=co["a"], revoked_at=None).count() == 0
    assert MobileSession.query.filter_by(company_id=co["a"], revoked_at=None).count() == 0
    assert TenantLicense.query.filter_by(company_id=co["a"], feature_key=PHONE_PWA_FEATURE).one().status == "canceled"
    assert SecurityAuditEvent.query.filter_by(action="account.closed", company_id=co["a"]).count() == 1

    # Owner (no other workspace): API → 410, pages → closed page.
    owner = _as(app, users["owner"])
    r = owner.get("/api/account/entitlements")
    assert r.status_code == 410 and r.get_json()["code"] == "workspace_closed"
    r = owner.get("/dashboard")
    assert r.status_code == 302 and r.headers["Location"].endswith("/account/closed")
    assert owner.get("/account/closed").status_code == 200

    # Viewer also belongs to C: moved there, keeps working.
    viewer = _as(app, users["viewer"])
    r = viewer.get("/api/account/entitlements")
    assert db.session.get(User, users["viewer"]).default_company_id == co["c"]
    r = viewer.get("/api/account/entitlements")
    assert r.status_code == 200 and r.get_json()["workspace"]["id"] == co["c"]

    # Bystander tenant unaffected.
    r = _as(app, users["b_owner"]).get("/api/account/entitlements")
    assert r.status_code == 200 and r.get_json()["workspace"]["id"] == co["b"]
    assert db.session.get(Company, co["b"]).is_active is not False


def test_inactive_company_without_closure_row_keeps_todays_behavior(world):
    app, tag, co, users = world
    db.session.get(Company, co["a"]).is_active = False
    db.session.commit()
    assert _as(app, users["owner"]).get("/api/account/entitlements").status_code == 200


def test_platform_admin_reopens_before_purge(world):
    app, tag, co, users = world
    assert _close(app, users["owner"], f"Acme {tag}").status_code == 200
    assert _as(app, users["b_owner"]).post(f"/saas/accounts/{co['a']}/reopen").status_code == 403
    assert db.session.get(Company, co["a"]).is_active is False
    r = _as(app, users["padmin"]).post(f"/saas/accounts/{co['a']}/reopen")
    assert r.status_code == 302
    assert db.session.get(Company, co["a"]).is_active is True
    db.session.get(User, users["owner"]).default_company_id = co["a"]
    db.session.commit()
    r = _as(app, users["owner"]).get("/api/account/entitlements")
    assert r.status_code == 200 and r.get_json()["workspace"]["id"] == co["a"]


# ── purge ──────────────────────────────────────────────────────────────── #

def test_purge_respects_retention_then_deletes_only_that_workspace(world):
    from services.workspace_lifecycle import ClosureError, purge_workspace

    app, tag, co, users = world
    with pytest.raises(ClosureError) as exc:
        purge_workspace(co["a"])
    assert exc.value.code == "not_closed"
    assert _close(app, users["owner"], f"Acme {tag}").status_code == 200
    with pytest.raises(ClosureError) as exc:
        purge_workspace(co["a"], apply=True)
    assert exc.value.code == "retention"

    later = datetime.utcnow() + timedelta(days=31)
    dry = purge_workspace(co["a"], now=later)
    assert dry["applied"] is False and dry["rows"]["contact"] == 1 and dry["rows"]["deal_activity"] == 1
    assert Contact.query.filter_by(company_id=co["a"]).count() == 1               # dry run changed nothing

    done = purge_workspace(co["a"], apply=True, now=later)
    assert done["applied"] is True
    assert Contact.query.filter_by(company_id=co["a"]).count() == 0
    assert Deal.query.filter_by(company_id=co["a"]).count() == 0
    assert DealActivity.query.filter(DealActivity.description == f"called {tag}").count() == 0
    assert CompanySecret.query.filter_by(company_id=co["a"]).count() == 0
    assert UserCompanyAccess.query.filter_by(company_id=co["a"]).count() == 0
    tomb = db.session.get(Company, co["a"])
    assert tomb is not None and tomb.name == f"Closed workspace {co['a']}"
    assert SecurityAuditEvent.query.filter_by(action="account.closed", company_id=co["a"]).count() == 1
    assert Contact.query.filter_by(company_id=co["b"]).count() == 1                # bystander intact
    with pytest.raises(ClosureError) as exc:
        purge_workspace(co["a"], apply=True, now=later)
    assert exc.value.code == "purged"


def test_reopened_workspace_is_never_purged(world):
    from services.workspace_lifecycle import ClosureError, purge_workspace, reopen_workspace

    app, tag, co, users = world
    assert _close(app, users["owner"], f"Acme {tag}").status_code == 200
    reopen_workspace(db.session.get(Company, co["a"]), None)
    with pytest.raises(ClosureError):
        purge_workspace(co["a"], apply=True, now=datetime.utcnow() + timedelta(days=60))
    assert Contact.query.filter_by(company_id=co["a"]).count() == 1


def test_billing_page_shows_export_and_close_by_role(world):
    app, tag, co, users = world
    owner = _as(app, users["owner"]).get("/app/billing").get_data(as_text=True)
    admin = _as(app, users["admin"]).get("/app/billing").get_data(as_text=True)
    viewer = _as(app, users["viewer"]).get("/app/billing").get_data(as_text=True)
    assert 'id="account-data"' in owner and 'id="account-close"' in owner and "30 days" in owner
    assert 'id="account-data"' in admin and 'id="account-close"' not in admin
    assert 'id="account-data"' not in viewer and 'id="account-close"' not in viewer
