#!/bin/bash
set -e

PGDATA="/home/runner/.postgresql/data"
PGLOG="/home/runner/.postgresql/logfile"
PG_SOCKET_DIR="/tmp"

# ── 1. Ensure data directory exists ─────────────────────────────────────────
if [ ! -d "$PGDATA" ]; then
    echo "[start.sh] Initializing PostgreSQL data directory..."
    initdb -D "$PGDATA" --auth=trust -U runner
fi

# ── 2. Patch postgresql.conf to use /tmp socket (persists across shell resets)
if ! grep -q "unix_socket_directories = '/tmp'" "$PGDATA/postgresql.conf" 2>/dev/null; then
    echo "unix_socket_directories = '/tmp'" >> "$PGDATA/postgresql.conf"
    echo "listen_addresses = 'localhost'" >> "$PGDATA/postgresql.conf"
fi

# ── 3. Start PostgreSQL if not already running ───────────────────────────────
mkdir -p /run/postgresql
if pg_ctl status -D "$PGDATA" > /dev/null 2>&1; then
    echo "[start.sh] PostgreSQL already running."
else
    echo "[start.sh] Starting PostgreSQL..."
    pg_ctl -D "$PGDATA" -l "$PGLOG" start
    sleep 2
fi

psql_local() {
    PGHOST="$PG_SOCKET_DIR" PGPORT=5432 PGUSER=runner psql -d postgres "$@"
}

# ── 4. Create user if it doesn't exist ──────────────────────────────────────
USER_EXISTS=$(psql_local -tAc "SELECT 1 FROM pg_roles WHERE rolname='luxuser'" 2>/dev/null || echo "")
if [ "$USER_EXISTS" != "1" ]; then
    echo "[start.sh] Creating luxuser..."
    psql_local -c "CREATE USER luxuser WITH PASSWORD 'LuxPass2024!';"
fi

# ── 5. Create database if it doesn't exist ───────────────────────────────────
DB_EXISTS=$(psql_local -tAc "SELECT 1 FROM pg_database WHERE datname='lux_marketing'" 2>/dev/null || echo "")
if [ "$DB_EXISTS" != "1" ]; then
    echo "[start.sh] Creating lux_marketing database..."
    psql_local --no-psqlrc -c "CREATE DATABASE lux_marketing OWNER luxuser;"
    psql_local -c "GRANT ALL PRIVILEGES ON DATABASE lux_marketing TO luxuser;"
fi

echo "[start.sh] PostgreSQL ready."

# ── 6. Start gunicorn ────────────────────────────────────────────────────────
echo "[start.sh] Starting gunicorn..."
exec gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 12 \
    --worker-class gthread --timeout 120 wsgi:app
