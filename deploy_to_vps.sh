#!/bin/bash

# LUX Marketing VPS Deployment Script
# Deploy to your VPS at 194.195.92.52 with domain lux.lucifercruz.com

set -e  # Exit on any error

# Configuration
VPS_HOST="194.195.92.52"
VPS_USER="root"
VPS_PATH="/var/www/lux-marketing"
BACKUP_PATH="/var/backups/lux-marketing-$(date +%Y%m%d_%H%M%S)"
DOMAIN="lux.lucifercruz.com"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 LUX Marketing VPS Deployment Script${NC}"
echo "======"

echo -e "${YELLOW}📋 Deployment Configuration:${NC}"
echo "  VPS Host: $VPS_HOST"
echo "  VPS User: $VPS_USER" 
echo "  VPS Path: $VPS_PATH"
echo "  Domain: $DOMAIN"
echo ""

# Ask for confirmation
read -p "Continue with deployment? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Deployment cancelled."
    exit 0
fi

echo -e "${YELLOW}🔍 Testing VPS connection...${NC}"
if ! ssh -o ConnectTimeout=10 $VPS_USER@$VPS_HOST "echo 'Connection successful'"; then
    echo -e "${RED}❌ Cannot connect to VPS. Please check:${NC}"
    echo "  1. VPS is accessible: ssh $VPS_USER@$VPS_HOST"
    echo "  2. User '$VPS_USER' exists (create with: useradd -m $VPS_USER)"
    echo "  3. SSH keys are set up or password authentication works"
    exit 1
fi

echo -e "${GREEN}✅ VPS connection successful${NC}"

echo -e "${YELLOW}💾 Creating backup of existing deployment...${NC}"
ssh $VPS_USER@$VPS_HOST "
    if [ -d '$VPS_PATH' ]; then
        sudo mkdir -p /var/backups
        sudo cp -r $VPS_PATH $BACKUP_PATH 2>/dev/null || true
        echo 'Backup created at: $BACKUP_PATH'
    else
        echo 'No existing deployment found, skipping backup'
    fi
"

echo -e "${YELLOW}📁 Creating deployment directory...${NC}"
ssh $VPS_USER@$VPS_HOST "
    sudo mkdir -p $VPS_PATH
    sudo chown $VPS_USER:www-data $VPS_PATH
    sudo chmod 755 $VPS_PATH
"

echo -e "${YELLOW}📤 Uploading files to VPS...${NC}"
# Upload current directory (LUX Marketing platform) to VPS
rsync -avz --progress \
    --exclude='.git*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='node_modules' \
    --exclude='venv*' \
    --exclude='.env*' \
    --exclude='instance' \
    --exclude='*.log' \
    --exclude='tmp' \
    ./ $VPS_USER@$VPS_HOST:$VPS_PATH/

echo -e "${YELLOW}🔧 Setting up deployment environment...${NC}"
ssh $VPS_USER@$VPS_HOST "
    cd $VPS_PATH
    
    # Update system and install dependencies
    apt update
    apt install -y python3 python3-pip python3-venv nginx postgresql postgresql-contrib
    
    # Create application user (if not exists)
    useradd -m -s /bin/bash luxapp || echo 'User luxapp already exists'
    
    # Set proper permissions
    chown -R luxapp:www-data $VPS_PATH
    chmod -R 755 $VPS_PATH
    
    # Create necessary directories
    mkdir -p logs static/uploads instance /var/log/lux-marketing
    chown luxapp:www-data /var/log/lux-marketing
    
    # Create Python virtual environment
    echo 'Creating Python virtual environment...'
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
    
    # Install application dependencies
    pip install flask gunicorn flask-sqlalchemy flask-login flask-wtf
    pip install psycopg2-binary msal openai requests twilio apscheduler
    pip install email-validator itsdangerous werkzeug jinja2
    
    echo 'Virtual environment created and dependencies installed'
"

echo -e "${YELLOW}⚙️  Setting up production services...${NC}"
ssh $VPS_USER@$VPS_HOST "
    cd $VPS_PATH
    
    # Setup PostgreSQL database
    sudo -u postgres psql -c \"CREATE DATABASE lux_marketing;\" 2>/dev/null || echo 'Database may already exist'
    sudo -u postgres psql -c \"CREATE USER luxuser WITH PASSWORD 'luxpass123';\" 2>/dev/null || echo 'User may already exist'  
    sudo -u postgres psql -c \"GRANT ALL PRIVILEGES ON DATABASE lux_marketing TO luxuser;\" 2>/dev/null || true
    
    # Create environment file
    cat > /etc/environment << 'EOF'
