# LUX IT - VPS Deployment Guide

## Prerequisites

- Ubuntu 20.04+ or Debian 11+ VPS
- Python 3.11+
- PostgreSQL 14+
- Nginx (for reverse proxy)
- Domain name pointed to your VPS IP

## Quick Deployment Steps

### 1. Server Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install dependencies
sudo apt install python3.11 python3.11-venv python3-pip postgresql nginx certbot python3-certbot-nginx -y
```

### 2. Clone Repository

```bash
cd /var/www
git clone <your-repo-url> luxit
cd luxit
```

### 3. Create Virtual Environment

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. PostgreSQL Setup

```bash
sudo -u postgres psql

# In PostgreSQL shell:
CREATE DATABASE luxit_prod;
CREATE USER luxit_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE luxit_prod TO luxit_user;
\q
```

### 5. Environment Variables

Create `/var/www/luxit/.env`:

```bash
DATABASE_URL=postgresql://luxit_user:your_secure_password@localhost/luxit_prod
SESSION_SECRET=generate_a_long_random_string_here
FLASK_ENV=production
```

### 6. Initialize Database

```bash
source venv/bin/activate
python -c "
from app import app
from extensions import db
from models import User, Company, UserCompanyAccess
from werkzeug.security import generate_password_hash

with app.app_context():
    db.create_all()
    admin = User(
        username='admin',
        email='admin@yourdomain.com',
        password_hash=generate_password_hash('change_this_password'),
        is_admin=True
    )
    db.session.add(admin)
    company = Company(name='Your Company')
    db.session.add(company)
    db.session.commit()
    access = UserCompanyAccess(user_id=admin.id, company_id=company.id, role='owner', is_default=True)
    db.session.add(access)
    admin.default_company_id = company.id
    db.session.commit()
    print('Setup complete!')
"
```

### 7. Systemd Service

Create `/etc/systemd/system/luxit.service`:

```ini
[Unit]
Description=LUX IT Gunicorn Application
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/luxit
Environment="PATH=/var/www/luxit/venv/bin"
EnvironmentFile=/var/www/luxit/.env
ExecStart=/var/www/luxit/venv/bin/gunicorn --workers 4 --bind unix:luxit.sock -m 007 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable luxit
sudo systemctl start luxit
```

### 8. Nginx Configuration

Create `/etc/nginx/sites-available/luxit`:

```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    location / {
        include proxy_params;
        proxy_pass http://unix:/var/www/luxit/luxit.sock;
    }

    location /static {
        alias /var/www/luxit/static;
        expires 30d;
    }
}
```

Enable site:

```bash
sudo ln -s /etc/nginx/sites-available/luxit /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 9. SSL Certificate

```bash
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

## Maintenance Commands

```bash
# View logs
sudo journalctl -u luxit -f

# Restart application
sudo systemctl restart luxit

# Update application
cd /var/www/luxit
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart luxit
```

## Environment Variables Reference

| Variable | Description | Example |
|----------|-------------|---------|
| DATABASE_URL | PostgreSQL connection string | postgresql://user:pass@host/db |
| SESSION_SECRET | Flask session secret key | long_random_string |
| OPENAI_API_KEY | OpenAI API key for AI features | sk-... |
| TWILIO_ACCOUNT_SID | Twilio for SMS | AC... |
| TWILIO_AUTH_TOKEN | Twilio auth token | ... |

## Admin Login

After deployment, login at `https://yourdomain.com/auth/login` with the admin credentials you set during setup.
