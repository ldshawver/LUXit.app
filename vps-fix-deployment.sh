#!/bin/bash
# Fixed LUX Email Marketing App VPS Deployment Script
# Run this on your local machine to upload and fix your VPS

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
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 LUX Email Marketing Bot - VPS Fix Deployment${NC}"
echo "======"
echo "Target VPS: $VPS_IP"
echo "Domain: $DOMAIN"
echo "App Directory: $APP_DIR"
echo ""

# Check if the fixed package exists
if [ ! -f "lux-email-app-fixed.tar.gz" ]; then
    echo -e "${RED}❌ Error: lux-email-app-fixed.tar.gz not found!${NC}"
    echo "Please ensure you have downloaded the fixed package from your Replit project."
    exit 1
fi

echo -e "${YELLOW}📁 Uploading fixed application files...${NC}"
scp lux-email-app-fixed.tar.gz $VPS_USER@$VPS_IP:/tmp/

echo -e "${YELLOW}🔧 Connecting to VPS and applying fixes...${NC}"
ssh $VPS_USER@$VPS_IP << 'EOF'
    set -e
    
    echo "Stopping LUX service..."
    systemctl stop lux || true
    
    echo "Creating backup of current installation..."
    cp -r /root/lux-email-bot /root/lux-email-bot.backup.$(date +%Y%m%d_%H%M%S) || true
    
    echo "Extracting fixed application files..."
    cd /root/lux-email-bot
    rm -rf LUX-Email-Marketing-Bot.old || true
    mv LUX-Email-Marketing-Bot LUX-Email-Marketing-Bot.old || true
    mkdir -p LUX-Email-Marketing-Bot
    cd LUX-Email-Marketing-Bot
    tar -xzf /tmp/lux-email-app-fixed.tar.gz
    
    echo "Setting correct permissions..."
    chown -R lux-user:lux-user /root/lux-email-bot/LUX-Email-Marketing-Bot || true
    chmod +x wsgi.py || true
    
    echo "Activating virtual environment and installing dependencies..."
    cd /root/lux-email-bot
    source venv/bin/activate
    cd LUX-Email-Marketing-Bot
    pip install -r requirements.txt
    
    echo "Resetting any failed systemd attempts..."
    systemctl reset-failed lux || true
    
    echo "Starting LUX service..."
    systemctl start lux
    
    echo "Enabling service for auto-start..."
    systemctl enable lux
    
    sleep 5
    
    echo "Checking service status..."
    systemctl status lux --no-pager -l
    
    echo "Testing application..."
    echo "Testing backend health..."
    if curl -sSf http://127.0.0.1:5000/ > /dev/null 2>&1; then
        echo "✅ Backend is responding!"
    else
        echo "❌ Backend not responding, checking logs..."
        journalctl -u lux -n 20 --no-pager
        exit 1
    fi
    
    echo "Testing domain access..."
    if curl -k -sSf https://lux.lucifercruz.com/ > /dev/null 2>&1; then
        echo "✅ Domain is accessible via HTTPS!"
    else
        echo "⚠️  Domain not accessible via HTTPS (SSL might need setup)"
    fi
    
    echo "Cleaning up..."
    rm -f /tmp/lux-email-app-fixed.tar.gz
    
EOF

echo ""
echo -e "${GREEN}✅ Deployment completed successfully!${NC}"
echo ""
echo "🌐 Your LUX Email Marketing app should now be accessible at:"
echo "   https://$DOMAIN"
echo ""
echo "📊 To check the application status:"
echo "   ssh $VPS_USER@$VPS_IP 'systemctl status lux'"
echo ""
echo "📱 To view logs:"
echo "   ssh $VPS_USER@$VPS_IP 'journalctl -u lux -f'"
echo ""
echo "🔧 Next steps:"
echo "   1. Visit https://$DOMAIN to test the application"
echo "   2. Log in with: admin / admin123"
echo "   3. Configure your Microsoft Graph API credentials"
echo "   4. Add your OpenAI API key for LUX AI features"
echo ""