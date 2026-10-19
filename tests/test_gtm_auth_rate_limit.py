"""Brute-force brake on the unauthenticated auth endpoints."""
import pytest

from app import create_app
from extensions import db, limiter


@pytest.fixture
def limited_app(monkeypatch):
    monkeypatch.setenv("LUXIT_RATELIMIT_IN_TESTS", "1")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret")
    with app.app_context():
        db.create_all()
        limiter.reset()
        yield app
        limiter.reset()
        db.session.remove()
        db.drop_all()


def test_login_attempts_are_throttled_per_client(limited_app):
    client = limited_app.test_client()
    codes = [client.post("/auth/login", data={"username": "nobody", "password": "wrong"},
                         headers={"X-Real-IP": "203.0.113.7"}).status_code for _ in range(11)]
    assert 429 not in codes[:10]
    assert codes[10] == 429
    # A different client is unaffected.
    other = client.post("/auth/login", data={"username": "nobody", "password": "wrong"},
                        headers={"X-Real-IP": "203.0.113.8"})
    assert other.status_code != 429


def test_password_reset_requests_are_throttled(limited_app):
    client = limited_app.test_client()
    codes = [client.post("/auth/forgot-password", data={"identifier": "x"},
                         headers={"X-Real-IP": "203.0.113.9"}).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5
    assert codes[5] == 429


def test_login_page_get_is_not_throttled(limited_app):
    client = limited_app.test_client()
    assert all(client.get("/auth/login", headers={"X-Real-IP": "203.0.113.10"}).status_code == 200 for _ in range(15))
