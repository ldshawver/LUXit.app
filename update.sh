#!/bin/bash

# Update script for Email Marketing App
# Updates application code while preserving data

APP_NAME="email-marketing"
APP_USER="email-marketing"
APP_DIR="/opt/$APP_NAME"
VENV_DIR="$APP_DIR/venv"

echo "=== Email Marketing App Update Script ==="

# Create backup before update
echo "Creating backup before update..."
./backup.sh

# Stop the application
echo "Stopping application..."
systemctl stop "$APP_NAME"

# Backup current .env file
echo "Preserving configuration..."
cp "$APP_DIR/.env" "/tmp/.env.backup"

# Update application files (assuming new files are in current directory)
echo "Updating application files..."
rsync -av --exclude='.env' --exclude='*.db' --exclude='venv' ./ "$APP_DIR/"

# Restore .env file
cp "/tmp/.env.backup" "$APP_DIR/.env"

# Set correct permissions
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Update Python dependencies
echo "Updating Python dependencies..."
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade -r "$APP_DIR/deploy_requirements.txt"

# Run database migrations + tenant data sync
echo "Running database migrations..."
cd "$APP_DIR"
sudo -u "$APP_USER" bash -c "source $VENV_DIR/bin/activate && source .env && python3 scripts/migrate_db.py"

echo "Ensuring company + access rows exist..."
sudo -u "$APP_USER" bash -c "source $VENV_DIR/bin/activate && source .env && python3 scripts/create_company.py"

# Start the application
echo "Starting application..."
systemctl start "$APP_NAME"

# Check status
sleep 5
if systemctl is-active --quiet "$APP_NAME"; then
    echo "✓ Application updated and running successfully!"
else
    echo "✗ Application failed to start. Check logs:"
    echo "  journalctl -u $APP_NAME -n 20"
fi

echo ""
echo "Update completed!"
