# LUX Marketing Platform - Project Documentation

## Overview
LUX Marketing is a multi-channel marketing automation platform designed to streamline marketing efforts. It features a tile-based dashboard with 11 AI agents, AI-powered campaign generation using GPT-4o, and seamless contact capture via Zapier webhooks. The platform integrates an advanced error logging and diagnostics system and an AI chatbot with auto-repair capabilities. Its core ambition is to provide a launch-ready, automated marketing solution with centralized management for all integrations and API keys, alongside robust social media publishing and management.

## User Preferences
- Black background with purple, cyan, pink branding
- Launch-ready with all features on Replit and VPS
- Automated systems that work without manual intervention
- Clear error diagnostics and auto-repair capabilities
- Centralized API keys & secrets management (not scattered modal dialogs)
- All social media platforms available in Settings → Integrations for all companies

## System Architecture
The platform is built around a tile-based dashboard with 11 AI agents and leverages GPT-4o for AI-powered campaign generation. The UI/UX features a pure black background accented with brand colors (purple, cyan, pink). Authentication is handled via Replit Auth OAuth, supporting Google, GitHub, Apple, and email sign-in, with JWKS-based JWT signature verification.

Key technical implementations and features include:
- **Admin Approval Queue System**: Mandatory approval workflow for ALL marketing content (AI-generated and manual). Features ApprovalQueue model with status workflow (pending→approved/rejected→published), FeatureToggle model with 27 safe-off defaults, ApprovalService for submit/approve/reject/edit/cancel operations, and ApprovalAuditLog for immutable action tracking. AI agents (SocialMediaAgent, EmailCRMAgent) route content through submit_for_approval() instead of direct execution. Publishing routes verify approved queue items before dispatching. Includes emergency stop capability to halt all automation.
- **Social Media Integrations**: Full OAuth 2.0 implementations for platforms like Instagram and TikTok, supporting content publishing, media listing, and insights, including secure token management. Integrations also exist for Facebook, Reddit, YouTube, LinkedIn, Snapchat, and X/Twitter.
- **Centralized API Keys & Secrets Management**: A dedicated section in Settings → Integrations for secure, encrypted storage and management of API keys and secrets per company.
- **Zapier Webhook Integration**: An authenticated endpoint (`/api/webhook/zapier-contact`) for contact capture and auto-segmentation.
- **Error Logging & Diagnostics**: A robust system logs all application errors to a `error_log` database table, providing detailed diagnostics and system health information.
- **AI Chatbot with Auto-Repair**: Functions as a marketing assistant and troubleshooter, capable of analyzing server logs and triggering an automated error repair system using ChatGPT to generate and test fix plans.
- **Keyword Research Integrations**: Multi-provider keyword research supporting DataForSEO, SEMrush, and Moz, with dedicated API endpoints.
- **Event Integrations**: Multi-provider event search supporting Eventbrite and Ticketmaster, with dedicated API endpoints.
- **Customer Profile & Engagement Tracking**: Comprehensive customer management with editable profiles, lead scoring, activity timelines, and quick-action buttons for logging interactions.
- **CRM Hub Transformation**: An action-oriented coaching system with pipeline stages, "Next Actions" coaching widgets, and activity-based metrics.
- **Comprehensive Analytics Hub**: An analytics system providing 10 metric categories (Acquisition, Conversion, Revenue, CAC, Retention, Engagement, Attribution, Segments, Campaigns, Compliance) with Chart.js visualizations and a dark theme UI.
- **Contact-Subscriber Sync**: Bidirectional synchronization between contacts and newsletter subscribers, including automatic daily sync via an AI agent.
- **Automation Trigger Library**: An expanded library of 25+ automation templates across various marketing categories (Ecommerce, Engagement, Nurture, Retention, SMS, Social) with full CRUD API support.
- **SMS Service Integration**: Full Twilio integration for SMS campaign creation, bulk sending, AI-powered content generation, and compliance checking.
- **Marketing Calendar**: Full FullCalendar.js integration with drag-and-drop rescheduling, click-to-edit events, type filtering, and an upcoming 30-day view.
- **AI Auto-Generate for Campaigns**: AI-powered subject line generation for campaigns with CSRF protection and graceful fallback.
- **Social Media Post Creation**: Enhanced features including advanced image handling (stock search via Unsplash/Pexels, upload, URL import, AI generation), AI-powered hashtag generation, and URL shortening.
- **TikTok Pixel Integration**: Automatic injection of TikTok Pixel into all pages for tracking.
- **Configuration Status Service**: Enhanced service to check and differentiate between missing API credentials and disconnected OAuth sessions for various integrations.

## External Dependencies
### Core Integrations
- **OpenAI API**: AI-powered campaign generation, content optimization, and diagnostics.
- **Microsoft Graph API**: Microsoft 365 email delivery service (OAuth 2.0).
- **Zapier**: Contact capture and automation via webhooks.
- **Replit Auth (OpenID Connect)**: OAuth authentication (Google, GitHub, Apple, email).
- **Instagram Graph API**: OAuth integration, content publishing, insights.
- **TikTok API**: OAuth integration, user info, video listing, upload, publishing.
- **Facebook API**: Facebook Page integration.
- **Reddit API**: Reddit integration.
- **YouTube Data API**: YouTube integration.
- **LinkedIn API**: LinkedIn integration.
- **Snapchat API**: Snapchat integration.
- **X (formerly Twitter) API**: Twitter integration.
- **DataForSEO API**: Keyword research data.
- **SEMrush API**: Premium keyword and competitor data.
- **Moz API**: Domain authority and keyword difficulty.
- **Eventbrite API**: Local events, ticketing, categories.
- **Ticketmaster Discovery API**: Concerts, sports, theater, attractions.
- **Twilio API**: SMS service integration.
- **Unsplash API**: Stock image search.
- **Pexels API**: Alternative stock image search.
- **TinyURL/Bitly API**: URL shortening.

### Framework & Data Layer
- **Flask**: Web framework with SQLAlchemy, Login, and other extensions.
- **APScheduler**: Background task scheduling for automated campaigns.
- **SQLAlchemy**: ORM supporting multiple database backends.
- **Database**: SQLite for development; PostgreSQL/MySQL in production.

### Frontend Dependencies
- **Bootstrap 5**: Dark theme UI framework.
- **Feather Icons**: Lightweight icon set.
- **Custom CSS/JS**: Glassmorphism UI and interaction helpers.
