"""Marketing site routes."""
import logging
import re
from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, url_for, make_response, request
from flask_login import current_user

logger = logging.getLogger(__name__)

marketing_bp = Blueprint("marketing", __name__, template_folder="marketing/templates")

PRODUCTS = [
    {
        "slug": "crm",
        "name": "CRM & Pipeline",
        "category": "Sales",
        "tagline": "Track every lead, deal, and conversation — from first touch to closed won.",
        "bullets": [
            "Visual pipeline with drag-and-drop stages",
            "AI-powered lead scoring that prioritizes your hottest prospects",
            "Full contact timeline: emails, calls, notes, and activity",
            "Deal forecasting and win-rate analytics",
            "Team assignments and task management built in",
            "One-click connect to your email and calendar",
        ],
        "outcomes": [
            {"stat": "2x", "label": "Faster deal cycles with AI follow-up nudges"},
            {"stat": "40%", "label": "Higher close rates with lead scoring"},
            {"stat": "100%", "label": "Pipeline visibility — no spreadsheets"},
        ],
        "testimonial": "We ditched Salesforce after a month. LUX IT CRM does everything we need and our reps actually use it.",
        "testimonial_author": "VP of Sales, B2B Tech Company",
        "faqs": [
            {"q": "Can I import my existing contacts?", "a": "Yes. Import from CSV, HubSpot, Salesforce, or any other CRM in minutes."},
            {"q": "Does it integrate with my email?", "a": "Yes. Connect Gmail, Outlook, or any IMAP/SMTP email and all correspondence logs automatically."},
            {"q": "How does AI lead scoring work?", "a": "The AI analyzes engagement signals — email opens, link clicks, visit frequency — to score leads automatically and surface the ones most likely to close."},
        ],
    },
    {
        "slug": "campaigns",
        "name": "Email Campaigns",
        "category": "Marketing",
        "tagline": "Launch, test, and optimize email campaigns with AI writing the copy.",
        "bullets": [
            "Drag-and-drop campaign builder with branded templates",
            "AI content generation — subject lines, body copy, CTAs",
            "A/B testing with automated winner selection",
            "Smart scheduling based on recipient time zones",
            "UTM tracking and revenue attribution per campaign",
            "Unsubscribe and compliance management built in",
        ],
        "outcomes": [
            {"stat": "3x", "label": "More emails sent with the same team"},
            {"stat": "28%", "label": "Average open rate improvement with AI subject lines"},
            {"stat": "60%", "label": "Time saved on campaign creation"},
        ],
        "testimonial": "Our email performance doubled in six weeks. The AI subject line tool alone was worth the switch.",
        "testimonial_author": "Email Marketing Manager, eCommerce Brand",
        "faqs": [
            {"q": "Can the AI write the entire email?", "a": "Yes. Give it a brief — product, audience, goal — and it generates a complete campaign including subject, preview, and body copy."},
            {"q": "How do I handle unsubscribes?", "a": "Unsubscribes are managed automatically in compliance with CAN-SPAM and GDPR. One-click unsubscribe is included in every email."},
            {"q": "Can I send to segmented lists?", "a": "Yes. Segment by any contact field, behavior, engagement score, or custom tag. Send different versions to different segments in the same campaign."},
        ],
    },
    {
        "slug": "sms",
        "name": "SMS Marketing",
        "category": "Marketing",
        "tagline": "Reach customers where they actually read — with AI-crafted texts that convert.",
        "bullets": [
            "Two-way SMS powered by Twilio",
            "AI message generation and personalization",
            "Automated drip sequences triggered by behavior",
            "Opt-in collection and compliance built in",
            "Campaign analytics with click-through and conversion tracking",
            "Integrates with email campaigns for omnichannel journeys",
        ],
        "outcomes": [
            {"stat": "98%", "label": "Average SMS open rate"},
            {"stat": "6x", "label": "Higher response rate vs email"},
            {"stat": "45%", "label": "Of conversions happen within 1 hour of message"},
        ],
        "testimonial": "SMS campaigns with LUX IT outperform our email by 4x. We use both together for maximum reach.",
        "testimonial_author": "Marketing Director, Retail Brand",
        "faqs": [
            {"q": "Do I need a Twilio account?", "a": "You'll connect your Twilio account to LUX IT. Setup takes under 5 minutes and Twilio's pricing is pass-through with no markup."},
            {"q": "How is compliance handled?", "a": "LUX IT enforces opt-in requirements, tracks consent timestamps, and handles opt-outs automatically. TCPA-compliant by default."},
            {"q": "Can I automate SMS based on user actions?", "a": "Yes. Trigger texts when someone fills a form, abandons a cart, opens an email, or hits a custom event you define."},
        ],
    },
    {
        "slug": "social-media",
        "name": "Social Media",
        "category": "Marketing",
        "tagline": "Schedule, publish, and analyze across every platform — from one dashboard.",
        "bullets": [
            "Connect Instagram, Facebook, LinkedIn, X, TikTok, and YouTube",
            "AI content generation tailored to each platform's tone",
            "Visual calendar for scheduling and bulk upload",
            "Content approval workflow before publishing",
            "Engagement tracking and audience growth analytics",
            "Social listening for brand mentions and competitor activity",
        ],
        "outcomes": [
            {"stat": "5x", "label": "More posts published with the same effort"},
            {"stat": "3x", "label": "Engagement growth in 90 days"},
            {"stat": "Zero", "label": "Off-brand posts with approval workflows"},
        ],
        "testimonial": "Managing 6 platforms used to take a full-time person. Now one person handles it all with LUX IT in a few hours a week.",
        "testimonial_author": "Social Media Manager, Agency",
        "faqs": [
            {"q": "Which platforms do you support?", "a": "Instagram, Facebook, LinkedIn, X (Twitter), TikTok, YouTube, and Pinterest. More platforms are added regularly."},
            {"q": "Can the AI write platform-specific content?", "a": "Yes. The AI generates content optimized for each platform — short and punchy for X, professional for LinkedIn, visual-first for Instagram."},
            {"q": "How does the approval workflow work?", "a": "Posts go into an approval queue before publishing. Reviewers can approve, edit, or reject with comments. Nothing publishes without sign-off."},
        ],
    },
    {
        "slug": "analytics",
        "name": "Analytics & Reporting",
        "category": "Intelligence",
        "tagline": "See exactly what's working — and what to do next — with AI-powered insights.",
        "bullets": [
            "Unified dashboard across all channels and campaigns",
            "Multi-touch revenue attribution from first click to close",
            "Customer Acquisition Cost (CAC) and Lifetime Value (LTV) tracking",
            "Campaign ROI and payback period calculations",
            "Cohort analysis, churn prediction, and retention metrics",
            "Executive-ready reports with one-click export",
        ],
        "outcomes": [
            {"stat": "10", "label": "Metric categories in one dashboard"},
            {"stat": "100%", "label": "Attribution visibility — no more gut calls"},
            {"stat": "90sec", "label": "To generate a board-ready report"},
        ],
        "testimonial": "For the first time I can walk into a board meeting and show exactly which campaigns drove revenue. LUX IT made that possible.",
        "testimonial_author": "CMO, SaaS Company",
        "faqs": [
            {"q": "How does multi-touch attribution work?", "a": "LUX IT tracks every touchpoint — ads, emails, social, landing pages — and assigns revenue credit based on the attribution model you choose (first touch, last touch, linear, or time decay)."},
            {"q": "Can I build custom reports?", "a": "Yes. The report builder lets you combine any metrics, apply filters, and save views. Reports can be scheduled for automatic delivery to your team."},
            {"q": "How far back does the data go?", "a": "Historical data is retained for the lifetime of your account. You can analyze trends from day one."},
        ],
    },
    {
        "slug": "ai-agents",
        "name": "11 AI Agents",
        "category": "Automation",
        "tagline": "Your always-on AI marketing department — 11 specialists working 24/7 without a salary.",
        "bullets": [
            "Lead Research Agent — finds and qualifies prospects automatically",
            "Content Writer Agent — blogs, emails, social posts on demand",
            "Email CRM Agent — manages sequences and follow-ups",
            "Social Media Agent — schedules and publishes across platforms",
            "Analytics Agent — generates reports and surfaces insights",
            "SEO Agent — tracks rankings and optimizes content",
            "Competitor Intelligence Agent — monitors rival activity",
            "Advertising Agent — manages ad campaigns across networks",
            "Brand & Strategy Agent — quarterly planning and research",
            "Customer Retention Agent — churn prediction and win-back",
            "Operations Agent — system health and integration sync",
        ],
        "outcomes": [
            {"stat": "85%", "label": "Reduction in manual marketing tasks"},
            {"stat": "24/7", "label": "AI agents running without breaks"},
            {"stat": "11", "label": "Specialists for the price of one platform"},
        ],
        "testimonial": "It's like having a full marketing department that never sleeps. The agents handle research, writing, publishing, and reporting — we just review and approve.",
        "testimonial_author": "CEO, Digital Marketing Agency",
        "faqs": [
            {"q": "Do the agents work automatically or do I have to prompt them?", "a": "Both. Agents run on schedules automatically, but you can also prompt them directly with specific tasks and get results in seconds."},
            {"q": "Can I customize what each agent does?", "a": "Yes. Each agent has configurable parameters — tone, frequency, focus areas, approval requirements — so they fit your workflow exactly."},
            {"q": "What happens if an agent makes a mistake?", "a": "Content approval workflows catch errors before they go live. You can also roll back any action and the audit trail shows exactly what happened and when."},
        ],
    },
]

