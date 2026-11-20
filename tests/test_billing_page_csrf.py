"""The billing page's buttons work with CSRF enforced (as in production).

Regression: ``templates/billing/billing.html`` posted to the checkout and
portal endpoints without ``X-CSRFToken``. With CSRF on, Flask-WTF rejected
the call and the handler redirected; ``fetch`` followed the redirect, got an
HTML page, and the button showed "No checkout URL returned." The tests that
covered billing all ran with CSRF disabled.
"""
import re
import uuid

import pytest

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=True)
    with app.app_context():
        tag = uuid.uuid4().hex[:10]
        company = Company(name=f"Csrf {tag}")
        db.session.add(company)
        db.session.flush()
        user = User(username=f"csrf-{tag}", email=f"csrf-{tag}@example.com", password_hash="x",
                    default_company_id=company.id)
        db.session.add(user)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="owner", is_default=True))
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(user.id)
            s["_fresh"] = True
        yield client, company.id


def _page_token(client):
    from flask import g
    g.pop("_login_user", None)
    page = client.get("/app/billing")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert '"X-CSRFToken": csrfToken()' in html          # the page's postJSON sends it
    return re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)


@pytest.mark.parametrize("url", ["/api/stripe/create-portal-session", "/api/stripe/create-checkout-session"])
def test_billing_buttons_pass_csrf_with_the_page_token(env, url):
    client, company_id = env
    token = _page_token(client)
    r = client.post(url, json={"company_id": company_id, "lookup_key": "luxit_starter_monthly"},
                    headers={"X-CSRFToken": token})
    assert r.status_code != 302
    body = r.get_json()
    assert body is not None and body.get("code") != "csrf_failed"   # reached the endpoint itself


def test_api_csrf_failure_is_json_not_a_redirect(env):
    client, company_id = env
    r = client.post("/api/stripe/create-portal-session", json={"company_id": company_id})
    assert r.status_code == 400
    assert r.get_json()["code"] == "csrf_failed"


def test_base_layout_adds_csrf_to_same_origin_fetch_and_forms(env):
    """Pages on base.html get the token for fetch() and POST forms that forgot it
    (segments, automations, contacts, deals ...). Behavior is proven in a real
    browser by scripts/acceptance/isolated_browser_rig.sh."""
    client, _ = env
    from flask import g
    g.pop("_login_user", None)
    html = client.get("/segments").get_data(as_text=True)
    assert "__luxCsrf" in html and 'headers.set("X-CSRFToken", token())' in html
    assert 'input.name = "csrf_token"' in html
    title = html[html.index("<title>"):html.index("</title>")]
    assert "<script" not in title and "document." not in title
