#!/bin/bash
set -e

echo "=== Post-merge setup ==="

echo "Installing Python dependencies..."
pip install -q -r requirements.txt 2>&1 || echo "pip install completed with warnings"

echo "Running database migrations..."
python -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('Database tables synced')
"

echo "=== Post-merge setup complete ==="
