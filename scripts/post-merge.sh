#!/bin/bash
set -e

echo "=== Post-merge setup ==="

echo "Installing Python dependencies..."
pip install -q -r requirements.txt 2>&1 || echo "pip install completed with warnings"

echo "Running database migrations..."
python scripts/migrate_db.py

echo "Ensuring tenant company + user access links..."
python scripts/create_company.py

echo "=== Post-merge setup complete ==="
