"""Self-serve signup: org creation, email verification, login gate, no enumeration."""
import re

import pytest
from werkzeug.security import generate_password_hash

import auth as auth_module
from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess, UserEmailVerification


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SELF_SERVE_SIGNUP_ENABLED", "1")
    sent = []

    def _capture(to_email, subject, html):
        sent.append({"to": to_email, "subject": subject, "html": html})
        return True

    monkeypatch.setattr(auth_module, "_send_account_email", _capture)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        legacy_co = Company(name="Existing Co")
        db.session.add(legacy_co)
        db.session.flush()
        legacy = User(username="legacy", email="legacy@example.com", default_company_id=legacy_co.id,
                      password_hash=generate_password_hash("legacy-pass-1"))
        db.session.add(legacy)
        db.session.commit()
        yield app, sent
        db.session.remove()
        db.drop_all()


FORM = {
    "org_name": "Acme Plumbing",
    "full_name": "Pat Doe",
    "email": "Pat@Acme.example",
    "password": "correct-horse-1",
    "confirm_password": "correct-horse-1",
    "accept_terms": "1",
}


def _link(mail):
    return re.search(r'href="(http[^"]+/auth/verify-email/[^"]+)"', mail["html"]).group(1)


def _path(url):
    return url.split("localhost", 1)[1]


def test_signup_disabled_by_default(monkeypatch, env):
    app, _ = env
    monkeypatch.delenv("SELF_SERVE_SIGNUP_ENABLED")
    assert app.test_client().get("/auth/signup").status_code == 404
    assert app.test_client().post("/auth/signup", data=FORM).status_code == 404


def test_signup_creates_org_owner_and_requires_verification(env):
    app, sent = env
    c = app.test_client()
    r = c.post("/auth/signup", data=FORM)
    assert r.status_code == 200 and b"Check your email" in r.data

    user = User.query.filter_by(email="pat@acme.example").one()
    assert user.is_admin is False
    company = db.session.get(Company, user.default_company_id)
    assert company.name == "Acme Plumbing"
    access = UserCompanyAccess.query.filter_by(user_id=user.id).one()
    assert (access.company_id, access.role, access.is_default) == (company.id, "owner", True)
    row = db.session.get(UserEmailVerification, user.id)
    assert row.verified_at is None and row.terms_accepted_at is not None
    assert len(sent) == 1 and sent[0]["to"] == "pat@acme.example"

    # Can't sign in before verifying.
    r = c.post("/auth/login", data={"username": "pat@acme.example", "password": "correct-horse-1"})
    assert b"Confirm your email" in r.data
    with c.session_transaction() as s:
        assert "_user_id" not in s


def test_verification_link_signs_in_and_unlocks_login(env):
    app, sent = env
    c = app.test_client()
    c.post("/auth/signup", data=FORM)
    r = c.get(_path(_link(sent[0])))
    assert r.status_code == 302 and "/billing" in r.headers["Location"]
    user = User.query.filter_by(email="pat@acme.example").one()
    assert db.session.get(UserEmailVerification, user.id).verified_at is not None
    with c.session_transaction() as s:
        assert s.get("_user_id") == str(user.id)

    # Reused link doesn't sign anyone in again.
    c2 = app.test_client()
    r = c2.get(_path(_link(sent[0])))
    assert b"already confirmed" in r.data
    with c2.session_transaction() as s:
        assert "_user_id" not in s

    r = c2.post("/auth/login", data={"username": "pat@acme.example", "password": "correct-horse-1"})
    assert r.status_code == 302


def test_existing_email_gets_same_response_and_no_new_account(env):
    app, sent = env
    c = app.test_client()
    new = c.post("/auth/signup", data=FORM).data
    dup = c.post("/auth/signup", data={**FORM, "email": "legacy@example.com", "org_name": "Takeover"}).data
    strip = lambda b: re.sub(rb'name="csrf_token" value="[^"]*"', b"", b)
    assert strip(new) == strip(dup)
    assert Company.query.filter_by(name="Takeover").count() == 0
    legacy = User.query.filter_by(email="legacy@example.com").one()
    assert UserCompanyAccess.query.filter_by(user_id=legacy.id).count() == 0
    # The real owner is told, and the mail carries no verification link.
    assert sent[-1]["to"] == "legacy@example.com" and "verify-email" not in sent[-1]["html"]


