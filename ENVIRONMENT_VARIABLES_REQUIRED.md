# Environment Variables Required

| Name | Required | Feature | Where Set | Example |
| --- | --- | --- | --- | --- |
| `SESSION_SECRET` | Required | App sessions, hashing salt | `/etc/lux-marketing/lux.env` | `replace-with-long-secret` |
| `DATABASE_URL` | Required | Postgres connection | `/etc/lux-marketing/lux.env` | `postgresql://user:pass@localhost/lux_marketing` |
| `WOOCOMMERCE_URL` | Optional | WooCommerce sync | `/etc/lux-marketing/lux.env` | `https://shop.example.com` |
| `WOOCOMMERCE_KEY` | Optional | WooCommerce sync | `/etc/lux-marketing/lux.env` | `ck_...` |
| `WOOCOMMERCE_SECRET` | Optional | WooCommerce sync | `/etc/lux-marketing/lux.env` | `cs_...` |
| `STRIPE_SECRET_KEY` | Optional | Stripe sync | `/etc/lux-marketing/lux.env` | `sk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | Optional | Stripe webhooks | `/etc/lux-marketing/lux.env` | `whsec_...` |
| `SMTP_HOST` | Optional | Email sending/analytics | `/etc/lux-marketing/lux.env` | `smtp.mailgun.org` |
| `SMTP_USER` | Optional | Email sending/analytics | `/etc/lux-marketing/lux.env` | `postmaster@example.com` |
| `SMTP_PASS` | Optional | Email sending/analytics | `/etc/lux-marketing/lux.env` | `password` |
| `MAILGUN_API_KEY` | Optional | Email analytics | `/etc/lux-marketing/lux.env` | `key-...` |
| `TWILIO_ACCOUNT_SID` | Optional | SMS analytics | `/etc/lux-marketing/lux.env` | `AC...` |
| `TWILIO_AUTH_TOKEN` | Optional | SMS analytics | `/etc/lux-marketing/lux.env` | `...` |
| `FACEBOOK_ACCESS_TOKEN` | Optional | Meta ads/social insights | `/etc/lux-marketing/lux.env` | `EAAB...` |
| `GOOGLE_ADS_CLIENT_ID` | Optional | Google Ads reporting | GitHub secrets | `...apps.googleusercontent.com` |
| `GOOGLE_ADS_CLIENT_SECRET` | Optional | Google Ads reporting | GitHub secrets | `...` |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Optional | Google Ads reporting | GitHub secrets | `...` |
| `GSC_CLIENT_ID` | Optional | Search Console | GitHub secrets | `...apps.googleusercontent.com` |
| `GSC_CLIENT_SECRET` | Optional | Search Console | GitHub secrets | `...` |
| `TWITTER_BEARER_TOKEN` | Optional | X/Twitter insights | GitHub secrets | `...` |
| `TIKTOK_ACCESS_TOKEN` | Optional | TikTok Ads insights | GitHub secrets | `...` |