SOLUTIONS = [
    {
        "slug": "book-more-calls",
        "name": "Book More Calls",
        "tagline": "Turn cold traffic into warm meetings — automatically.",
        "bullets": [
            "AI-personalized outreach sequences that feel human",
            "Landing pages with lead magnets that capture intent",
            "Automated follow-up that triggers on opens, clicks, and visits",
            "Calendar routing so booked calls land on the right rep",
            "Lead scoring so your best prospects get priority",
            "Full attribution from first touch to booked call",
        ],
        "testimonial": "We went from 3 booked calls a week to 18 in the first month — without adding headcount.",
        "testimonial_author": "Founder, Digital Agency",
        "faqs": [
            {"q": "How does automated follow-up work?", "a": "LUX IT monitors engagement signals — email opens, link clicks, website visits — and triggers personalized follow-ups at the right moment automatically."},
            {"q": "Can I connect my calendar?", "a": "Yes. Connect Google Calendar or Outlook and routed leads can book directly into your team's available slots."},
            {"q": "How quickly can I launch this solution?", "a": "Most teams have their first sequence running within 30 minutes of signup."},
        ],
    },
    {
        "slug": "retain-customers",
        "name": "Retain Customers",
        "tagline": "Reduce churn with lifecycle automation that keeps customers engaged.",
        "bullets": [
            "Onboarding sequences that drive early product adoption",
            "Health scoring that flags at-risk customers before they churn",
            "Automated win-back campaigns triggered by disengagement",
            "NPS collection and follow-up built into the journey",
            "Loyalty and reward campaigns for your best customers",
            "Renewal reminders and upsell sequences",
        ],
        "testimonial": "Our churn dropped 30% in 90 days after implementing LUX IT's retention playbook. The at-risk alerts alone saved three enterprise accounts.",
        "testimonial_author": "Customer Success Lead, SaaS Company",
        "faqs": [
            {"q": "How does health scoring work?", "a": "LUX IT tracks engagement signals — logins, feature usage, email activity — to calculate a health score. Low scores trigger automatic win-back sequences."},
            {"q": "Can I customize the onboarding sequence?", "a": "Yes. Full control over timing, content, and branching logic. Start with the pre-built template and customize from there."},
            {"q": "Does this work for subscription businesses?", "a": "Yes, this solution is specifically designed for subscription and recurring revenue models."},
        ],
    },
    {
        "slug": "scale-without-hiring",
        "name": "Scale Without Hiring",
        "tagline": "Do the work of a 5-person marketing team with AI agents and automation.",
        "bullets": [
            "11 AI agents handling research, writing, and publishing",
            "Automated campaign creation from brief to live in minutes",
            "Content calendar managed and executed by AI",
            "Approval workflows so you stay in control without doing everything",
            "Performance monitoring with automated optimization",
            "Weekly AI-generated strategy reports so nothing slips",
        ],
        "testimonial": "I replaced two marketing contractors with LUX IT. Output went up 3x and costs went down 60%.",
        "testimonial_author": "Founder, B2B Consultancy",
        "faqs": [
            {"q": "Will the AI content sound generic?", "a": "No. You configure each agent with your brand voice, audience, and messaging guidelines. Output is personalized to your brand."},
            {"q": "What does the approval workflow look like?", "a": "All AI-generated content goes to a queue where you approve, edit, or reject before anything publishes. You're always in control."},
            {"q": "How many tasks can the agents handle per week?", "a": "There's no limit. Agents run on schedule and on demand — the more tasks you queue, the more they execute."},
        ],
    },
    {
        "slug": "prove-roi",
        "name": "Prove Marketing ROI",
        "tagline": "Connect every campaign to revenue — and show it in a board-ready report.",
        "bullets": [
            "Multi-touch attribution across all channels",
            "CAC and LTV tracking per campaign and channel",
            "Pipeline influence reporting for every marketing activity",
            "One-click board-ready report generation",
            "Revenue forecasting based on current pipeline velocity",
            "Competitive benchmarking so you can show relative performance",
        ],
        "testimonial": "For the first time in my career I can walk into a board meeting and say exactly which campaigns drove which deals. That's a career-changing capability.",
        "testimonial_author": "CMO, Growth-Stage SaaS",
        "faqs": [
            {"q": "What attribution models are supported?", "a": "First touch, last touch, linear, time decay, and position-based. You can switch between models and compare results side by side."},
            {"q": "How does this connect to CRM data?", "a": "LUX IT's CRM and marketing data share the same database — attribution is native, not an integration. Every deal traces back to its originating campaign automatically."},
            {"q": "Can I share reports with people who don't have a LUX IT account?", "a": "Yes. Reports can be exported to PDF, shared via a secure link, or scheduled for automatic email delivery to stakeholders."},
        ],
    },
    {
        "slug": "run-client-campaigns",
        "name": "Run Client Campaigns",
        "tagline": "Manage multiple clients without the chaos — white-label ready.",
        "bullets": [
            "Multi-company dashboard — one login, all clients",
            "Client-specific workspaces with isolated data",
            "White-label the entire platform under your brand",
            "Approval workflows so clients review before anything publishes",
            "Role-based access — clients see only their data",
            "Consolidated reporting across all client accounts",
        ],
        "testimonial": "We onboarded 12 clients in two weeks. The multi-company dashboard and approval workflows made it manageable. Our clients love the white-label experience.",
        "testimonial_author": "Agency Director, Full-Service Marketing Agency",
        "faqs": [
            {"q": "Can clients log in and see their own data?", "a": "Yes. You can create client-access accounts with view-only or limited permissions. They see their campaigns, reports, and results — nothing from other clients."},
            {"q": "Is the white-label fully custom?", "a": "On the Enterprise plan, yes. Custom domain, logo, colors, and email sender name. Your clients never see the LUX IT brand unless you want them to."},
            {"q": "How many clients can I manage?", "a": "The Professional plan supports multiple companies. The Enterprise plan supports unlimited clients with dedicated workspaces."},
        ],
    },
]

