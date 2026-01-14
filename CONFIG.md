# Configuration Overview

This document lists required and optional environment variables used by the app.
Optional integrations are disabled gracefully when credentials are missing.

## Required

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy database connection string. |
| `SESSION_SECRET` (or `SECRET_KEY`) | Flask session secret. |

## Optional (Feature-Dependent)

| Feature | Variables | Notes |
| --- | --- | --- |
| OpenAI (AI content) | `OPENAI_API_KEY` | Enables AI features; disabled when missing. |
| Replit Auth | `REPL_ID`, `ISSUER_URL` | Replit OAuth is disabled without `REPL_ID`. |
| TikTok OAuth | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | TikTok integration disabled without both. |
| Microsoft Graph Email | `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT_ID`, `MS_FROM_EMAIL` | Email via Microsoft Graph; SMTP fallback available. |
| SMTP fallback | `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Used only if configured. |
| Twilio SMS | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` | SMS disabled without all three. |
| Stripe | `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | Payments for events. |
| WooCommerce | `WC_STORE_URL`, `WC_CONSUMER_KEY`, `WC_CONSUMER_SECRET` | WooCommerce integrations. |
| GA4 | `GA4_PROPERTY_ID`, `GOOGLE_APPLICATION_CREDENTIALS` | Analytics integration. |
| Mailgun | `MAILGUN_API_KEY`, `MAILGUN_DOMAIN`, `MAILGUN_FROM` | Optional email provider. |
| Bitly | `BITLY_ACCESS_TOKEN` | Optional URL shortening. |
| Unsplash | `UNSPLASH_ACCESS_KEY` | Optional image search. |
| Pexels | `PEXELS_API_KEY` | Optional image search. |
| Ad networks | `EXOCLICK_API_BASE`, `EXOCLICK_API_TOKEN`, `CLICKADILLA_TOKEN`, `TUBECORPORATE_*` | Optional ad integrations. |
