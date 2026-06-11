"""
Incremental, idempotent backfill — syncs provider env-var credentials into the
ProviderCredential table.

Usage (from project root):
    python scripts/backfill_provider_credentials.py

Safe to run multiple times — existing DB rows are skipped, only env vars that
have no matching DB row are inserted.  This means it works correctly on both:
  • First deploy  (all rows missing → full import)
  • Subsequent runs / new env vars added later (only the new ones are inserted)

Only masked values appear in the log output — never raw secrets.

The script requires an app context so it can use the SQLAlchemy models and the
SecretVault cipher.  It is designed for use on Replit and VPS deployments.
"""
import logging
import os
import sys

# When run as a standalone script, ensure the project root is importable.
# When imported as a module from inside the Flask app, this is a no-op because
# the project root is already on sys.path — insert only when missing.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Use a named logger so the app's logging config controls formatting.
# Do NOT call logging.basicConfig here — that would override the app's handler
# when this module is imported at startup. basicConfig is only applied when
# running as __main__ (standalone CLI mode).
logger = logging.getLogger("backfill")


# ---------------------------------------------------------------------------
# Credential map: (provider_slug, scope, key/env_var_name, field_label)
# scope: 'platform' = no company_id; 'company' would need a company_id
# ---------------------------------------------------------------------------
CREDENTIALS = [
    # AI
    ("openai",      "platform", "OPENAI_API_KEY",             "api_key"),
    # Twilio
    ("twilio",      "platform", "TWILIO_ACCOUNT_SID",         "account_sid"),
    ("twilio",      "platform", "TWILIO_AUTH_TOKEN",          "auth_token"),
    # TWILIO_PHONE_NUMBER is NOT a platform credential — per-company phone
    # numbers live in TwilioAccount.from_phone (DB). Do not backfill here.
    # Stripe
    ("stripe",      "platform", "STRIPE_SECRET_KEY",          "secret_key"),
    ("stripe",      "platform", "STRIPE_PUBLISHABLE_KEY",     "publishable_key"),
    ("stripe",      "platform", "STRIPE_WEBHOOK_SECRET",      "webhook_secret"),
    # Email
    ("smtp",        "platform", "SMTP_HOST",                  "host"),
    ("smtp",        "platform", "SMTP_PORT",                  "port"),
    ("smtp",        "platform", "SMTP_USER",                  "username"),
    ("smtp",        "platform", "SMTP_PASS",                  "password"),
    ("smtp",        "platform", "SMTP_FROM",                  "from_email"),
    ("smtp",        "platform", "SMTP_USERNAME",              "username_alt"),
    ("smtp",        "platform", "SMTP_PASSWORD",              "password_alt"),
    ("mailgun",     "platform", "MAILGUN_API_KEY",            "api_key"),
    ("mailgun",     "platform", "MAILGUN_DOMAIN",             "domain"),
    ("mailgun",     "platform", "MAILGUN_FROM",               "from_email"),
    # Microsoft 365
    ("ms365",       "platform", "MS_CLIENT_ID",               "client_id"),
    ("ms365",       "platform", "MS_CLIENT_SECRET",           "client_secret"),
    ("ms365",       "platform", "MS_TENANT_ID",               "tenant_id"),
    ("ms365",       "platform", "MS_FROM_EMAIL",              "from_email"),
    # Social
    ("facebook",    "platform", "FACEBOOK_APP_ID",            "app_id"),
    ("facebook",    "platform", "FACEBOOK_APP_SECRET",        "app_secret"),
    ("facebook",    "platform", "FACEBOOK_ACCESS_TOKEN",      "access_token"),
    ("facebook",    "platform", "FACEBOOK_PAGE_ID",           "page_id"),
    ("tiktok",      "platform", "TIKTOK_CLIENT_KEY",          "client_key"),
    ("tiktok",      "platform", "TIKTOK_CLIENT_SECRET",       "client_secret"),
    ("twitter",     "platform", "TWITTER_API_KEY",            "api_key"),
    ("twitter",     "platform", "TWITTER_API_SECRET",         "api_secret"),
    ("twitter",     "platform", "TWITTER_BEARER_TOKEN",       "bearer_token"),
    ("twitter",     "platform", "TWITTER_CLIENT_ID",          "client_id"),
    ("twitter",     "platform", "TWITTER_CLIENT_SECRET",      "client_secret"),
    ("linkedin",    "platform", "LINKEDIN_CLIENT_ID",         "client_id"),
    ("linkedin",    "platform", "LINKEDIN_CLIENT_SECRET",     "client_secret"),
    ("linkedin",    "platform", "LINKEDIN_ACCESS_TOKEN",      "access_token"),
    ("youtube",     "platform", "YOUTUBE_API_KEY",            "api_key"),
    ("youtube",     "platform", "YOUTUBE_CHANNEL_ID",         "channel_id"),
    ("reddit",      "platform", "REDDIT_CLIENT_ID",           "client_id"),
    ("reddit",      "platform", "REDDIT_CLIENT_SECRET",       "client_secret"),
    # SEO / marketing
    ("google_ads",  "platform", "GOOGLE_ADS_CLIENT_ID",       "client_id"),
    ("google_ads",  "platform", "GOOGLE_ADS_CLIENT_SECRET",   "client_secret"),
    ("google_ads",  "platform", "GOOGLE_ADS_DEVELOPER_TOKEN", "developer_token"),
    # Analytics
    ("posthog",     "platform", "POSTHOG_API_KEY",            "api_key"),
    ("posthog",     "platform", "POSTHOG_HOST",               "host"),
    # Images
    ("unsplash",    "platform", "UNSPLASH_ACCESS_KEY",        "access_key"),
    ("pexels",      "platform", "PEXELS_API_KEY",             "api_key"),
    # GitHub
    ("github",      "platform", "GITHUB_PERSONAL_ACCESS_TOKEN", "token"),
    # Airtable
    ("airtable",    "platform", "AIRTABLE_API_KEY",           "api_key"),
    ("airtable",    "platform", "AIRTABLE_BASE_ID",           "base_id"),
    # RevenueCat
    ("revenuecat",  "platform", "REVENUECAT_WEBHOOK_SECRET",  "webhook_secret"),
    # WooCommerce
    ("woocommerce", "platform", "WOOCOMMERCE_URL",            "url"),
    ("woocommerce", "platform", "WOOCOMMERCE_KEY",            "api_key"),
    ("woocommerce", "platform", "WOOCOMMERCE_SECRET",         "secret"),
]


