# LUX Marketing Automation Platform

A comprehensive email marketing automation platform built with Flask, featuring multi-channel marketing capabilities including email, SMS, social media, events management, and AI-powered content generation.

**Current Version:** 3.8.7

## 🚀 Features

### Email Marketing
- **Campaign Management**: Create, schedule, and send email campaigns to segmented audiences
- **Drag & Drop Editor**: Visual email template builder with customizable components
- **A/B Testing**: Test different email variants to optimize engagement
- **Automation Workflows**: Trigger-based email sequences (welcome series, abandoned cart, etc.)
- **Analytics**: Track opens, clicks, conversions, and engagement metrics
- **Template Library**: Reusable email templates with personalization support

### Multi-Channel Marketing
- **SMS Marketing**: Send SMS campaigns via Twilio with compliance features
- **Social Media**: Schedule posts across Facebook, Instagram, LinkedIn, and Twitter
- **SEO Tools**: Analyze websites for SEO optimization opportunities
- **Events Management**: Create and manage events with registration and payment processing

### Contact Management
- **Contact Database**: Store and organize contacts with custom fields
- **Segmentation**: Create dynamic segments based on behavior and attributes
- **Web Forms**: Capture leads with customizable embedded forms
- **Landing Pages**: Build conversion-optimized landing pages
- **Polls & Surveys**: Collect feedback and insights from your audience

### AI-Powered Features
- **LUX AI Agent**: GPT-4o powered marketing assistant for content generation
- **Campaign Optimization**: AI-driven recommendations for improving campaigns
- **Content Generation**: Automated email and social media content creation
- **Image Generation**: DALL-E integration for creating campaign visuals

### Advanced Features
- **Brand Kit**: Centralized brand assets and guidelines
- **Newsletter Archive**: Public archive of past newsletters
- **Email Tracking**: Open and click tracking with detailed analytics
- **Revenue Attribution**: Track sales and revenue from campaigns

### First-Party Analytics & Agentic Insights
- **First-party analytics**: `/e` endpoint with consent/GPC enforcement and append-only events
- **Analytics exports**: CSV/Excel/PDF + printable reports
- **Agentic department**: Executive strategy reports, action proposals, and approval workflow scaffolding

#### Sample `/e` Payload
```json
{
  "company_id": 1,
  "event_name": "page_view",
  "consent": true,
  "session_id": "sess_123",
  "page_url": "https://luxit.app/dashboard",
  "referrer": "https://luxit.app",
  "utm_source": "newsletter",
  "utm_medium": "email",
  "utm_campaign": "launch",
  "device_type": "mobile",
  "viewport_width": 390,
  "orientation": "portrait"
}
```

## 📋 Requirements

- Python 3.11+
- PostgreSQL (or SQLite for development)
- Redis (optional, for caching)

## 🌐 Domain Architecture
- Marketing site: `https://luxit.app`
- Web app: `https://app.luxit.app`
- API: `https://api.luxit.app`

### External Services (Optional)
- **Microsoft Graph API**: For email sending (requires Azure app registration)
- **Twilio**: For SMS marketing (requires account SID, auth token, phone number)
- **OpenAI**: For AI features (requires API key)
- **Stripe**: For paid event registration (requires secret key)

## 🛠️ Installation

### 1. Clone the Repository
```bash
git clone <repository-url>
cd lux-marketing
```

### 2. Set Up Python Environment
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Create a `.env` file or set environment variables:

```bash
# Database
DATABASE_URL=postgresql://user:password@localhost/lux_marketing

# Session Security
SESSION_SECRET=your-secret-key-here

# Microsoft Graph (Email)
GRAPH_CLIENT_ID=your-client-id
GRAPH_CLIENT_SECRET=your-client-secret
GRAPH_TENANT_ID=your-tenant-id

# OpenAI (AI Features)
OPENAI_API_KEY=your-openai-api-key

# Twilio (SMS Marketing) - Optional
TWILIO_ACCOUNT_SID=your-account-sid
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_PHONE_NUMBER=+1234567890

# Stripe (Paid Events) - Optional
STRIPE_SECRET_KEY=your-stripe-secret-key
```

### 4. Initialize Database
```bash
python
>>> from app import app, db
>>> with app.app_context():
...     db.create_all()
>>> exit()
```

### 5. Create Admin User
```bash
python
>>> from app import app, db
>>> from models import User
>>> from werkzeug.security import generate_password_hash
>>> with app.app_context():
...     admin = User(username='admin', email='admin@example.com', 
...                  password_hash=generate_password_hash('changeme'), 
...                  is_admin=True)
...     db.session.add(admin)
...     db.session.commit()
>>> exit()
```

