# LUX IT - VPS Deployment Guide

Complete step-by-step instructions for deploying LUX IT on a VPS server.

## Prerequisites

- Ubuntu 22.04+ or Debian 12+ VPS
- Root or sudo access
- Domain name pointed to your VPS IP (optional, can use IP directly)

---

## Step 1: System Setup

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install Python with venv support (fixes externally-managed-environment error)
sudo apt install -y python3-full python3-venv python3-pip

# Install other dependencies
sudo apt install -y postgresql postgresql-contrib nginx certbot python3-certbot-nginx git curl
```

---

## Step 2: Clone the Application

```bash
# Create directory and clone repository
sudo mkdir -p /var/www
cd /var/www
sudo git clone https://github.com/ldshawver/LUX-Marketing.git luxit
sudo chown -R $USER:$USER /var/www/luxit
cd /var/www/luxit
```

---

## Step 3: Create Virtual Environment

**Important:** This is required due to PEP 668 (externally-managed-environment protection).

```bash
cd /var/www/luxit

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Upgrade pip first
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt

# Verify gunicorn is installed
gunicorn --version
```

**Note:** You must activate the virtual environment (`source venv/bin/activate`) before running any Python/pip commands.

---

## Step 4: PostgreSQL Database Setup

```bash
# Start PostgreSQL
sudo systemctl start postgresql
sudo systemctl enable postgresql

# Create database and user
sudo -u postgres psql << EOF
CREATE DATABASE luxit_prod;
CREATE USER luxit_user WITH PASSWORD 'YOUR_SECURE_PASSWORD_HERE';
GRANT ALL PRIVILEGES ON DATABASE luxit_prod TO luxit_user;
ALTER DATABASE luxit_prod OWNER TO luxit_user;
\c luxit_prod
GRANT ALL ON SCHEMA public TO luxit_user;
EOF
```

---

## Step 5: Create Environment File

Create `/var/www/luxit/.env`:

```bash
cat > /var/www/luxit/.env << 'EOF'
DATABASE_URL=postgresql://luxit_user:YOUR_SECURE_PASSWORD_HERE@localhost:5432/luxit_prod
SESSION_SECRET=GENERATE_A_LONG_RANDOM_STRING_HERE_AT_LEAST_32_CHARS
FLASK_ENV=production
EOF
```

Generate a random session secret:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Step 6: Initialize Database

```bash
cd /var/www/luxit
source venv/bin/activate

# Load environment variables
export $(cat .env | xargs)

# Initialize database tables and admin user
python3 << 'EOF'
from app import app
from extensions import db
from models import User, Company, UserCompanyAccess
from werkzeug.security import generate_password_hash

with app.app_context():
    db.create_all()
    
    # Check if admin already exists
    existing = User.query.filter_by(username='admin').first()
    if not existing:
        admin = User(
            username='admin',
            email='admin@yourdomain.com',
            password_hash=generate_password_hash('admin123'),
            is_admin=True
        )
        db.session.add(admin)
        company = Company(name='Default Company')
        db.session.add(company)
        db.session.commit()
        
        access = UserCompanyAccess(user_id=admin.id, company_id=company.id, role='owner', is_default=True)
        db.session.add(access)
        admin.default_company_id = company.id
        db.session.commit()
        print('Admin user created: admin / admin123')
        print('CHANGE THIS PASSWORD IMMEDIATELY!')
    else:
        print('Admin user already exists')
EOF
```

---

## Step 7: Test Gunicorn Manually

Before setting up systemd, verify Gunicorn works:

```bash
cd /var/www/luxit
source venv/bin/activate
export $(cat .env | xargs)

# Test run (Ctrl+C to stop)
gunicorn --bind 0.0.0.0:8000 wsgi:app