INDUSTRIES = [
    {
        "slug": "agencies",
        "name": "Agencies",
        "tagline": "Run client campaigns without the chaos. Scale without hiring.",
        "bullets": [
            "Multi-client dashboard with isolated workspaces per client",
            "White-label the platform under your own brand",
            "Client-facing approval workflows so they review before you publish",
            "Role-based access — clients see only their campaigns and reports",
            "AI agents handle content creation, scheduling, and reporting",
            "Consolidated analytics across all your client accounts",
        ],
        "testimonial": "LUX IT is the backbone of our agency. We manage 20 clients from one dashboard and white-label the whole thing. Our clients think we built it.",
        "testimonial_author": "Founder, Digital Marketing Agency",
        "faqs": [
            {"q": "Can I resell LUX IT to my clients?", "a": "Yes. The Enterprise plan includes full white-label and reseller rights. Set your own pricing and margin."},
            {"q": "How do client approvals work?", "a": "Every piece of AI-generated content goes into an approval queue. Clients can log in, review, approve, or request changes — nothing publishes without sign-off."},
            {"q": "What happens when I onboard a new client?", "a": "Create a new company workspace in minutes. Connect their channels, import their contacts, and launch the first campaign the same day."},
        ],
    },
    {
        "slug": "ecommerce",
        "name": "eCommerce",
        "tagline": "Recover carts, grow repeat purchases, and turn buyers into loyalists.",
        "bullets": [
            "Abandoned cart recovery via email and SMS — automated",
            "Post-purchase sequences that drive repeat orders",
            "AI product recommendation emails based on purchase history",
            "Loyalty and reward campaign automation",
            "Seasonal promotion campaigns with AI copy generation",
            "Revenue attribution per email, SMS, and social campaign",
        ],
        "testimonial": "Abandoned cart recovery alone paid for LUX IT in the first week. We now use every module and our repeat purchase rate is up 45%.",
        "testimonial_author": "Head of eCommerce, DTC Brand",
        "faqs": [
            {"q": "Does LUX IT integrate with Shopify or WooCommerce?", "a": "Yes. Connect your store to sync products, orders, and customer data for personalized campaigns and attribution."},
            {"q": "How does abandoned cart recovery work?", "a": "When a customer adds to cart but doesn't checkout, LUX IT automatically sends a timed email and/or SMS sequence — personalized with the exact products they left behind."},
            {"q": "Can I run flash sale campaigns quickly?", "a": "Yes. The AI campaign builder can generate a complete flash sale campaign — email, SMS, and social — in under 5 minutes."},
        ],
    },
    {
        "slug": "saas",
        "name": "SaaS",
        "tagline": "Convert trials, reduce churn, and grow expansion revenue — on autopilot.",
        "bullets": [
            "Trial-to-paid onboarding sequences that drive activation",
            "In-app behavior triggers for targeted upgrade campaigns",
            "Churn prediction alerts and automated win-back flows",
            "Expansion revenue campaigns for upsell and cross-sell",
            "NPS collection and closed-loop follow-up automation",
            "Product-led growth analytics and cohort tracking",
        ],
        "testimonial": "Our trial-to-paid conversion went from 12% to 28% after implementing LUX IT's onboarding sequences. That was a business-changing result.",
        "testimonial_author": "Head of Growth, B2B SaaS",
        "faqs": [
            {"q": "Can LUX IT trigger campaigns based on in-app behavior?", "a": "Yes. Connect via webhook or API and trigger campaigns when users hit — or miss — key activation events."},
            {"q": "How does churn prediction work?", "a": "LUX IT monitors engagement signals — logins, feature usage, email activity — to score health and flag at-risk accounts before they cancel."},
            {"q": "Does this work for product-led growth models?", "a": "Yes. LUX IT is particularly effective for PLG — it automates the nudges that convert free users and expand accounts without requiring sales involvement."},
        ],
    },
    {
        "slug": "professional-services",
        "name": "Professional Services",
        "tagline": "Build authority, generate referrals, and fill your pipeline with qualified leads.",
        "bullets": [
            "Thought leadership content generation on autopilot",
            "Referral program automation and tracking",
            "Long-cycle nurture sequences for high-value prospects",
            "Event promotion and follow-up automation",
            "Client anniversary and milestone campaigns",
            "Pipeline management with deal value forecasting",
        ],
        "testimonial": "Our inbound leads doubled in 90 days after we started using LUX IT's content and SEO tools. We're now the top result for our target keywords.",
        "testimonial_author": "Managing Partner, Consulting Firm",
        "faqs": [
            {"q": "How does thought leadership content work?", "a": "The Content & SEO agent researches trending topics in your industry, generates articles, optimizes them for search, and schedules publication on a cadence you set."},
            {"q": "Can I track referrals through the platform?", "a": "Yes. Tag referral sources on contacts and attribute revenue back to specific referrers for reporting and incentive tracking."},
            {"q": "Is this good for businesses with long sales cycles?", "a": "Yes. LUX IT's long-form nurture sequences are built for 6-18 month sales cycles — keeping prospects warm without manual follow-up."},
        ],
    },
    {
        "slug": "b2b",
        "name": "B2B Teams",
        "tagline": "Generate pipeline, accelerate deals, and align sales and marketing in one platform.",
        "bullets": [
            "Account-based marketing (ABM) campaign automation",
            "Lead scoring based on ICP fit and engagement signals",
            "Sales and marketing alignment with shared contact timeline",
            "LinkedIn and email outreach sequences for prospecting",
            "Deal room content — case studies, proposals, one-pagers",
            "Pipeline analytics with stage-by-stage conversion data",
        ],
        "testimonial": "The alignment between sales and marketing has been transformational. Everyone works from the same data and the same platform. Deal velocity is up 35%.",
        "testimonial_author": "VP of Revenue, B2B Tech",
        "faqs": [
            {"q": "Does LUX IT support account-based marketing?", "a": "Yes. Create account lists, run targeted campaigns to specific companies, and track engagement at the account level."},
            {"q": "How does it help align sales and marketing?", "a": "Both teams use the same CRM and campaign data. Sales sees every marketing touchpoint on a contact's timeline. Marketing sees which campaigns influence open deals."},
            {"q": "Can the AI help with outbound prospecting?", "a": "Yes. The Lead Research Agent identifies target accounts, the Content Agent writes personalized outreach, and the Email Agent sends and manages the sequence."},
        ],
    },
    {
        "slug": "media",
        "name": "Media & Publishers",
        "tagline": "Grow your audience, monetize content, and build subscriber revenue.",
        "bullets": [
            "Newsletter growth and monetization automation",
            "Subscriber segmentation by interest and engagement",
            "Automated content distribution across email and social",
            "Sponsorship and ad inventory tracking",
            "Audience re-engagement campaigns for inactive subscribers",
            "Content performance analytics and editorial recommendations",
        ],
        "testimonial": "Our newsletter subscriber base grew 80% in six months using LUX IT's audience growth tools. The AI content generation saves us 15 hours a week.",
        "testimonial_author": "Editor-in-Chief, Industry Media Company",
        "faqs": [
            {"q": "Can LUX IT manage newsletter subscriptions?", "a": "Yes. Full subscription management including opt-in collection, segmentation, preference centers, and compliance with CAN-SPAM and GDPR."},
            {"q": "How does AI content generation work for publishers?", "a": "Give the AI a topic, audience, and angle — it generates a complete article or newsletter issue. You edit, approve, and publish. Most publishers use it to create first drafts they then refine."},
            {"q": "Can I track which content drives conversions or subscriptions?", "a": "Yes. Every content piece is tracked with UTM parameters and tied back to subscriber acquisition, upgrade, or purchase events."},
        ],
    },
]


