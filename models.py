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
    custom_fields = db.Column(JSON)
    engagement_score = db.Column(db.Float, default=0.0)
    last_activity = db.Column(db.DateTime)
    bio = db.Column(Text)

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    status = db.Column(db.String(50))
    scheduled_at = db.Column(db.DateTime)
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


class AutomationExecution(db.Model):
    __tablename__ = "automation_execution"

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime)


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
    current_position = db.Column(db.Integer)


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SEOPage(db.Model):
    __tablename__ = "seo_page"

    id = db.Column(db.Integer, primary_key=True)


class TicketPurchase(db.Model):
    __tablename__ = "ticket_purchase"

    id = db.Column(db.Integer, primary_key=True)


class EventCheckIn(db.Model):
    __tablename__ = "event_check_in"

    id = db.Column(db.Integer, primary_key=True)


class SocialMediaAccount(db.Model):
    __tablename__ = "social_media_account"

    id = db.Column(db.Integer, primary_key=True)


class SocialMediaSchedule(db.Model):
    __tablename__ = "social_media_schedule"

    id = db.Column(db.Integer, primary_key=True)


class AutomationTest(db.Model):
    __tablename__ = "automation_test"

    id = db.Column(db.Integer, primary_key=True)


class AutomationTriggerLibrary(db.Model):
    __tablename__ = "automation_trigger_library"

    id = db.Column(db.Integer, primary_key=True)


class AutomationABTest(db.Model):
    __tablename__ = "automation_ab_test"

    id = db.Column(db.Integer, primary_key=True)


class Deal(db.Model):
    __tablename__ = "deal"

    id = db.Column(db.Integer, primary_key=True)
    stage = db.Column(db.String(100))
    value = db.Column(db.Float)


class LeadScore(db.Model):
    __tablename__ = "lead_score"

    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)


class PersonalizationRule(db.Model):
    __tablename__ = "personalization_rule"

    id = db.Column(db.Integer, primary_key=True)


class KeywordResearch(db.Model):
    __tablename__ = "keyword_research"

    id = db.Column(db.Integer, primary_key=True)


class AgentTask(db.Model):
    __tablename__ = "agent_task"

    id = db.Column(db.Integer, primary_key=True)
    agent_type = db.Column(db.String(100))
    status = db.Column(db.String(50))
    scheduled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AgentLog(db.Model):
    __tablename__ = "agent_log"

    id = db.Column(db.Integer, primary_key=True)
    agent_type = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AgentReport(db.Model):
    __tablename__ = "agent_report"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AgentSchedule(db.Model):
    __tablename__ = "agent_schedule"

    id = db.Column(db.Integer, primary_key=True)


class AgentDeliverable(db.Model):
    __tablename__ = "agent_deliverable"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AgentPerformance(db.Model):
    __tablename__ = "agent_performance"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AgentMemory(db.Model):
    __tablename__ = "agent_memory"

    id = db.Column(db.Integer, primary_key=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


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


class WordPressIntegration(db.Model):
    __tablename__ = "wordpress_integration"

    id = db.Column(db.Integer, primary_key=True)


class CompetitorProfile(db.Model):
    __tablename__ = "competitor_profile"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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


class AgentAutomation(db.Model):
    __tablename__ = "agent_automation"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
