import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import User


@pytest.fixture
def client(monkeypatch):
    import scheduler

    monkeypatch.setattr(scheduler, "init_scheduler", lambda app: None)

    app = create_app()
    app.config.update(TESTING=True, SECRET_KEY="test-secret", SERVER_NAME="localhost")

    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _login(client, user_id):
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def test_whoami_requires_admin(client):
    user = User(
        username="basic",
        email="basic@example.com",
        password_hash=generate_password_hash("secretpass"),
        is_admin=False,
    )
    db.session.add(user)
    db.session.commit()

    _login(client, user.id)

    response = client.get("/__whoami")

    assert response.status_code == 403


def test_whoami_returns_admin_payload(client):
    user = User(
        username="admin",
        email="admin@example.com",
        password_hash=generate_password_hash("secretpass"),
        is_admin=True,
    )
    db.session.add(user)
    db.session.commit()

    _login(client, user.id)

    response = client.get("/__whoami")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["app"] == "luxit"
    assert payload["user"]["id"] == user.id