@marketing_bp.context_processor
def inject_now():
    return {'now': datetime.utcnow()}


@marketing_bp.route("/")
def home():
    if current_user.is_authenticated:
        hub = getattr(current_user, 'default_hub', 'sales')
        if hub == 'marketing':
            return redirect(url_for("main.marketing_hub"))
        return redirect(url_for("main.dashboard"))
    return render_template("marketing/home.html", active_page="home")


@marketing_bp.route("/features")
def features():
    return render_template("marketing/features.html", active_page="features")


@marketing_bp.route("/products")
def products():
    return render_template("marketing/products.html", items=PRODUCTS, active_page="products")


@marketing_bp.route("/products/<slug>")
def product_detail(slug):
    item = next((x for x in PRODUCTS if x["slug"] == slug), None)
    if not item:
        abort(404)
    return render_template("marketing/product_detail.html", item=item, active_page="products")


@marketing_bp.route("/solutions")
def solutions():
    return render_template("marketing/solutions.html", items=SOLUTIONS, active_page="solutions")


@marketing_bp.route("/solutions/<slug>")
def solution_detail(slug):
    item = next((x for x in SOLUTIONS if x["slug"] == slug), None)
    if not item:
        abort(404)
    return render_template("marketing/solution_detail.html", item=item, active_page="solutions")


