#!/usr/bin/env python3
"""Ledgered PostgreSQL SQL migration runner for LUXit deploys."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

LOCK_KEY = 0x4C555849544D4947  # "LUXITMIG" truncated into a signed bigint-safe value
VALID_NAME = re.compile(r"^[0-9A-Za-z_.-]+\.sql$")
NONTRANSACTIONAL_MARKER = "-- luxit-migration: transaction=off"
INCOMPATIBLE_TRANSACTION_RE = re.compile(
    r"\b(CREATE\s+INDEX\s+CONCURRENTLY|REINDEX|VACUUM|CLUSTER|CREATE\s+DATABASE|DROP\s+DATABASE|ALTER\s+TYPE\b.*\bADD\s+VALUE)\b",
    re.IGNORECASE | re.DOTALL,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply LUXit SQL migrations with a PostgreSQL ledger and advisory lock.")
    parser.add_argument("database_url", nargs="?", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("migrations_dir", nargs="?", default="migrations")
    parser.add_argument("--ledger-table", default=os.environ.get("MIGRATION_LEDGER_TABLE", "schema_migrations"))
    parser.add_argument("--lock-timeout", type=int, default=int(os.environ.get("MIGRATION_LOCK_TIMEOUT_SECONDS", "60")))
    parser.add_argument("--deployment-id", default=os.environ.get("GITHUB_SHA") or os.environ.get("DEPLOYMENT_ID"))
    parser.add_argument("--allow-nontransactional", action="append", default=[], help="Explicit migration basename allowed to run without --single-transaction when marked transaction=off.")
    return parser.parse_args()


def quote_ident(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise SystemExit(f"Unsafe ledger table name: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if not digest or len(digest) != 64:
        raise SystemExit(f"Failed to calculate SHA-256 checksum for {path}")
    return digest


def discover_migrations(root: Path) -> list[Path]:
    if not root.is_dir():
        raise SystemExit(f"Migrations directory not found: {root}")
    root_real = root.resolve()
    seen: set[str] = set()
    migrations: list[Path] = []
    for child in root.iterdir():
        if child.name.lower().endswith("rollback.sql") or "rollback" in child.name.lower():
            continue
        if child.suffix != ".sql":
            if child.is_file():
                raise SystemExit(f"Unexpected migration extension: {child.name}")
            continue
        if not VALID_NAME.match(child.name):
            raise SystemExit(f"Unsafe migration filename: {child.name!r}")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in child.name):
            raise SystemExit(f"Migration filename contains control characters: {child.name!r}")
        resolved = child.resolve()
        if root_real not in [resolved, *resolved.parents]:
            raise SystemExit(f"Migration symlink escapes migrations directory: {child}")
        if child.name in seen:
            raise SystemExit(f"Duplicate migration basename: {child.name}")
        seen.add(child.name)
        migrations.append(child)
    ordered = sorted(migrations, key=lambda p: p.name)
    if len({p.name for p in ordered}) != len(ordered):
        raise SystemExit("Ambiguous migration ordering: duplicate basenames")
    return ordered


def acquire_lock(conn, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    with conn.cursor() as cur:
        while True:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
            if cur.fetchone()[0]:
                return
            if time.monotonic() >= deadline:
                raise SystemExit(f"Timed out waiting for migration advisory lock after {timeout}s; another migration process may be running")
            time.sleep(1)


def ensure_ledger(conn, table_ident: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_ident} (
              filename text PRIMARY KEY,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now(),
              duration_ms integer NOT NULL DEFAULT 0,
              deployment_id text NULL,
              applied_by text NULL,
              database_identity text NULL
            )
        """)
    conn.commit()


def database_identity(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database() || '@' || inet_server_addr()::text || ':' || inet_server_port()::text")
        return cur.fetchone()[0]


def applied_checksum(conn, table_ident: str, filename: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT checksum FROM {table_ident} WHERE filename = %s", (filename,))
        row = cur.fetchone()
    return row[0] if row else None


def insert_ledger(conn, table_ident: str, filename: str, checksum: str, duration_ms: int, deployment_id: str | None, db_ident: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {table_ident} (filename, checksum, duration_ms, deployment_id, applied_by, database_identity) VALUES (%s, %s, %s, %s, current_user, %s)",
            (filename, checksum, duration_ms, deployment_id, db_ident),
        )
    conn.commit()


def migration_transaction_mode(path: Path, allow_nontransactional: set[str]) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    head = text[:4096].lower()
    if NONTRANSACTIONAL_MARKER in head:
        if path.name not in allow_nontransactional:
            raise SystemExit(f"Migration {path.name} is marked nontransactional but is not explicitly allowlisted")
        return []
    if INCOMPATIBLE_TRANSACTION_RE.search(text):
        raise SystemExit(f"Migration {path.name} contains transaction-incompatible SQL and must be explicitly marked/allowlisted with rollback consequences documented")
    if re.search(r"^\s*BEGIN\s*;", text, re.IGNORECASE | re.MULTILINE) and re.search(r"^\s*COMMIT\s*;", text, re.IGNORECASE | re.MULTILINE):
        return []
    return ["--single-transaction"]


def main() -> int:
    args = parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")
    if "sha256" not in hashlib.algorithms_available:
        raise SystemExit("Python hashlib SHA-256 support is required")
    migrations = discover_migrations(Path(args.migrations_dir))
    table_ident = quote_ident(args.ledger_table)
    conn = psycopg2.connect(args.database_url)
    conn.autocommit = True
    try:
        acquire_lock(conn, args.lock_timeout)
        db_ident = database_identity(conn)
        ensure_ledger(conn, table_ident)
        for path in migrations:
            checksum = sha256_file(path)
            current = applied_checksum(conn, table_ident, path.name)
            if current == checksum:
                print(f"Skipping already-applied migration: {path.name}")
                continue
            if current and current != checksum:
                raise SystemExit(f"Migration checksum changed after application: {path.name}")
            flags = migration_transaction_mode(path, set(args.allow_nontransactional))
            cmd = ["psql", args.database_url, "-v", "ON_ERROR_STOP=1", *flags, "-f", str(path)]
            print("Running migration:", path)
            started = time.monotonic()
            subprocess.run(cmd, check=True)
            duration_ms = int((time.monotonic() - started) * 1000)
            insert_ledger(conn, table_ident, path.name, checksum, duration_ms, args.deployment_id, db_ident)
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
