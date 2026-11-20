"""Immersive experience layer — progressive-enhancement guarantees.

The immersive scenes are an enhancement. These tests assert the page is a
complete, usable marketing site with the immersive JS absent (which is exactly
the state of the test client — it runs no JavaScript).
"""
from app import create_app


def _client():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    return app.test_client()


def test_homepage_is_complete_without_js():
    body = _client().get("/").get_data(as_text=True)
    # semantic hero + positioning intact
    assert "Turn customer conversations into growth" in body
    assert "One customer. One history." in body
    assert "Reach the right customers, not every customer." in body
    assert "The interface can suggest. The system still decides." in body
    # CTA + navigation still work with no JS
    assert body.count("/book-demo") >= 3
    assert "LUX Connect" in body
    # no dead trial CTA reintroduced
    assert "Start Free Trial" not in body
    assert 'href="/auth/login"' not in _hero(body)


def test_scene_continuity_and_quality_markup():
    body = _client().get("/").get_data(as_text=True)
    # storefront product silhouettes
    assert 'data-shape="duffel"' in body and body.count("imx-p-fig") >= 6
    # customer scene ties back to the store, and names the person
    assert 'data-tie="store"' in body and "Weekender Duffel" in body
    assert "Alex Rivera" in body
    # segmentation names its target + summary; security layers have inspectable detail
    assert "imx-seg-summary" in body
    assert body.count("imx-l-detail") == 6
    assert "AI assists across these layers. It does not bypass them." in body


def test_immersive_assets_referenced_on_home_only():
    home = _client().get("/").get_data(as_text=True)
    assert "marketing/immersive/experience.css" in home
    assert "marketing/immersive/experience.js" in home
    assert "gsap/3.12.5" in home
    for other in ("/pricing", "/about", "/products/lux-connect"):
        page = _client().get(other).get_data(as_text=True)
        assert "marketing/immersive/experience.js" not in page, other


def test_immersive_static_files_serve():
    c = _client()
    for f in ("experience.css", "experience.js", "zappy.js", "segments.js"):
        r = c.get("/static/marketing/immersive/" + f)
        assert r.status_code == 200, f


def test_reduced_motion_and_optout_paths_exist():
    css = _client().get("/static/marketing/immersive/experience.css").get_data(as_text=True)
    assert "prefers-reduced-motion: reduce" in css
    js = _client().get("/static/marketing/immersive/experience.js").get_data(as_text=True)
    assert 'immersive") === "0"' in js or "immersive') === '0'" in js
    assert "prefers-reduced-motion" in js
    assert "saveData" in js
    # opt-out still renders a full page
    assert _client().get("/?immersive=0").status_code == 200


def test_lux_connect_page_still_ok():
    r = _client().get("/products/lux-connect")
    assert r.status_code == 200
    assert "LUX Connect" in r.get_data(as_text=True)


def _hero(body):
    i = body.find('data-scene="storefront"')
    j = body.find('data-scene="customer"')
    return body[i:j] if i >= 0 and j > i else body
