from app import create_app


def test_healthz_endpoint():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_version_endpoint_defaults():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/__version")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["app"] == "luxit"
    assert "version" in payload
    assert "git_sha" in payload


def test_healthz_skips_canonical_redirect():
    app = create_app()
    app.config.update(TESTING=False, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/healthz", headers={"Host": "evil.example"})

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
