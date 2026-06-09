"""
RBAC proof tests for POST /api/api-hub/<slug>/save

Proves:
  - platform_admin  → 200 on platform-scoped save
  - company_admin   → 403 on platform-scoped save
  - company_admin   → 200 on company-scoped save
  - regular user    → 403 on any save
  - unauthenticated → 302 redirect to login
  - raw secret never appears in JSON response or ApiHubAuditLog.notes
  - unknown slug → 400
"""
import os, sys
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app
from extensions import db as _db
from models import User, Company, UserCompanyAccess, ApiHubAuditLog
from werkzeug.security import generate_password_hash

PLATFORM_SLUG = "openai"           # scope="platform" in _HUB_PROVIDERS
COMPANY_SLUG  = "facebook_page"    # scope="company"  in _COMPANY_PROVIDERS
VALID_KEY     = "OPENAI_API_KEY"
COMPANY_KEY   = "FACEBOOK_ACCESS_TOKEN"
SECRET_VALUE  = "sk-testrbac-0000000000000000000000000000000000000000"


@pytest.fixture
def app():
    a = create_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False, LOGIN_DISABLED=False)
    yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture
def users(app):
    """
    Create platform_admin, company_admin, and regular_user with unique
    timestamped credentials so concurrent/repeated runs never collide.
    Teardown deletes only UserCompanyAccess rows (which have no dependents),
    then NULLs out actor_user_id in audit rows before removing the users.
    """
    import time
    from sqlalchemy import text

    ts = str(int(time.time() * 1000))[-8:]  # 8-digit suffix, unique per run

    with app.app_context():
        company = Company(name=f"RBAC Test Co {ts}")
        _db.session.add(company)
        _db.session.flush()

        p_admin = User(
            username=f"rbac_pa_{ts}",
            email=f"rbac_pa_{ts}@rbactest.local",
            password_hash=generate_password_hash("x"),
            is_admin=True,
            default_company_id=company.id,
        )
        c_admin = User(
            username=f"rbac_ca_{ts}",
            email=f"rbac_ca_{ts}@rbactest.local",
            password_hash=generate_password_hash("x"),
            is_admin=False,
            default_company_id=company.id,
        )
        regular = User(
            username=f"rbac_re_{ts}",
            email=f"rbac_re_{ts}@rbactest.local",
            password_hash=generate_password_hash("x"),
            is_admin=False,
            default_company_id=company.id,
        )
        _db.session.add_all([p_admin, c_admin, regular])
        _db.session.flush()

        uca_admin = UserCompanyAccess(
            user_id=c_admin.id, company_id=company.id,
            role=UserCompanyAccess.ROLE_ADMIN,
        )
        uca_regular = UserCompanyAccess(
            user_id=regular.id, company_id=company.id,
            role=UserCompanyAccess.ROLE_VIEWER,
        )
        _db.session.add_all([uca_admin, uca_regular])
        _db.session.commit()

        ids = dict(
            company_id=company.id,
            platform_admin=p_admin.id,
            company_admin=c_admin.id,
            regular=regular.id,
        )

    yield ids

    with app.app_context():
        try:
            uids = [ids["platform_admin"], ids["company_admin"], ids["regular"]]
            cid  = ids["company_id"]
            # NULL actor_user_id in audit rows so the user delete doesn't hit FK
            _db.session.execute(
                text("UPDATE api_hub_audit_log SET actor_user_id = NULL "
                     "WHERE actor_user_id = ANY(:ids)"),
                {"ids": uids},
            )
            # NULL actor_user_id in provider_credential rows (source=manual)
            _db.session.execute(
                text("UPDATE provider_credential SET company_id = NULL "
                     "WHERE company_id = :cid"),
                {"cid": cid},
            )
            UserCompanyAccess.query.filter_by(company_id=cid).delete()
            User.query.filter(User.id.in_(uids)).delete(
                synchronize_session=False
            )
            Company.query.filter_by(id=cid).delete()
            _db.session.commit()
        except Exception:
            _db.session.rollback()


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_unauthenticated_is_redirected(client):
    """No session → 302 to login, not 200."""
    r = client.post(
        f"/api/api-hub/{PLATFORM_SLUG}/save",
        json={"key": VALID_KEY, "value": SECRET_VALUE},
    )
    assert r.status_code == 302, f"Expected 302 redirect, got {r.status_code}"


