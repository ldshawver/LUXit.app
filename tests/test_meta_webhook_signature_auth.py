"""Regression coverage for the Meta webhook signature-verification bypass.

Original defect (same class as ``zapier_contact_webhook``): the
``X-Hub-Signature-256`` check in ``legal.py::meta_webhook`` only ran

    if app_secret and sig_header:
        ...verify...

so a POST with **no** ``X-Hub-Signature-256`` header skipped verification
entirely and fell through to ``200 {"status": "ok"}``. The GET verification
also fell back to a guessable in-source constant when no verify token was
configured.

The fix makes both mandatory and fail-closed:
- POST: missing ``META_APP_SECRET`` -> 503; missing/invalid signature -> 403,
  before the payload is parsed.
- GET: missing verify token -> 503; token compared in constant time.
"""

import hashlib
import hmac
import os
from unittest.mock import patch

import pytest

from app import app


SECRET = "dev-synthetic-meta-app-secret-0001"
VERIFY = "dev-synthetic-meta-verify-token-0001"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()


# -- POST: signature verification is mandatory and fail-closed ----------------

def test_post_without_signature_header_is_rejected(client):
    with patch.dict(os.environ, {"META_APP_SECRET": SECRET}, clear=False):
        resp = client.post("/webhooks/meta", json={"object": "page"})
    assert resp.status_code == 403


def test_post_with_invalid_signature_is_rejected(client):
    with patch.dict(os.environ, {"META_APP_SECRET": SECRET}, clear=False):
        resp = client.post(
            "/webhooks/meta",
            data=b'{"object": "page"}',
            headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
        )
    assert resp.status_code == 403


def test_post_fails_closed_when_app_secret_unset(client):
    with patch.dict(os.environ, {"META_APP_SECRET": ""}, clear=False):
        body = b'{"object": "page"}'
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={"X-Hub-Signature-256": _sig(body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 503


def test_post_with_valid_signature_is_accepted(client):
    with patch.dict(os.environ, {"META_APP_SECRET": SECRET}, clear=False):
        body = b'{"object": "page"}'
        resp = client.post(
            "/webhooks/meta",
            data=body,
            headers={"X-Hub-Signature-256": _sig(body), "Content-Type": "application/json"},
        )
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


# -- GET: no guessable in-source fallback token ------------------------------

def test_get_fails_closed_when_verify_token_unset(client):
    with patch.dict(os.environ, {"META_WEBHOOK_VERIFY_TOKEN": "", "META_VERIFY_TOKEN": ""}, clear=False):
        resp = client.get(
            "/webhooks/meta",
            query_string={"hub.mode": "subscribe", "hub.verify_token": "luxit_meta_webhook_token", "hub.challenge": "x"},
        )
    assert resp.status_code == 503


def test_get_with_correct_verify_token_returns_challenge(client):
    with patch.dict(os.environ, {"META_WEBHOOK_VERIFY_TOKEN": VERIFY}, clear=False):
        resp = client.get(
            "/webhooks/meta",
            query_string={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "challenge-123"},
        )
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "challenge-123"


def test_get_with_wrong_verify_token_is_rejected(client):
    with patch.dict(os.environ, {"META_WEBHOOK_VERIFY_TOKEN": VERIFY}, clear=False):
        resp = client.get(
            "/webhooks/meta",
            query_string={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "x"},
        )
    assert resp.status_code == 403


def test_no_guessable_meta_token_constant_in_source():
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "legal.py")
    with open(src, encoding="utf-8") as fh:
        assert "luxit_meta_webhook_token" not in fh.read()
