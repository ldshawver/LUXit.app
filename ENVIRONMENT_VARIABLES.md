# LUX Marketing Environment Variables

## Core runtime
These are required for a production deployment.

- `SESSION_SECRET` (or `SECRET_KEY`): Flask session signing key.
- `DATABASE_URL`: SQLAlchemy database connection string.
- `MS_FROM_EMAIL`: From address used for outbound email (defaults to `noreply@luxemail.com`).

## Email delivery (Microsoft Graph)
Required for sending email and password resets.

- `MS_CLIENT_ID`
- `MS_CLIENT_SECRET`
- `MS_TENANT_ID`

## AI features (LUX Agent)
Required for AI campaign generation, content optimization, and image generation.

- `OPENAI_API_KEY`

## SMS (Twilio)
Required for SMS campaigns.

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER`

## Security & encryption
Required for encrypted secrets storage.

- `ENCRYPTION_MASTER_KEY`

## OAuth & Social integrations
Enable platform integrations as needed.

- `FACEBOOK_APP_ID`
- `FACEBOOK_APP_SECRET`
- `FACEBOOK_CLIENT_ID`
- `FACEBOOK_CLIENT_SECRET`
- `INSTAGRAM_CLIENT_ID`
- `INSTAGRAM_CLIENT_SECRET`
- `TIKTOK_CLIENT_KEY`
- `TIKTOK_CLIENT_SECRET`
- `TIKTOK_REDIRECT_URI`
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `LINKEDIN_CLIENT_ID`
- `LINKEDIN_CLIENT_SECRET`
- `SNAPCHAT_CLIENT_ID`
- `SNAPCHAT_CLIENT_SECRET`
- `X_CLIENT_ID`
- `X_CLIENT_SECRET`

## Commerce and billing
- `WC_STORE_URL`
- `WC_CONSUMER_KEY`
- `WC_CONSUMER_SECRET`
- `STRIPE_SECRET_KEY`

## Analytics & tracking
- `GA4_PROPERTY_ID`
- `TIKTOK_PIXEL_ID`
- `FACEBOOK_PIXEL_ID`

## Auth (Replit)
Only needed if Replit Auth is enabled.

- `REPL_ID`
- `REPLIT_CLIENT_ID`
- `REPLIT_CLIENT_SECRET`
- `ISSUER_URL`

## Content & URL helpers
- `UNSPLASH_ACCESS_KEY`
- `PEXELS_API_KEY`
- `BITLY_ACCESS_TOKEN`
