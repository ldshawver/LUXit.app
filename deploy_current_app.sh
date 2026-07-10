#!/bin/bash

# LUX Marketing - Deploy Current Application to VPS
# This script creates an artifact from the current Replit workspace and deploys it to VPS

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
VPS_HOST="194.195.92.52"
VPS_USER="root"
VPS_PATH="/opt/lux"
CURRENT_PATH="/opt/lux/current"
DOMAIN="lux.lucifercruz.com"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RELEASE_DIR="/opt/lux/releases/$TIMESTAMP"
ARTIFACT_NAME="lux-marketing-$TIMESTAMP.tar.gz"

echo -e "${GREEN}🚀 LUX Marketing Current App Deployment${NC}"
echo -e "${BLUE}======${NC}"
echo "Deploying from: $(pwd)"
echo "Target VPS: $VPS_HOST"
echo "Release: $TIMESTAMP"
echo ""

# Test VPS connection
echo -e "${YELLOW}🔍 Testing VPS connection...${NC}"
if ! ssh -o ConnectTimeout=10 $VPS_USER@$VPS_HOST "echo 'Connection successful'"; then
    echo -e "${RED}❌ Cannot connect to VPS${NC}"
    exit 1
fi
echo -e "${GREEN}✅ VPS connection successful${NC}"

