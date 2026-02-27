from datetime import datetime
import logging

from flask_login import UserMixin
from sqlalchemy import JSON, Text

from extensions import db

user_company = db.metadata.tables.get("user_company")
if user_company is None:
    user_company = db.Table('user_company',
        db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
        db.Column('company_id', db.Integer, db.ForeignKey('company.id'), primary_key=True),
        db.Column('is_default', db.Boolean, default=False),
        db.Column('created_at', db.DateTime, default=datetime.utcnow)
    )

# ============================================================
# UserCompanyAccess (authoritative access + role model)
# ============================================================

class UserCompanyAccess(db.Model):
    __tablename__ = "user_company_access"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)

    role = db.Column(db.String(20), default="viewer")
    is_default = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "company_id", name="uq_user_company_access"),
        db.Index("ix_user_company_access_user", "user_id"),
        db.Index("ix_user_company_access_company", "company_id"),
    )

    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_EDITOR = "editor"
    ROLE_VIEWER = "viewer"

    ROLE_HIERARCHY = {
        ROLE_OWNER: 4,
        ROLE_ADMIN: 3,
        ROLE_EDITOR: 2,
        ROLE_VIEWER: 1,
    }

    user = db.relationship("User", backref=db.backref("company_access", lazy="dynamic"))
    company = db.relationship(
        "Company", backref=db.backref("user_access", lazy="dynamic")
    )

    def __repr__(self):
        return f"<UserCompanyAccess user={self.user_id} company={self.company_id} role={self.role}>"

    def can_edit(self):
        return self.role in {self.ROLE_OWNER, self.ROLE_ADMIN, self.ROLE_EDITOR}

    def can_admin(self):
        return self.role in {self.ROLE_OWNER, self.ROLE_ADMIN}

    def can_own(self):
        return self.role == self.ROLE_OWNER


# ============================================================
# User model  ✅ EVERYTHING USER-RELATED LIVES HERE
# ============================================================

class User(UserMixin, db.Model):
    __table_args__ = {"extend_existing": True}
    __tablename__ = "user"

    # -------------------------
    # Core identity
    # -------------------------
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    is_admin = db.Column(db.Boolean, default=False)

    # Replit Auth
    replit_id = db.Column(db.String(64), unique=True, nullable=True)

    # Default company pointer
    default_company_id = db.Column(
        db.Integer, db.ForeignKey("company.id"), nullable=True
    )

    # -------------------------
    # Profile fields
    # -------------------------
    first_name = db.Column(db.String(64))
    last_name = db.Column(db.String(64))
    phone = db.Column(db.String(20))
    avatar_path = db.Column(db.String(255))
    tags = db.Column(db.String(255))
    segment = db.Column(db.String(100), default="user")
    default_hub = db.Column(db.String(20), default="sales")  # 'sales' or 'marketing'
    custom_fields = db.Column(JSON)
    engagement_score = db.Column(db.Float, default=0.0)
    last_activity = db.Column(db.DateTime)
    bio = db.Column(db.Text)  # User bio/description
    preferred_hub = db.Column(db.String(20), default='marketing')  # 'marketing' or 'sales'
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # -------------------------
    # Relationships
    # -------------------------
    companies = db.relationship(
        "Company",
        secondary=user_company,
        backref=db.backref("users", lazy="dynamic"),
    )

    default_company = db.relationship(
        "Company",
        foreign_keys=[default_company_id],
        backref="default_users",
    )

    replit_oauth = db.relationship(
        "ReplitOAuth",
        backref="user",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    # -------------------------
    # Helpers / properties
    # -------------------------
    def __repr__(self):
        return f"<User {self.username}>"

    @property
    def full_name(self):
        return (
            f"{self.first_name} {self.last_name}"
            if self.first_name and self.last_name
            else self.first_name
            or self.last_name
            or self.username
        )

    @property
    def is_admin_user(self):
        return (
            self.is_admin
            or self.segment == "admin"
            or (self.tags and "admin" in self.tags.lower())
        )

    # -------------------------
    # 🔑 DEFAULT COMPANY LOGIC
    # -------------------------
    def get_default_company(self):
        """Get the user's default company safely (never poisons the DB session)."""
        """
        Safe default company resolver.
        NEVER raises, NEVER poisons session.
        """
        logger = logging.getLogger(__name__)

        try:
            # 1) Explicit default_company_id
            if self.default_company_id:
                if hasattr(self, "default_company") and self.default_company is not None:
                    return self.default_company
                return Company.query.get(self.default_company_id)

            access = (
                UserCompanyAccess.query
                .filter_by(user_id=self.id, is_default=True)
                .join(Company, Company.id == UserCompanyAccess.company_id)
                .filter(Company.is_active == True)
                .first()
            )
            if access:
                return access.company

            return Company.query.filter_by(is_active=True).order_by(Company.id.asc()).first()
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.warning("Default company lookup failed for user %s: %s", self.id, exc)
            return None

    # -------------------------
    # Company / role helpers
    # -------------------------
    def set_default_company(self, company_id):
        self.default_company_id = company_id
        db.session.commit()

    def get_all_companies(self):
        return Company.query.filter_by(is_active=True).order_by(Company.name).all()

    def get_companies_safe(self):
        """Get companies safely for rendering contexts."""
        logger = logging.getLogger(__name__)
        try:
            return list(self.companies)
        except Exception as exc:
            logger.warning("Company list lookup failed for user %s: %s", self.id, exc)
            return []
    
    def get_company_access(self, company_id):
        return UserCompanyAccess.query.filter_by(
            user_id=self.id, company_id=company_id
        ).first()

    def get_company_role(self, company_id):
        access = self.get_company_access(company_id)
        return access.role if access else "viewer"

    def can_edit_company(self, company_id):
        if self.is_admin:
            return True
        access = self.get_company_access(company_id)
        return bool(access and access.can_edit())

    def can_admin_company(self, company_id):
        if self.is_admin:
            return True
        access = self.get_company_access(company_id)
        return bool(access and access.can_admin())

    def ensure_company_access(self, company_id, role="viewer"):
        access = self.get_company_access(company_id)
        if not access:
            access = UserCompanyAccess(
                user_id=self.id,
                company_id=company_id,
                role=role,
            )
            db.session.add(access)
            db.session.commit()
        return access


# ============================================================
# OAuth models
# ============================================================

class ReplitOAuth(db.Model):
    __tablename__ = "replit_oauth"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    browser_session_key = db.Column(db.String(255), nullable=False)
    provider = db.Column(db.String(50), nullable=False, default="replit_auth")
    token = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "browser_session_key",
            "provider",
            name="uq_replit_oauth_user_session_provider",
        ),
    )

    def __repr__(self):
        return f"<ReplitOAuth user_id={self.user_id}>"


