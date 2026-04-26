# LUX Marketing Platform - Project Documentation

## Overview
LUX Marketing is a multi-channel marketing automation platform designed to streamline marketing efforts. It features a tile-based dashboard with 11 AI agents and AI-powered campaign generation using GPT-4o. The platform aims to be a launch-ready, automated marketing solution offering centralized management for all integrations and API keys, robust social media publishing, and comprehensive contact capture. It includes advanced error logging, diagnostics, and an AI chatbot with auto-repair capabilities to ensure seamless operation. The business vision is to provide an all-in-one, automated marketing solution, tapping into the growing demand for AI-driven marketing tools.

## User Preferences
- Black background with purple, cyan, pink branding
- Launch-ready with all features on Replit and VPS
- Automated systems that work without manual intervention
- Clear error diagnostics and auto-repair capabilities
- Centralized API keys & secrets management (not scattered modal dialogs)
- All social media platforms available in Settings → Integrations for all companies

## System Architecture
The platform is built around a tile-based dashboard with 11 AI agents, leveraging GPT-4o for AI-powered campaign generation. The UI/UX features a pure black background accented with brand colors (purple, cyan, pink), utilizing a comprehensive design system with glassmorphism elements and semantic CSS tokens for consistent branding and theming. Authentication is handled via Replit Auth OAuth (Google, GitHub, Apple, email) with JWKS-based JWT verification.

Key architectural decisions and features include:
- **Admin Approval Queue System**: Mandatory approval workflow for marketing content with status tracking, feature toggles, and an immutable audit log.
- **Social Media Integrations**: Full OAuth 2.0 implementations for various platforms supporting publishing, media listing, insights, and secure token management.
- **Centralized API Keys & Secrets Management**: Dedicated section for secure, encrypted storage and management of API keys and secrets per company. Company model has `set_secret(key, value)` / `get_secret(key)` / `delete_secret(key)` methods that encrypt/decrypt via `services.secret_vault.vault` (Fernet). Supports 2-arg `(key, value)` and 3-arg `(provider, sub_key, value)` call styles. CompanySecret table has a unique constraint on `(company_id, key)` and stores Fernet-encrypted values. The `/api/company/<id>/secrets` GET returns masked values only; POST `/secrets/save` encrypts before storing.
- **Contact Management**: Zapier webhook integration for authenticated contact capture, auto-segmentation, comprehensive customer profiles with lead scoring, activity timelines, and bidirectional sync with newsletter subscribers.
- **Error Logging & Diagnostics**: Robust system for logging application errors to a database with detailed diagnostics.
- **AI Chatbot with Auto-Repair**: Marketing assistant and troubleshooter analyzing server logs and triggering automated error repair.
- **Modular Layout**: 224px left sidebar and a horizontal category tab bar for navigation, with notification and inbox systems providing real-time updates.
- **Feature Toggle System**: Manages per-company feature flags for AI agents, channels, automation, and safety controls, including emergency stop capabilities.
- **CRM Hub**: Action-oriented coaching system with pipeline stages, "Next Actions" widgets, and a visual Kanban pipeline for deal management.
- **Autonomous Systems Governor (Orchestrator Agent)**: Master oversight agent with a 6-tab dashboard for system health, agent orchestration, and reporting.
- **Comprehensive Analytics Hub**: Provides 10 metric categories with Chart.js visualizations, including customizable dashboard analytics and predictive analytics (lead scoring, churn risk, send time optimization).
- **Marketing Automations Hub**: Restructured dashboard for email, SMS, and social DM automations, featuring quick-create cards, trigger selection, a 3-step creation wizard, and a rich automation trigger library.
- **SMS Service Integration**: Full Twilio integration for campaigns, bulk sending, and AI-powered content.
- **Multi-Tenant Twilio SMS/Call Platform** (`twilio_sms.py`): Production-ready per-company Twilio platform under `/twilio/*` with encrypted credential storage, conversation threading, auto-reply rules engine (keyword/first-contact/after-hours/stop triggers), business hours, lead auto-capture, missed-call SMS, call log, analytics dashboard, and a full inbox UI. Accessible from sidebar under "SMS Inbox". Webhook endpoints: `POST /twilio/sms/inbound`, `POST /twilio/sms/status`, `POST /twilio/voice/inbound`.
- **Marketing Calendar**: FullCalendar.js integration for managing scheduled posts, SMS, and email campaigns with drag-and-drop rescheduling.
- **AI Auto-Generate for Campaigns**: AI-powered subject line generation and advanced social media post creation features.
- **Email Campaign Builder**: Drag-and-drop builder with content blocks, templates, and A/B testing.
- **User and Company Management**: Includes user profiles, password management, and company-specific branding settings.
- **Onboarding Wizard**: Multi-step process for new company setup, including company info, brand kit, API key configuration, and a launch checklist, with robust file upload validation.
- **In-App Help & Walkthroughs**: Contextual help system with help icons on key screens, slide-out help drawer with instructions/video/PDF, JSON-driven interactive walkthroughs with step highlighting, and onboarding progress tracking (setup/training/docs/go-live readiness). Models: HelpContent, WalkthroughDef, WalkthroughProgress, OnboardingProgress. JS: `static/js/walkthrough.js`. Templates: `templates/partials/help_icon.html`, `help_drawer.html`, `onboarding_progress.html`.
- **Security Enhancements**: Tenant scoping, IDOR protection, CSRF tokens, XSS prevention, and secure cookie configurations.

## External Dependencies
- **OpenAI API**: AI-powered campaign generation, content optimization, and diagnostics.
- **Microsoft Graph API**: Microsoft 365 email delivery.
- **Zapier**: Contact capture via webhooks.
- **Replit Auth (OpenID Connect)**: OAuth authentication (Google, GitHub, Apple, email).
- **Social Media APIs**: Instagram Graph API, TikTok API, Facebook API, Reddit API, YouTube Data API, LinkedIn API, Snapchat API, X (formerly Twitter) API.
- **SEO/Marketing APIs**: DataForSEO API, SEMrush API, Moz API.
- **Event APIs**: Eventbrite API, Ticketmaster Discovery API.
- **Twilio API**: SMS service.
- **Image APIs**: Unsplash API, Pexels API.
- **URL Shortening APIs**: TinyURL/Bitly API.
- **Frameworks/Libraries**: Flask, APScheduler, SQLAlchemy, Bootstrap 5, Feather Icons.