### 6. Run the Application
```bash
# Development
flask run

# Production (using Gunicorn)
gunicorn --bind 0.0.0.0:5000 --workers 4 main:app
```

The application will be available at `http://localhost:5000`

## 🔧 Configuration

### Microsoft Graph API Setup
1. Register an app in Azure Portal
2. Add Mail.Send application permission
3. Generate a client secret
4. Add credentials to environment variables

### Twilio SMS Setup
1. Create a Twilio account
2. Get a phone number
3. Copy Account SID and Auth Token
4. Add credentials to environment variables

### OpenAI Setup
1. Create an OpenAI account
2. Generate an API key
3. Add to environment variables

## 📱 Usage

### Creating Email Campaigns
1. Navigate to **Email Marketing > Campaigns**
2. Click **Create Campaign**
3. Select a template or use the drag & drop editor
4. Choose recipient segments or tags
5. Schedule or send immediately

### SMS Marketing
1. Navigate to **Multi-Channel > SMS Campaigns**
2. Click **Create SMS Campaign**
3. Write your message (160 characters max)
4. Select target contacts with phone numbers
5. Send or schedule

### A/B Testing
1. Navigate to **Email Marketing > A/B Testing**
2. Click **Create A/B Test**
3. Create two email variants
4. Set test parameters (sample size, duration)
5. Launch test and review results

### Automation Workflows
1. Navigate to **Email Marketing > Automation**
2. Click **Create Automation**
3. Choose a trigger (signup, purchase, etc.)
4. Add workflow steps (emails, delays, conditions)
5. Activate automation

### Events Management
1. Navigate to **Multi-Channel > Events**
2. Click **Create Event**
3. Set date, location, and pricing
4. Share registration link
5. Track attendees and payments

## 🎨 Customization

### Brand Kit
Configure your brand assets in **Tools > Brand Kit**:
- Logo and brand colors
- Email signature
- Social media handles
- Brand guidelines

### Email Templates
Create reusable templates:
1. Navigate to **Email Marketing > Templates**
2. Use the drag & drop editor
3. Save as template for future campaigns

## 📊 Analytics

View performance metrics in **Analytics**:
- Email deliverability rates
- Open and click-through rates
- Conversion tracking
- Revenue attribution
- Engagement trends

## 🔒 Security Features

- Password hashing with Werkzeug
- CSRF protection on all forms
- SQL injection prevention via SQLAlchemy ORM
- XSS protection with Jinja2 auto-escaping
- SSRF protection in SEO tools
- Secure session management

## 🚀 Production Deployment

### Using Nginx + Gunicorn
```nginx
server {
    listen 80;
    server_name your-domain.com;
    
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Using Systemd
Create `/etc/systemd/system/lux-marketing.service`:
```ini
[Unit]
Description=LUX Marketing Platform
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/lux-marketing
Environment="PATH=/var/www/lux-marketing/venv/bin"
EnvironmentFile=/etc/lux/lux-marketing.env
ExecStart=/var/www/lux-marketing/venv/bin/gunicorn --bind 0.0.0.0:5000 --workers 4 main:app

[Install]
WantedBy=multi-user.target
```

## 📝 API Documentation

### Email Service
```python
from email_service import EmailService

service = EmailService()
result = service.send_email(
    to_email='user@example.com',
    subject='Welcome!',
    html_content='<h1>Welcome to our platform</h1>'
)
```

### SMS Service
```python
from sms_service import sms_service

result = sms_service.send_sms(
    to_number='+1234567890',
    message='Your verification code is 123456'
)
```

### SEO Service
```python
from seo_service import seo_service

analysis = seo_service.analyze_page('https://example.com')
if analysis['success']:
    print(analysis['data']['recommendations'])
```

## 🤝 Contributing

Contributions are welcome! Please follow these guidelines:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📄 License

This project is proprietary software. All rights reserved.

## 🆘 Support

For support, please contact the development team or refer to the internal documentation.

## 🎯 Roadmap

- [ ] WhatsApp integration
- [ ] SMS two-way conversations
- [ ] Advanced reporting dashboard
- [ ] Mobile app
- [ ] API webhooks
- [ ] Multi-language support

## 📚 Additional Resources

- [Flask Documentation](https://flask.palletsprojects.com/)
- [SQLAlchemy Documentation](https://www.sqlalchemy.org/)
- [Microsoft Graph API](https://docs.microsoft.com/en-us/graph/)
- [Twilio SMS API](https://www.twilio.com/docs/sms)
- [OpenAI API](https://platform.openai.com/docs/)

---

Built with ❤️ using Flask, PostgreSQL, and modern web technologies.
# LUX-Marketing