class TikTokOAuth(db.Model):
    __tablename__ = "tiktok_oauth"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)

    open_id = db.Column(db.String(255), nullable=False)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text)
    expires_at = db.Column(db.DateTime)
    refresh_expires_at = db.Column(db.DateTime)
    scope = db.Column(db.String(500))
    token_type = db.Column(db.String(50), default="Bearer")

    display_name = db.Column(db.String(255))
    avatar_url = db.Column(db.String(500))
    raw_token = db.Column(JSON)
    status = db.Column(db.String(50), default="active")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class Company(db.Model):
    __tablename__ = "company"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    logo_path = db.Column(db.String(255))
    icon_path = db.Column(db.String(255))
    website_url = db.Column(db.String(255))
    primary_color = db.Column(db.String(20), default='#bc00ed')
    secondary_color = db.Column(db.String(20), default='#00ffb4')
    accent_color = db.Column(db.String(20), default='#e4055c')
    font_family = db.Column(db.String(100), default='Inter, sans-serif')
    apply_brand_colors = db.Column(db.Boolean, default=False)
    industry = db.Column(db.String(100))
    description = db.Column(Text)


class Contact(db.Model):
    __tablename__ = "contact"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    email = db.Column(db.String(255))
    first_name = db.Column(db.String(120))
    last_name = db.Column(db.String(120))
    company = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    tags = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    is_subscribed = db.Column(db.Boolean, default=True)
    source = db.Column(db.String(100))
    segment = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Campaign(db.Model):
    __tablename__ = "campaign"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    name = db.Column(db.String(255))
    status = db.Column(db.String(50))
    scheduled_at = db.Column(db.DateTime)
    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CampaignRecipient(db.Model):
    __tablename__ = "campaign_recipient"

    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True)
    opened_at = db.Column(db.DateTime)
    clicked_at = db.Column(db.DateTime)


