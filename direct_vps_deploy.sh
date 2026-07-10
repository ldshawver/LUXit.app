#!/bin/bash

# Direct VPS Deployment - Copy Current Files from Replit to VPS
# Run this from your LOCAL machine where you have access to both Replit and VPS

set -e

VPS_HOST="194.195.92.52"
VPS_USER="root"
VPS_PATH="/var/www/lux-marketing"
BACKUP_PATH="/var/backups/lux-$(date +%Y%m%d_%H%M%S)"

echo "🚀 Direct VPS Deployment from Current Replit"
echo "======"

# Step 1: SSH into VPS and prepare
echo "📋 Preparing VPS environment..."
ssh $VPS_USER@$VPS_HOST << 'EOF'
    # Backup current deployment
    if [ -d "/var/www/lux-marketing" ]; then
        mkdir -p /var/backups
        cp -r /var/www/lux-marketing /var/backups/lux-$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
    fi
    
    # Stop service
    systemctl stop lux-marketing 2>/dev/null || true
    
    # Clear old files
    rm -rf /var/www/lux-marketing/*
    mkdir -p /var/www/lux-marketing
    
    echo "VPS prepared for new deployment"
EOF

echo "✅ VPS prepared"

# Step 2: Get current files from Replit
echo "📥 Downloading current files from Replit..."

# Create a temporary directory
TEMP_DIR="/tmp/lux-replit-$(date +%s)"
mkdir -p "$TEMP_DIR"

# This is where you need to put the Replit files
echo "📝 IMPORTANT: Copy your Replit files to: $TEMP_DIR"
echo ""
echo "Run these commands in your Replit terminal:"
echo "cd /home/runner/\$(basename \$PWD)"
echo "tar czf /tmp/current-lux.tar.gz --exclude='*.tar.gz' --exclude='.git*' --exclude='__pycache__' --exclude='*.pyc' --exclude='instance/*.db' --exclude='venv*' ."
echo ""
echo "Then download the tar.gz file and extract it to: $TEMP_DIR"
echo ""
read -p "Press Enter when you've copied the current Replit files to $TEMP_DIR..."

# Step 3: Upload to VPS
echo "📤 Uploading current files to VPS..."
rsync -avz --progress \
    --exclude='*.tar.gz' \
    --exclude='.git*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='venv*' \
    --exclude='instance/*.db' \
    --exclude='*.log' \
    "$TEMP_DIR/" $VPS_USER@$VPS_HOST:$VPS_PATH/

echo "✅ Files uploaded"

# Step 4: Configure VPS
echo "⚙️ Configuring VPS..."
ssh $VPS_USER@$VPS_HOST << 'EOF'
    cd /var/www/lux-marketing
    
    # Set permissions
    chown -R luxapp:www-data /var/www/lux-marketing
    chmod -R 755 /var/www/lux-marketing
    
    # Create virtual environment
    sudo -u luxapp python3 -m venv venv
    sudo -u luxapp venv/bin/pip install --upgrade pip
    
    # Install dependencies
    sudo -u luxapp venv/bin/pip install flask gunicorn flask-sqlalchemy flask-login flask-wtf
    sudo -u luxapp venv/bin/pip install psycopg2-binary msal openai requests twilio apscheduler
    sudo -u luxapp venv/bin/pip install email-validator itsdangerous werkzeug jinja2
    
    # Create directories
    mkdir -p static/uploads instance logs
    chown -R luxapp:www-data static/uploads instance logs
    
    # Initialize database
    sudo -u luxapp -H bash -c "
        cd /var/www/lux-marketing
        source venv/bin/activate
        export DATABASE_URL='postgresql://luxuser:LuxPass2024!@localhost/lux_marketing'
        export SESSION_SECRET='lux-secret-2024'
        python3 -c \"
from app import app, db
with app.app_context():
    db.create_all()
    print('Database initialized')
\"
    "
    
    # Start service
    systemctl start lux-marketing
    systemctl status lux-marketing --no-pager
    
    echo "✅ VPS configuration complete"
EOF

# Cleanup
rm -rf "$TEMP_DIR"

echo ""
echo "🎉 Deployment Complete!"
echo "Visit: http://lux.lucifercruz.com"
echo ""
echo "To verify your current templates are deployed:"
echo "ssh $VPS_USER@$VPS_HOST 'ls -la /var/www/lux-marketing/templates/'"