"""Regression coverage for the machine-webhook authentication bypass.

Original defect (P1A, ``routes.py::zapier_contact_webhook``):

    auth = request.authorization
    if auth:
        if auth.username != '<user>' or auth.password != '<pass>':
            return 401
    # ...falls through and writes a contact when NO Authorization header is sent

i.e. authentication only ran *if the caller chose to send credentials*. A
request with no ``Authorization`` header skipped the check entirely and
performed an unauthenticated cross-tenant contact write. The credential was
also a hard-coded plaintext constant.

The fix (``_require_shared_secret_auth``) makes authentication mandatory and
fail-closed, reads the secret from the environment, and compares in constant
time. These tests pin every rejected path to **zero mutation** and cover the
two sibling webhooks hardened at the same time.
"""

import os
from unittest.mock import patch

import pytest

from app import app, db
from models import Company, Contact


TOKEN = "dev-synthetic-webhook-secret-0001"

# (route, env var, minimal valid JSON body)
WEBHOOKS = [
    ("/api/webhook/zapier-contact", "ZAPIER_WEBHOOK_TOKEN",
     {"email": "who@example.com", "name": "Syn Thetic", "source": "Zapier"}),
    ("/admin/forminator-webhook", "FORMINATOR_WEBHOOK_TOKEN",
     {"form_id": 3482, "email": "who@example.com", "first_name": "Syn"}),
    ("/admin/wordpress-webhook", "WORDPRESS_WEBHOOK_TOKEN",
     {"email": "who@example.com", "username": "syn", "role": "subscriber"}),
]


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        active = Company(name="Synthetic Active Co", is_active=True)
        inactive = Company(name="Synthetic Inactive Co", is_active=False)
        db.session.add_all([active, inactive])
        db.session.commit()
        app.config["_ACTIVE_CID"] = active.id
        app.config["_INACTIVE_CID"] = inactive.id
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _contact_count():
    return db.session.query(Contact).count()


def _url(route):
    # Route the write at the deliberately-active tenant unless a test overrides.
    return f"{route}?company_id={app.config['_ACTIVE_CID']}"


@pytest.fixture(autouse=True)
def _env():
    with patch.dict(os.environ, {
        "ZAPIER_WEBHOOK_TOKEN": TOKEN,
        "FORMINATOR_WEBHOOK_TOKEN": TOKEN,
        "WORDPRESS_WEBHOOK_TOKEN": TOKEN,
        "PUBLIC_NEWSLETTER_COMPANY_ID": "",
    }, clear=False):
        yield


# --------------------------------------------------------------------------
# Rejected paths — each must reject BEFORE any mutation.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_no_authorization_header_is_rejected_with_zero_mutation(client, route, env_var, body):
    before = _contact_count()
    resp = client.post(_url(route), json=body)
    assert resp.status_code == 401
    assert _contact_count() == before


@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_malformed_authorization_header_is_rejected_with_zero_mutation(client, route, env_var, body):
    before = _contact_count()
    resp = client.post(_url(route), json=body, headers={"Authorization": "potato"})
    assert resp.status_code == 401
    assert _contact_count() == before


@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_invalid_bearer_token_is_rejected_with_zero_mutation(client, route, env_var, body):
    before = _contact_count()
    resp = client.post(_url(route), json=body, headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
    assert _contact_count() == before


@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_invalid_basic_password_is_rejected_with_zero_mutation(client, route, env_var, body):
    before = _contact_count()
    resp = client.post(_url(route), json=body, auth=("anyone", "wrong-password"))
    assert resp.status_code == 401
    assert _contact_count() == before


@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_server_secret_unset_fails_closed(client, route, env_var, body):
    """A missing server-side secret must reject (503), never allow."""
    before = _contact_count()
    with patch.dict(os.environ, {env_var: ""}, clear=False):
        resp = client.post(_url(route), json=body,
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    assert _contact_count() == before


@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_valid_token_but_untrusted_tenant_is_rejected_with_zero_mutation(client, route, env_var, body):
    """Valid credential, but the target company is not an active tenant.

    A shared-secret webhook has no user principal; the tenant guard is
    ``_trusted_public_company_id``. A caller who authenticates but points the
    write at an inactive / unknown company must be rejected, not silently
    retargeted onto a fallback tenant.
    """
    before = _contact_count()
    for cid in (app.config["_INACTIVE_CID"], 999999):
        resp = client.post(f"{route}?company_id={cid}", json=body,
                           headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 400, (route, cid, resp.data)
        assert _contact_count() == before


# --------------------------------------------------------------------------
# Accepted paths — valid credential against an active tenant writes exactly one.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_valid_bearer_token_is_accepted_and_writes_into_the_named_tenant(client, route, env_var, body):
    before = _contact_count()
    resp = client.post(_url(route), json=body,
                       headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.data
    assert _contact_count() == before + 1
    c = db.session.query(Contact).order_by(Contact.id.desc()).first()
    assert c.company_id == app.config["_ACTIVE_CID"]


@pytest.mark.parametrize("route,env_var,body", WEBHOOKS)
def test_valid_basic_password_is_accepted(client, route, env_var, body):
    before = _contact_count()
    resp = client.post(_url(route), json=body, auth=("zapier", TOKEN))
    assert resp.status_code == 200, resp.data
    assert _contact_count() == before + 1


def test_no_hardcoded_credential_remains_in_source():
    """The burned plaintext constant must not survive the fix."""
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "routes.py")
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "Wow548302!" not in text
    assert "auth.username != 'luke'" not in text
