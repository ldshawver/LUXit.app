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

        print(f"\nDone — {added} column(s) added.\n")

if __name__ == "__main__":
    run()
