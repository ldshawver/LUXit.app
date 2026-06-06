"""
SQLite → PostgreSQL migration script.

Reads every table from a SQLite database and bulk-inserts the rows into
a target PostgreSQL database that already has the correct schema (created
by db.create_all() / scripts/bootstrap.py).

USAGE — run on the machine that has the SQLite file:

    # Option A: use individual PG connection parts
    python3 scripts/sqlite_to_postgres.py \
        --sqlite  /root/lux-email-bot/instance/email_marketing.db \
        --pg-host <pg_host> \
        --pg-port 5432 \
        --pg-user <pg_user> \
        --pg-password <pg_password> \
        --pg-db   <pg_database>

    # Option B: full connection URL
    python3 scripts/sqlite_to_postgres.py \
        --sqlite  /root/lux-email-bot/instance/email_marketing.db \
        --pg-url  postgresql://user:password@host:5432/dbname

    # Dry-run (reads SQLite only, prints row counts — no writes)
    python3 scripts/sqlite_to_postgres.py --sqlite /path/to/db --dry-run

REQUIREMENTS (on the machine running this script):
    pip install psycopg2-binary

NOTES:
  • The script disables FK constraint enforcement during import and re-enables
    it afterwards, so table ordering doesn't matter.
  • All existing rows in the target PostgreSQL are DELETED before import —
    run on a fresh / empty database.
  • Auto-increment sequences are reset to max(id)+1 after each table.
  • SQLite boolean integers (0/1) and JSON text columns are handled correctly.
"""

import argparse
import json
import sqlite3
import sys
import os

# ── argument parsing ──────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Migrate SQLite → PostgreSQL")
parser.add_argument("--sqlite", required=True, help="Path to the SQLite .db file")
parser.add_argument("--pg-url", default=None,
                    help="Full PostgreSQL URL (postgresql://user:pass@host:5432/db)")
parser.add_argument("--pg-host", default=None)
parser.add_argument("--pg-port", default="5432")
parser.add_argument("--pg-user", default=None)
parser.add_argument("--pg-password", default=None)
parser.add_argument("--pg-db", default=None)
parser.add_argument("--dry-run", action="store_true",
                    help="Read SQLite only — do not write to PostgreSQL")
parser.add_argument("--tables", default=None,
                    help="Comma-separated list of tables to migrate (default: all)")
parser.add_argument("--skip-tables", default=None,
                    help="Comma-separated list of tables to skip")
args = parser.parse_args()

# ── validate SQLite path ──────────────────────────────────────────────────────

if not os.path.exists(args.sqlite):
    print(f"  ❌  SQLite file not found: {args.sqlite}", file=sys.stderr)
    sys.exit(1)

print(f"[migrate] SQLite source : {args.sqlite}  ({os.path.getsize(args.sqlite)//1024} KB)")

# ── build PostgreSQL URL ──────────────────────────────────────────────────────

