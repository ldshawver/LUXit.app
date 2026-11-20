"""Security audit trail (T-26).

Covers: sign-in outcomes recorded without the typed identifier or password,
role changes with before/after, cross-tenant edit attempts recorded as denied,
invitations and archive recorded, the read API is tenant-scoped, the detail
allowlist, and a broken audit table never breaks the action being audited.
"""
import json
import uuid

import pytest
from flask import g
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

import auth as auth_module
from app import create_app
from extensions import db
from models import Company, SecurityAuditEvent, TeamInvitation, User, UserCompanyAccess
from services import security_audit

PASSWORD = "correct-horse-battery-9"


class _FreshUserClient(FlaskClient):
    """The fixture keeps one app context open; drop Flask-Login's cached user per request."""

    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(auth_module, "_send_account_email", lambda *a, **k: True)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost",
                      RATELIMIT_ENABLED=False)
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        a = Company(name=f"Audit A {tag}", is_active=True, max_team_members=10)
        b = Company(name=f"Audit B {tag}", is_active=True)
        db.session.add_all([a, b])
        db.session.flush()
        ids = {"a": a.id, "b": b.id}
        for key, company, role in (("owner", a, "owner"), ("viewer", a, "viewer"),
                                   ("owner_b", b, "owner"), ("member_b", b, "viewer")):
            u = User(username=f"aud-{key}-{tag}", email=f"aud-{key}-{tag}@example.com",
                     password_hash=generate_password_hash(PASSWORD), default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            ids[key] = u.id
        admin = User(username=f"aud-admin-{tag}", email=f"aud-admin-{tag}@example.com", password_hash="x",
                     is_admin=True, default_company_id=a.id)
        db.session.add(admin)
        db.session.flush()
        ids["admin"] = admin.id
        db.session.commit()
        yield app, tag, ids
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    if uid:
        with c.session_transaction() as s:
            s["_user_id"] = str(uid)
            s["_fresh"] = True
    return c


def _events(action=None, **filters):
    q = SecurityAuditEvent.query
    if action:
        q = q.filter_by(action=action)
    if filters:
        q = q.filter_by(**filters)
    return q.order_by(SecurityAuditEvent.id).all()


def _row_text(evt):
    return " ".join(str(v) for v in (evt.action, evt.outcome, evt.ip, evt.detail))


def test_login_success_and_bad_password_are_recorded_without_secrets(env):
    app, tag, ids = env
    username = f"aud-viewer-{tag}"
    r = _as(app, None).post("/auth/login", data={"username": username, "password": "wrong-password-123"})
    assert r.status_code == 200
    fail = _events("auth.login", target_user_id=ids["viewer"])
    assert [(e.outcome, json.loads(e.detail)["reason"]) for e in fail] == [("failure", "bad_password")]
    assert fail[0].actor_user_id is None
    assert "wrong-password" not in _row_text(fail[0]) and username not in _row_text(fail[0])

    r = _as(app, None).post("/auth/login", data={"username": username, "password": PASSWORD})
    assert r.status_code == 302
    ok = _events("auth.login", target_user_id=ids["viewer"], outcome="success")
    assert len(ok) == 1
    assert (ok[0].actor_user_id, ok[0].company_id) == (ids["viewer"], ids["a"])
    assert PASSWORD not in _row_text(ok[0])


def test_unknown_account_login_does_not_store_the_typed_identifier(env):
    app, tag, ids = env
    typed = f"Typo-{tag}-maybe-a-password"
    _as(app, None).post("/auth/login", data={"username": typed, "password": "whatever-123"})
    rows = [e for e in _events("auth.login") if e.target_user_id is None]
    assert rows and json.loads(rows[-1].detail) == {"reason": "unknown_account"}
    assert all(typed not in _row_text(e) for e in SecurityAuditEvent.query.all())


def test_role_change_records_before_and_after(env):
    app, tag, ids = env
    viewer = db.session.get(User, ids["viewer"])
    r = _as(app, ids["owner"]).post(f"/user/edit/{ids['viewer']}", data={
        "username": viewer.username, "email": viewer.email, "first_name": "", "last_name": "", "role": "editor"})
    assert r.status_code == 302
    evt = _events("team.user.updated", target_user_id=ids["viewer"])[-1]
    assert (evt.outcome, evt.actor_user_id, evt.company_id) == ("success", ids["owner"], ids["a"])
    detail = json.loads(evt.detail)
    assert (detail["role_from"], detail["role_to"]) == ("viewer", "editor")


def test_cross_tenant_edit_attempt_is_recorded_as_denied(env):
    app, tag, ids = env
    r = _as(app, ids["owner"]).post(f"/user/edit/{ids['member_b']}", data={"username": "taken-over", "role": "owner"})
    assert r.status_code == 302
    assert db.session.get(User, ids["member_b"]).username != "taken-over"
    evt = _events("team.user.updated", target_user_id=ids["member_b"])[-1]
    assert (evt.outcome, evt.actor_user_id, evt.company_id) == ("denied", ids["owner"], ids["a"])
    assert json.loads(evt.detail) == {"reason": "other_tenant"}


def test_invitation_created_revoked_and_archive_are_recorded(env):
    app, tag, ids = env
    c = _as(app, ids["owner"])
    assert c.post("/user/invite", data={"email": f"new-{tag}@example.com", "role": "editor"}).status_code == 302
    inv = TeamInvitation.query.filter_by(email=f"new-{tag}@example.com").one()
    created = _events("team.invitation.created", company_id=ids["a"])
    assert json.loads(created[-1].detail) == {"invitation_id": inv.id, "role": "editor"}
    assert f"new-{tag}" not in _row_text(created[-1])  # the address stays on the invitation row only

    assert c.post(f"/user/invite/{inv.id}/revoke").status_code == 302
    assert _events("team.invitation.revoked", company_id=ids["a"])[-1].actor_user_id == ids["owner"]

    assert c.post(f"/user/delete/{ids['viewer']}").status_code == 302
    archived = _events("team.user.archived", target_user_id=ids["viewer"])
    assert len(archived) == 1 and archived[0].company_id == ids["a"]


def test_read_api_is_scoped_to_the_callers_workspace(env):
    app, tag, ids = env
    security_audit.record("test.event", actor=db.session.get(User, ids["owner"]), company_id=ids["a"])
    security_audit.record("test.event", actor=db.session.get(User, ids["owner_b"]), company_id=ids["b"])

    r = _as(app, ids["owner"]).get("/api/security/audit-events?action=test.event")
    assert r.status_code == 200
    assert {e["company_id"] for e in r.get_json()["events"]} == {ids["a"]}

    assert _as(app, ids["owner"]).get(f"/api/security/audit-events?company_id={ids['b']}").status_code == 403
    assert _as(app, ids["viewer"]).get("/api/security/audit-events").status_code == 403
    assert _as(app, None).get("/api/security/audit-events").status_code in (302, 401)

    r = _as(app, ids["admin"]).get(f"/api/security/audit-events?company_id={ids['b']}&action=test.event")
    assert r.status_code == 200 and {e["company_id"] for e in r.get_json()["events"]} == {ids["b"]}


def test_detail_allowlist_drops_secrets_and_long_values():
    out = json.loads(security_audit._clean_detail({
        "reason": "x" * 500, "password": "hunter2", "token": "abc", "fields": ["email", "password_hash"],
        "role": None, "nested": {"a": 1},
    }))
    assert out == {"reason": "x" * 120, "fields": ["email", "password_hash"]}
    assert security_audit._clean_detail({"password": "hunter2"}) is None


def test_broken_audit_table_never_breaks_the_action(env, monkeypatch):
    app, tag, ids = env

    def _boom(*a, **k):
        raise RuntimeError("relation security_audit_event does not exist")

    monkeypatch.setattr(db.session, "begin_nested", _boom)
    r = _as(app, None).post("/auth/login", data={"username": f"aud-viewer-{tag}", "password": PASSWORD})
    assert r.status_code == 302  # still signed in
    assert security_audit.record("test.event") is False