class EmailTemplate(db.Model):
    __tablename__ = "email_template"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EmailTracking(db.Model):
    __tablename__ = "email_tracking"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True)
    event_type = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class BlogPost(db.Model):
    __tablename__ = "blog_post"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    title = db.Column(db.String(255))
    content = db.Column(db.Text)
    excerpt = db.Column(db.Text)
    category = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CompanySecret(db.Model):
    __tablename__ = "company_secret"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    key = db.Column(db.String(255))
    value = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ContactActivity(db.Model):
    __tablename__ = "contact_activity"

    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    activity_type = db.Column(db.String(50))
    touchpoint = db.Column(db.String(50))
    source = db.Column(db.String(100))
    title = db.Column(db.String(255))
    description = db.Column(db.Text)
    extra_data = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    contact = db.relationship("Contact", backref="activities")
    company = db.relationship("Company", backref="contact_activities")


class AnalyticsData(db.Model):
    __tablename__ = "analytics_data"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class BrandKit(db.Model):
    __tablename__ = "brand_kit"

    id = db.Column(db.Integer, primary_key=True)


class EmailComponent(db.Model):
    __tablename__ = "email_component"

    id = db.Column(db.Integer, primary_key=True)


class Poll(db.Model):
    __tablename__ = "poll"

    id = db.Column(db.Integer, primary_key=True)


class PollResponse(db.Model):
    __tablename__ = "poll_response"

    id = db.Column(db.Integer, primary_key=True)


class ABTest(db.Model):
    __tablename__ = "ab_test"

    id = db.Column(db.Integer, primary_key=True)


class Automation(db.Model):
    __tablename__ = "automation"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(50), default='active')
    trigger_type = db.Column(db.String(100))
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AutomationStep(db.Model):
    __tablename__ = "automation_step"

    id = db.Column(db.Integer, primary_key=True)
    step_order = db.Column(db.Integer)


class SMSCampaign(db.Model):
    __tablename__ = "sms_campaign"

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(50))
    scheduled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SMSRecipient(db.Model):
    __tablename__ = "sms_recipient"

    id = db.Column(db.Integer, primary_key=True)


class SMSTemplate(db.Model):
    __tablename__ = "sms_template"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SocialPost(db.Model):
    __tablename__ = "social_post"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    platform = db.Column(db.String(50))
    content = db.Column(db.Text)
    image_url = db.Column(db.String(500))
    status = db.Column(db.String(50))
    scheduled_at = db.Column(db.DateTime)
    published_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Segment(db.Model):
    __tablename__ = "segment"

    id = db.Column(db.Integer, primary_key=True)


class SegmentMember(db.Model):
    __tablename__ = "segment_member"

    id = db.Column(db.Integer, primary_key=True)


class WebForm(db.Model):
    __tablename__ = "web_form"

    id = db.Column(db.Integer, primary_key=True)


class FormSubmission(db.Model):
    __tablename__ = "form_submission"

    id = db.Column(db.Integer, primary_key=True)


class Event(db.Model):
    __tablename__ = "event"

    id = db.Column(db.Integer, primary_key=True)
    start_date = db.Column(db.DateTime)


class EventRegistration(db.Model):
    __tablename__ = "event_registration"

    id = db.Column(db.Integer, primary_key=True)


class EventTicket(db.Model):
    __tablename__ = "event_ticket"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=True)


class Product(db.Model):
    __tablename__ = "product"

    id = db.Column(db.Integer, primary_key=True)


class Order(db.Model):
    __tablename__ = "order"

    id = db.Column(db.Integer, primary_key=True)


class CalendarEvent(db.Model):
    __tablename__ = "calendar_event"

    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(50))
    start_date = db.Column(db.DateTime)


class AutomationTemplate(db.Model):
    __tablename__ = "automation_template"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    description = db.Column(db.Text)
    category = db.Column(db.String(100))
    trigger_type = db.Column(db.String(100))
    is_predefined = db.Column(db.Boolean, default=False)
    template_data = db.Column(db.Text)


class AutomationExecution(db.Model):
    __tablename__ = "automation_execution"

    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=True)
    status = db.Column(db.String(50), default='pending')
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)


class AutomationAction(db.Model):
    __tablename__ = "automation_action"

    id = db.Column(db.Integer, primary_key=True)


class LandingPage(db.Model):
    __tablename__ = "landing_page"

    id = db.Column(db.Integer, primary_key=True)


class NewsletterArchive(db.Model):
    __tablename__ = "newsletter_archive"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255))
    html_content = db.Column(db.Text)
    published_at = db.Column(db.DateTime)


