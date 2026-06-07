"""
Idempotent backfill script — imports provider env-var credentials into the
ProviderCredential table.

Usage (from project root):
    python scripts/backfill_provider_credentials.py

Safe to run multiple times.  Existing rows are skipped; new ones are inserted.
Only masked values appear in the log output — never raw secrets.

The script requires an app context so it can use the SQLAlchemy models and the
SecretVault cipher.  It is designed for use on Replit and VPS deployments.
"""
import logging
import os
import sys

# Make sure the project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backfill] %(levelname)s %(message)s",
)
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


def run_backfill(app):
    from models import ProviderCredential
    from services.secret_vault import vault
    from extensions import db
    from datetime import datetime, timezone

    imported = 0
    skipped = 0
    missing = 0

    with app.app_context():
        for provider, scope, env_key, field in CREDENTIALS:
            raw = os.environ.get(env_key)
            if not raw:
                logger.info("[MISSING ] %-14s %-10s %-35s — env var not set, skipping",
                            provider, scope, env_key)
                missing += 1
                continue

            # Check for existing row
            existing = ProviderCredential.query.filter_by(
                provider_slug=provider,
                scope=scope,
                key=env_key,
            ).first()

            if existing:
                logger.info("[SKIPPED ] %-14s %-10s %-35s — already in DB (source=%s)",
                            provider, scope, env_key, existing.source)
                skipped += 1
                continue

            # Encrypt and insert
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


if __name__ == "__main__":
    from app import create_app
    application = create_app()
    run_backfill(application)