# Create artifact from current workspace
echo -e "${YELLOW}📦 Creating application artifact...${NC}"
tar czf $ARTIFACT_NAME \
    --exclude='*.tar.gz' \
    --exclude='.git*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='node_modules' \
    --exclude='venv*' \
    --exclude='.env*' \
    --exclude='instance/*.db' \
    --exclude='*.log' \
    --exclude='tmp' \
    --exclude='.replit*' \
    --exclude='replit.nix' \
    .

# Create checksum
sha256sum $ARTIFACT_NAME > $ARTIFACT_NAME.sha256

echo -e "${GREEN}✅ Artifact created: $ARTIFACT_NAME${NC}"
ls -lh $ARTIFACT_NAME

# Upload artifact to VPS
echo -e "${YELLOW}📤 Uploading artifact to VPS...${NC}"
scp $ARTIFACT_NAME $ARTIFACT_NAME.sha256 $VPS_USER@$VPS_HOST:/tmp/

# Setup VPS environment and deploy
echo -e "${YELLOW}🔧 Setting up VPS environment...${NC}"
ssh $VPS_USER@$VPS_HOST << EOF
    set -e
    
    echo "🔄 Installing system dependencies..."
    apt update
    apt install -y python3 python3-pip python3-venv nginx postgresql postgresql-contrib
    apt install -y curl wget unzip git htop ufw
    
    echo "👤 Creating application user..."
    useradd -m -s /bin/bash luxapp 2>/dev/null || echo "User exists"
    usermod -aG www-data luxapp
    
    echo "📁 Creating directory structure..."
    mkdir -p $VPS_PATH/releases $VPS_PATH/shared/logs $VPS_PATH/shared/instance
    chown -R luxapp:www-data $VPS_PATH
    
    echo "🗄️ Setting up PostgreSQL..."
    systemctl start postgresql
    systemctl enable postgresql
    
    sudo -u postgres psql -c "CREATE DATABASE lux_marketing;" 2>/dev/null || echo "Database exists"
    sudo -u postgres psql -c "CREATE USER luxuser WITH PASSWORD 'LuxPass2024!';" 2>/dev/null || echo "User exists"
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE lux_marketing TO luxuser;" 2>/dev/null || true
    
    echo "📦 Verifying and extracting artifact..."
    cd /tmp
    if sha256sum -c $ARTIFACT_NAME.sha256; then
        echo "✅ Checksum verified"
    else
        echo "❌ Checksum failed"
        exit 1
    fi
    
    mkdir -p $RELEASE_DIR
    tar xzf $ARTIFACT_NAME -C $RELEASE_DIR
    chown -R luxapp:www-data $RELEASE_DIR
    
    echo "🔗 Creating symlinks..."
    rm -f $CURRENT_PATH
    ln -sf $RELEASE_DIR $CURRENT_PATH
    
    # Link shared directories
    rm -rf $CURRENT_PATH/instance $CURRENT_PATH/logs 2>/dev/null || true
    ln -sf $VPS_PATH/shared/instance $CURRENT_PATH/instance
    ln -sf $VPS_PATH/shared/logs $CURRENT_PATH/logs
    
    echo "🐍 Setting up Python environment..."
    cd $CURRENT_PATH
    sudo -u luxapp python3 -m venv venv
    sudo -u luxapp venv/bin/pip install --upgrade pip
    
    # Install dependencies from requirements or manually
    if [ -f requirements.txt ]; then
        sudo -u luxapp venv/bin/pip install -r requirements.txt
    else
        sudo -u luxapp venv/bin/pip install flask gunicorn flask-sqlalchemy flask-login flask-wtf
        sudo -u luxapp venv/bin/pip install psycopg2-binary msal openai requests twilio apscheduler
        sudo -u luxapp venv/bin/pip install email-validator itsdangerous werkzeug jinja2
    fi
    
    echo "⚙️ Creating configuration files..."
    
    # Environment file
    cat > /etc/lux.env << 'ENVEOF'
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
ENVEOF

    # Gunicorn config
    cat > $CURRENT_PATH/gunicorn.conf.py << 'CONFEOF'
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
CONFEOF

    # Systemd service
    cat > /etc/systemd/system/lux-marketing.service << 'SERVICEEOF'
[Unit]
Description=LUX Marketing Flask Application
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=notify
User=luxapp
Group=www-data
WorkingDirectory=/opt/lux/current
Environment=PATH=/opt/lux/current/venv/bin
EnvironmentFile=/etc/lux.env
ExecStart=/opt/lux/current/venv/bin/gunicorn --config gunicorn.conf.py --reuse-port main:app
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=always
RestartSec=5
StandardOutput=append:/opt/lux/shared/logs/access.log
StandardError=append:/opt/lux/shared/logs/error.log
RuntimeDirectory=gunicorn
RuntimeDirectoryMode=755

[Install]
WantedBy=multi-user.target
SERVICEEOF

    # Nginx config
    cat > /etc/nginx/sites-available/lux-marketing << 'NGINXEOF'
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;
    
    client_max_body_size 50M;
    
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_redirect off;
        
        proxy_connect_timeout 30;
        proxy_send_timeout 60;
        proxy_read_timeout 60;
    }
    
    location /static {
        alias /opt/lux/current/static;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
    
    # Health check endpoint
    location /health {
        access_log off;
        proxy_pass http://127.0.0.1:5000;
    }
    
    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header X-LUX-Version "$TIMESTAMP" always;
}
NGINXEOF

    # Enable Nginx site
    ln -sf /etc/nginx/sites-available/lux-marketing /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    
    # Test Nginx config
    nginx -t
    
    echo "🔐 Configuring firewall..."
    ufw --force enable
    ufw allow 22    # SSH
    ufw allow 80    # HTTP
    ufw allow 443   # HTTPS
    
    echo "🚀 Starting services..."
    systemctl daemon-reload
    systemctl enable lux-marketing nginx postgresql
    systemctl restart nginx
    
    # Initialize database
    cd $CURRENT_PATH
    sudo -u luxapp -H bash -c "
        source venv/bin/activate
        export \$(cat /etc/lux.env | xargs)
        python3 -c \"
from app import app, db
with app.app_context():
    db.create_all()
    print('Database initialized')
\"
    "
    
    systemctl start lux-marketing
    
    echo "✅ Deployment completed!"
    
    # Clean up
    rm -f /tmp/$ARTIFACT_NAME /tmp/$ARTIFACT_NAME.sha256
    
    # Keep only last 5 releases
    cd $VPS_PATH/releases
    ls -t | tail -n +6 | xargs rm -rf 2>/dev/null || true
EOF

# Clean up local artifacts
rm -f $ARTIFACT_NAME $ARTIFACT_NAME.sha256

echo -e "${GREEN}🎉 Deployment completed successfully!${NC}"
echo ""
echo -e "${BLUE}======${NC}"
echo -e "${GREEN}✅ LUX Marketing Platform is LIVE!${NC}"
echo -e "${BLUE}======${NC}"
echo ""
echo -e "${YELLOW}📋 Access Information:${NC}"
echo "  🌐 Website: http://$DOMAIN"
echo "  📊 Version: $TIMESTAMP"
echo ""
echo -e "${YELLOW}🧪 Testing deployment...${NC}"
if curl -s -I http://$DOMAIN | grep -q "200 OK"; then
    echo -e "${GREEN}✅ Website is responding${NC}"
else
    echo -e "${YELLOW}⚠️  Website may still be starting...${NC}"
fi

echo ""
echo -e "${YELLOW}🔧 Post-deployment commands:${NC}"
echo "  • Check status: ssh $VPS_USER@$VPS_HOST 'systemctl status lux-marketing'"
echo "  • View logs: ssh $VPS_USER@$VPS_HOST 'journalctl -u lux-marketing -f'"
echo "  • Setup SSL: ssh $VPS_USER@$VPS_HOST 'apt install certbot python3-certbot-nginx -y && certbot --nginx -d $DOMAIN'"
echo "  • Update API keys: ssh $VPS_USER@$VPS_HOST 'nano /etc/lux.env && systemctl restart lux-marketing'"
echo ""
echo -e "${GREEN}🚀 Your complete LUX Marketing platform is now deployed!${NC}"
