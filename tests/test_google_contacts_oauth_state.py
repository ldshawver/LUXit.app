"""Google Contacts OAuth: the callback only accepts the state this session started."""
import urllib.parse
import uuid

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess


class _FreshUserClient(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    calls = []
    monkeypatch.setattr("services.google_contacts.exchange_code", lambda uid, code: calls.append((uid, code)) or {})
    monkeypatch.setattr("services.google_contacts.sync_contacts",
                        lambda uid, cid, **kw: {"synced": 0, "matched": 0, "error": None})
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        co = Company(name=f"Google {tag}")
        db.session.add(co)
        db.session.flush()
        u = User(username=f"g-{tag}", email=f"g-{tag}@example.com", password_hash="x", default_company_id=co.id)
        db.session.add(u)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role="editor", is_default=True))
        db.session.commit()
        c = app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True
        yield c, u.id, calls
        db.session.remove()


def test_state_round_trip(env):
    c, uid, calls = env
    r = c.get("/twilio/google-contacts/connect")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["Location"]).query)["state"][0]
    assert len(state) >= 32 and state != str(uid)
    c.get(f"/twilio/google-contacts/callback?code=good&state={state}")
    assert calls == [(uid, "good")]
    # Single use.
    c.get(f"/twilio/google-contacts/callback?code=again&state={state}")
    assert calls == [(uid, "good")]


@pytest.mark.parametrize("query", ["code=evil", "code=evil&state=", "code=evil&state=guess"])
def test_callback_without_this_sessions_state_is_refused(env, query):
    c, uid, calls = env
    c.get("/twilio/google-contacts/connect")
    c.get(f"/twilio/google-contacts/callback?{query}")
    assert calls == []
    # The old predictable state (the user id) is refused too.
    c.get(f"/twilio/google-contacts/callback?code=evil&state={uid}")
    assert calls == []