@marketing_bp.route("/industries")
def industries():
    return render_template("marketing/industries.html", items=INDUSTRIES, active_page="industries")


@marketing_bp.route("/industries/<slug>")
def industry_detail(slug):
    item = next((x for x in INDUSTRIES if x["slug"] == slug), None)
    if not item:
        abort(404)
    return render_template("marketing/industry_detail.html", item=item, active_page="industries")


@marketing_bp.route("/security")
def security():
    return render_template("marketing/security.html", active_page="security")


@marketing_bp.route("/pricing")
def pricing():
    return render_template("marketing/pricing.html", active_page="pricing")


@marketing_bp.route("/about")
def about():
    return render_template("marketing/about.html", active_page="about")


@marketing_bp.route("/contact")
def contact():
    return render_template("marketing/contact.html", active_page="contact")


@marketing_bp.route("/book-demo", methods=["GET", "POST"])
def book_demo():
    if request.method == "POST":
        from extensions import db
        from models import DemoRequest

        first_name = request.form.get("first_name", "").strip()[:100]
        last_name = request.form.get("last_name", "").strip()[:100]
        email = request.form.get("email", "").strip()[:200]
        phone = request.form.get("phone", "").strip()[:50]
        company_name = request.form.get("company_name", "").strip()[:200]
        job_title = request.form.get("job_title", "").strip()[:200]
        team_size = request.form.get("team_size", "")[:50]
        message = request.form.get("message", "").strip()[:2000]
        preferred_contact = request.form.get("preferred_contact", "email")
        source_page = request.form.get("source_page", "direct")[:100]

        errors = []
        if not first_name:
            errors.append("First name is required.")
        if not last_name:
            errors.append("Last name is required.")
        if not email:
            errors.append("Email is required.")
        elif not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
            errors.append("Please enter a valid email address.")
        if preferred_contact not in ("email", "phone", "video"):
            preferred_contact = "email"

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("marketing/book_demo.html", active_page="demo")

        try:
            demo = DemoRequest(
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                company_name=company_name,
                job_title=job_title,
                team_size=team_size,
                message=message,
                preferred_contact=preferred_contact,
                source_page=source_page,
            )
            db.session.add(demo)
            db.session.commit()
            return render_template("marketing/demo_success.html", active_page="demo", demo=demo)
        except Exception as e:
            logger.error("Demo request save failed: %s", e)
            db.session.rollback()
            flash("Something went wrong. Please try again or contact us directly.", "error")
            return redirect(url_for("marketing.book_demo"))

    return render_template("marketing/book_demo.html", active_page="demo")