def test_regular_user_blocked_platform_scope(client, users):
    """Viewer role → 403 on platform-scoped provider."""
    _login(client, users["regular"])
    r = client.post(
        f"/api/api-hub/{PLATFORM_SLUG}/save",
        json={"key": VALID_KEY, "value": SECRET_VALUE},
    )
    assert r.status_code == 403, (
        f"Regular user must get 403, got {r.status_code}: {r.data[:200]}"
    )


def test_regular_user_blocked_company_scope(client, users):
    """Viewer role → 403 even on company-scoped provider."""
    _login(client, users["regular"])
    r = client.post(
        f"/api/api-hub/{COMPANY_SLUG}/save",
        json={"key": COMPANY_KEY, "value": SECRET_VALUE},
    )
    assert r.status_code == 403, (
        f"Regular user must get 403 on company scope, got {r.status_code}"
    )


def test_company_admin_blocked_from_platform_scope(client, users):
    """Company admin (ROLE_ADMIN) → 403 when saving a platform-scoped provider."""
    _login(client, users["company_admin"])
    r = client.post(
        f"/api/api-hub/{PLATFORM_SLUG}/save",
        json={"key": VALID_KEY, "value": SECRET_VALUE},
    )
    assert r.status_code == 403, (
        f"Company admin must be blocked from platform scope, got {r.status_code}: {r.data[:200]}"
    )


def test_company_admin_can_save_company_scope(client, users):
    """Company admin → 200 on company-scoped provider."""
    _login(client, users["company_admin"])
    r = client.post(
        f"/api/api-hub/{COMPANY_SLUG}/save",
        json={"key": COMPANY_KEY, "value": SECRET_VALUE},
    )
    body = r.get_json() or {}
    assert r.status_code == 200 and body.get("ok"), (
        f"Company admin should save company-scoped, got {r.status_code}: {body}"
    )
    assert "masked" in body
    assert SECRET_VALUE not in body.get("masked", ""), "Raw secret in masked field"


def test_platform_admin_can_save_platform_scope(client, users):
    """Platform admin → 200 on platform-scoped provider."""
    _login(client, users["platform_admin"])
    r = client.post(
        f"/api/api-hub/{PLATFORM_SLUG}/save",
        json={"key": VALID_KEY, "value": SECRET_VALUE},
    )
    body = r.get_json() or {}
    assert r.status_code == 200 and body.get("ok"), (
        f"Platform admin should save platform-scoped, got {r.status_code}: {body}"
    )
    assert "masked" in body
    assert SECRET_VALUE not in body.get("masked", ""), "Raw secret in masked field"


def test_raw_secret_never_in_json_response(client, users):
    """The full raw secret value must not appear anywhere in the HTTP response."""
    _login(client, users["platform_admin"])
    r = client.post(
        f"/api/api-hub/{PLATFORM_SLUG}/save",
        json={"key": VALID_KEY, "value": SECRET_VALUE},
    )
    assert SECRET_VALUE not in r.data.decode("utf-8", errors="replace"), (
        "Raw secret found in HTTP response body!"
    )


def test_audit_log_row_written_on_save(client, users, app):
    """ApiHubAuditLog must gain a new row after a successful save."""
    _login(client, users["platform_admin"])
    with app.app_context():
        before = ApiHubAuditLog.query.count()
    client.post(
        f"/api/api-hub/{PLATFORM_SLUG}/save",
        json={"key": VALID_KEY, "value": SECRET_VALUE},
    )
    with app.app_context():
        after = ApiHubAuditLog.query.count()
    assert after > before, "ApiHubAuditLog should gain a row after a successful save"


def test_audit_log_notes_never_contain_raw_secret(client, users, app):
    """No ApiHubAuditLog row may store the raw secret in notes."""
    _login(client, users["platform_admin"])
    client.post(
        f"/api/api-hub/{PLATFORM_SLUG}/save",
        json={"key": VALID_KEY, "value": SECRET_VALUE},
    )
    with app.app_context():
        rows = ApiHubAuditLog.query.filter_by(provider_slug=PLATFORM_SLUG).all()
        for row in rows:
            assert SECRET_VALUE not in (row.notes or ""), (
                f"Raw secret in audit log row id={row.id} notes={row.notes!r}"
            )


def test_unknown_slug_returns_400(client, users):
    """Unrecognised provider slug → 400, not 404 or 500."""
    _login(client, users["platform_admin"])
    r = client.post(
        "/api/api-hub/nonexistent_slug_xyz/save",
        json={"key": "SOME_KEY", "value": SECRET_VALUE},
    )
    assert r.status_code == 400, f"Unknown slug should return 400, got {r.status_code}"