def test_pre_signup_accounts_are_not_gated(env):
    app, _ = env
    r = app.test_client().post("/auth/login", data={"username": "legacy", "password": "legacy-pass-1"})
    assert r.status_code == 302


def test_tampered_or_mismatched_token_is_rejected(env):
    app, sent = env
    c = app.test_client()
    c.post("/auth/signup", data=FORM)
    link = _path(_link(sent[0]))
    assert b"t valid" in c.get(link[:-3] + "xyz").data

    # Token is bound to the address: changing the email kills it.
    user = User.query.filter_by(email="pat@acme.example").one()
    user.email = "someone-else@acme.example"
    db.session.commit()
    assert b"t valid" in c.get(link).data
    assert db.session.get(UserEmailVerification, user.id).verified_at is None


def test_validation_errors_do_not_create_anything(env):
    app, sent = env
    c = app.test_client()
    for bad in ({"accept_terms": ""}, {"confirm_password": "nope-nope-1"}, {"email": "not-an-email"},
                {"password": "short", "confirm_password": "short"}, {"org_name": ""}):
        r = c.post("/auth/signup", data={**FORM, **bad})
        assert r.status_code == 400
    assert User.query.filter_by(email="pat@acme.example").count() == 0
    assert Company.query.filter_by(name="Acme Plumbing").count() == 0
    assert sent == []


def test_honeypot_creates_nothing(env):
    app, sent = env
    r = app.test_client().post("/auth/signup", data={**FORM, "website": "http://spam"})
    assert r.status_code == 200
    assert User.query.filter_by(email="pat@acme.example").count() == 0 and sent == []


def test_resend_has_cooldown_and_does_not_enumerate(env):
    app, sent = env
    c = app.test_client()
    c.post("/auth/signup", data=FORM)
    assert len(sent) == 1
    # Within the cooldown: no second mail. Unknown address: same page, no mail.
    a = c.post("/auth/verify-email/resend", data={"email": "pat@acme.example"})
    b = c.post("/auth/verify-email/resend", data={"email": "nobody@nowhere.example"})
    assert a.status_code == b.status_code == 200
    assert len(sent) == 1

    row = db.session.get(UserEmailVerification, User.query.filter_by(email="pat@acme.example").one().id)
    from datetime import datetime, timedelta
    row.last_sent_at = datetime.utcnow() - timedelta(minutes=5)
    db.session.commit()
    c.post("/auth/verify-email/resend", data={"email": "pat@acme.example"})
    assert len(sent) == 2


def test_password_reset_counts_as_verification(env):
    app, sent = env
    c = app.test_client()
    c.post("/auth/signup", data=FORM)
    from models import PasswordResetToken
    from datetime import datetime, timedelta
    user = User.query.filter_by(email="pat@acme.example").one()
    db.session.add(PasswordResetToken(user_id=user.id, token="t" * 40, expires_at=datetime.utcnow() + timedelta(hours=1)))
    db.session.commit()
    c.post("/auth/reset-password/" + "t" * 40, data={"password": "brand-new-pass", "confirm": "brand-new-pass"})
    assert db.session.get(UserEmailVerification, user.id).verified_at is not None


def test_signup_never_creates_platform_admin(env):
    app, _ = env
    app.test_client().post("/auth/signup", data={**FORM, "is_admin": "1", "role": "admin"})
    assert User.query.filter_by(email="pat@acme.example").one().is_admin is False


def test_login_page_links_signup_only_when_enabled(monkeypatch, env):
    app, _ = env
    assert b"/auth/signup" in app.test_client().get("/auth/login").data
    monkeypatch.delenv("SELF_SERVE_SIGNUP_ENABLED")
    assert b"/auth/signup" not in app.test_client().get("/auth/login").data
