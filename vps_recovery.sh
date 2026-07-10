
#!/bin/bash

# LUX Marketing VPS Recovery Script
# Fixes "Bad Gateway" errors

VPS_HOST="194.195.92.52"
VPS_USER="root"
APP_PATH="/var/www/lux-marketing"

echo "🔧 LUX Marketing VPS Recovery"
echo "======"

echo "📋 Step 1: Checking service status..."
ssh $VPS_USER@$VPS_HOST "systemctl status lux-marketing --no-pager -l" || true

echo ""
echo "📋 Step 2: Checking recent errors..."
ssh $VPS_USER@$VPS_HOST "journalctl -u lux-marketing -n 50 --no-pager | tail -20"

echo ""
echo "🛠️ Step 3: Attempting automatic recovery..."
ssh $VPS_USER@$VPS_HOST << 'RECOVERY_SCRIPT'
    set -e
    
    echo "Stopping services..."
    systemctl stop lux-marketing 2>/dev/null || true
    
    echo "Checking for port conflicts..."
    if lsof -ti:5000 >/dev/null 2>&1; then
        echo "Port 5000 in use, killing process..."
        kill -9 $(lsof -ti:5000) 2>/dev/null || true
    fi
    
    echo "Verifying application files..."
    cd /var/www/lux-marketing
    
    if [ ! -f "main.py" ]; then
        echo "ERROR: main.py not found!"
        exit 1
    fi
    
    echo "Testing Python imports..."
    source venv/bin/activate
    python3 -c "import app; print('✅ App imports successfully')" || {
        echo "ERROR: App import failed"
        exit 1
    }
    
    echo "Checking database connection..."
    python3 -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('✅ Database connected')
" || echo "⚠️ Database issue detected"
    
    deactivate
    
    echo "Setting proper permissions..."
    chown -R luxapp:www-data /var/www/lux-marketing
    chmod -R 755 /var/www/lux-marketing
    
    echo "Starting service..."
    systemctl daemon-reload
    systemctl start lux-marketing
    
    sleep 3
    
    echo "Checking service status..."
    systemctl status lux-marketing --no-pager -l | head -20
    
    echo ""
    echo "Testing local endpoint..."
    curl -s http://localhost:5000 >/dev/null && echo "✅ App responding on port 5000" || echo "❌ App not responding"
    
    echo ""
    echo "Restarting Nginx..."
    systemctl restart nginx
    
    echo "✅ Recovery complete!"
RECOVERY_SCRIPT

echo ""
echo "🧪 Step 4: Testing deployment..."
sleep 2

ssh $VPS_USER@$VPS_HOST "curl -s -o /dev/null -w 'HTTP Status: %{http_code}\n' http://localhost:5000"

echo ""
echo "🌐 Testing public domain..."
curl -s -o /dev/null -w "HTTP Status: %{http_code}\n" https://lux.lucifercruz.com || echo "Still having issues"

echo ""
echo "📊 Current Status:"
ssh $VPS_USER@$VPS_HOST "systemctl is-active lux-marketing && echo 'Service: ✅ Running' || echo 'Service: ❌ Stopped'"
ssh $VPS_USER@$VPS_HOST "systemctl is-active nginx && echo 'Nginx: ✅ Running' || echo 'Nginx: ❌ Stopped'"

echo ""
echo "If still having issues, check logs with:"
echo "  ssh root@194.195.92.52"
echo "  journalctl -u lux-marketing -f"
