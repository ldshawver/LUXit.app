import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import User

@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SERVER_NAME"] = "localhost"

    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

def test_login_redirects_to_dashboard(client):
    user = User(
        username="lux",
        email="lux@example.com",
        password_hash=generate_password_hash("supersecret"),
        is_admin=True,
    )
    db.session.add(user)
    db.session.commit()

    response = client.post(
        "/auth/login",
        data={"username": "lux", "password": "supersecret"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


def test_login_with_email_redirects_to_dashboard(client):
    user = User(
        username="lux-admin",
        email="admin@luxit.app",
        password_hash=generate_password_hash("supersecret"),
        is_admin=True,
    )
    db.session.add(user)
    db.session.commit()

    response = client.post(
        "/auth/login",
        data={"username": "admin@luxit.app", "password": "supersecret"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


def test_next_param_redirects_back_to_login(client):
    response = client.get("/?next=https://194.195.92.52/", follow_redirects=False)

    assert response.status_code == 200
    assert "LUX IT" in response.get_data(as_text=True)
