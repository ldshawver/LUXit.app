#!/usr/bin/env python3
"""Dry-run-first helper for bootstrapping the migration ledger safely."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from apply_migrations import discover_migrations, quote_ident, sha256_file

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None


def parse_args():
    p = argparse.ArgumentParser(description="Audit/bootstrap LUXit schema_migrations without guessing uncertain migrations.")
    p.add_argument("database_url", nargs="?", default=os.environ.get("DATABASE_URL"))
    p.add_argument("migrations_dir", nargs="?", default="migrations")
    p.add_argument("--ledger-table", default=os.environ.get("MIGRATION_LEDGER_TABLE", "schema_migrations"))
    p.add_argument("--commit", default=os.environ.get("GITHUB_SHA") or os.environ.get("DEPLOYMENT_ID"))
    p.add_argument("--actor", default=os.environ.get("USER") or "unknown")
    p.add_argument("--confirm", action="store_true", help="Insert ledger rows only for migrations proven_applied by automatic checks.")
    p.add_argument("--json", action="store_true")
    return p.parse_args()


def db_identity(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr()::text, inet_server_port()::text")
        db, addr, port = cur.fetchone()
    return {"database": db, "server": addr, "port": port}


def classify(conn, filename: str) -> tuple[str, str]:
    # This bootstrap is intentionally conservative. It does not infer application from later tables.
    # Specific per-migration proof rules can be added here only when they verify exact effects.
    return "cannot_determine_automatically", "No exact proof rule is defined for this migration; ledger row will not be inserted."


def main() -> int:
    args = parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    if psycopg2 is None:
        raise SystemExit("psycopg2 is required")
    table_ident = quote_ident(args.ledger_table)
    migrations = discover_migrations(Path(args.migrations_dir))
    conn = psycopg2.connect(args.database_url)
    conn.set_session(readonly=not args.confirm, autocommit=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "actor": args.actor,
        "host": socket.gethostname(),
        "application_commit": args.commit,
        "ledger_table": args.ledger_table,
        "database_identity": db_identity(conn),
        "dry_run": not args.confirm,
        "migrations": [],
    }
    try:
        for path in migrations:
            status, reason = classify(conn, path.name)
            item = {"filename": path.name, "checksum": sha256_file(path), "status": status, "reason": reason}
            report["migrations"].append(item)
        if args.confirm:
            proven = [m for m in report["migrations"] if m["status"] == "proven_applied"]
            if len(proven) != len(report["migrations"]):
                raise SystemExit("Refusing to seed ledger: one or more migrations are partially applied or cannot be determined automatically")
            with conn.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {table_ident} (filename text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now(), duration_ms integer NOT NULL DEFAULT 0, deployment_id text NULL, applied_by text NULL, database_identity text NULL)")
                for m in proven:
                    cur.execute(f"INSERT INTO {table_ident} (filename, checksum, deployment_id, applied_by, database_identity) VALUES (%s, %s, %s, %s, current_database()) ON CONFLICT (filename) DO NOTHING", (m["filename"], m["checksum"], args.commit, args.actor))
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for m in report["migrations"]:
            print(f"{m['filename']}: {m['status']} - {m['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
