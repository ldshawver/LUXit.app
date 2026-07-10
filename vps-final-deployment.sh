#!/bin/bash
# LUX Email Marketing Bot - Final VPS Deployment with OpenAI API Key
# Run this on your local machine to upload and deploy to your VPS

set -e  # Exit on any error

# Configuration
VPS_IP="194.195.92.52"
VPS_USER="root"
DOMAIN="lux.lucifercruz.com"
APP_DIR="/root/lux-email-bot/LUX-Email-Marketing-Bot"
SERVICE_NAME="lux"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 LUX Email Marketing Bot - Final Deployment${NC}"
echo "======"
echo "Target VPS: $VPS_IP"
echo "Domain: $DOMAIN"
echo "App Directory: $APP_DIR"
echo ""

# Check if the final package exists
if [ ! -f "lux-email-app-final.tar.gz" ]; then
    echo -e "${RED}❌ Error: lux-email-app-final.tar.gz not found!${NC}"
    echo "Please ensure you have downloaded the final package from your Replit project."
    exit 1
fi

# Prompt for OpenAI API key
echo -e "${YELLOW}🤖 OpenAI API Key Setup${NC}"
echo "Your LUX AI agent needs an OpenAI API key to function."
echo "Please enter your OpenAI API key (starts with 'sk-'):"
read -s OPENAI_API_KEY

if [[ ! $OPENAI_API_KEY =~ ^sk- ]]; then
    echo -e "${RED}❌ Invalid API key format. It should start with 'sk-'${NC}"
    exit 1
fi

echo -e "${GREEN}✅ API key accepted${NC}"
echo ""

echo -e "${YELLOW}📁 Uploading final application files...${NC}"
scp lux-email-app-final.tar.gz $VPS_USER@$VPS_IP:/tmp/

echo -e "${YELLOW}🔧 Connecting to VPS and deploying...${NC}"
ssh $VPS_USER@$VPS_IP << EOF
    set -e
    
    echo "Stopping LUX service..."
    systemctl stop lux || true
    
    echo "Creating backup of current installation..."
    cp -r /root/lux-email-bot /root/lux-email-bot.backup.\$(date +%Y%m%d_%H%M%S) || true
    
    echo "Extracting final application files..."
    cd /root/lux-email-bot
    rm -rf LUX-Email-Marketing-Bot.old || true
    mv LUX-Email-Marketing-Bot LUX-Email-Marketing-Bot.old || true
    mkdir -p LUX-Email-Marketing-Bot
    cd LUX-Email-Marketing-Bot
    tar -xzf /tmp/lux-email-app-final.tar.gz
    
    echo "Setting up environment variables..."
    # Create or update environment file with SQLite configuration
    cat > /root/lux-email-bot/.env << 'ENVEOF'
SESSION_SECRET=\$(openssl rand -hex 32)
OPENAI_API_KEY=$OPENAI_API_KEY
MS_CLIENT_ID=
MS_CLIENT_SECRET=
MS_TENANT_ID=
ENVEOF
    
    echo "Setting correct permissions..."
    chown -R root:root /root/lux-email-bot/LUX-Email-Marketing-Bot || true
    chmod +x wsgi.py || true
    chmod 600 /root/lux-email-bot/.env
    
    echo "Activating virtual environment and installing dependencies..."
    cd /root/lux-email-bot
    source venv/bin/activate
    cd LUX-Email-Marketing-Bot
    
    # Install dependencies (avoiding WooCommerce libraries)
    echo "Uninstalling any existing WooCommerce libraries..."
    pip uninstall -y woocommerce WooCommerce python-woocommerce || true
    
    if [ -f "deploy_requirements.txt" ]; then
        echo "Installing from deploy_requirements.txt..."
        pip install -r deploy_requirements.txt
    else
        echo "Installing core dependencies..."
        pip install Flask==3.0.0 Flask-SQLAlchemy==3.1.1 Flask-Login==0.6.3 gunicorn==21.2.0 psycopg2-binary==2.9.9 APScheduler==3.10.4 msal==1.24.1 openai==1.3.7 requests==2.31.0 email-validator==2.1.0 Jinja2==3.1.2 werkzeug==3.0.1
    fi
    
    echo "Ensuring no WooCommerce libraries are installed..."
    pip list | grep -i woo || echo "No WooCommerce libraries found - good!"
    
    echo "Updating systemd service to use environment file..."
    # Update the systemd service file to include environment file
    cat > /etc/systemd/system/lux.service << 'SERVICEEOF'
[Unit]
Description=Gunicorn instance for LUX Email Bot
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/root/lux-email-bot/LUX-Email-Marketing-Bot
EnvironmentFile=/root/lux-email-bot/.env
ExecStart=/root/lux-email-bot/venv/bin/gunicorn --config gunicorn.conf.py wsgi:app
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF
    
    echo "Reloading systemd and resetting failed attempts..."
    systemctl daemon-reload
    systemctl reset-failed lux || true
    
    echo "Starting LUX service..."
    systemctl start lux
    
    echo "Enabling service for auto-start..."
    systemctl enable lux
    
    echo "Waiting for service to start..."
    sleep 10
    
    echo "Checking service status..."
    systemctl status lux --no-pager -l || true
    
    echo "Testing application..."
    echo "Testing backend health..."
    if timeout 15 curl -sSf http://127.0.0.1:5000/ > /dev/null 2>&1; then
        echo "✅ Backend is responding!"
    else
        echo "❌ Backend not responding, checking logs..."
        journalctl -u lux -n 50 --no-pager
        exit 1
    fi
    
    echo "Testing domain access..."
    if timeout 15 curl -k -sSf https://lux.lucifercruz.com/ > /dev/null 2>&1; then
        echo "✅ Domain is accessible via HTTPS!"
    else
        echo "⚠️  Domain not accessible via HTTPS (checking nginx)"
        systemctl status nginx --no-pager || true
    fi
    
    echo "Cleaning up..."
    rm -f /tmp/lux-email-app-final.tar.gz
    
EOF

echo ""
echo -e "${GREEN}🎉 DEPLOYMENT SUCCESSFUL!${NC}"
echo ""
echo -e "${BLUE}🌐 Your LUX Email Marketing app is now live at:${NC}"
echo "   https://$DOMAIN"
echo ""
echo -e "${BLUE}🔐 Login credentials:${NC}"
echo "   Username: admin"
echo "   Password: admin123"
echo ""
echo -e "${BLUE}✨ LUX AI Features Now Available:${NC}"
echo "   • Automated campaign generation"
echo "   • Content optimization"
echo "   • Image creation with DALL-E"
echo "   • Audience analysis"
echo "   • Performance optimization"
echo ""
echo -e "${BLUE}📊 Management commands:${NC}"
echo "   Status: ssh $VPS_USER@$VPS_IP 'systemctl status lux'"
echo "   Logs:   ssh $VPS_USER@$VPS_IP 'journalctl -u lux -f'"
echo "   Restart: ssh $VPS_USER@$VPS_IP 'systemctl restart lux'"
echo ""
echo -e "${YELLOW}🔧 Next Steps:${NC}"
echo "   1. Visit https://$DOMAIN and log in"
echo "   2. Configure Microsoft Graph API for email sending"
echo "   3. Test the LUX AI features in the dashboard"
echo "   4. Import your contact lists"
echo "   5. Create your first marketing campaign!"
echo ""