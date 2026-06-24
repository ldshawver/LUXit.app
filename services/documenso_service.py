"""Documenso production safety helpers.

Centralizes config validation, webhook signature checking, and persistence SQL for
contract signing requests without logging secrets.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text


@dataclass(frozen=True)
class DocumensoConfig:
    api_key_present: bool
    webhook_secret_present: bool
    base_url: str
    enabled: bool


def load_documenso_config(env: dict[str, str] | None = None) -> DocumensoConfig:
    env = env or os.environ
    api_key = (env.get("DOCUMENSO_API_KEY") or "").strip()
    webhook_secret = (env.get("DOCUMENSO_WEBHOOK_SECRET") or "").strip()
    base_url = (env.get("DOCUMENSO_BASE_URL") or env.get("DOCUMENSO_PUBLIC_URL") or "https://document.luxit.app").strip().rstrip("/")
    return DocumensoConfig(bool(api_key), bool(webhook_secret), base_url, bool(api_key))


def validate_documenso_startup(require_api_key: bool | None = None, env: dict[str, str] | None = None) -> DocumensoConfig:
    cfg = load_documenso_config(env)
    if require_api_key is None:
        require_api_key = (env or os.environ).get("DOCUMENSO_REQUIRED", "").lower() in {"1", "true", "yes"}
    if require_api_key and not cfg.api_key_present:
        raise RuntimeError("DOCUMENSO_API_KEY is required; send-for-signature is disabled until configured")
    return cfg


def send_for_signature_available(env: dict[str, str] | None = None) -> bool:
    return load_documenso_config(env).enabled


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, secret: str | None = None) -> bool:
    secret = secret if secret is not None else os.environ.get("DOCUMENSO_WEBHOOK_SECRET")
    if not secret:
        return True
    if not signature_header:
        return False
    provided = signature_header.split(",")[-1].split("=")[-1].strip()
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def persist_signature_request(db_session: Any, *, contract_id: str, document_id: str, signing_url: str | None, recipient_id: str | None, status: str = "sent", response_payload: dict[str, Any] | None = None) -> None:
    """Persist Documenso IDs idempotently across all contract signing tables."""
    payload = response_payload or {}
    db_session.execute(text("""
        UPDATE contractor_contracts
           SET documenso_document_id = :document_id,
               documenso_signing_url = COALESCE(:signing_url, documenso_signing_url),
               signature_status = :status,
               updated_at = NOW()
         WHERE id = CAST(:contract_id AS uuid)
    """), {"contract_id": contract_id, "document_id": document_id, "signing_url": signing_url, "status": status})
    db_session.execute(text("""
        UPDATE contract_signers
           SET documenso_recipient_id = COALESCE(:recipient_id, documenso_recipient_id),
               signing_url = COALESCE(:signing_url, signing_url),
               status = :status,
               updated_at = NOW()
         WHERE contract_id = CAST(:contract_id AS uuid)
    """), {"contract_id": contract_id, "recipient_id": recipient_id, "signing_url": signing_url, "status": status})
    db_session.execute(text("""
        INSERT INTO documenso_signature_requests
            (contract_id, documenso_document_id, signing_url, recipient_id, status, response_payload, created_at, updated_at)
        VALUES
            (CAST(:contract_id AS uuid), :document_id, :signing_url, :recipient_id, :status, CAST(:payload AS jsonb), NOW(), NOW())
        ON CONFLICT (contract_id, documenso_document_id, COALESCE(recipient_id, ''))
        DO UPDATE SET signing_url = COALESCE(EXCLUDED.signing_url, documenso_signature_requests.signing_url),
                      status = EXCLUDED.status,
                      response_payload = EXCLUDED.response_payload,
                      updated_at = NOW()
    """), {"contract_id": contract_id, "document_id": document_id, "signing_url": signing_url, "recipient_id": recipient_id, "status": status, "payload": __import__('json').dumps(payload)})
