"""
Central credential resolver for all provider API keys and secrets.

Usage:
    from services.provider_config import get_provider_config, mask_secret

    api_key = get_provider_config('openai', 'platform')
    sid     = get_provider_config('twilio', 'platform', key='TWILIO_ACCOUNT_SID')

Resolution order (per call):
    1. ProviderCredential table (encrypted DB) — scope + provider + key match
    2. os.environ.get(key) — env fallback (permanent; logs a WARNING so migration
       progress is visible)

Secrets are NEVER returned to the frontend.  Callers must keep them server-side.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider → default env-key mapping
# ---------------------------------------------------------------------------
# Maps (provider_slug, field_name) → env var name so callers don't need to
# know the exact env var names.  The resolver accepts an explicit key= override
# when the default doesn't match.
_DEFAULT_ENV_KEYS: dict[tuple[str, str], str] = {
    ("openai",   "api_key"):       "OPENAI_API_KEY",
    ("twilio",   "account_sid"):   "TWILIO_ACCOUNT_SID",
    ("twilio",   "auth_token"):    "TWILIO_AUTH_TOKEN",
    # phone_number is a routing bootstrap credential — multi-tenant sends use
    # TwilioAccount.from_phone (DB) first; this env path is a last resort
    ("twilio",   "phone_number"):  "TWILIO_PHONE_NUMBER",
    ("stripe",   "secret_key"):    "STRIPE_SECRET_KEY",
    ("stripe",   "publishable_key"): "STRIPE_PUBLISHABLE_KEY",
    ("stripe",   "webhook_secret"): "STRIPE_WEBHOOK_SECRET",
    ("smtp",     "host"):          "SMTP_HOST",
    ("smtp",     "port"):          "SMTP_PORT",
    ("smtp",     "user"):          "SMTP_USER",
    ("smtp",     "pass"):          "SMTP_PASS",
    ("smtp",     "from_email"):    "SMTP_FROM",
    ("mailgun",  "api_key"):       "MAILGUN_API_KEY",
    ("mailgun",  "domain"):        "MAILGUN_DOMAIN",
    ("ms365",    "client_id"):     "MS_CLIENT_ID",
    ("ms365",    "client_secret"): "MS_CLIENT_SECRET",
    ("ms365",    "tenant_id"):     "MS_TENANT_ID",
    ("facebook", "app_id"):        "FACEBOOK_APP_ID",
    ("facebook", "app_secret"):    "FACEBOOK_APP_SECRET",
    ("facebook", "access_token"):  "FACEBOOK_ACCESS_TOKEN",
    ("google_ads", "client_id"):   "GOOGLE_ADS_CLIENT_ID",
    ("google_ads", "client_secret"): "GOOGLE_ADS_CLIENT_SECRET",
    ("google_ads", "developer_token"): "GOOGLE_ADS_DEVELOPER_TOKEN",
    ("posthog",  "api_key"):       "POSTHOG_API_KEY",
    ("posthog",  "host"):          "POSTHOG_HOST",
    ("unsplash", "access_key"):    "UNSPLASH_ACCESS_KEY",
    ("pexels",   "api_key"):       "PEXELS_API_KEY",
    ("github",   "token"):         "GITHUB_PERSONAL_ACCESS_TOKEN",
    ("github",   "github_token"):  "GITHUB_TOKEN",           # legacy alias
    ("airtable", "api_key"):       "AIRTABLE_API_KEY",
    ("airtable", "token"):         "AIRTABLE_TOKEN",         # legacy alias
    ("airtable", "base_id"):       "AIRTABLE_BASE_ID",
    ("revenuecat", "webhook_secret"): "REVENUECAT_WEBHOOK_SECRET",
    ("revenuecat", "api_key"):      "REVENUECAT_API_KEY",
    ("revenuecat", "secret_key"):   "REVENUECAT_SECRET_KEY", # legacy alias
}


def _env_key_for(provider: str, field: str) -> str:
    """Return the canonical env var name for a given provider+field combo."""
    return _DEFAULT_ENV_KEYS.get((provider, field), "")


def mask_secret(val: Optional[str], show_chars: int = 4) -> str:
    """Return a masked version of a secret value safe to log or display."""
    if not val or len(val) <= show_chars:
        return "****"
    return f"{val[:2]}***{val[-show_chars:]}"


def get_provider_config(
    provider: str,
    scope: str,
    field: str = "api_key",
    company_id: Optional[int] = None,
    key: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve a credential for *provider* with the given *scope*.

    Parameters
    ----------
    provider    : slug matching IntegrationServiceRegistry.SERVICES (e.g. 'openai')
    scope       : 'platform' | 'company' | 'user'
    field       : logical field within the provider (default 'api_key')
    company_id  : required when scope == 'company'
    key         : explicit env-var / DB key name override; auto-derived if omitted

    Returns
    -------
    Decrypted string value, or None if not found anywhere.
    """
    env_var = key or _env_key_for(provider, field)

    # --- 1. Try DB (ProviderCredential table) --------------------------------
    try:
        from models import ProviderCredential
        from services.secret_vault import vault

        q = ProviderCredential.query.filter_by(
            provider_slug=provider,
            scope=scope,
            key=env_var or field,
            is_active=True,
        )
        if scope == "company" and company_id is not None:
            q = q.filter_by(company_id=company_id)
        elif scope == "platform":
            q = q.filter(
                (ProviderCredential.company_id == None) |  # noqa: E711
                (ProviderCredential.company_id == company_id)
            )

        row = q.first()
        if row and row.encrypted_value:
            try:
                return vault.decrypt(row.encrypted_value)
            except Exception as dec_err:
                logger.warning(
                    "provider_config: DB decrypt failed for %s/%s/%s: %s",
                    provider, scope, env_var or field, dec_err,
                )
    except Exception as db_err:
        logger.debug(
            "provider_config: DB lookup skipped for %s/%s (table may not exist yet): %s",
            provider, scope, db_err,
        )

    # --- 2. Env fallback -----------------------------------------------------
    if env_var:
        val = os.environ.get(env_var)
        if val:
            logger.warning(
                "provider_config: using env fallback for %s/%s/%s [%s] — "
                "consider importing via backfill script",
                provider, scope, field, mask_secret(val),
            )
            _write_fallback_audit(provider, scope, field, company_id, env_var)
            return val

    return None


