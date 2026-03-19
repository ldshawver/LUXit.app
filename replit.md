# LUX Marketing Platform - Project Documentation

## Overview
LUX Marketing is a multi-channel marketing automation platform designed to streamline marketing efforts. It features a tile-based dashboard with 11 AI agents and AI-powered campaign generation using GPT-4o. The platform aims to be a launch-ready, automated marketing solution offering centralized management for all integrations and API keys, robust social media publishing, and comprehensive contact capture. It includes advanced error logging, diagnostics, and an AI chatbot with auto-repair capabilities to ensure seamless operation.

## User Preferences
- Black background with purple, cyan, pink branding
- Launch-ready with all features on Replit and VPS
- Automated systems that work without manual intervention
- Clear error diagnostics and auto-repair capabilities
- Centralized API keys & secrets management (not scattered modal dialogs)
- All social media platforms available in Settings → Integrations for all companies

## System Architecture
The platform is built around a tile-based dashboard with 11 AI agents, leveraging GPT-4o for AI-powered campaign generation. The UI/UX features a pure black background accented with brand colors (purple, cyan, pink). Authentication is handled via Replit Auth OAuth, supporting Google, GitHub, Apple, and email sign-in, with JWKS-based JWT signature verification.

Key architectural decisions and features include:
- **Admin Approval Queue System**: Implements a mandatory approval workflow for all marketing content with status tracking, feature toggles for safe-off defaults, and an immutable audit log. AI agents route content through this system before execution.
- **Social Media Integrations**: Full OAuth 2.0 implementations for various platforms (Instagram, TikTok, Facebook, Reddit, YouTube, LinkedIn, Snapchat, X/Twitter) supporting content publishing, media listing, insights, and secure token management.
- **Centralized API Keys & Secrets Management**: A dedicated section for secure, encrypted storage and management of API keys and secrets per company.
- **Contact Management**: Features Zapier webhook integration for authenticated contact capture and auto-segmentation, comprehensive customer profiles with lead scoring, activity timelines, and quick actions. Bidirectional synchronization between contacts and newsletter subscribers is included.
- **Error Logging & Diagnostics**: A robust system logs application errors to a database, providing detailed diagnostics.
- **AI Chatbot with Auto-Repair**: Functions as a marketing assistant and troubleshooter, capable of analyzing server logs and triggering an automated error repair system using ChatGPT.
- **Layout**: 224px left sidebar (glassmorphism) with Marketing/Sales toggle at top, followed by grouped nav items under section labels (Core Marketing, Content Engine, Intelligence, Customers, Automations, Engagement). A horizontal category tab bar (38px, sticky below header) mirrors these groups as top-level tabs with AI button, inbox, and notification icons on the right. Active tab/sidebar item highlights in cyan. LUX logo links to Marketing Hub or CRM Hub based on user's `preferred_hub` (auto-set when visiting either hub).
- **Notifications & Inbox**: Full notification center (`/notifications`) with category filters (system, campaign, automation, contact, team), mark-as-read, and clear-all. Inbox (`/inbox`) with message detail view, starring, and read tracking. Badge counts in the category bar update from real data.
- **FeatureToggle Model**: Manages per-company feature flags for AI agents, channels, automation, and safety controls with emergency stop capability.
- **CRM Hub Transformation**: An action-oriented coaching system with pipeline stages, "Next Actions" widgets, and activity-based metrics, featuring a unified Sales Hub with a visual Kanban pipeline and comprehensive deal management.
- **Autonomous Systems Governor (Orchestrator Agent)**: A master oversight agent providing a 6-tab dashboard for system health, agent orchestration, UX optimization, feature governance, competitive intelligence, and executive reporting.
- **Comprehensive Analytics Hub**: An analytics system providing 10 metric categories with Chart.js visualizations and a dark theme UI. Uses shared global sidebar/topbar from base.html (no duplicate navigation).
- **Dashboard Customizable Analytics**: Interactive Chart.js widget on the Sales Hub dashboard with data source selector (Leads, Revenue, Deals by Stage, Email Performance, Tasks) and date range picker (Week/Month/Quarter/Year). Charts and KPI summaries update dynamically based on user selection.
- **Marketing Automations Hub**: Restructured automation dashboard focused on marketing automations (email, SMS, social DM) with channel-based quick-create cards, trigger selection (signup, purchase, abandoned cart, email open, link click, date-based, tag added, inactivity, plus social-specific triggers), 3-step creation wizard (channel → trigger → details), and per-channel content editors. `Automation` model includes `channel_type` field. AI agent automations moved to AI Hub.
- **Automation Trigger Library**: An expanded library of 25+ automation templates across various marketing categories with full CRUD API support.
- **SMS Service Integration**: Full Twilio integration for SMS campaign creation, bulk sending, AI-powered content generation, and compliance checking.
- **Marketing Calendar**: Full FullCalendar.js integration with drag-and-drop rescheduling and event management. Pulls scheduled social posts, SMS campaigns, and email campaigns into both month view and upcoming list panel. Filter chips for SMS/Social/Email/Deadlines/Notes.
- **AI Auto-Generate for Campaigns**: AI-powered subject line generation for campaigns and advanced social media post creation features (image handling, hashtag generation, URL shortening).
- **TikTok Pixel Integration**: Automatic injection of TikTok Pixel for tracking.
- **Email Campaign Builder**: A drag-and-drop email builder with content blocks, a visual canvas, template picker, and A/B testing capabilities.
- **User and Company Management**: Includes user profiles, password management, user management, and company-specific branding settings.

