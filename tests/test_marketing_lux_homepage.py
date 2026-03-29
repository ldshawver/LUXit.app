import os

import pytest


@pytest.fixture(scope="module")
def client():
    os.environ["SESSION_SECRET"] = "test-secret"

    from lux import create_app

    app = create_app(testing=True)
    app.config.update(SERVER_NAME="localhost")

    with app.test_client() as test_client:
        yield test_client


def test_lux_marketing_homepage_renders(client):
    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "LUXit" in body


def test_lux_login_alias_redirects(client):
    response = client.get("/login")

    assert response.status_code == 200