class NonOpenerResend(db.Model):
    __tablename__ = "non_opener_resend"

    id = db.Column(db.Integer, primary_key=True)


class SEOKeyword(db.Model):
    __tablename__ = "seo_keyword"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    keyword = db.Column(db.String(255), nullable=True, default="")
    monthly_volume = db.Column(db.Integer, default=0)
    difficulty = db.Column(db.Integer, default=0)
    current_position = db.Column(db.Integer)
    is_tracking = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="seo_keywords")


class KeywordRanking(db.Model):
    __tablename__ = "keyword_ranking"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    keyword_id = db.Column(db.Integer, db.ForeignKey("seo_keyword.id"), nullable=True)
    position = db.Column(db.Integer)
    url = db.Column(db.Text)
    change = db.Column(db.Integer, default=0)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="keyword_rankings")
    keyword = db.relationship("SEOKeyword", backref="rankings")


class SEOBacklink(db.Model):
    __tablename__ = "seo_backlink"

    id = db.Column(db.Integer, primary_key=True)
    domain_authority = db.Column(db.Float)


class SEOCompetitor(db.Model):
    __tablename__ = "seo_competitor"

    id = db.Column(db.Integer, primary_key=True)


class SEOAudit(db.Model):
    __tablename__ = "seo_audit"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    url = db.Column(db.String(500))
    score = db.Column(db.Integer, default=0)
    issues = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="seo_audits")


class SEOPage(db.Model):
    __tablename__ = "seo_page"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    url = db.Column(db.String(500))
    title = db.Column(db.String(255))
    meta_description = db.Column(db.Text)
    h1_count = db.Column(db.Integer, default=0)
    word_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="seo_pages")


class CompetitorSnapshot(db.Model):
    __tablename__ = "competitor_snapshot"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    competitor_id = db.Column(db.Integer, db.ForeignKey("seo_competitor.id"), nullable=True)
    domain_authority = db.Column(db.Integer, default=0)
    organic_keywords = db.Column(db.Integer, default=0)
    organic_traffic = db.Column(db.Integer, default=0)
    backlinks = db.Column(db.Integer, default=0)
    recorded_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="competitor_snapshots")
    competitor = db.relationship("SEOCompetitor", backref="snapshots")


class TicketPurchase(db.Model):
    __tablename__ = "ticket_purchase"

    id = db.Column(db.Integer, primary_key=True)


class EventCheckIn(db.Model):
    __tablename__ = "event_check_in"

    id = db.Column(db.Integer, primary_key=True)


class SocialMediaAccount(db.Model):
    __tablename__ = "social_media_account"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    platform = db.Column(db.String(50), nullable=True, default="")
    account_name = db.Column(db.String(255))
    account_id = db.Column(db.String(255))
    access_token = db.Column(db.Text)
    refresh_token = db.Column(db.Text)
    token_expires_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    followers_count = db.Column(db.Integer, default=0)
    last_sync_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="social_media_accounts")


class SocialMediaSchedule(db.Model):
    __tablename__ = "social_media_schedule"

    id = db.Column(db.Integer, primary_key=True)


class AutomationTest(db.Model):
    __tablename__ = "automation_test"

    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=True)
    test_contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    test_data = db.Column(db.JSON)
    status = db.Column(db.String(50), default="pending")
    test_results = db.Column(db.JSON)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AutomationTriggerLibrary(db.Model):
    __tablename__ = "automation_trigger_library"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    trigger_type = db.Column(db.String(100))
    description = db.Column(db.Text)
    category = db.Column(db.String(100))
    trigger_config = db.Column(db.JSON)
    steps_template = db.Column(db.JSON)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AutomationABTest(db.Model):
    __tablename__ = "automation_ab_test"

    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=True)
    name = db.Column(db.String(200))
    variant_a = db.Column(db.JSON)
    variant_b = db.Column(db.JSON)
    status = db.Column(db.String(50), default="running")
    winner = db.Column(db.String(10))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Deal(db.Model):
    __tablename__ = "deal"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    stage = db.Column(db.String(100))
    value = db.Column(db.Float)
    name = db.Column(db.String(200))
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    expected_close_date = db.Column(db.DateTime)
    probability = db.Column(db.Float, default=0.5)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    company = db.relationship("Company", backref="deals")
    contact = db.relationship("Contact", backref="deals")


