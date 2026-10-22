"""Regression coverage for RevenueCat webhook authentication.

Original defect: ``integrations_bp.py::revenuecat_webhook`` had no
authentication at all ("no auth (validate in prod via shared secret)" was a
comment, not an implementation) and was not ``@csrf.exempt``, so it was only
*accidentally* fail-closed (CSRFProtect 400s any POST with no valid CSRF
token — which a real RevenueCat server can never supply). The fix applies the
same mandatory, fail-closed ``_require_shared_secret_auth`` helper used for
the Zapier/Forminator/WordPress webhooks (see
``tests/test_webhook_shared_secret_auth.py``), keyed on
``REVENUECAT_WEBHOOK_SECRET``, and marks the route explicitly ``@csrf.exempt``
so a real provider webhook (which cannot carry a CSRF token) is authenticated
on its own merits rather than by an incidental CSRF rejection.
"""

import os
from unittest.mock import patch

import pytest

from app import app, db
from models import IntegrationEvent


TOKEN = "dev-synthetic-revenuecat-secret-0001"
URL = "/api/webhooks/revenuecat"
BODY = {"event": {"type": "INITIAL_PURCHASE", "app_user_id": "synthetic-user", "product_id": "luxit_access"}}


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _event_count():
    return db.session.query(IntegrationEvent).filter_by(provider="revenuecat").count()


@pytest.fixture(autouse=True)
def _env():
    with patch.dict(os.environ, {"REVENUECAT_WEBHOOK_SECRET": TOKEN}, clear=False):
        yield


def test_no_authorization_header_is_rejected_with_zero_mutation(client):
    before = _event_count()
    resp = client.post(URL, json=BODY)
    assert resp.status_code == 401
    assert _event_count() == before


def test_malformed_authorization_header_is_rejected_with_zero_mutation(client):
    before = _event_count()
    resp = client.post(URL, json=BODY, headers={"Authorization": "potato"})
    assert resp.status_code == 401
    assert _event_count() == before


def test_invalid_bearer_token_is_rejected_with_zero_mutation(client):
    before = _event_count()
    resp = client.post(URL, json=BODY, headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
    assert _event_count() == before


def test_server_secret_unset_fails_closed(client):
    """A missing server-side secret must reject (503), never allow — this is
    the production-default state today (RevenueCat is not configured)."""
    before = _event_count()
    with patch.dict(os.environ, {"REVENUECAT_WEBHOOK_SECRET": ""}, clear=False):
        resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    assert _event_count() == before


def test_valid_bearer_token_is_accepted_and_writes_one_audit_event(client):
    before = _event_count()
    resp = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.data
    assert _event_count() == before + 1


def test_valid_basic_password_is_accepted(client):
    before = _event_count()
    resp = client.post(URL, json=BODY, auth=("revenuecat", TOKEN))
    assert resp.status_code == 200, resp.data
    assert _event_count() == before + 1


def test_no_credential_leaks_in_response_body(client):
    resp = client.post(URL, json=BODY)
    assert TOKEN not in resp.get_data(as_text=True)
