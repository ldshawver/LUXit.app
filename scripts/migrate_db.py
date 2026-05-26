"""
Auto-migration: adds missing columns to an existing SQLite or PostgreSQL database.
Safe to run multiple times — skips columns that already exist.

Usage (run from /root/lux-email-bot):
  python3 scripts/migrate_db.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    ("twilio_account", "webhook_base_url",            "VARCHAR(500)", "DEFAULT NULL"),
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
    ("user_company_access", "can_access_mobile_inbox", "BOOLEAN", "DEFAULT 0"),
    ("user_company_access", "can_access_full_app",     "BOOLEAN", "DEFAULT 1"),

    # saas_automation_log — Stripe webhook audit columns
    ("saas_automation_log", "stripe_event_id", "VARCHAR(120)", "DEFAULT NULL"),
    ("saas_automation_log", "customer_id",     "VARCHAR(120)", "DEFAULT NULL"),
    ("saas_automation_log", "subscription_id", "VARCHAR(120)", "DEFAULT NULL"),
    ("saas_automation_log", "received_at",     "DATETIME",     "DEFAULT (CURRENT_TIMESTAMP)"),
    ("saas_automation_log", "processed_at",    "DATETIME",     "DEFAULT NULL"),
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
