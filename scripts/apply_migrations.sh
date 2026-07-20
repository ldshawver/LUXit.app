#!/usr/bin/env bash
set -euo pipefail

# Force the execution wrapper to find python context dependencies
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="$(dirname "$SCRIPT_DIR")"

export PATH="${APP_DIR}/.venv/bin:${PATH}"

migration_python="$(command -v python3)"
echo "Executing runner via sandbox Python context: $migration_python"

exec python3 "${SCRIPT_DIR}/apply_migrations.py" "$@"
