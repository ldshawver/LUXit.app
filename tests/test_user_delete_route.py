import os

import pytest
from flask import g
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess


@pytest.fixture
def app():
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False, SECRET_KEY="delete-test-secret")
    yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def login(client, user_id, legacy_keys=True):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        if legacy_keys:
            sess["user_id"] = user_id
            sess["logged_in"] = True


@pytest.fixture
def delete_world(app):
    with app.app_context():
        co = Company(name="Delete Co", is_active=True)
        other = Company(name="Delete Other Co", is_active=True)
        db.session.add_all([co, other])
        db.session.flush()
        owner = User(username="delete_owner", email="delete_owner@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
        manager = User(username="delete_manager", email="delete_manager@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
        staff = User(username="delete_staff", email="delete_staff@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
        outsider = User(username="delete_out", email="delete_out@test.com", password_hash=generate_password_hash("pw"), default_company_id=other.id)
        viewer = User(username="delete_viewer", email="delete_viewer@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
        platform = User(username="delete_platform", email="delete_platform@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id, is_admin=True)
        db.session.add_all([owner, manager, staff, outsider, viewer, platform])
        db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=owner.id, company_id=co.id, role="owner", is_default=True),
            UserCompanyAccess(user_id=manager.id, company_id=co.id, role="manager", is_default=True, manage_users_enabled=True),
            UserCompanyAccess(user_id=staff.id, company_id=co.id, role="staff", is_default=True, can_access_full_app=True, pwa_access_enabled=True),
            UserCompanyAccess(user_id=viewer.id, company_id=co.id, role="viewer", is_default=True),
            UserCompanyAccess(user_id=outsider.id, company_id=other.id, role="staff", is_default=True),
            UserCompanyAccess(user_id=platform.id, company_id=co.id, role="admin", is_default=True),
        ])
        db.session.commit()
        return {"co": co.id, "owner": owner.id, "manager": manager.id, "staff": staff.id, "outsider": outsider.id, "viewer": viewer.id, "platform": platform.id}


def flashed(client):
    with client.session_transaction() as sess:
        return [m for _cat, m in sess.get("_flashes", [])]


def clear_login_cache():
    if hasattr(g, "_login_user"):
        del g._login_user


def test_manage_users_and_delete_accept_same_flask_login_auth_without_legacy_session_keys(client, delete_world):
    login(client, delete_world["manager"], legacy_keys=False)
    assert client.get("/user/manage-users").status_code == 200

    resp = client.post(f"/user/delete/{delete_world['staff']}")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/user/manage-users")
    assert not any("session" in msg.lower() for msg in flashed(client))
    with client.application.app_context():
        staff = db.session.get(User, delete_world["staff"])
        access = UserCompanyAccess.query.filter_by(user_id=staff.id, company_id=delete_world["co"]).first()
        assert staff.active is False
        assert staff.archived_at is not None
        assert access.can_access_full_app is False
        assert access.pwa_access_enabled is False


def test_unauthenticated_delete_is_only_session_expired_like_path(client, delete_world):
    resp = client.post(f"/user/delete/{delete_world['staff']}")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_unauthorized_role_gets_permission_denied_not_session_expired(client, delete_world):
    login(client, delete_world["viewer"])
    resp = client.post(f"/user/delete/{delete_world['staff']}")
    assert resp.status_code == 302
    messages = flashed(client)
    assert "You do not have permission to delete users." in messages
    assert not any("session" in msg.lower() for msg in messages)


def test_cross_tenant_delete_is_blocked_with_403(client, delete_world):
    login(client, delete_world["manager"])
    resp = client.post(f"/user/delete/{delete_world['outsider']}")
    assert resp.status_code == 403


def test_nonexistent_and_self_and_last_owner_are_controlled(client, delete_world):
    login(client, delete_world["manager"])
    assert client.post("/user/delete/999999").status_code == 302
    assert "The selected user no longer exists." in flashed(client)

    resp = client.post(f"/user/delete/{delete_world['manager']}")
    assert resp.status_code == 302
    assert "You cannot delete your own account." in flashed(client)

    resp = client.post(f"/user/delete/{delete_world['owner']}")
    assert resp.status_code == 302
    assert "Another administrator or owner must be assigned before this account can be removed." in flashed(client)


def test_protected_global_admin_blocked_for_tenant_manager(client, delete_world):
    login(client, delete_world["manager"])
    resp = client.post(f"/user/delete/{delete_world['platform']}")
    assert resp.status_code == 302
    assert "You do not have permission to delete protected global administrators." in flashed(client)


def test_unexpected_exception_rolls_back_and_is_not_session_expired(client, delete_world, monkeypatch):
    login(client, delete_world["manager"])

    def boom():
        raise RuntimeError("forced")

    monkeypatch.setattr(db.session, "commit", boom)
    resp = client.post(f"/user/delete/{delete_world['staff']}")
    assert resp.status_code == 302
    messages = flashed(client)
    assert any("Unable to update this user right now" in msg for msg in messages)
    assert not any("session" in msg.lower() for msg in messages)


def test_integrity_error_dependency_conflict_is_controlled(client, delete_world, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    login(client, delete_world["manager"])

    def fail_integrity():
        raise IntegrityError("statement", "params", Exception("fk"))

    monkeypatch.setattr(db.session, "commit", fail_integrity)
    resp = client.post(f"/user/delete/{delete_world['staff']}")
    assert resp.status_code == 302
    messages = flashed(client)
    assert any("related records" in msg for msg in messages)
    assert not any("session" in msg.lower() for msg in messages)

def test_deactivated_user_cannot_login_remember_or_access_protected_routes(client, delete_world):
    login(client, delete_world["manager"])
    client.post(f"/user/delete/{delete_world['staff']}")

    # Existing protected-route access with the archived user's session is denied by user_loader/is_active.
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(delete_world["staff"])
        sess["_fresh"] = True
    clear_login_cache()
    protected = client.get("/user/manage-users")
    assert protected.status_code == 302
    assert "/auth/login" in protected.headers["Location"]

    with client.session_transaction() as sess:
        sess.clear()
    clear_login_cache()
    login_resp = client.post("/auth/login", data={"username": "delete_staff", "password": "pw"})
    assert login_resp.status_code == 200
    assert b"deactivated" in login_resp.data.lower() or any("deactivated" in msg.lower() for msg in flashed(client))

def test_deactivated_user_remember_cookie_cannot_restore_session(app, delete_world):
    with app.test_client() as staff_client:
        login_resp = staff_client.post("/auth/login", data={"username": "delete_staff", "password": "pw"})
        assert login_resp.status_code in (200, 302)
        with app.app_context():
            staff = db.session.get(User, delete_world["staff"])
            staff.active = False
            staff.archived_at = staff.created_at
            db.session.commit()
        with staff_client.session_transaction() as sess:
            sess.clear()
        clear_login_cache()
        protected = staff_client.get("/user/manage-users")
        assert protected.status_code == 302
        assert "/auth/login" in protected.headers["Location"]


def test_active_user_queries_seats_and_permissions_exclude_deactivated_users(client, delete_world):
    login(client, delete_world["manager"])
    client.post(f"/user/delete/{delete_world['staff']}")

    with client.application.app_context():
        co = db.session.get(Company, delete_world["co"])
        active_ids = [u.id for u in User.query.filter(User.active.is_(True)).all()]
        assert delete_world["staff"] not in active_ids
        assert delete_world["staff"] not in [u.id for u in User.query.filter_by(default_company_id=co.id, active=True).all()]
        assert delete_world["staff"] not in {row[0] for row in db.session.query(UserCompanyAccess.user_id).join(User, User.id == UserCompanyAccess.user_id).filter(UserCompanyAccess.company_id == co.id, User.active.is_(True)).all()}
        assert delete_world["staff"] not in {row.user_id for row in UserCompanyAccess.query.filter_by(company_id=co.id).all() if row.user.is_active and row.can_manage_users()}
        assert co.team_member_count == 4
        staff = db.session.get(User, delete_world["staff"])
        assert staff.archived_at is not None
        assert staff.archived_by_user_id == delete_world["manager"]


def test_final_owner_protection_ignores_inactive_owners(client, delete_world):
    with client.application.app_context():
        co_id = delete_world["co"]
        inactive_owner = User(username="inactive_owner", email="inactive_owner@test.com", password_hash=generate_password_hash("pw"), default_company_id=co_id, active=False)
        db.session.add(inactive_owner)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=inactive_owner.id, company_id=co_id, role="owner", is_default=True))
        db.session.commit()

    login(client, delete_world["manager"])
    resp = client.post(f"/user/delete/{delete_world['owner']}")
    assert resp.status_code == 302
    assert "Another administrator or owner must be assigned before this account can be removed." in flashed(client)


def _csrf_from_manage_users(client):
    import re
    page = client.get("/user/manage-users")
    assert page.status_code == 200
    match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
    assert match, page.data[:500]
    return match.group(1).decode()


def test_authenticated_admin_post_with_valid_csrf_succeeds_integration_style(csrf_app):
    with csrf_app.test_client() as client:
        with csrf_app.app_context():
            co = Company(name="CSRF Success Co", is_active=True)
            db.session.add(co)
            db.session.flush()
            owner = User(username="csrf_success_owner", email="csrf_success_owner@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
            staff = User(username="csrf_success_staff", email="csrf_success_staff@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
            db.session.add_all([owner, staff])
            db.session.flush()
            db.session.add_all([
                UserCompanyAccess(user_id=owner.id, company_id=co.id, role="owner", is_default=True),
                UserCompanyAccess(user_id=staff.id, company_id=co.id, role="staff", is_default=True, can_access_full_app=True),
            ])
            db.session.commit()
            owner_id, staff_id = owner.id, staff.id
        login(client, owner_id)
        csrf_token = _csrf_from_manage_users(client)
        resp = client.post(f"/user/delete/{staff_id}", data={"csrf_token": csrf_token})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/user/manage-users")
        assert any("has been deactivated" in msg for msg in flashed(client))
        with csrf_app.app_context():
            assert db.session.get(User, staff_id).active is False


@pytest.fixture
def csrf_app():
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=True, WTF_CSRF_TIME_LIMIT=None, SECRET_KEY="delete-test-secret")
    yield a


def test_delete_requires_csrf_when_enabled(csrf_app):
    with csrf_app.test_client() as client:
        with csrf_app.app_context():
            co = Company(name="CSRF Co", is_active=True)
            db.session.add(co)
            db.session.flush()
            owner = User(username="csrf_owner", email="csrf_owner@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
            staff = User(username="csrf_staff", email="csrf_staff@test.com", password_hash=generate_password_hash("pw"), default_company_id=co.id)
            db.session.add_all([owner, staff])
            db.session.flush()
            db.session.add_all([
                UserCompanyAccess(user_id=owner.id, company_id=co.id, role="owner", is_default=True),
                UserCompanyAccess(user_id=staff.id, company_id=co.id, role="staff", is_default=True),
            ])
            db.session.commit()
            owner_id, staff_id = owner.id, staff.id
        login(client, owner_id)
        resp = client.post(f"/user/delete/{staff_id}")
        assert resp.status_code == 302
        assert any("Security check failed" in msg for msg in flashed(client))
