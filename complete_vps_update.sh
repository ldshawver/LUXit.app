#!/bin/bash

# Complete VPS Update Script - Deploy Current Replit App
# This will completely replace your VPS app with the current version

VPS_HOST="194.195.92.52"
VPS_USER="root"
VPS_PATH="/var/www/lux-marketing"

echo "🚀 Complete VPS Update - Deploy Current LUX Marketing App"
echo "======"

# Check if we have the current deployment package
if [ ! -f "current-lux-complete.tar.gz" ]; then
    echo "❌ Current deployment package not found!"
    echo "Please make sure current-lux-complete.tar.gz exists in the current directory"
    exit 1
fi

echo "📦 Found deployment package: $(ls -lh current-lux-complete.tar.gz)"
echo ""

# Upload the current app to VPS
echo "📤 Uploading current app to VPS..."
scp current-lux-complete.tar.gz $VPS_USER@$VPS_HOST:/tmp/

# Deploy on VPS
echo "🔧 Deploying on VPS..."
ssh $VPS_USER@$VPS_HOST << 'EOF'
    set -e
    
    echo "🛑 Stopping services..."
    systemctl stop lux-marketing 2>/dev/null || true
    
    echo "💾 Creating backup of current deployment..."
    if [ -d "/var/www/lux-marketing" ]; then
        cp -r /var/www/lux-marketing /var/backups/lux-backup-$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
        echo "Backup created"
    fi
    
    echo "🗑️ Clearing old deployment..."
    rm -rf /var/www/lux-marketing/*
    rm -rf /var/www/lux-marketing/.[^.]*
    
    echo "📦 Extracting current app..."
    cd /var/www/lux-marketing
    tar xzf /tmp/current-lux-complete.tar.gz
    
    echo "🔐 Setting permissions..."
    chown -R luxapp:www-data /var/www/lux-marketing
    chmod -R 755 /var/www/lux-marketing
    
    echo "🐍 Setting up Python environment..."
    sudo -u luxapp python3 -m venv venv
    sudo -u luxapp venv/bin/pip install --upgrade pip
    
    # Install all required packages
    sudo -u luxapp venv/bin/pip install flask gunicorn flask-sqlalchemy flask-login flask-wtf
    sudo -u luxapp venv/bin/pip install psycopg2-binary msal openai requests twilio apscheduler
    sudo -u luxapp venv/bin/pip install email-validator itsdangerous werkzeug jinja2
    
    echo "📁 Creating required directories..."
    mkdir -p static/uploads instance logs
    chown -R luxapp:www-data static/uploads instance logs
    
    echo "⚙️ Creating environment configuration..."
    cat > /etc/lux.env << 'ENVFILE'
DATABASE_URL=postgresql://luxuser:LuxPass2024!@localhost/lux_marketing
SESSION_SECRET=lux-marketing-super-secret-key-production-2024
FLASK_ENV=production
FLASK_DEBUG=False
OPENAI_API_KEY=your-openai-key-here
MS_CLIENT_ID=your-ms-client-id
MS_CLIENT_SECRET=your-ms-client-secret
MS_TENANT_ID=your-ms-tenant-id
TWILIO_ACCOUNT_SID=your-twilio-sid
TWILIO_AUTH_TOKEN=your-twilio-token
TWILIO_PHONE_NUMBER=your-twilio-phone
ENVFILE

    echo "🗄️ Initializing database with current models..."
    sudo -u luxapp -H bash -c "
        cd /var/www/lux-marketing
        source venv/bin/activate
        export \$(cat /etc/lux.env | xargs)
        python3 -c \"
from app import app, db
with app.app_context():
    db.create_all()
    
    # Create admin user if doesn't exist
    from models import User
    from werkzeug.security import generate_password_hash
    
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            email='admin@lux.local',
            password_hash=generate_password_hash('LuxAdmin2024!'),
            is_admin=True
        )
        db.session.add(admin)
        db.session.commit()
        print('Admin user created - Username: admin, Password: LuxAdmin2024!')
    else:
        print('Admin user already exists')
    
    print('Database initialized successfully')
\"
    "
    
    echo "🔧 Updating Gunicorn configuration..."
    cat > gunicorn.conf.py << 'GUNICORNCONF'
import multiprocessing

bind = "127.0.0.1:5000"
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "sync"
timeout = 60
keepalive = 2
max_requests = 1000
max_requests_jitter = 50
user = "luxapp"
group = "www-data"
daemon = False
pidfile = "/var/run/gunicorn/lux-marketing.pid"
GUNICORNCONF

    echo "🌐 Updating Nginx configuration..."
    cat > /etc/nginx/sites-available/lux-marketing << 'NGINXCONF'
server {
    listen 80;
    server_name lux.lucifercruz.com www.lux.lucifercruz.com;
    
    client_max_body_size 50M;
    
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        
        proxy_connect_timeout 30;
        proxy_send_timeout 60;
        proxy_read_timeout 60;
    }
    
    location /static {
        alias /var/www/lux-marketing/static;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
    
    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
NGINXCONF

    # Test Nginx config
    nginx -t
    
    echo "🚀 Starting services..."
    systemctl daemon-reload
    systemctl restart nginx
    systemctl start lux-marketing
    
    echo "✅ Deployment completed successfully!"
    
    # Verify the deployment
    echo ""
    echo "📋 Verification:"
    echo "Current files in /var/www/lux-marketing:"
    ls -la /var/www/lux-marketing/ | grep -E "(app\.py|main\.py|templates|static)"
    
    echo ""
    echo "Template files:"
    ls -la /var/www/lux-marketing/templates/ | head -10
    
    echo ""
    echo "Service status:"
    systemctl status lux-marketing --no-pager -l | head -10
    
    # Cleanup
    rm -f /tmp/current-lux-complete.tar.gz
    
    echo ""
    echo "🎉 VPS updated with current LUX Marketing app!"
    echo "Visit: https://lux.lucifercruz.com"
EOF

echo ""
echo "🎉 VPS Update Complete!"
echo "======"
echo ""
echo "Your VPS now has the current version of LUX Marketing"
echo "Visit: https://lux.lucifercruz.com"
echo ""
echo "Login credentials:"
echo "Username: admin"
echo "Password: LuxAdmin2024!"
echo ""
echo "The app should now match the design and features from your current Replit environment"