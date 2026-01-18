import pytest
from werkzeug.security import generate_password_hash

from lux import create_app
from lux.extensions import db
from lux.models.user import User

@pytest.fixture
def client(monkeypatch):
    import scheduler

    monkeypatch.setattr(scheduler, "init_scheduler", lambda app: None)

    app = create_app("testing")
    app.config["SECRET_KEY"] = "test-secret"

    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()

def test_login_ignores_next_param_and_redirects_to_dashboard(client):
    user = User(
        username="lux",
        email="lux@example.com",
        password_hash=generate_password_hash("supersecret"),
        is_admin=True,
    )
    db.session.add(user)
    db.session.commit()

    response = client.post(
        "/auth/login?next=https://194.195.92.52/",
        data={"username": "lux", "password": "supersecret"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