class LeadScore(db.Model):
    __tablename__ = "lead_score"

    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    score = db.Column(db.Integer, default=0)
    engagement_score = db.Column(db.Integer, default=0)
    fit_score = db.Column(db.Integer, default=0)
    intent_score = db.Column(db.Integer, default=0)
    last_activity_date = db.Column(db.DateTime)
    scoring_factors = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    contact = db.relationship("Contact", backref="lead_scores")


class SalesStage(db.Model):
    __tablename__ = "sales_stage"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    order = db.Column(db.Integer, default=0)
    probability = db.Column(db.Float, default=0.0)
    color = db.Column(db.String(20), default="#bc00ed")
    description = db.Column(db.Text)
    suggested_actions = db.Column(JSON)
    materials = db.Column(JSON)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="sales_stages")


class CRMTask(db.Model):
    __tablename__ = "crm_task"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    deal_id = db.Column(db.Integer, db.ForeignKey("deal.id"), nullable=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    task_type = db.Column(db.String(50))
    priority = db.Column(db.String(20), default="medium")
    status = db.Column(db.String(20), default="pending")
    due_date = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    reminder_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    company = db.relationship("Company", backref="crm_tasks")
    user = db.relationship("User", backref="crm_tasks")
    contact = db.relationship("Contact", backref="tasks")
    deal = db.relationship("Deal", backref="tasks")


class Meeting(db.Model):
    __tablename__ = "meeting"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    deal_id = db.Column(db.Integer, db.ForeignKey("deal.id"), nullable=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    meeting_type = db.Column(db.String(50))
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    location = db.Column(db.String(255))
    meeting_url = db.Column(db.String(500))
    status = db.Column(db.String(20), default="scheduled")
    notes = db.Column(db.Text)
    ai_prep_notes = db.Column(db.Text)
    ai_follow_up = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="meetings")
    user = db.relationship("User", backref="meetings")
    contact = db.relationship("Contact", backref="meetings")
    deal = db.relationship("Deal", backref="meetings")


class Playbook(db.Model):
    __tablename__ = "playbook"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    playbook_type = db.Column(db.String(50))
    content = db.Column(JSON)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="playbooks")


class Document(db.Model):
    __tablename__ = "document"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    file_path = db.Column(db.String(500))
    file_type = db.Column(db.String(50))
    document_type = db.Column(db.String(50))
    view_count = db.Column(db.Integer, default=0)
    share_count = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="documents")


class TouchpointEvent(db.Model):
    __tablename__ = "touchpoint_event"
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    visitor_id = db.Column(db.String(100))
    touchpoint_type = db.Column(db.String(50), nullable=False)
    source = db.Column(db.String(100))
    page_url = db.Column(db.String(500))
    referrer = db.Column(db.String(500))
    event_data = db.Column(JSON)
    ip_address = db.Column(db.String(50))
    user_agent = db.Column(db.String(500))
    session_id = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="touchpoint_events")
    contact = db.relationship("Contact", backref="touchpoint_events")


class PersonalizationRule(db.Model):
    __tablename__ = "personalization_rule"

    id = db.Column(db.Integer, primary_key=True)


class KeywordResearch(db.Model):
    __tablename__ = "keyword_research"

    id = db.Column(db.Integer, primary_key=True)


class AgentTask(db.Model):
    __tablename__ = "agent_task"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    agent_type = db.Column(db.String(100), nullable=False, index=True)
    agent_name = db.Column(db.String(200))
    task_name = db.Column(db.String(255))
    task_type = db.Column(db.String(100))
    task_data = db.Column(db.Text)
    result_data = db.Column(db.Text)
    status = db.Column(db.String(50), default='pending', index=True)
    priority = db.Column(db.Integer, default=5)
    scheduled_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_tasks")
    user = db.relationship("User", backref="agent_tasks")
    
    __table_args__ = (
        db.Index("ix_agent_task_company_status", "company_id", "status"),
        db.Index("ix_agent_task_agent_type_status", "agent_type", "status"),
    )


class AgentLog(db.Model):
    __tablename__ = "agent_log"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    agent_type = db.Column(db.String(100), nullable=False)
    agent_name = db.Column(db.String(200))
    activity_type = db.Column(db.String(100))
    details = db.Column(db.Text)
    status = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_logs")


