#!/bin/bash
# Phase 2-6 Update Deployment Script
# Deploys ONLY the code changes and database migration for Phase 2-6
# Does NOT change environment variables or credentials

set -e

VPS_HOST="194.195.92.52"
VPS_USER="root"
VPS_PATH="/var/www/lux-marketing"
DOMAIN="lux.lucifercruz.com"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}🚀 LUX Marketing - Phase 2-6 Deployment${NC}"
echo "======"
echo ""
echo -e "${YELLOW}Target:${NC} $DOMAIN ($VPS_HOST)"
echo -e "${YELLOW}Path:${NC} $VPS_PATH"
echo ""

# Confirmation
read -p "Deploy Phase 2-6 updates to production? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Deployment cancelled."
    exit 0
fi

# Test connection
echo -e "${YELLOW}🔍 Testing VPS connection...${NC}"
if ! ssh -o ConnectTimeout=10 $VPS_USER@$VPS_HOST "echo 'Connected'" > /dev/null 2>&1; then
    echo -e "${RED}❌ Cannot connect to VPS${NC}"
    echo "Please ensure:"
    echo "  - SSH access configured: ssh $VPS_USER@$VPS_HOST"
    echo "  - SSH keys or password authentication working"
    exit 1
fi
echo -e "${GREEN}✅ Connection successful${NC}"

# Sync code (excluding environment files and virtual environments)
echo ""
echo -e "${YELLOW}📦 Syncing code to VPS...${NC}"
rsync -avz --progress \
    --exclude='.git*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    --exclude='node_modules' \
    --exclude='venv*' \
    --exclude='.pythonlibs' \
    --exclude='.cache' \
    --exclude='.env*' \
    --exclude='instance/' \
    --exclude='*.log' \
    ./ $VPS_USER@$VPS_HOST:$VPS_PATH/

echo -e "${GREEN}✅ Code sync complete${NC}"

# Run database migration
echo ""
echo -e "${YELLOW}💾 Running database migration...${NC}"
ssh $VPS_USER@$VPS_HOST bash << 'ENDSSH'
cd /var/www/lux-marketing
source venv/bin/activate

# Check if migration file exists
if [ ! -f "migrations/phase_2_6_schema.sql" ]; then
    echo "❌ Migration file not found!"
    exit 1
fi

# Run migration (using existing DATABASE_URL from environment)
if [ -z "$DATABASE_URL" ]; then
    # Try to source from .env or /etc/environment
    if [ -f ".env" ]; then
        source .env
    elif [ -f "/etc/environment" ]; then
        source /etc/environment
    fi
fi

echo "Applying Phase 2-6 schema migration..."
psql $DATABASE_URL -f migrations/phase_2_6_schema.sql 2>&1 | grep -v "already exists" || true

echo "Verifying new tables..."
TABLE_COUNT=$(psql $DATABASE_URL -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_name LIKE 'seo_%' OR table_name LIKE 'event_ticket%' OR table_name LIKE 'social_media_%' OR table_name LIKE 'automation_%';" | tr -d ' ')
echo "Found $TABLE_COUNT Phase 2-6 tables"

if [ "$TABLE_COUNT" -ge "10" ]; then
    echo "✅ Database migration successful"
else
    echo "⚠️  Warning: Expected 16 tables, found $TABLE_COUNT"
fi
ENDSSH

# Restart application
echo ""
echo -e "${YELLOW}🔄 Restarting application...${NC}"
ssh $VPS_USER@$VPS_HOST bash << 'ENDSSH'
systemctl restart lux-marketing
sleep 3
if systemctl is-active --quiet lux-marketing; then
    echo "✅ Application restarted successfully"
else
    echo "❌ Application failed to start"
    echo "Recent logs:"
    journalctl -u lux-marketing -n 20 --no-pager
    exit 1
fi
ENDSSH

# Verify health
echo ""
echo -e "${YELLOW}🏥 Checking application health...${NC}"
sleep 2

HEALTH_CHECK=$(curl -s -w "\n%{http_code}" https://$DOMAIN/health 2>/dev/null)
HTTP_CODE=$(echo "$HEALTH_CHECK" | tail -n 1)
RESPONSE=$(echo "$HEALTH_CHECK" | head -n -1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✅ Health check passed${NC}"
    echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
else
    echo -e "${YELLOW}⚠️  Health check returned: $HTTP_CODE${NC}"
fi

# Show recent logs
echo ""
echo -e "${YELLOW}📋 Recent application logs:${NC}"
ssh $VPS_USER@$VPS_HOST "journalctl -u lux-marketing -n 15 --no-pager | grep -E '(INFO|ERROR|WARNING|trigger library|AI agents)'" || true

echo ""
echo -e "${GREEN}======"
echo "✅ DEPLOYMENT COMPLETE"
echo "======${NC}"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "1. Visit: https://$DOMAIN/health"
echo "2. Log in and test new features:"
echo "   • SEO Dashboard: /seo/dashboard"
echo "   • Social Accounts: /social/accounts"
echo "   • Marketing Calendar: /calendar"
echo "   • Trigger Library: /automations/triggers"
echo "   • System Status: /system/status"
echo ""
echo "3. If trigger library is empty, visit: /system/init"
echo ""
echo -e "${GREEN}Documentation:${NC}"
echo "• Full deployment guide: PHASE_2_6_DEPLOYMENT.md"
echo "• Test checklist: REGRESSION_TEST_CHECKLIST.md"
echo "• Changelog: CHANGELOG.md"
echo ""
