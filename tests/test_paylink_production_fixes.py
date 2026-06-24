import json

from services.api_json_guard import should_warn_api_non_json
from services.appdoctor_sql import insert_appdoctor_run_sql
from services.contract_notifications import activation_recipients_sql
from services.documenso_service import load_documenso_config, send_for_signature_available, verify_webhook_signature


def test_documenso_config_validation_disables_send_when_api_key_missing():
    env = {"DOCUMENSO_WEBHOOK_SECRET": "whsec"}
    cfg = load_documenso_config(env)
    assert not cfg.api_key_present
    assert not send_for_signature_available(env)


def test_documenso_webhook_signature_verification():
    import hmac, hashlib
    body = b'{"event":"document.completed"}'
    sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, f"sha256={sig}", "secret") is True
    assert verify_webhook_signature(body, "sha256=bad", "secret") is False
    assert verify_webhook_signature(body, None, "secret") is False


def test_contract_notification_query_does_not_reference_missing_users_phone_as_only_source():
    sql = activation_recipients_sql()
    assert "u.phone" not in sql
    assert "COALESCE(NULLIF(c.phone" in sql
    assert "cs.phone" in sql


def test_appdoctor_insert_casts_nullable_parameters():
    sql = str(insert_appdoctor_run_sql())
    for cast in ["CAST(:metadata AS jsonb)", "CAST(:error_message AS text)", "CAST(:completed_at AS timestamptz)"]:
        assert cast in sql


def test_api_json_guard_ignores_304_and_pdf_but_warns_html_fallthrough():
    assert should_warn_api_non_json("/api/contracts", 304, "text/html") is False
    assert should_warn_api_non_json("/api/content/export/pdf", 200, "application/pdf") is False
    assert should_warn_api_non_json("/api/missing", 200, "text/html") is True
    assert should_warn_api_non_json("/dashboard", 200, "text/html") is False


def test_documenso_persist_sql_contains_idempotent_conflict_clause():
    import inspect
    from services import documenso_service
    src = inspect.getsource(documenso_service.persist_signature_request)
    assert "ON CONFLICT" in src
    assert "contractor_contracts" in src
    assert "contract_signers" in src
    assert "documenso_signature_requests" in src


def test_schema_drift_preference_columns_are_narrow_compatibility_only():
    sql = open("migrations/20260623_paylink_documenso_production_fix.sql").read()
    assert "ADD COLUMN IF NOT EXISTS push_enabled" in sql
    assert "ADD COLUMN IF NOT EXISTS pwa_after_hours_push_enabled" in sql


def test_paylink_signing_deploy_smoke_checks_index_and_port_guard():
    script = open("scripts/verify_paylink_deploy.sh").read()
    assert "dist/public/index.html" in script
    assert "multiple listeners" in script
    assert "pm2 list" in script
