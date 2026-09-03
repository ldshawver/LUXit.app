"""Startup self-heal: add columns the current models expect but an existing
database predates, without requiring every environment to have run every
migration in lockstep.

This is a safety net, not the canonical migration mechanism -- prefer writing
a real migrations/*.sql file (applied via scripts/apply_migrations.py) for
anything more involved than "a column is missing." See MEMORY/task notes on
the 2026-08-22 luxdb_dev schema-drift investigation for why this exists
alongside, not instead of, the ledgered migration runner.

Historical bug: table/column identifiers were interpolated unquoted into the
ALTER TABLE statement (f"ALTER TABLE {table} ADD COLUMN {col_name} ...").
PostgreSQL requires double-quoting for any identifier that collides with a
reserved word -- "user" being the obvious one in this schema -- so every
attempt to self-heal the user table silently failed with a syntax error
that was swallowed by the per-column try/except and only visible as a
WARNING log line. Every identifier is now quoted.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text


def apply_missing_columns(engine, migrations: dict[str, list[tuple[str, str]]]) -> list[str]:
    """Add any column in `migrations` that is missing from its table.

    `migrations` maps table name -> list of (column_name, column_type_ddl).
    Returns the list of "table.column" strings that were actually added.
    Errors on an individual column are logged and skipped so one bad
    statement can't block healing the rest.
    """
    added: list[str] = []
    inspector = inspect(engine)
    for table, columns in migrations.items():
        if not inspector.has_table(table):
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for col_name, col_type in columns:
            if col_name in existing:
                continue
            with engine.begin() as conn:
                try:
                    conn.execute(text(
                        f'ALTER TABLE "{table}" ADD COLUMN "{col_name}" {col_type}'
                    ))
                    logging.info(f"Added column {col_name} to {table}")
                    added.append(f"{table}.{col_name}")
                except Exception as col_err:
                    logging.warning(f"Could not add {col_name} to {table}: {col_err}")
    return added
