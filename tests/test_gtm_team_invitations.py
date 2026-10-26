"""Team invitations (T-5): invite by email, invitee sets own password, role applied.

Covers: only managers invite, no role escalation, seat limits, single-use and
expiring tokens, revoke (own tenant only), wrong signed-in account, existing
accounts joining a second workspace.
"""
import re
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g
from flask.testing import FlaskClient
from werkzeug.security import check_password_hash

import auth as auth_module
from app import create_app
from extensions import db
from models import Company, TeamInvitation, User, UserCompanyAccess


class _FreshUserClient(FlaskClient):
    """The fixture keeps one app context open, so Flask-Login's per-context
    user cache would otherwise leak from one request (and client) to the next."""

    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env(monkeypatch):
    sent = []

    def _capture(to_email, subject, html):
        sent.append({"to": to_email, "subject": subject, "html": html})
        return True

    monkeypatch.setattr(auth_module, "_send_account_email", _capture)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost",
                      RATELIMIT_ENABLED=False)
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        a = Company(name=f"Invite A {tag}", is_active=True, max_team_members=5)
        b = Company(name=f"Invite B {tag}", is_active=True)
        db.session.add_all([a, b])
        db.session.flush()
        ids = {"a": a.id, "b": b.id}
        for key, company, role in (("owner", a, "owner"), ("manager", a, "manager"), ("viewer", a, "viewer"),
                                   ("owner_b", b, "owner"), ("outsider", b, "viewer")):
            u = User(username=f"inv-{key}-{tag}", email=f"inv-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True,
                                             manage_users_enabled=(key == "manager")))
            ids[key] = u.id
        db.session.commit()
        yield app, sent, tag, ids
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    if uid:
        with c.session_transaction() as s:
            s["_user_id"] = str(uid)
            s["_fresh"] = True
    return c


def _token(mail):
    return re.search(r'/auth/invite/([A-Za-z0-9_\-]+)"', mail["html"]).group(1)


def _invite(app, uid, email, role="viewer"):
    return _as(app, uid).post("/user/invite", data={"email": email, "role": role})


def test_owner_invite_new_person_sets_own_password_and_joins(env):
    app, sent, tag, ids = env
    email = f"new-{tag}@example.com"
    r = _invite(app, ids["owner"], email.upper(), role="editor")
    assert r.status_code == 302
    assert len(sent) == 1 and sent[0]["to"] == email
    token = _token(sent[0])
    inv = TeamInvitation.query.filter_by(email=email).one()
    assert inv.token_hash != token and len(inv.token_hash) == 64  # only the hash is stored

    c = _as(app, None)
    page = c.get(f"/auth/invite/{token}")
    assert page.status_code == 200 and b"Create account and join" in page.data
    bad = c.post(f"/auth/invite/{token}", data={"password": "short", "confirm_password": "short"})
    assert bad.status_code == 400
    r = c.post(f"/auth/invite/{token}", data={"full_name": "Sam New", "password": "long-enough-1",
                                              "confirm_password": "long-enough-1"})
    assert r.status_code == 302
    user = User.query.filter_by(email=email).one()
    assert user.is_admin is False and check_password_hash(user.password_hash, "long-enough-1")
    access = UserCompanyAccess.query.filter_by(user_id=user.id).one()
    assert (access.company_id, access.role, access.is_active) == (ids["a"], "editor", True)
    assert user.default_company_id == ids["a"]

    # Single use.
    assert b"already used" in _as(app, None).get(f"/auth/invite/{token}").data


