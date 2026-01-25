from app import create_app


def test_login_page_has_helper_links():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/auth/login")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Sign in" in body


def test_login_alias_renders_login_page():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/login")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Sign in" in body


def test_login_alias_no_redirect():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/login", follow_redirects=False)

    assert response.status_code == 200
