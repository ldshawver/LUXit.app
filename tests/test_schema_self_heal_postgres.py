"""Regression test for the startup schema self-heal reserved-word bug.

Historical bug: services/schema_self_heal.py (formerly inlined in app.py's
create_app()) interpolated table/column names unquoted into ALTER TABLE
statements. PostgreSQL requires double-quoting any identifier that collides
with a reserved word -- "user" being the one in this schema -- so every
attempt to self-heal the user table silently failed with a syntax error,
swallowed by a per-column try/except and visible only as a WARNING log line.
Runs against the real PostgreSQL instance used by the other *_postgres.py
suites (TEST_DATABASE_URL on port 5433) because SQLite has no such reserved
word and would not catch this regression.
"""
from __future__ import annotations

import os

import psycopg2
import pytest
from sqlalchemy import create_engine, inspect

from services.schema_self_heal import apply_missing_columns

SCHEMA = "schema_self_heal_test"


def test_apply_missing_columns_quotes_reserved_word_table_name():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL-only self-heal test")

    conn = psycopg2.connect(url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), inet_server_port()")
            assert cur.fetchone() == ("lux_identity_hardening_test", 5433)
            cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
            cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
            cur.execute(f'SET search_path TO "{SCHEMA}"')
            # "user" is the reserved word that previously broke self-heal;
            # a second, non-reserved table proves normal cases still work.
            cur.execute('CREATE TABLE "user" (id SERIAL PRIMARY KEY)')
            cur.execute("CREATE TABLE widget (id SERIAL PRIMARY KEY)")

        engine = create_engine(url, connect_args={"options": f"-csearch_path={SCHEMA}"})
        try:
            migrations = {
                "user": [("notification_sounds_enabled", "BOOLEAN DEFAULT TRUE")],
                "widget": [("label", "VARCHAR(80)")],
            }
            added = apply_missing_columns(engine, migrations)
            assert set(added) == {"user.notification_sounds_enabled", "widget.label"}

            inspector = inspect(engine)
            user_cols = {c["name"] for c in inspector.get_columns("user", schema=SCHEMA)}
            widget_cols = {c["name"] for c in inspector.get_columns("widget", schema=SCHEMA)}
            assert "notification_sounds_enabled" in user_cols
            assert "label" in widget_cols

            # Idempotent: re-running with the columns already present adds nothing
            # and raises nothing.
            added_again = apply_missing_columns(engine, migrations)
            assert added_again == []
        finally:
            engine.dispose()
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        conn.close()