class AgentReport(db.Model):
    __tablename__ = "agent_report"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    agent_type = db.Column(db.String(100), nullable=False, index=True)
    agent_name = db.Column(db.String(200))
    report_type = db.Column(db.String(50), nullable=False)
    report_frequency = db.Column(db.String(50))
    title = db.Column(db.String(500))
    summary = db.Column(db.Text)
    report_data = db.Column(db.Text)
    insights = db.Column(db.Text)
    recommendations = db.Column(db.Text)
    external_factors = db.Column(db.Text)
    report_period_start = db.Column(db.DateTime)
    report_period_end = db.Column(db.DateTime)
    status = db.Column(db.String(50), default='generated')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_reports")
    
    __table_args__ = (
        db.Index("ix_agent_report_company_agent", "company_id", "agent_type"),
    )


class AgentSchedule(db.Model):
    __tablename__ = "agent_schedule"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey('contact.id'), nullable=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    value = db.Column(db.Float)  # Deal value
    currency = db.Column(db.String(3), default='USD')
    stage = db.Column(db.String(50), default='prospecting')  # prospecting, qualification, proposal, negotiation, won, lost
    probability = db.Column(db.Float, default=0.0)  # 0-1.0
    expected_close_date = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_schedules")


class AgentDeliverable(db.Model):
    __tablename__ = "agent_deliverable"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    agent_type = db.Column(db.String(100), nullable=False, index=True)
    deliverable_type = db.Column(db.String(100))
    title = db.Column(db.String(500))
    description = db.Column(db.Text)
    request_data = db.Column(db.Text)
    output_data = db.Column(db.Text)
    file_path = db.Column(db.String(500))
    status = db.Column(db.String(50), default='requested')
    priority = db.Column(db.Integer, default=5)
    due_date = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    feedback = db.Column(db.Text)
    rating = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_deliverables")
    user = db.relationship("User", backref="agent_deliverables")
    
    __table_args__ = (
        db.Index("ix_agent_deliverable_company_status", "company_id", "status"),
    )


class AgentPerformance(db.Model):
    __tablename__ = "agent_performance"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    agent_type = db.Column(db.String(100), nullable=False)
    metric_type = db.Column(db.String(100))
    metric_value = db.Column(db.Float)
    metric_data = db.Column(db.Text)
    period_start = db.Column(db.DateTime)
    period_end = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_performance")


class AgentMemory(db.Model):
    __tablename__ = "agent_memory"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    agent_type = db.Column(db.String(100), nullable=False, index=True)
    memory_type = db.Column(db.String(100))
    memory_key = db.Column(db.String(255))
    memory_value = db.Column(db.Text)
    context = db.Column(db.Text)
    relevance_score = db.Column(db.Float, default=1.0)
    expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_memories")
    
    __table_args__ = (
        db.Index("ix_agent_memory_company_agent", "company_id", "agent_type"),
    )


class AgentConversation(db.Model):
    __tablename__ = "agent_conversation"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    agent_type = db.Column(db.String(100), nullable=False, index=True)
    session_id = db.Column(db.String(100))
    role = db.Column(db.String(20))
    message = db.Column(db.Text)
    message_metadata = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_conversations")
    user = db.relationship("User", backref="agent_conversations")
    
    __table_args__ = (
        db.Index("ix_agent_conversation_session", "company_id", "session_id"),
    )


class AgentReview(db.Model):
    __tablename__ = "agent_review"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    agent_type = db.Column(db.String(100), nullable=False)
    review_type = db.Column(db.String(100))
    item_type = db.Column(db.String(100))
    item_id = db.Column(db.Integer)
    item_content = db.Column(db.Text)
    analysis = db.Column(db.Text)
    suggestions = db.Column(db.Text)
    score = db.Column(db.Float)
    status = db.Column(db.String(50), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_reviews")
    user = db.relationship("User", backref="agent_reviews")


class MarketSignal(db.Model):
    __tablename__ = "market_signal"

    id = db.Column(db.Integer, primary_key=True)
    signal_date = db.Column(db.DateTime)


class StrategyRecommendation(db.Model):
    __tablename__ = "strategy_recommendation"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Competitor(db.Model):
    __tablename__ = "competitor"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))


class FacebookOAuth(db.Model):
    __tablename__ = "facebook_oauth"

    id = db.Column(db.Integer, primary_key=True)


class InstagramOAuth(db.Model):
    __tablename__ = "instagram_oauth"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)


class WordPressIntegration(db.Model):
    __tablename__ = "wordpress_integration"

    id = db.Column(db.Integer, primary_key=True)


