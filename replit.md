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
- **Global Sidebar Layout**: All authenticated pages share a consistent 224px left sidebar (glassmorphism style) with Marketing/Sales quick-switch toggle at the top, followed by evenly-spaced nav items (no section headers). Active item highlighting based on current route. A dynamic top tab bar (44px, below the header) shows context-sensitive tabs per section, with mailbox and notification icons right-aligned.
- **FeatureToggle Model**: Manages per-company feature flags for AI agents, channels, automation, and safety controls with emergency stop capability.
- **CRM Hub Transformation**: An action-oriented coaching system with pipeline stages, "Next Actions" widgets, and activity-based metrics, featuring a unified Sales Hub with a visual Kanban pipeline and comprehensive deal management.
- **Autonomous Systems Governor (Orchestrator Agent)**: A master oversight agent providing a 6-tab dashboard for system health, agent orchestration, UX optimization, feature governance, competitive intelligence, and executive reporting.
- **Comprehensive Analytics Hub**: An analytics system providing 10 metric categories with Chart.js visualizations and a dark theme UI.
- **Automation Trigger Library**: An expanded library of 25+ automation templates across various marketing categories with full CRUD API support.
- **SMS Service Integration**: Full Twilio integration for SMS campaign creation, bulk sending, AI-powered content generation, and compliance checking.
- **Marketing Calendar**: Full FullCalendar.js integration with drag-and-drop rescheduling and event management.
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