# In another terminal, test it works:
curl http://127.0.0.1:8000/
```

If you see HTML output, Gunicorn is working correctly.

---

## Step 8: Create Systemd Service

Create `/etc/systemd/system/luxit.service`:

```bash
sudo nano /etc/systemd/system/luxit.service
```

Paste this configuration:

```ini
[Unit]
Description=LUX IT Gunicorn Application
After=network.target postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/luxit
Environment="PATH=/var/www/luxit/venv/bin"
EnvironmentFile=/var/www/luxit/.env
ExecStart=/var/www/luxit/venv/bin/gunicorn --workers 4 --bind 127.0.0.1:8000 --access-logfile /var/log/luxit-access.log --error-logfile /var/log/luxit-error.log wsgi:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Set permissions and start service:

```bash
# Set ownership for www-data
sudo chown -R www-data:www-data /var/www/luxit

# Create log files
sudo touch /var/log/luxit-access.log /var/log/luxit-error.log
sudo chown www-data:www-data /var/log/luxit-*.log

# Reload systemd and start service
sudo systemctl daemon-reload
sudo systemctl enable luxit
sudo systemctl start luxit

# Check status
sudo systemctl status luxit
```

---

## Step 9: Configure Nginx

Create `/etc/nginx/sites-available/luxit`:

```bash
sudo nano /etc/nginx/sites-available/luxit
```

**Option A: Using IP address (no domain)**
```nginx
server {
    listen 80;
    server_name YOUR_VPS_IP;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 300s;
        proxy_read_timeout 300s;
    }

    location /static {
        alias /var/www/luxit/static;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

**Option B: Using domain name**
```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 300s;
        proxy_read_timeout 300s;
    }

    location /static {
        alias /var/www/luxit/static;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

Enable the site:

```bash
# Remove default site
sudo rm -f /etc/nginx/sites-enabled/default

# Enable luxit site
sudo ln -sf /etc/nginx/sites-available/luxit /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Reload nginx
sudo systemctl reload nginx
```

---

## Step 10: SSL Certificate (Optional - for domain only)

```bash
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

---

## Troubleshooting

### 502 Bad Gateway

1. **Check if Gunicorn is running:**
   ```bash
   sudo systemctl status luxit
   ```

2. **Check Gunicorn logs:**
   ```bash
   sudo journalctl -u luxit -n 100 --no-pager
   sudo cat /var/log/luxit-error.log
   ```

3. **Test Gunicorn directly:**
   ```bash
   curl -v http://127.0.0.1:8000/
   ```

4. **Check Nginx error logs:**
   ```bash
   sudo tail -50 /var/log/nginx/error.log
   ```

### gunicorn: command not found

Make sure you're in the virtual environment:
```bash
cd /var/www/luxit
source venv/bin/activate
which gunicorn  # Should show /var/www/luxit/venv/bin/gunicorn
```

### externally-managed-environment Error

Never use `pip install` without a virtual environment on modern Ubuntu/Debian:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Database Connection Issues

1. Check PostgreSQL is running:
   ```bash
   sudo systemctl status postgresql
   ```

2. Test connection:
   ```bash
   psql -U luxit_user -h localhost -d luxit_prod
   ```

---

## Maintenance Commands

```bash
# View live logs
sudo journalctl -u luxit -f

# Restart application
sudo systemctl restart luxit

# Update application
cd /var/www/luxit
sudo -u www-data git pull
sudo -u www-data /var/www/luxit/venv/bin/pip install -r requirements.txt
sudo systemctl restart luxit

# Check service status
sudo systemctl status luxit nginx postgresql
```

---

## Environment Variables Reference

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | PostgreSQL connection string | Yes |
| `SESSION_SECRET` | Flask session secret (32+ chars) | Yes |
| `OPENAI_API_KEY` | OpenAI API key for AI features | Optional |
| `TWILIO_ACCOUNT_SID` | Twilio account SID for SMS | Optional |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | Optional |

---

## Default Login

After deployment, access your site and login:

- **URL:** `http://YOUR_VPS_IP/auth/login` or `https://yourdomain.com/auth/login`
- **Username:** `admin`
- **Password:** `admin123`

**CHANGE THIS PASSWORD IMMEDIATELY after first login!**