class CompetitorProfile(db.Model):
    __tablename__ = "competitor_profile"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    name = db.Column(db.String(200))
    website_url = db.Column(db.String(255))
    industry = db.Column(db.String(100))
    description = db.Column(Text)
    strengths = db.Column(Text)
    weaknesses = db.Column(Text)
    market_position = db.Column(db.String(50))
    threat_level = db.Column(db.String(20), default='medium')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MultivariateTest(db.Model):
    __tablename__ = "multivariate_test"

    id = db.Column(db.Integer, primary_key=True)


class CampaignCost(db.Model):
    __tablename__ = "campaign_cost"

    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float)


class AttributionModel(db.Model):
    __tablename__ = "attribution_model"

    id = db.Column(db.Integer, primary_key=True)
    revenue = db.Column(db.Float)


class SurveyResponse(db.Model):
    __tablename__ = "survey_response"

    id = db.Column(db.Integer, primary_key=True)


class AgentConfiguration(db.Model):
    __tablename__ = "agent_configuration"

    id = db.Column(db.Integer, primary_key=True)


class CompanyIntegrationConfig(db.Model):
    __tablename__ = "company_integration_config"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    service_slug = db.Column(db.String(100), nullable=False)
    is_enabled = db.Column(db.Boolean, default=False)
    config_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class IntegrationAuditLog(db.Model):
    __tablename__ = "integration_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    config_id = db.Column(db.Integer, nullable=True)
    service_slug = db.Column(db.String(100), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    changes = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AgentAutomation(db.Model):
    __tablename__ = "agent_automation"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ApprovalQueue(db.Model):
    __tablename__ = "approval_queue"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    content_type = db.Column(db.String(100), nullable=False)
    content_id = db.Column(db.Integer, nullable=True)
    title = db.Column(db.String(500), nullable=False)
    content_preview = db.Column(db.Text)
    content_full = db.Column(db.Text)
    status = db.Column(db.String(50), default='pending', index=True)
    creation_mode = db.Column(db.String(50), default='manual')
    created_by_agent = db.Column(db.String(100))
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime)
    review_notes = db.Column(db.Text)
    ai_rationale = db.Column(db.Text)
    confidence_score = db.Column(db.Float)
    risk_level = db.Column(db.String(50), default='low')
    target_platform = db.Column(db.String(100))
    target_audience = db.Column(db.String(255))
    scheduled_publish_at = db.Column(db.DateTime)
    published_at = db.Column(db.DateTime)
    edit_history = db.Column(db.Text)
    version = db.Column(db.Integer, default=1)
    last_modified_by = db.Column(db.Integer)
    compliance_flags = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="approval_queue_items")

    def to_dict(self):
        return {
            'id': self.id,
            'company_id': self.company_id,
            'content_type': self.content_type,
            'content_id': self.content_id,
            'title': self.title,
            'content_preview': self.content_preview,
            'status': self.status,
            'creation_mode': self.creation_mode,
            'created_by_agent': self.created_by_agent,
            'created_by_user_id': self.created_by_user_id,
            'reviewed_by_user_id': self.reviewed_by_user_id,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'review_notes': self.review_notes,
            'ai_rationale': self.ai_rationale,
            'confidence_score': self.confidence_score,
            'risk_level': self.risk_level,
            'target_platform': self.target_platform,
            'target_audience': self.target_audience,
            'scheduled_publish_at': self.scheduled_publish_at.isoformat() if self.scheduled_publish_at else None,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'version': self.version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ApprovalAuditLog(db.Model):
    __tablename__ = "approval_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    approval_queue_id = db.Column(db.Integer, db.ForeignKey("approval_queue.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    previous_status = db.Column(db.String(50))
    new_status = db.Column(db.String(50))
    action_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action_by_agent = db.Column(db.String(100))
    notes = db.Column(db.Text)
    changes_made = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'approval_queue_id': self.approval_queue_id,
            'action': self.action,
            'previous_status': self.previous_status,
            'new_status': self.new_status,
            'action_by_user_id': self.action_by_user_id,
            'action_by_agent': self.action_by_agent,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ============= DEFAULT FEATURE TOGGLES =============
DEFAULT_FEATURE_TOGGLES = [
    # AI Agent Toggles (ALL OFF by default)
    {'feature_key': 'agent_brand_strategy', 'feature_name': 'Brand Strategy Agent', 'feature_category': 'ai_agents', 'agent_type': 'brand_strategy', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_content_seo', 'feature_name': 'Content & SEO Agent', 'feature_category': 'ai_agents', 'agent_type': 'content_seo', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_analytics', 'feature_name': 'Analytics Agent', 'feature_category': 'ai_agents', 'agent_type': 'analytics', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_creative_design', 'feature_name': 'Creative & Design Agent', 'feature_category': 'ai_agents', 'agent_type': 'creative_design', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_advertising', 'feature_name': 'Advertising Agent', 'feature_category': 'ai_agents', 'agent_type': 'advertising', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_social_media', 'feature_name': 'Social Media Agent', 'feature_category': 'ai_agents', 'agent_type': 'social_media', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_email_crm', 'feature_name': 'Email & CRM Agent', 'feature_category': 'ai_agents', 'agent_type': 'email_crm', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_sales_enablement', 'feature_name': 'Sales Enablement Agent', 'feature_category': 'ai_agents', 'agent_type': 'sales_enablement', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_retention', 'feature_name': 'Retention Agent', 'feature_category': 'ai_agents', 'agent_type': 'retention', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_operations', 'feature_name': 'Operations Agent', 'feature_category': 'ai_agents', 'agent_type': 'operations', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'agent_app_intelligence', 'feature_name': 'APP Agent', 'feature_category': 'ai_agents', 'agent_type': 'app_intelligence', 'is_enabled': False, 'require_approval': True},
    
    # Channel Toggles (ALL OFF by default)
    {'feature_key': 'channel_instagram', 'feature_name': 'Instagram Publishing', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'channel_facebook', 'feature_name': 'Facebook Publishing', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'channel_tiktok', 'feature_name': 'TikTok Publishing', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'channel_twitter', 'feature_name': 'X/Twitter Publishing', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'channel_linkedin', 'feature_name': 'LinkedIn Publishing', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'channel_email', 'feature_name': 'Email Campaigns', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'channel_sms', 'feature_name': 'SMS Campaigns', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'channel_blog', 'feature_name': 'Blog Publishing', 'feature_category': 'channels', 'is_enabled': False, 'require_approval': True},
    
    # Automation Toggles (ALL OFF by default)
    {'feature_key': 'automation_scheduled_posts', 'feature_name': 'Scheduled Post Publishing', 'feature_category': 'automation', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'automation_ai_content', 'feature_name': 'AI Content Generation', 'feature_category': 'automation', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'automation_workflow_triggers', 'feature_name': 'Automation Workflows', 'feature_category': 'automation', 'is_enabled': False, 'require_approval': True},
    {'feature_key': 'automation_ad_optimization', 'feature_name': 'Ad Campaign Optimization', 'feature_category': 'automation', 'is_enabled': False, 'require_approval': True},
    
    # Safety Toggles (ALL ON by default - these are safety features)
    {'feature_key': 'safety_content_review', 'feature_name': 'Content Review Required', 'feature_category': 'safety', 'is_enabled': True, 'require_approval': True},
    {'feature_key': 'safety_brand_check', 'feature_name': 'Brand Safety Check', 'feature_category': 'safety', 'is_enabled': True, 'require_approval': True},
    {'feature_key': 'safety_compliance_scan', 'feature_name': 'Compliance Scanning', 'feature_category': 'safety', 'is_enabled': True, 'require_approval': True},
    {'feature_key': 'safety_budget_limits', 'feature_name': 'Budget Limit Enforcement', 'feature_category': 'safety', 'is_enabled': True, 'require_approval': True},
]


def seed_feature_toggles(company_id):
    """Seed default feature toggles for a company with safe defaults (OFF)"""
    for toggle_data in DEFAULT_FEATURE_TOGGLES:
        existing = FeatureToggle.query.filter_by(
            company_id=company_id,
            feature_key=toggle_data['feature_key']
        ).first()
        
        if not existing:
            toggle = FeatureToggle(
                company_id=company_id,
                **toggle_data
            )
            db.session.add(toggle)
    
    db.session.commit()


class DemoRequest(db.Model):
    __tablename__ = 'demo_request'

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(50))
    company_name = db.Column(db.String(200))
    job_title = db.Column(db.String(200))
    team_size = db.Column(db.String(50))
    message = db.Column(db.Text)
    preferred_contact = db.Column(db.String(20), default='email')
    source_page = db.Column(db.String(100))
    status = db.Column(db.String(20), default='new')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DeletionRequest(db.Model):
    """Stores user data deletion requests submitted via /data-deletion."""
    __tablename__ = 'deletion_requests'

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.String(36), unique=True, nullable=False)
    email = db.Column(db.String(254), nullable=False)
    details = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