if not args.dry_run:
    pg_url = args.pg_url
    if not pg_url:
        # fall back to environment DATABASE_URL
        pg_url = os.environ.get("DATABASE_URL", "").strip()
        if pg_url and pg_url.startswith("postgres://"):
            pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    if not pg_url:
        # build from individual parts
        if not (args.pg_host and args.pg_user and args.pg_db):
            print(
                "  ❌  Provide --pg-url OR (--pg-host, --pg-user, --pg-db [, --pg-password]).\n"
                "  Or set DATABASE_URL in the environment.",
                file=sys.stderr,
            )
            sys.exit(1)
        import urllib.parse as _up
        pg_url = (
            f"postgresql://{_up.quote(args.pg_user, safe='')}:"
            f"{_up.quote(args.pg_password or '', safe='')}@"
            f"{args.pg_host}:{args.pg_port}/{args.pg_db}"
        )

    try:
        import psycopg2
    except ImportError:
        print("  ❌  psycopg2 not installed.  Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    try:
        pg_conn = psycopg2.connect(pg_url, connect_timeout=10)
        pg_conn.autocommit = False
        import urllib.parse as _up2
        _parsed = _up2.urlparse(pg_url)
        print(f"[migrate] PostgreSQL target: {_parsed.hostname}/{_parsed.path.lstrip('/')}")
    except Exception as e:
        print(f"  ❌  Cannot connect to PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)

# ── open SQLite ───────────────────────────────────────────────────────────────

sq_conn = sqlite3.connect(args.sqlite)
sq_conn.row_factory = sqlite3.Row

sq_cur = sq_conn.cursor()

all_tables = [
    r[0] for r in sq_cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
]

if args.tables:
    wanted = {t.strip() for t in args.tables.split(",")}
    all_tables = [t for t in all_tables if t in wanted]

skip_set = set()
if args.skip_tables:
    skip_set = {t.strip() for t in args.skip_tables.split(",")}

# apscheduler_jobs is managed by APScheduler at runtime — skip it
skip_set.add("apscheduler_jobs")

tables_to_migrate = [t for t in all_tables if t not in skip_set]

print(f"[migrate] Tables found in SQLite : {len(all_tables)}")
print(f"[migrate] Tables to migrate       : {len(tables_to_migrate)}  (skipping: {skip_set})")

# ── helper: detect column types from SQLite PRAGMA ───────────────────────────

def get_column_info(table: str):
    """Return list of (col_name, type_affinity) tuples."""
    return [
        (row["name"], (row["type"] or "").upper())
        for row in sq_cur.execute(f"PRAGMA table_info('{table}')")
    ]


def coerce_value(value, affinity: str):
    """Convert a SQLite value to a PostgreSQL-safe Python value."""
    if value is None:
        return None

    # Booleans stored as integers in SQLite
    if affinity in ("BOOLEAN", "BOOL"):
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            return value.lower() not in ("0", "false", "no", "")

    # JSON columns stored as TEXT in SQLite — pass through as-is;
    # psycopg2 will send them as strings and PostgreSQL/SQLAlchemy will accept it.
    if affinity in ("JSON", "JSONB"):
        if isinstance(value, str):
            try:
                json.loads(value)   # validate; re-raise on bad JSON
            except (json.JSONDecodeError, ValueError):
                return None         # corrupt JSON → NULL rather than crash
        return value

    return value


# ── dry-run: print counts only ────────────────────────────────────────────────

if args.dry_run:
    print("\n[dry-run] Row counts in SQLite:")
    total = 0
    for tbl in tables_to_migrate:
        try:
            n = sq_cur.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            total += n
            if n:
                print(f"  {tbl:50s}: {n:>8,}")
        except Exception as e:
            print(f"  {tbl}: ERROR — {e}")
    print(f"\n  Total rows: {total:,}")
    sq_conn.close()
    print("[dry-run] Done (no data written).")
    sys.exit(0)

# ── migrate ───────────────────────────────────────────────────────────────────

pg_cur = pg_conn.cursor()

print("\n[migrate] Step 1/3 — disabling FK constraints …")
pg_cur.execute("SET session_replication_role = replica;")

print("[migrate] Step 2/3 — clearing existing data (TRUNCATE CASCADE) …")
# Truncate in one shot — FK deferral is active so order doesn't matter
for tbl in tables_to_migrate:
    try:
        pg_cur.execute(f'TRUNCATE TABLE "{tbl}" CASCADE;')
    except Exception as e:
        pg_conn.rollback()
        print(f"  ⚠️  TRUNCATE {tbl} failed: {e} — skipping")
        # Re-disable FK constraints after rollback
        pg_cur.execute("SET session_replication_role = replica;")

print("[migrate] Step 3/3 — inserting rows …\n")

migrated_tables = []
skipped_tables  = []
total_rows      = 0

for tbl in tables_to_migrate:
    cols_info = get_column_info(tbl)
    if not cols_info:
        print(f"  ⚠️  {tbl}: no columns found — skipped")
        skipped_tables.append(tbl)
        continue

    col_names    = [c[0] for c in cols_info]
    col_affinity = {c[0]: c[1] for c in cols_info}

    try:
        rows = sq_cur.execute(
            f'SELECT {", ".join(chr(34)+c+chr(34) for c in col_names)} FROM "{tbl}"'
        ).fetchall()
    except Exception as e:
        print(f"  ❌  {tbl}: read error — {e}")
        skipped_tables.append(tbl)
        continue

    if not rows:
        continue   # nothing to insert — silent skip

    col_list    = ", ".join(f'"{c}"' for c in col_names)
    placeholders = ", ".join("%s" for _ in col_names)
    insert_sql  = f'INSERT INTO "{tbl}" ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING;'

    inserted = 0
    errors   = 0

    for row in rows:
        values = tuple(coerce_value(row[c], col_affinity[c]) for c in col_names)
        try:
            pg_cur.execute(insert_sql, values)
            inserted += 1
        except Exception as e:
            errors += 1
            if errors <= 3:   # show first few errors only
                print(f"  ⚠️  {tbl} row error: {str(e)[:120]}")
            pg_conn.rollback()
            # Re-disable FK after rollback
            pg_cur.execute("SET session_replication_role = replica;")

    if inserted:
        print(f"  ✓  {tbl:50s}: {inserted:>8,} rows  ({errors} errors)")
        total_rows += inserted
        migrated_tables.append(tbl)
    elif errors:
        print(f"  ✗  {tbl:50s}: 0 inserted, {errors} errors")
        skipped_tables.append(tbl)

print(f"\n[migrate] Committing {total_rows:,} rows across {len(migrated_tables)} tables …")
pg_conn.commit()

# ── reset sequences so future INSERTs don't collide ──────────────────────────

print("[migrate] Resetting auto-increment sequences …")
for tbl in migrated_tables:
    try:
        pg_cur.execute(f"""
            DO $$
            DECLARE seq text;
            BEGIN
                seq := pg_get_serial_sequence('"{tbl}"', 'id');
                IF seq IS NOT NULL THEN
                    EXECUTE format(
                        'SELECT setval(%L, COALESCE((SELECT MAX(id) FROM "{tbl}"), 1))',
                        seq
                    );
                END IF;
            END $$;
        """)
    except Exception:
        pass   # table has no 'id' serial column — fine

pg_conn.commit()

# ── re-enable FK constraints ──────────────────────────────────────────────────

pg_cur.execute("SET session_replication_role = DEFAULT;")
pg_conn.commit()

# ── summary ───────────────────────────────────────────────────────────────────

print(f"""
[migrate] ─── COMPLETE ───────────────────────────────────────
  Total rows migrated : {total_rows:,}
  Tables migrated     : {len(migrated_tables)}
  Tables skipped      : {len(skipped_tables)}  {skipped_tables if skipped_tables else ''}
──────────────────────────────────────────────────────────────
""")

sq_conn.close()
pg_conn.close()
