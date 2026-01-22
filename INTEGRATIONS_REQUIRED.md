# Integrations Required (LUX Marketing)

This map lists integrations required to unlock full analytics and agent insights.

## Commerce & Revenue
- **WooCommerce**
  - Metrics unlocked: Orders, revenue, AOV, refund rate, ROAS attribution.
  - Tokens: `WOOCOMMERCE_URL`, `WOOCOMMERCE_KEY`, `WOOCOMMERCE_SECRET`.
  - Webhooks: order.created, order.updated.
  - Retention: order records retained for accounting compliance.
- **Stripe**
  - Metrics unlocked: Payments, subscription MRR, chargebacks.
  - Tokens: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`.
  - Webhooks: charge.succeeded, invoice.payment_succeeded.

## Messaging
- **Email provider (SMTP/Mailgun)**
  - Metrics unlocked: delivery, open, click, bounce, unsubscribe.
  - Tokens: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS` or Mailgun keys.
  - Webhooks: delivery, open, click, unsub, spam complaints.
- **SMS (Twilio)**
  - Metrics unlocked: delivery, opt-out, click/response rates.
  - Tokens: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.
  - Webhooks: message status callbacks.

## Ads & Acquisition
- **Google Ads**
  - Metrics unlocked: spend, clicks, ROAS.
  - Tokens: `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_DEVELOPER_TOKEN`.
- **Meta Ads**
  - Metrics unlocked: spend, CAC, creative performance.
  - Tokens: `FACEBOOK_ACCESS_TOKEN`.
- **TikTok Ads**
  - Metrics unlocked: spend, CTR, creative fatigue.
  - Tokens: `TIKTOK_ACCESS_TOKEN` (placeholder).

## Organic Search
- **Google Search Console**
  - Metrics unlocked: impressions, clicks, queries, AI overviews exposure (where available).
  - Tokens: `GSC_CLIENT_ID`, `GSC_CLIENT_SECRET` (placeholder).

## Social
- **Meta Graph (IG/FB)**
  - Metrics unlocked: reach, engagement, follower growth.
  - Tokens: `FACEBOOK_ACCESS_TOKEN`.
- **X/Twitter**
  - Metrics unlocked: impressions, engagement.
  - Tokens: `TWITTER_BEARER_TOKEN` (placeholder).

## Compliance Notes
- No PII stored in analytics exports by default.
- Consent + GPC must be honored before event capture.
- IPs are hashed before storage.
