"""
Auto-migration: adds missing columns to an existing SQLite or PostgreSQL database.
Safe to run multiple times — skips columns that already exist.

Usage (run from /root/lux-email-bot):
  python3 scripts/migrate_db.py
"""
import sys, os

# ── Locate project root ─────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)


# ── Load .env before importing app (so DATABASE_URL is available) ───────────
def _load_dotenv():
    """Parse .env in the project root and populate os.environ for missing keys."""
    env_path = os.path.join(_PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key = _key.strip()
            _val = _val.strip().strip('"').strip("'")
            if _key and _key not in os.environ:
                os.environ[_key] = _val

_load_dotenv()


# ── PostgreSQL-only guard ─────────────────────────────────────────────────────
def _check_not_sqlite():
    """Abort if DATABASE_URL still points to a SQLite file.

    This script is designed for PostgreSQL only. Data migration from SQLite is
    handled by scripts/sqlite_to_postgres.py.
    """
    raw = os.environ.get("DATABASE_URL", "").strip()
    if raw and raw.startswith("sqlite:"):
        print(
            "\n  ✗  DATABASE_URL points to SQLite — this script requires PostgreSQL.\n"
            "  To migrate data from SQLite to PostgreSQL, use:\n"
            "    python3 scripts/sqlite_to_postgres.py --help\n",
            file=sys.stderr,
        )
        sys.exit(1)

_check_not_sqlite()

from app import create_app
from extensions import db
from sqlalchemy import inspect, text

OK  = lambda s: print(f"  \033[32m✓\033[0m  {s}")
SKP = lambda s: print(f"  \033[33m–\033[0m  {s}")
ERR = lambda s: print(f"  \033[31m✗\033[0m  {s}")

# ---------------------------------------------------------------------------
# Column definitions: (table, column, sql_type, default_clause)
# ---------------------------------------------------------------------------
MIGRATIONS = [
    # user table
    ("user", "replit_id",        "VARCHAR(64)",   "DEFAULT NULL"),
    ("user", "default_company_id","INTEGER",       "DEFAULT NULL"),
    ("user", "first_name",       "VARCHAR(64)",   "DEFAULT NULL"),
    ("user", "last_name",        "VARCHAR(64)",   "DEFAULT NULL"),
    ("user", "phone",            "VARCHAR(20)",   "DEFAULT NULL"),
    ("user", "avatar_path",      "VARCHAR(255)",  "DEFAULT NULL"),
    ("user", "tags",             "VARCHAR(255)",  "DEFAULT NULL"),
    ("user", "segment",          "VARCHAR(100)",  "DEFAULT 'user'"),
    ("user", "default_hub",      "VARCHAR(20)",   "DEFAULT 'sales'"),
    ("user", "custom_fields",    "TEXT",          "DEFAULT NULL"),
    ("user", "engagement_score", "REAL",          "DEFAULT 0.0"),
    ("user", "last_activity",    "DATETIME",      "DEFAULT NULL"),
    ("user", "bio",              "TEXT",          "DEFAULT NULL"),
    ("user", "preferred_hub",    "VARCHAR(20)",   "DEFAULT 'marketing'"),
    ("user", "active",           "BOOLEAN",       "DEFAULT 1"),
    ("user", "archived_at",      "DATETIME",      "DEFAULT NULL"),
    ("user", "archived_by_user_id","INTEGER",     "DEFAULT NULL"),
    ("user", "created_at",       "DATETIME",      "DEFAULT (CURRENT_TIMESTAMP)"),
    ("user", "updated_at",       "DATETIME",      "DEFAULT (CURRENT_TIMESTAMP)"),

    # twilio_account
    ("twilio_account", "sms_forward_to",              "VARCHAR(20)",  "DEFAULT NULL"),
    ("twilio_account", "call_forward_to",             "VARCHAR(20)",  "DEFAULT NULL"),
    ("twilio_account", "sms_forwarding_enabled",      "BOOLEAN",      "DEFAULT 1"),
    ("twilio_account", "voice_forwarding_enabled",    "BOOLEAN",      "DEFAULT 1"),
    ("twilio_account", "after_hours_sms_enabled",     "BOOLEAN",      "DEFAULT 1"),
    ("twilio_account", "after_hours_voicemail_enabled","BOOLEAN",     "DEFAULT 1"),
    ("twilio_account", "voicemail_greeting_text",     "TEXT",         "DEFAULT NULL"),
    ("twilio_account", "voicemail_greeting_audio_url","VARCHAR(500)", "DEFAULT NULL"),
    ("twilio_account", "missed_call_text",            "TEXT",         "DEFAULT NULL"),
    ("twilio_account", "after_hours_text",            "TEXT",         "DEFAULT NULL"),
    ("twilio_account", "ai_mode",                     "VARCHAR(20)",  "DEFAULT 'off'"),
    ("twilio_account", "ai_system_prompt",            "TEXT",         "DEFAULT NULL"),
    ("twilio_account", "webhook_base_url",             "VARCHAR(500)", "DEFAULT NULL"),
    ("twilio_account", "sms_fallback_url",             "VARCHAR(500)", "DEFAULT NULL"),
    ("twilio_account", "voice_fallback_url",           "VARCHAR(500)", "DEFAULT NULL"),
    ("twilio_account", "automation_enabled",          "BOOLEAN",      "DEFAULT 1"),
    ("twilio_account", "is_active",                   "BOOLEAN",      "DEFAULT 1"),
    ("twilio_account", "created_at",                  "DATETIME",     "DEFAULT (CURRENT_TIMESTAMP)"),
    ("twilio_account", "updated_at",                  "DATETIME",     "DEFAULT (CURRENT_TIMESTAMP)"),

    # twilio_conversation
    ("twilio_conversation", "sms_opt_in_at",  "DATETIME", "DEFAULT NULL"),
    ("twilio_conversation", "sms_opt_out_at", "DATETIME", "DEFAULT NULL"),

    # company SaaS billing fields
    ("company", "stripe_customer_id",         "VARCHAR(100)", "DEFAULT NULL"),
    ("company", "stripe_subscription_id",     "VARCHAR(100)", "DEFAULT NULL"),
    ("company", "stripe_subscription_status", "VARCHAR(50)",  "DEFAULT 'none'"),
    ("company", "stripe_price_lookup_key",    "VARCHAR(100)", "DEFAULT NULL"),
    ("company", "supabase_tenant_id",         "VARCHAR(100)", "DEFAULT NULL"),
    ("company", "mypaylink_id",               "VARCHAR(100)", "DEFAULT NULL"),
    ("company", "n8n_contact_id",             "VARCHAR(100)", "DEFAULT NULL"),
    ("company", "subscription_tier",          "VARCHAR(50)",  "DEFAULT 'free'"),
    ("company", "billing_tier",               "VARCHAR(50)",  "DEFAULT 'free'"),
    ("company", "billing_status",             "VARCHAR(50)",  "DEFAULT 'none'"),
    ("company", "max_team_members",           "INTEGER",      "DEFAULT NULL"),
    ("company", "grace_period_ends_at",       "DATETIME",     "DEFAULT NULL"),
    ("company", "current_period_start",       "DATETIME",     "DEFAULT NULL"),
    ("company", "current_period_end",         "DATETIME",     "DEFAULT NULL"),
    ("company", "cancel_at_period_end",       "BOOLEAN",      "DEFAULT 0"),
    # One-time setup/onboarding fee tracking
    ("company", "setup_fee_paid",                "BOOLEAN",      "DEFAULT 0"),
    ("company", "setup_fee_paid_at",             "DATETIME",     "DEFAULT NULL"),
    ("company", "setup_fee_checkout_session_id", "VARCHAR(120)", "DEFAULT NULL"),
    # Contact-usage / metered billing
    ("company", "included_contacts",                         "INTEGER",      "DEFAULT NULL"),
    ("company", "contacts_used",                             "INTEGER",      "DEFAULT 0"),
    ("company", "contacts_overage",                          "INTEGER",      "DEFAULT 0"),
    ("company", "stripe_contact_usage_subscription_item_id", "VARCHAR(120)", "DEFAULT NULL"),
    ("company", "last_reported_contact_usage",               "INTEGER",      "DEFAULT 0"),
    ("company", "last_usage_reported_at",                    "DATETIME",     "DEFAULT NULL"),
    ("company", "onboarding_status",          "VARCHAR(50)",  "DEFAULT 'pending'"),
    ("company", "implementation_status",      "VARCHAR(50)",  "DEFAULT 'none'"),
    ("company", "saas_notes",                 "TEXT",         "DEFAULT NULL"),

    # company extras sometimes missing
    ("company", "logo_path",          "VARCHAR(255)", "DEFAULT NULL"),
    ("company", "icon_path",          "VARCHAR(255)", "DEFAULT NULL"),
    ("company", "website_url",        "VARCHAR(255)", "DEFAULT NULL"),
    ("company", "primary_color",      "VARCHAR(20)",  "DEFAULT '#bc00ed'"),
    ("company", "secondary_color",    "VARCHAR(20)",  "DEFAULT '#00ffb4'"),
    ("company", "accent_color",       "VARCHAR(20)",  "DEFAULT '#e4055c'"),
    ("company", "font_family",        "VARCHAR(100)", "DEFAULT 'Inter, sans-serif'"),
    ("company", "apply_brand_colors", "BOOLEAN",      "DEFAULT 0"),
    ("company", "industry",           "VARCHAR(100)", "DEFAULT NULL"),
    ("company", "description",        "TEXT",         "DEFAULT NULL"),
    ("company", "require_approved_pwa_devices", "BOOLEAN", "DEFAULT 0"),

    # feedback_ticket — extended fields (PostHog, rating, screen, follow-up, admin notes)
    ("feedback_ticket", "rating",             "SMALLINT",     "DEFAULT NULL"),
    ("feedback_ticket", "allow_follow_up",    "BOOLEAN",      "DEFAULT 1"),
    ("feedback_ticket", "screen_width",       "INTEGER",      "DEFAULT NULL"),
    ("feedback_ticket", "screen_height",      "INTEGER",      "DEFAULT NULL"),
    ("feedback_ticket", "posthog_session_id", "VARCHAR(100)", "DEFAULT NULL"),
    ("feedback_ticket", "posthog_distinct_id","VARCHAR(100)", "DEFAULT NULL"),
    ("feedback_ticket", "posthog_replay_url", "VARCHAR(500)", "DEFAULT NULL"),
    ("feedback_ticket", "admin_notes",        "TEXT",         "DEFAULT NULL"),

    # user_company_access — per-user PWA & full-app access control (PostHog-free)
    ("user_company_access", "can_access_mobile_inbox", "BOOLEAN",      "DEFAULT 0"),
    ("user_company_access", "can_access_full_app",     "BOOLEAN",      "DEFAULT 1"),
    # Communications Hub per-user licensing & feature toggles
    ("user_company_access", "comms_hub_enabled",       "BOOLEAN",      "DEFAULT 0"),
    ("user_company_access", "pwa_access_enabled",      "BOOLEAN",      "DEFAULT 0"),
    ("user_company_access", "calls_enabled",           "BOOLEAN",      "DEFAULT 1"),
    ("user_company_access", "sms_enabled",             "BOOLEAN",      "DEFAULT 1"),
    ("user_company_access", "voicemail_enabled",       "BOOLEAN",      "DEFAULT 0"),
    ("user_company_access", "ai_comms_enabled",        "BOOLEAN",      "DEFAULT 0"),
    ("user_company_access", "forwarding_enabled",      "BOOLEAN",      "DEFAULT 0"),
    ("user_company_access", "communications_license",  "BOOLEAN",      "DEFAULT 0"),
    ("user_company_access", "assigned_number",         "VARCHAR(20)",  "DEFAULT NULL"),
    ("user_company_access", "number_type",             "VARCHAR(20)",  "DEFAULT 'shared'"),

    # PWA device approval/access control
    ("pwa_device", "last_login_at",       "DATETIME",     "DEFAULT NULL"),
    ("pwa_device", "last_ip",             "VARCHAR(64)",  "DEFAULT NULL"),
    ("pwa_device", "approved_status",     "VARCHAR(20)",  "DEFAULT 'pending'"),
    ("pwa_device", "approved_at",         "DATETIME",     "DEFAULT NULL"),
    ("pwa_device", "approved_by_user_id", "INTEGER",      "DEFAULT NULL"),
    ("pwa_device", "revoked_at",          "DATETIME",     "DEFAULT NULL"),
    ("pwa_device", "revoked_by_user_id",  "INTEGER",      "DEFAULT NULL"),

    # saas_automation_log — Stripe webhook audit columns
    ("saas_automation_log", "stripe_event_id", "VARCHAR(120)", "DEFAULT NULL"),
    ("saas_automation_log", "customer_id",     "VARCHAR(120)", "DEFAULT NULL"),
    ("saas_automation_log", "subscription_id", "VARCHAR(120)", "DEFAULT NULL"),
    ("saas_automation_log", "received_at",     "DATETIME",     "DEFAULT (CURRENT_TIMESTAMP)"),
    ("saas_automation_log", "processed_at",    "DATETIME",     "DEFAULT NULL"),

    # ── contact — tenant scoping + subscriber fields added after initial create ──
    ("contact", "company_id",    "INTEGER",      "DEFAULT NULL"),
    ("contact", "is_active",     "BOOLEAN",      "DEFAULT 1"),
    ("contact", "is_subscribed", "BOOLEAN",      "DEFAULT 1"),
    ("contact", "source",        "VARCHAR(100)", "DEFAULT NULL"),
    ("contact", "segment",       "VARCHAR(100)", "DEFAULT NULL"),
    ("contact", "tags",          "VARCHAR(255)", "DEFAULT NULL"),
    ("contact", "phone",         "VARCHAR(50)",  "DEFAULT NULL"),
    ("contact", "created_at",    "DATETIME",     "DEFAULT (CURRENT_TIMESTAMP)"),

    # ── campaign — tenant scoping ────────────────────────────────────────────────
    ("campaign", "company_id",        "INTEGER",      "DEFAULT NULL"),
    ("campaign", "revenue_generated", "REAL",         "DEFAULT 0.0"),
    ("campaign", "utm_keyword",       "VARCHAR(255)", "DEFAULT NULL"),
    ("campaign", "ai_generated",      "BOOLEAN",      "DEFAULT 0"),
    ("campaign", "updated_at",        "DATETIME",     "DEFAULT (CURRENT_TIMESTAMP)"),
    ("campaign", "automation_id",     "INTEGER",      "DEFAULT NULL"),
    ("campaign", "ab_test_id",        "INTEGER",      "DEFAULT NULL"),

    # ── blog_post — tenant scoping ───────────────────────────────────────────────
    ("blog_post", "company_id", "INTEGER", "DEFAULT NULL"),
    ("blog_post", "excerpt",    "TEXT",    "DEFAULT NULL"),
    ("blog_post", "category",   "VARCHAR(120)", "DEFAULT NULL"),

    # ── newsletter_subscriber — tenant scoping ───────────────────────────────────
    ("newsletter_subscriber", "company_id", "INTEGER", "DEFAULT NULL"),

    # ── agent_task — required NOT NULL resolved at runtime ───────────────────────
    ("agent_task", "company_id", "INTEGER", "DEFAULT NULL"),
    ("agent_task", "user_id",    "INTEGER", "DEFAULT NULL"),

    # ── agent_report — tenant scoping ────────────────────────────────────────────
    ("agent_report", "company_id", "INTEGER", "DEFAULT NULL"),

    # ── agent_deliverable — tenant scoping ───────────────────────────────────────
    ("agent_deliverable", "company_id", "INTEGER", "DEFAULT NULL"),

    # ── Phase A: multi-number VoIP — twilio_conversation / call_log FKs ────────
    ("twilio_conversation", "phone_number_id", "INTEGER",      "DEFAULT NULL"),
    ("auto_reply_rule",     "phone_number_id", "INTEGER",      "DEFAULT NULL"),
    ("notification",        "phone_number_id", "INTEGER",      "DEFAULT NULL"),
    ("notification",        "event_type",      "VARCHAR(50)",  "DEFAULT 'system'"),
    ("push_subscription",   "device_key",      "VARCHAR(120)", "DEFAULT NULL"),
    ("twilio_call_log",     "phone_number_id", "INTEGER",      "DEFAULT NULL"),
    ("twilio_call_log",     "voicemail_url",   "VARCHAR(500)", "DEFAULT NULL"),
    ("twilio_call_log",     "recording_url",   "VARCHAR(500)", "DEFAULT NULL"),

    # ── Google Contacts sync — extended tracking fields ──────────────────────
    ("google_oauth_token",  "sync_error",      "TEXT",         "DEFAULT NULL"),

    # ── Contact name source tracking on conversations ────────────────────────
    ("twilio_conversation", "contact_source",  "VARCHAR(50)",  "DEFAULT NULL"),
]


# Indexes — created idempotently after column adds.
INDEXES = [
    # (index_name, table, column, unique?)
    ("ux_saas_automation_log_stripe_event_id", "saas_automation_log", "stripe_event_id", True),
    ("ix_saas_automation_log_customer_id",     "saas_automation_log", "customer_id",     False),
    ("ix_saas_automation_log_subscription_id", "saas_automation_log", "subscription_id", False),
]

def run():
    app = create_app()
    with app.app_context():
        inspector = inspect(db.engine)
        existing_tables = inspector.get_table_names()

        print("\n" + "="*55)
        print("  LUXit — Database Migration")
        print("="*55)

        added = 0
        for table, column, col_type, default in MIGRATIONS:
            if table not in existing_tables:
                SKP(f"{table}.{column}  (table doesn't exist yet)")
                continue

            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            if column in existing_cols:
                SKP(f"{table}.{column}  (already exists)")
                continue

            try:
                sql = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {col_type} {default}'
                db.session.execute(text(sql))
                db.session.commit()
                OK(f"{table}.{column}  ({col_type})")
                added += 1
            except Exception as exc:
                db.session.rollback()
                ERR(f"{table}.{column}  — {exc}")

        # Also run db.create_all() to create any entirely missing tables
        print("\n── Creating any missing tables ──")
        try:
            db.create_all()
            OK("db.create_all() complete")
        except Exception as exc:
            ERR(f"db.create_all() failed: {exc}")

        # Indexes — best-effort, safe to skip on existing.
        print("\n── Ensuring indexes ──")
        existing_tables = inspector.get_table_names()
        for idx_name, table, column, unique in INDEXES:
            if table not in existing_tables:
                SKP(f"{idx_name}  (table missing)")
                continue
            try:
                existing_idx = {i["name"] for i in inspector.get_indexes(table)}
                if idx_name in existing_idx:
                    SKP(f"{idx_name}  (already exists)")
                    continue
                uniq = "UNIQUE " if unique else ""
                db.session.execute(text(
                    f'CREATE {uniq}INDEX IF NOT EXISTS "{idx_name}" ON "{table}" ("{column}")'
                ))
                db.session.commit()
                OK(f"{idx_name}  ({'unique ' if unique else ''}index)")
            except Exception as exc:
                db.session.rollback()
                ERR(f"{idx_name}  — {exc}")

        print(f"\nDone — {added} column(s) added.\n")

if __name__ == "__main__":
    run()
