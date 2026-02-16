from app import create_app


def test_marketing_homepage_renders():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "LUX IT" in body
    assert "Feature comparison" in body