## External Dependencies
- **OpenAI API**: AI-powered campaign generation, content optimization, and diagnostics.
- **Microsoft Graph API**: Microsoft 365 email delivery.
- **Zapier**: Contact capture via webhooks.
- **Replit Auth (OpenID Connect)**: OAuth authentication (Google, GitHub, Apple, email).
- **Instagram Graph API**: Instagram integration.
- **TikTok API**: TikTok integration.
- **Facebook API**: Facebook Page integration.
- **Reddit API**: Reddit integration.
- **YouTube Data API**: YouTube integration.
- **LinkedIn API**: LinkedIn integration.
- **Snapchat API**: Snapchat integration.
- **X (formerly Twitter) API**: Twitter integration.
- **DataForSEO API**: Keyword research.
- **SEMrush API**: Keyword and competitor data.
- **Moz API**: Domain authority and keyword difficulty.
- **Eventbrite API**: Event search.
- **Ticketmaster Discovery API**: Event search.
- **Twilio API**: SMS service.
- **Unsplash API**: Stock image search.
- **Pexels API**: Stock image search.
- **TinyURL/Bitly API**: URL shortening.
- **Flask**: Web framework.
- **APScheduler**: Background task scheduling.
- **SQLAlchemy**: ORM.
- **Bootstrap 5**: UI framework.
- **Feather Icons**: Icon set.

## Recent Major Fixes & Feature Implementations (March 2026)

### Phase 1: Crash Fixes
- **14 crashing pages fixed**: All 500-error routes resolved by syncing SQLAlchemy models with actual DB schemas. Models updated: SEOBacklink, SEOCompetitor, ABTest, LeadScore, AgentMemory, AgentConfiguration, CampaignCost, AttributionModel, PersonalizationRule, KeywordResearch, CompetitorProfile, WordPressIntegration, Competitor, MarketSignal, StrategyRecommendation, InstagramOAuth, SurveyResponse, CompanyIntegrationConfig.
- **Health check fixed**: Removed corrupted A/B test code mixed into `/health` route; added inline `_db_status()` function.
- **Security fix**: SESSION_COOKIE_SECURE set to True.

### Phase 2: Feature Implementations
- **Affiliate Dashboard** (`/affiliate/dashboard`): Full CRUD for influencers and affiliate link generation. KPI cards (clicks, conversions, commission, conversion rate). Influencer CRM tab with filtering. Affiliate link builder with QR code generation. Payout tracking. Detail pages for influencers. Models: AffiliateLink, Influencer, AffiliateClick, AffiliateConversion.
- **Press Releases** (`/press-releases`): Full CRUD — create, view detail, edit, delete. Media contact management. Press release sending. Status tracking (draft/published/sent). Embargo support. Models: PressRelease, MediaContact. Tables created in DB.
- **Workflow Builder** (`/workflow-builder`): Visual drag-and-drop workflow canvas with node palette (triggers, actions, logic, exit). Save/activate/delete workflows. Node configuration panel. Model: Workflow. Table created in DB.
- **Orchestrator** (`/orchestrator`): Dashboard showing active automations and recent agent tasks with status KPIs.
- **Predictive Analytics** (`/analytics/predictive`): Lead scoring with hot/warm/cold/frozen classifications. Churn risk prediction based on inactivity. Send time optimization recommendations. Subject line performance analyzer with scoring algorithm.
- **Attribution Analytics** (`/analytics/attribution`): Campaign attribution breakdown with revenue and conversion tracking.
- **LTV Analytics** (`/analytics/ltv`): Company-scoped customer lifetime value calculations. Avg LTV, revenue per customer, lifespan, LTV:CAC ratio. Health status assessment.
- **ROI Analytics** (`/roi-analytics`): Campaign investment vs revenue tracking.
- **UTM Builder** (`/utm-builder`): Client-side UTM link generator with copy, QR code, and template saving.

### API Endpoints Added
- `POST /affiliate/generate-link` — Generate tracked affiliate links with QR codes
- `POST /influencer/create` — Add influencers to CRM
- `POST /workflow/save` — Save workflow with nodes/connections
- `POST /workflow/<id>/activate` — Activate a workflow
- `GET /analytics/lead-scores` — Lead scoring data (company-scoped)
- `GET /analytics/churn-risks` — Churn risk predictions (company-scoped)
- `GET /analytics/send-time-optimization` — Send time recommendations
- `POST /analytics/predict-content-performance` — Subject line analysis
- `POST /api/press-releases/<id>/send` — Send press release (with ownership check)

### Security
- All new endpoints enforce tenant scoping (company_id checks)
- Workflow activate and press release send have IDOR protection
- CSRF tokens added to all JavaScript fetch calls
- CompetitorProfile model: DB column is `competitor_name`, model exposes `.name` property for backward compatibility.