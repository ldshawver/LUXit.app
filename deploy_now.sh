#!/bin/bash

# Direct deployment from Replit to VPS
# Run this script from Replit

VPS_HOST="194.195.92.52"
VPS_USER="root"
VPS_PATH="/var/www/lux-marketing"

echo "🚀 Deploying current LUX Marketing app to VPS..."
echo "======"
echo ""

# Check if tar file exists
if [ ! -f "current-lux-complete.tar.gz" ]; then
    echo "Creating deployment package..."
    tar czf current-lux-complete.tar.gz \
        --exclude='*.tar.gz' \
        --exclude='.git*' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='instance/*.db' \
        --exclude='venv*' \
        --exclude='attached_assets' \
        --exclude='.replit*' \
        --exclude='replit.nix' \
        --exclude='uv.lock' \
        .
    echo "✅ Package created"
fi

echo ""
echo "📤 Uploading to VPS..."
scp -o StrictHostKeyChecking=no current-lux-complete.tar.gz $VPS_USER@$VPS_HOST:/tmp/

echo ""
echo "🔧 Deploying on VPS..."

ssh -o StrictHostKeyChecking=no $VPS_USER@$VPS_HOST bash << 'ENDSSH'
set -e

echo "🛑 Stopping service..."
systemctl stop lux-marketing 2>/dev/null || true

echo "💾 Backing up current deployment..."
if [ -d "/var/www/lux-marketing" ]; then
    cp -r /var/www/lux-marketing /var/backups/lux-backup-$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
fi

echo "🗑️ Clearing old files..."
rm -rf /var/www/lux-marketing/*
rm -rf /var/www/lux-marketing/.[^.]*

echo "📦 Extracting new app..."
cd /var/www/lux-marketing
tar xzf /tmp/current-lux-complete.tar.gz

echo "🔐 Setting permissions..."
chown -R luxapp:www-data /var/www/lux-marketing
chmod -R 755 /var/www/lux-marketing

echo "🐍 Installing Python packages..."
if [ ! -d "venv" ]; then
    sudo -u luxapp python3 -m venv venv
fi

sudo -u luxapp venv/bin/pip install --quiet --upgrade pip
sudo -u luxapp venv/bin/pip install --quiet flask gunicorn flask-sqlalchemy flask-login flask-wtf
sudo -u luxapp venv/bin/pip install --quiet psycopg2-binary msal openai requests twilio apscheduler
sudo -u luxapp venv/bin/pip install --quiet email-validator itsdangerous werkzeug jinja2

echo "📁 Creating directories..."
mkdir -p static/uploads instance logs
chown -R luxapp:www-data static/uploads instance logs

echo "🗄️ Initializing database..."
sudo -u luxapp -H bash -c "
    cd /var/www/lux-marketing
    source venv/bin/activate
    export DATABASE_URL='postgresql://luxuser:LuxPass2024!@localhost/lux_marketing'
    export SESSION_SECRET='lux-marketing-super-secret-key-production-2024'
    export FLASK_ENV='production'
    python3 << 'EOPY'
from app import app, db
with app.app_context():
    db.create_all()
    
    from models import User
    from werkzeug.security import generate_password_hash
    
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            email='admin@lux.local',
            password_hash=generate_password_hash('LuxAdmin2024!')
        )
        if hasattr(admin, 'is_admin'):
            admin.is_admin = True
        db.session.add(admin)
        db.session.commit()
        print('✅ Admin user created')
    else:
        print('✅ Admin user exists')
EOPY
"

echo "🚀 Starting service..."
systemctl start lux-marketing

echo ""
echo "✅ Deployment Complete!"
echo "======"

echo ""
echo "📋 Verification:"
ls -la /var/www/lux-marketing/templates/*.html | head -5

echo ""
echo "Service status:"
systemctl status lux-marketing --no-pager | head -5

rm -f /tmp/current-lux-complete.tar.gz

ENDSSH

echo ""
echo "🎉 VPS Updated Successfully!"
echo "======"
echo ""
echo "Your site: https://lux.lucifercruz.com"
echo "Login: admin / LuxAdmin2024!"
echo ""
