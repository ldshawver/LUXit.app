"""Regression tests for the inbox_pwa blueprint registration bug.

Historical bug: inbox_pwa.py used ``@inbox_pwa.route("/sw.js")`` — a bare,
undefined name — instead of ``@inbox_pwa_bp.route("/sw.js")``. Flask route
decorators run at module-body evaluation time, so this raised a NameError
while *importing* inbox_pwa.py. app.py's blueprint registration wraps that
import in a broad ``except Exception`` that only logs a warning, so the
whole app kept booting with the inbox_pwa blueprint silently missing:
/app/* and /sw.js all 404'd, and the inbound SMS webhook's push-dispatch
`from inbox_pwa import _fire_push_notification` raised the same NameError
on every message.

Fixed by commits b86894a (PWA root-route registration) and bd6f5b9
(POST support for the /login alias).
"""
import logging
import os
import subprocess
import sys
import types

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, TwilioConversation, TwilioPhoneNumber, User, UserCompanyAccess


def test_inbox_pwa_imports_without_nameerror_in_fresh_interpreter():
    """Guards against a bare `@inbox_pwa.route(...)` decorator regression."""
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["SESSION_SECRET"] = "test-secret"
    env.setdefault("OPENAI_API_KEY", "test")

    result = subprocess.run(
        [sys.executable, "-c", "import inbox_pwa; print('ok')"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "NameError" not in result.stderr
    assert result.stdout.strip() == "ok"


@pytest.fixture
def app():
    os.environ.setdefault("SESSION_SECRET", "test-secret")
    a = create_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    return a


def test_inbox_pwa_blueprint_registers_with_expected_routes(app):
    assert "inbox_pwa" in app.blueprints
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "inbox_pwa.service_worker" in endpoints
    assert "inbox_pwa.pwa_manifest" in endpoints
    assert "inbox_pwa.pwa_root" in endpoints
    assert "inbox_pwa.pwa_index" in endpoints


def test_sw_js_manifest_and_app_root_are_reachable(app):
    with app.test_client() as client:
        sw = client.get("/sw.js")
        assert sw.status_code == 200

        manifest = client.get("/manifest.json")
        assert manifest.status_code == 200
        assert manifest.mimetype == "application/json"

        root = client.get("/app/", follow_redirects=False)
        assert root.status_code == 302
        assert root.headers["Location"].endswith("/app/inbox")


def test_login_alias_accepts_get_and_post(app):
    with app.test_client() as client:
        get_resp = client.get("/login")
        assert get_resp.status_code == 200

        post_resp = client.post("/login", data={})
        assert post_resp.status_code != 405
        assert post_resp.status_code == 200


@pytest.fixture
def authed_client(app):
    with app.app_context():
        db.create_all()
        company = Company(name="Registration Regression Co", is_active=True)
        db.session.add(company)
        db.session.flush()
        user = User(
            username="reg_user",
            email="reg_user@example.com",
            password_hash=generate_password_hash("pw"),
            default_company_id=company.id,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(
            UserCompanyAccess(
                user_id=user.id,
                company_id=company.id,
                role="admin",
                is_default=True,
                can_access_mobile_inbox=True,
            )
        )
        db.session.commit()
        company_id, user_id = company.id, user.id

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True

    yield client, company_id, user_id

    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_authenticated_app_inbox_no_longer_returns_404(authed_client):
    client, _company_id, _user_id = authed_client
    resp = client.get("/app/inbox")
    assert resp.status_code != 404
    assert resp.status_code == 200


def test_inbound_push_dispatch_does_not_raise_nameerror(app, authed_client, monkeypatch):
    client, company_id, _user_id = authed_client

    import inbox_pwa

    sent = {}
    monkeypatch.setattr(
        inbox_pwa,
        "send_pwa_push_notification",
        lambda cid, **kw: sent.setdefault("kw", kw) or {"sent": 0, "errors": []},
    )

    with app.app_context():
        line = TwilioPhoneNumber(
            company_id=company_id,
            phone_number="+15550001000",
            friendly_name="Line",
            sms_enabled=True,
            voice_enabled=True,
            is_active=True,
        )
        db.session.add(line)
        db.session.flush()
        conv = TwilioConversation(
            company_id=company_id,
            phone_number_id=line.id,
            from_number="+14155551212",
            to_number="+15550001000",
            contact_name="Jane Doe",
        )
        db.session.add(conv)
        db.session.commit()

        # Historically this raised NameError, because importing inbox_pwa
        # itself failed at the `@inbox_pwa.route("/sw.js")` decorator line.
        inbox_pwa._fire_push_notification(company_id, conv, "hello")

    assert "kw" in sent


def test_broken_inbox_pwa_import_is_surfaced_as_a_warning_not_silent(monkeypatch, caplog):
    """create_app() intentionally degrades gracefully if inbox_pwa fails to
    import (it's one optional blueprint among many), but that failure must
    be visible in logs rather than vanishing without a trace, which is what
    made the original regression hard to notice.
    """
    broken_module = types.ModuleType("inbox_pwa")  # no inbox_pwa_bp attribute
    monkeypatch.setitem(sys.modules, "inbox_pwa", broken_module)

    with caplog.at_level(logging.WARNING):
        app = create_app()

    assert "inbox_pwa" not in app.blueprints
    assert any(
        "Failed to register inbox_pwa blueprint" in record.getMessage()
        for record in caplog.records
    )