def _mask(val: str, show: int = 4) -> str:
    if not val or len(val) <= show:
        return "****"
    return f"{val[:2]}***{val[-show:]}"


def _do_backfill():
    """
    Core backfill logic — must be called inside an active app context.
    Returns (imported, skipped, missing) counts.
    """
    from models import ProviderCredential
    from services.secret_vault import vault
    from extensions import db

    imported = 0
    skipped = 0
    missing = 0

    for provider, scope, env_key, field in CREDENTIALS:
        raw = os.environ.get(env_key)
        if not raw:
            logger.debug("[MISSING ] %-14s %-10s %-35s — env var not set, skipping",
                         provider, scope, env_key)
            missing += 1
            continue

        existing = ProviderCredential.query.filter_by(
            provider_slug=provider,
            scope=scope,
            key=env_key,
        ).first()

        if existing:
            logger.debug("[SKIPPED ] %-14s %-10s %-35s — already in DB (source=%s)",
                         provider, scope, env_key, existing.source)
            skipped += 1
            continue

        try:
            encrypted = vault.encrypt(raw)
            row = ProviderCredential(
                provider_slug=provider,
                scope=scope,
                company_id=None,
                key=env_key,
                encrypted_value=encrypted,
                source="env",
                is_active=True,
                audit_notes=f"Backfilled from env var {env_key}",
            )
            db.session.add(row)
            db.session.commit()
            logger.info("[IMPORTED] %-14s %-10s %-35s — value=%s",
                        provider, scope, env_key, _mask(raw))
            imported += 1
        except Exception as exc:
            db.session.rollback()
            logger.error("[ERROR   ] %-14s %-10s %-35s — %s",
                         provider, scope, env_key, exc)

    logger.info(
        "Backfill complete: %d imported, %d skipped (already exist), %d missing from env",
        imported, skipped, missing,
    )
    return imported, skipped, missing


def run_backfill(app=None):
    """
    Public entry point.

    When called from inside an active Flask app context (e.g. from app.py
    startup), pass app=None or any app — the existing context is reused.

    When called as a standalone CLI script, pass the Flask app instance so
    a new context is pushed.
    """
    from flask import has_app_context
    if has_app_context():
        return _do_backfill()
    if app is None:
        raise RuntimeError("run_backfill() requires an app when called outside an app context")
    with app.app_context():
        return _do_backfill()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [backfill] %(levelname)s %(message)s",
    )
    from app import create_app
    application = create_app()
    run_backfill(application)