@marketing_bp.route("/robots.txt")
def robots_txt():
    content = """User-agent: *
Allow: /
Disallow: /dashboard/
Disallow: /admin/
Disallow: /api/

Sitemap: {}/sitemap.xml
""".format(request.host_url.rstrip("/"))
    response = make_response(content)
    response.headers["Content-Type"] = "text/plain"
    return response


@marketing_bp.route("/sitemap.xml")
def sitemap_xml():
    base_url = request.host_url.rstrip("/")
    pages = [
        {"loc": "/", "priority": "1.0", "changefreq": "weekly"},
        {"loc": "/features", "priority": "0.8", "changefreq": "monthly"},
        {"loc": "/solutions", "priority": "0.8", "changefreq": "monthly"},
        {"loc": "/pricing", "priority": "0.8", "changefreq": "monthly"},
        {"loc": "/security", "priority": "0.7", "changefreq": "monthly"},
        {"loc": "/book-demo", "priority": "0.9", "changefreq": "weekly"},
        {"loc": "/auth/login", "priority": "0.6", "changefreq": "monthly"},
        {"loc": "/auth/register", "priority": "0.6", "changefreq": "monthly"},
    ]

    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml_content += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'

    for page in pages:
        xml_content += "  <url>\n"
        xml_content += f"    <loc>{base_url}{page['loc']}</loc>\n"
        xml_content += f"    <lastmod>{datetime.now().strftime('%Y-%m-%d')}</lastmod>\n"
        xml_content += f"    <changefreq>{page['changefreq']}</changefreq>\n"
        xml_content += f"    <priority>{page['priority']}</priority>\n"
        xml_content += "  </url>\n"

    xml_content += "</urlset>"

    response = make_response(xml_content)
    response.headers["Content-Type"] = "application/xml"
    return response