DATABASE_URL=postgresql://luxuser:luxpass123@localhost/lux_marketing
SESSION_SECRET=lux-super-secret-key-production-2024
OPENAI_API_KEY=your-openai-key-here
FLASK_ENV=production
FLASK_DEBUG=False
EOF
    
    # Create systemd service
    tee /etc/systemd/system/lux-marketing.service > /dev/null << 'EOF'
[Unit]
Description=LUX Marketing Flask Application
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=luxapp
Group=www-data
WorkingDirectory=/var/www/lux-marketing
Environment=PATH=/var/www/lux-marketing/venv/bin
EnvironmentFile=/etc/environment
ExecStart=/var/www/lux-marketing/venv/bin/gunicorn --config gunicorn.conf.py main:app
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=always
RestartSec=5
StandardOutput=append:/var/log/lux-marketing/access.log
StandardError=append:/var/log/lux-marketing/error.log

[Install]
WantedBy=multi-user.target
EOF
    
    # Create Gunicorn configuration
    tee gunicorn.conf.py > /dev/null << 'EOF'
import multiprocessing

bind = \"127.0.0.1:8000\"
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = \"sync\"
timeout = 30
keepalive = 2
max_requests = 1000
max_requests_jitter = 50
user = \"luxapp\"
group = \"www-data\"
tmp_upload_dir = None
EOF
    
    systemctl daemon-reload
    systemctl enable lux-marketing
    
    echo 'System services configured'
"

echo -e "${YELLOW}🌐 Configuring Nginx reverse proxy...${NC}"
ssh $VPS_USER@$VPS_HOST "
    # Create Nginx configuration
    tee /etc/nginx/sites-available/lux-marketing > /dev/null << 'EOF'
server {
    listen 80;
    server_name lux.lucifercruz.com www.lux.lucifercruz.com;
    
    client_max_body_size 10M;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_redirect off;
    }
    
    location /static {
        alias /var/www/lux-marketing/static;
        expires 1y;
        add_header Cache-Control \"public, immutable\";
    }
    
    # Security headers
    add_header X-Frame-Options \"SAMEORIGIN\" always;
    add_header X-XSS-Protection \"1; mode=block\" always;
    add_header X-Content-Type-Options \"nosniff\" always;
}
EOF
    
    # Enable site
    ln -sf /etc/nginx/sites-available/lux-marketing /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    
    # Test and restart nginx
    nginx -t
    systemctl restart nginx
    systemctl enable nginx
    
    echo 'Nginx configured and started'
"

echo -e "${GREEN}🎉 Deployment completed successfully!${NC}"
echo ""
echo -e "${YELLOW}📋 Next steps to complete setup:${NC}"
echo ""
echo "  1. Update environment variables (if needed):"
echo "     ssh $VPS_USER@$VPS_HOST"
echo "     nano /etc/environment"
echo "     # Add your actual API keys"
echo ""
echo "  2. Start the LUX Marketing application:"
echo "     ssh $VPS_USER@$VPS_HOST"
echo "     systemctl start lux-marketing"
echo "     systemctl status lux-marketing"
echo ""
echo "  3. Setup SSL certificate for production:"
echo "     ssh $VPS_USER@$VPS_HOST"
echo "     apt install certbot python3-certbot-nginx"
echo "     certbot --nginx -d lux.lucifercruz.com -d www.lux.lucifercruz.com"
echo ""
echo "  4. Your site will be available at: http://lux.lucifercruz.com"
echo ""
echo -e "${GREEN}✅ Your LUX Marketing platform is ready for production!${NC}"

# Optional: Test the deployment
read -p "Would you like to test the deployment now? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}🧪 Testing deployment...${NC}"
    ssh $VPS_USER@$VPS_HOST "
        cd $VPS_PATH
        python3 -c 'import app; print(\"✅ Flask app imports successfully\")' 2>/dev/null || echo '⚠️  Check Python dependencies'
    "
fi