def test_existing_account_signs_in_then_joins_second_workspace(env):
    app, sent, tag, ids = env
    outsider = db.session.get(User, ids["outsider"])
    _invite(app, ids["owner"], outsider.email)
    token = _token(sent[0])

    page = _as(app, None).get(f"/auth/invite/{token}")
    assert b"Sign in to join" in page.data and b"next=" in page.data

    c = _as(app, ids["outsider"])
    assert c.get(f"/auth/invite/{token}").status_code == 200
    assert c.post(f"/auth/invite/{token}").status_code == 302
    rows = {a.company_id: a.role for a in UserCompanyAccess.query.filter_by(user_id=ids["outsider"]).all()}
    assert rows == {ids["b"]: "viewer", ids["a"]: "viewer"}
    assert db.session.get(User, ids["outsider"]).default_company_id == ids["b"]  # unchanged


def test_wrong_signed_in_account_cannot_accept(env):
    app, sent, tag, ids = env
    _invite(app, ids["owner"], f"someone-{tag}@example.com")
    token = _token(sent[0])
    c = _as(app, ids["outsider"])
    assert c.get(f"/auth/invite/{token}").status_code == 403
    assert c.post(f"/auth/invite/{token}").status_code == 403
    assert UserCompanyAccess.query.filter_by(user_id=ids["outsider"], company_id=ids["a"]).first() is None


def test_viewer_cannot_invite(env):
    app, sent, tag, ids = env
    r = _invite(app, ids["viewer"], f"x-{tag}@example.com")
    assert r.status_code == 302 and "/dashboard" in r.headers["Location"]
    assert not sent and TeamInvitation.query.filter_by(company_id=ids["a"]).count() == 0


def test_manager_cannot_invite_above_own_role(env):
    app, sent, tag, ids = env
    _invite(app, ids["manager"], f"boss-{tag}@example.com", role="admin")
    assert not sent and TeamInvitation.query.filter_by(company_id=ids["a"]).count() == 0
    _invite(app, ids["manager"], f"peer-{tag}@example.com", role="viewer")
    assert len(sent) == 1


def test_unknown_role_and_existing_member_rejected(env):
    app, sent, tag, ids = env
    _invite(app, ids["owner"], f"r-{tag}@example.com", role="owner")
    _invite(app, ids["owner"], db.session.get(User, ids["viewer"]).email)
    assert not sent


def test_seat_limit_counts_pending_invites(env):
    app, sent, tag, ids = env
    company = db.session.get(Company, ids["a"])
    company.max_team_members = 4  # 3 members today
    db.session.commit()
    _invite(app, ids["owner"], f"one-{tag}@example.com")
    _invite(app, ids["owner"], f"two-{tag}@example.com")
    assert len(sent) == 1


def test_revoke_and_expiry_invalidate_link(env):
    app, sent, tag, ids = env
    _invite(app, ids["owner"], f"rev-{tag}@example.com")
    _invite(app, ids["owner"], f"exp-{tag}@example.com")
    t_rev, t_exp = _token(sent[0]), _token(sent[1])
    rev = TeamInvitation.query.filter_by(email=f"rev-{tag}@example.com").one()

    # Another tenant's owner can't revoke it.
    _as(app, ids["owner_b"]).post(f"/user/invite/{rev.id}/revoke")
    db.session.refresh(rev)
    assert rev.revoked_at is None

    _as(app, ids["owner"]).post(f"/user/invite/{rev.id}/revoke")
    assert b"isn't valid" in _as(app, None).get(f"/auth/invite/{t_rev}").data

    exp = TeamInvitation.query.filter_by(email=f"exp-{tag}@example.com").one()
    exp.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert b"expired" in _as(app, None).get(f"/auth/invite/{t_exp}").data
    assert _as(app, None).get("/auth/invite/not-a-real-token").status_code == 200


def test_team_page_lists_pending_invites_for_managers_only(env):
    app, sent, tag, ids = env
    _invite(app, ids["owner"], f"listed-{tag}@example.com")
    assert f"listed-{tag}@example.com".encode() in _as(app, ids["owner"]).get("/user/manage-users").data
    viewer_page = _as(app, ids["viewer"]).get("/user/manage-users")
    assert f"listed-{tag}@example.com".encode() not in viewer_page.data
