from app import create_app


def test_marketing_homepage_renders():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # Positioning: capability-stack, not "revenue operations / 11 AI agents"
    assert "LUXit" in body
    assert "Turn customer conversations" in body
    assert "Consent-aware SMS" in body
    assert "LUX Connect" in body
    # Primary CTA is Book a Demo, and the dead "Start Free Trial" CTA is gone
    assert "/book-demo" in body
    assert "Start Free Trial" not in body


def test_lux_connect_product_page_renders():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")

    with app.test_client() as client:
        response = client.get("/products/lux-connect")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "LUX Connect" in body
    assert "browser" in body.lower()
