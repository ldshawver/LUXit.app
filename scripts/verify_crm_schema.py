#!/usr/bin/env python3
"""Fail deployment before restart when the shared CRM schema is incomplete."""
from __future__ import annotations

import os
import sys

import psycopg2


REQUIRED_COLUMNS = {
    "user": {"active"},
    "contact": {
        "company_id", "is_active", "archived_at", "normalized_phone",
        "normalized_email", "lifecycle_stage", "owner_user_id", "status",
        "original_source", "latest_source", "google_match_status",
        "duplicate_status", "next_follow_up_at", "do_not_contact", "business_name",
        "primary_email", "last_activity_at", "lead_status",
    },
    "contact_phone_number": {"company_id", "contact_id", "normalized_value", "is_primary"},
    "contact_email_address": {"company_id", "contact_id", "normalized_value", "is_primary"},
    "contact_source_event": {"company_id", "contact_id", "source", "event_at"},
    "opportunity": {"company_id", "contact_id", "status", "estimated_value"},
    "twilio_conversation": {"company_id", "from_number", "to_number", "contact_id"},
    "twilio_message": {"company_id", "conversation_id", "twilio_sid", "direction"},
}


def verify(database_url: str) -> list[str]:
    missing: list[str] = []
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
        present: dict[str, set[str]] = {}
        for table, column in cur.fetchall():
            present.setdefault(table, set()).add(column)
        for table, columns in REQUIRED_COLUMNS.items():
            if table not in present:
                missing.append(f"table:{table}")
                continue
            missing.extend(f"column:{table}.{column}" for column in sorted(columns - present[table]))

        cur.execute("SELECT count(*) FROM \"user\" WHERE active IS NULL")
        if cur.fetchone()[0]:
            missing.append("backfill:user.active")
        cur.execute("SELECT count(*) FROM contact WHERE do_not_contact IS NULL")
        if cur.fetchone()[0]:
            missing.append("backfill:contact.do_not_contact")
    return missing


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("CRM schema verification failed: DATABASE_URL is required", file=sys.stderr)
        return 2
    missing = verify(database_url)
    if missing:
        print("CRM schema verification failed; missing objects:", file=sys.stderr)
        for item in missing:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("CRM schema verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