def get_provider_config_bool(
    provider: str,
    scope: str,
    field: str = "api_key",
    company_id: Optional[int] = None,
    key: Optional[str] = None,
) -> bool:
    """Convenience: return True if any value resolves, False otherwise."""
    return bool(get_provider_config(provider, scope, field, company_id, key))


def _write_fallback_audit(
    provider: str,
    scope: str,
    field: str,
    company_id: Optional[int],
    env_var: str,
) -> None:
    """Write an ApiHubAuditLog row recording that the env fallback was used."""
    try:
        from models import ApiHubAuditLog
        from extensions import db
        log = ApiHubAuditLog(
            provider_slug=provider,
            action="fallback_used",
            scope=scope,
            company_id=company_id,
            result="env_fallback",
            notes=f"env_var={env_var} field={field}",
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        pass  # audit log failures must never crash the application


def save_provider_credential(
    provider: str,
    scope: str,
    key: str,
    plaintext_value: str,
    company_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    source: str = "manual",
    notes: str = "",
) -> tuple[bool, str]:
    """
    Encrypt and upsert a ProviderCredential row.

    Returns (success: bool, message: str).
    NEVER log or return the plaintext_value.
    """
    if not plaintext_value or not plaintext_value.strip():
        return False, "Value must not be empty"
    if not provider or not scope or not key:
        return False, "provider, scope, and key are required"

    try:
        from models import ProviderCredential, ApiHubAuditLog
        from services.secret_vault import vault
        from extensions import db

        encrypted = vault.encrypt(plaintext_value.strip())

        existing = ProviderCredential.query.filter_by(
            provider_slug=provider,
            scope=scope,
            key=key,
            company_id=company_id if scope != "platform" else None,
        ).first()

        action = "updated" if existing else "created"

        if existing:
            existing.encrypted_value = encrypted
            existing.source          = source
            existing.is_active       = True
            existing.audit_notes     = notes[:500] if notes else ""
            existing.updated_at      = datetime.now(timezone.utc)
        else:
            row = ProviderCredential(
                provider_slug   = provider,
                scope           = scope,
                key             = key,
                company_id      = company_id if scope != "platform" else None,
                encrypted_value = encrypted,
                source          = source,
                is_active       = True,
                audit_notes     = notes[:500] if notes else "",
            )
            db.session.add(row)

        db.session.flush()

        # Audit log — no raw value
        log = ApiHubAuditLog(
            provider_slug  = provider,
            action         = action,
            scope          = scope,
            company_id     = company_id,
            actor_user_id  = actor_user_id,
            result         = "ok",
            notes          = f"key={key} source={source} {notes}"[:500],
        )
        db.session.add(log)
        db.session.commit()

        logger.info("provider_config: %s credential %s/%s/%s", action, provider, scope, key)
        return True, action

    except Exception as exc:
        logger.error("save_provider_credential failed for %s/%s/%s: %s", provider, scope, key, exc)
        try:
            from extensions import db
            db.session.rollback()
        except Exception:
            pass
        return False, str(exc)


def delete_provider_credential(
    provider: str,
    scope: str,
    key: str,
    company_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
) -> tuple[bool, str]:
    """
    Soft-delete (deactivate) a ProviderCredential row.
    Returns (success, message).
    """
    try:
        from models import ProviderCredential, ApiHubAuditLog
        from extensions import db

        row = ProviderCredential.query.filter_by(
            provider_slug=provider,
            scope=scope,
            key=key,
            company_id=company_id if scope != "platform" else None,
            is_active=True,
        ).first()

        if not row:
            return False, "Credential not found or already inactive"

        row.is_active   = False
        row.updated_at  = datetime.now(timezone.utc)

        log = ApiHubAuditLog(
            provider_slug  = provider,
            action         = "deleted",
            scope          = scope,
            company_id     = company_id,
            actor_user_id  = actor_user_id,
            result         = "ok",
            notes          = f"key={key} soft-deleted",
        )
        db.session.add(log)
        db.session.commit()

        logger.info("provider_config: deleted credential %s/%s/%s", provider, scope, key)
        return True, "deleted"

    except Exception as exc:
        logger.error("delete_provider_credential failed for %s/%s/%s: %s", provider, scope, key, exc)
        try:
            from extensions import db
            db.session.rollback()
        except Exception:
            pass
        return False, str(exc)


def write_audit_log(
    provider: str,
    action: str,
    scope: str,
    company_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    result: str = "ok",
    notes: str = "",
) -> None:
    """
    Write an ApiHubAuditLog row from route handlers.

    Never include raw secret values in *notes*.
    """
    try:
        from models import ApiHubAuditLog
        from extensions import db
        log = ApiHubAuditLog(
            provider_slug=provider,
            action=action,
            actor_user_id=actor_user_id,
            scope=scope,
            company_id=company_id,
            result=result,
            notes=notes[:500] if notes else "",
        )
        db.session.add(log)
        db.session.commit()
    except Exception as exc:
        logger.debug("write_audit_log failed (non-fatal): %s", exc)
