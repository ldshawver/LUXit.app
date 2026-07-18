#!/usr/bin/env bash
set -euo pipefail

DATABASE_URL="${1:-${DATABASE_URL:-}}"
MIGRATIONS_DIR="${2:-migrations}"
LEDGER_TABLE="${MIGRATION_LEDGER_TABLE:-schema_migrations}"

if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL is required" >&2
  exit 1
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required to apply migrations" >&2
  exit 1
fi
if [ ! -d "$MIGRATIONS_DIR" ]; then
  echo "Migrations directory not found: $MIGRATIONS_DIR" >&2
  exit 1
fi

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
CREATE TABLE IF NOT EXISTS ${LEDGER_TABLE} (
  filename text PRIMARY KEY,
  checksum text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);"

find "$MIGRATIONS_DIR" -maxdepth 1 -type f -name "*.sql" ! -iname "*rollback*" | sort | while read -r f; do
  filename="$(basename "$f")"
  checksum="$(sha256sum "$f" | awk '{print $1}')"
  applied_checksum="$(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -At -c "SELECT checksum FROM ${LEDGER_TABLE} WHERE filename = '$filename';")"
  if [ "$applied_checksum" = "$checksum" ]; then
    echo "Skipping already-applied migration: $filename"
    continue
  fi
  if [ -n "$applied_checksum" ] && [ "$applied_checksum" != "$checksum" ]; then
    echo "Migration checksum changed after application: $filename" >&2
    exit 1
  fi
  echo "Running migration: $f"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "INSERT INTO ${LEDGER_TABLE} (filename, checksum) VALUES ('$filename', '$checksum');"
done
