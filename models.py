from datetime import datetime, timedelta
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

    # Per-user feature access flags (PWA access control — no PostHog required)
    can_access_mobile_inbox = db.Column(db.Boolean, default=False)
    can_access_full_app     = db.Column(db.Boolean, default=True)

    # ── Communications Hub feature toggles (per-user licensing) ──────────────
    # comms_hub_enabled: can access /twilio/comms hub page
    comms_hub_enabled         = db.Column(db.Boolean, default=False)
    # pwa_access_enabled: can install/use the Mobile Inbox PWA (/app/inbox)
    pwa_access_enabled        = db.Column(db.Boolean, default=False)
    # Granular channel toggles — ignored unless comms_hub_enabled or pwa_access_enabled
    calls_enabled             = db.Column(db.Boolean, default=True)
    sms_enabled               = db.Column(db.Boolean, default=True)
    voicemail_enabled         = db.Column(db.Boolean, default=False)
    ai_comms_enabled          = db.Column(db.Boolean, default=False)
    forwarding_enabled        = db.Column(db.Boolean, default=False)
    manage_users_enabled      = db.Column(db.Boolean, default=False)
    # License flag — admin explicitly marks user as having a comms license
    communications_license    = db.Column(db.Boolean, default=False)
    # Number assignment — 'shared' = company shared number, 'dedicated' = own DID
    assigned_number           = db.Column(db.String(20), nullable=True)
    number_type               = db.Column(db.String(20), default="shared")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        db.UniqueConstraint("user_id", "company_id", name="uq_user_company_access"),
        db.Index("ix_user_company_access_user", "user_id"),
        db.Index("ix_user_company_access_company", "company_id"),
    )

    ROLE_OWNER      = "owner"
    ROLE_ADMIN      = "admin"
    ROLE_MANAGER    = "manager"
    ROLE_EDITOR     = "editor"
    ROLE_VIEWER     = "viewer"
    ROLE_STAFF      = "staff"
    ROLE_INBOX_ONLY = "inbox_only"

    ROLE_ALIASES = {
        "superadmin": ROLE_OWNER,
        "super_admin": ROLE_OWNER,
        "super-admin": ROLE_OWNER,
        "administrator": ROLE_ADMIN,
        "tenant_admin": ROLE_ADMIN,
        "company_admin": ROLE_ADMIN,
        "supervisor": ROLE_MANAGER,
        "member": ROLE_STAFF,
        "user": ROLE_VIEWER,
    }

    ROLE_HIERARCHY = {
        ROLE_OWNER:      5,
        ROLE_ADMIN:      4,
        ROLE_MANAGER:    3,
        ROLE_EDITOR:     2,
        ROLE_VIEWER:     1,
        ROLE_STAFF:      1,
        ROLE_INBOX_ONLY: 0,
    }

    user = db.relationship("User", backref=db.backref("company_access", lazy="dynamic"))
    company = db.relationship(
        "Company", backref=db.backref("user_access", lazy="dynamic")
    )

    def __repr__(self):
        return f"<UserCompanyAccess user={self.user_id} company={self.company_id} role={self.role}>"

    @classmethod
    def normalize_role(cls, role):
        value = (role or cls.ROLE_VIEWER).strip().lower().replace(" ", "_")
        return cls.ROLE_ALIASES.get(value, value)

    @property
    def normalized_role(self):
        return self.normalize_role(self.role)

    def can_edit(self):
        return self.normalized_role in {self.ROLE_OWNER, self.ROLE_ADMIN, self.ROLE_MANAGER, self.ROLE_EDITOR}

    def can_admin(self):
        return self.normalized_role in {self.ROLE_OWNER, self.ROLE_ADMIN}

    def can_own(self):
        return self.normalized_role == self.ROLE_OWNER

    def can_manage_users(self):
        return self.normalized_role == self.ROLE_OWNER or bool(self.manage_users_enabled)

    def has_mobile_inbox_access(self) -> bool:
        """Mobile Inbox / PWA requires explicit admin approval.

        Rules:
        - owner/admin role: always allowed (implicit comms access)
        - any role with pwa_access_enabled=True: allowed
        - any role with can_access_mobile_inbox=True: allowed (legacy flag, backward compat)
        - all other roles: denied
        """
        if self.normalized_role in (self.ROLE_OWNER, self.ROLE_ADMIN):
            return True
        if self.pwa_access_enabled:
            return True
        return bool(self.can_access_mobile_inbox)

    def has_comms_hub_access(self) -> bool:
        """Communications Hub (/twilio/comms) access check.

        - owner/admin: always allowed
        - any role with comms_hub_enabled=True: allowed
        - any role with communications_license=True: allowed
        """
        if self.normalized_role in (self.ROLE_OWNER, self.ROLE_ADMIN):
            return True
        if self.comms_hub_enabled:
            return True
        return bool(self.communications_license)

    def has_full_app_access(self) -> bool:
        """Inbox-only role never gets full app; all other roles default to True."""
        if self.normalized_role == self.ROLE_INBOX_ONLY:
            return False
        if self.can_access_full_app is None:
            return True
        return bool(self.can_access_full_app)


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
    pwa_palette_id = db.Column(db.String(30), default='lux')
    pwa_theme_mode = db.Column(db.String(20), default='dark')
    notification_sounds_enabled = db.Column(db.Boolean, default=True)
    pwa_text_alerts_enabled = db.Column(db.Boolean, default=True)
    pwa_call_alerts_enabled = db.Column(db.Boolean, default=True)
    pwa_voicemail_alerts_enabled = db.Column(db.Boolean, default=True)
    pwa_unread_reminder_alerts_enabled = db.Column(db.Boolean, default=True)
    pwa_vibration_enabled = db.Column(db.Boolean, default=True)
    pwa_alerts_business_hours_only = db.Column(db.Boolean, default=True)
    pwa_quiet_hours_start = db.Column(db.String(5), nullable=True)
    pwa_quiet_hours_end = db.Column(db.String(5), nullable=True)
    pwa_unread_repeat_minutes = db.Column(db.Integer, default=1)
    pwa_preferences_updated_at = db.Column(db.DateTime)
    
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

    def set_password(self, password: str):
        """Set password_hash for legacy tests and admin-created users."""
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Verify a plaintext password against password_hash."""
        from werkzeug.security import check_password_hash
        return bool(self.password_hash and check_password_hash(self.password_hash, password))

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
            # 1) Explicit default_company_id (fall through if company was deleted)
            if self.default_company_id:
                if hasattr(self, "default_company") and self.default_company is not None:
                    return self.default_company
                found = db.session.get(Company, self.default_company_id)
                if found:
                    return found
                # Company was deleted — clear stale FK and fall through to fallbacks
                logger.warning("User %s default_company_id=%s not found, clearing", self.id, self.default_company_id)
                self.default_company_id = None
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            access = (
                UserCompanyAccess.query
                .filter_by(user_id=self.id, is_default=True)
                .join(Company, Company.id == UserCompanyAccess.company_id)
                .filter(Company.is_active == True)
                .first()
            )
            if access:
                return access.company

            user_companies = self.get_all_companies()
            if user_companies:
                return user_companies[0]

            # Platform admins are the last line of defense for tenant setup.
            # If a restore/deploy leaves the database with no active company (or
            # this admin is orphaned from every company), create or attach a safe
            # default company immediately so company-scoped pages do not crash on
            # ``None.id``. Non-admin users still require an explicit assignment.
            if self.is_admin:
                return self.ensure_default_company_context()

            return None
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

    def ensure_default_company_context(self, company_name=None):
        """Ensure an admin user has a usable active company context.

        Production restores can leave an admin account without a company or can
        leave only inactive companies behind. This method self-heals that state
        by creating (or reactivating) a fallback company, assigning the user as
        owner/admin, syncing the legacy ``user_company`` join table, and setting
        ``default_company_id``. It returns ``None`` instead of raising so callers
        can keep rendering graceful no-company states if the database is not
        writable.
        """
        logger = logging.getLogger(__name__)
        if not self.is_admin:
            return None

        try:
            company = Company.query.filter_by(is_active=True).order_by(Company.id.asc()).first()
            if company is None:
                company = Company.query.order_by(Company.id.asc()).first()
                if company is not None:
                    company.is_active = True
                    logger.warning(
                        "Company self-heal reactivated fallback company %s (%s)",
                        company.id,
                        company.name,
                    )

            if company is None:
                company = Company(
                    name=company_name or "LUXit Marketing",
                    is_active=True,
                    billing_tier="professional",
                    billing_status="active",
                    subscription_tier="professional",
                    onboarding_status="complete",
                )
                db.session.add(company)
                db.session.flush()
                logger.warning(
                    "Company self-heal created fallback company '%s' (id=%s)",
                    company.name,
                    company.id,
                )

            self.default_company_id = company.id

            access = UserCompanyAccess.query.filter_by(
                user_id=self.id, company_id=company.id
            ).first()
            if access is None:
                access = UserCompanyAccess(
                    user_id=self.id,
                    company_id=company.id,
                    role=UserCompanyAccess.ROLE_OWNER,
                    is_default=True,
                    can_access_full_app=True,
                    can_access_mobile_inbox=True,
                )
                db.session.add(access)
            else:
                if access.role not in (UserCompanyAccess.ROLE_OWNER, UserCompanyAccess.ROLE_ADMIN):
                    access.role = UserCompanyAccess.ROLE_OWNER
                access.is_default = True
                access.can_access_full_app = True
                access.can_access_mobile_inbox = True

            # Keep older code paths that still read the secondary relationship in
            # sync without depending on database-specific UPSERT syntax.
            linked = db.session.execute(
                user_company.select().where(
                    (user_company.c.user_id == self.id)
                    & (user_company.c.company_id == company.id)
                )
            ).first()
            if linked is None:
                db.session.execute(
                    user_company.insert().values(user_id=self.id, company_id=company.id)
                )

            db.session.commit()
            return company
        except Exception as exc:
            db.session.rollback()
            logger.warning("Company self-heal failed for user %s: %s", self.id, exc)
            return None

    def set_default_company(self, company_id):
        self.default_company_id = company_id
        db.session.commit()

    def get_all_companies(self):
        try:
            companies_by_access = (
                Company.query
                .join(UserCompanyAccess, UserCompanyAccess.company_id == Company.id)
                .filter(
                    UserCompanyAccess.user_id == self.id,
                    Company.is_active == True,
                )
                .all()
            )

            companies_by_legacy_link = (
                Company.query
                .join(
                    user_company,
                    (user_company.c.company_id == Company.id)
                    & (user_company.c.user_id == self.id),
                )
                .filter(Company.is_active == True)
                .all()
            )

            merged = {c.id: c for c in companies_by_access + companies_by_legacy_link}

            if self.default_company_id:
                default_company = Company.query.filter_by(
                    id=self.default_company_id, is_active=True
                ).first()
                if default_company:
                    merged[default_company.id] = default_company

            return sorted(merged.values(), key=lambda c: (c.name or "").lower())
        except Exception:
            return list(self.companies)

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

    # ── SaaS / Billing Integration ───────────────────────────────────────────
    stripe_customer_id         = db.Column(db.String(100))
    stripe_subscription_id     = db.Column(db.String(100))
    stripe_subscription_status = db.Column(db.String(50), default='none')
    stripe_price_lookup_key    = db.Column(db.String(100))
    supabase_tenant_id         = db.Column(db.String(100))
    mypaylink_id               = db.Column(db.String(100))
    n8n_contact_id             = db.Column(db.String(100))
    subscription_tier          = db.Column(db.String(50), default='free')
    billing_tier               = db.Column(db.String(50), default='free')
    billing_status             = db.Column(db.String(50), default='none')
    max_team_members           = db.Column(db.Integer, nullable=True)  # NULL = unlimited
    grace_period_ends_at       = db.Column(db.DateTime, nullable=True)
    current_period_start       = db.Column(db.DateTime, nullable=True)
    current_period_end         = db.Column(db.DateTime, nullable=True)
    cancel_at_period_end       = db.Column(db.Boolean, default=False)
    # ── One-time setup / onboarding fee ─────────────────────────────────────
    # Charged exactly once per company on the very first Checkout Session.
    # Once paid, future Checkout Sessions for this company must NOT include
    # the setup-fee line item again.
    setup_fee_paid                = db.Column(db.Boolean, default=False, nullable=False)
    setup_fee_paid_at             = db.Column(db.DateTime, nullable=True)
    setup_fee_checkout_session_id = db.Column(db.String(120), nullable=True)
    # ── Contact-usage / metered billing ─────────────────────────────────────
    # ``included_contacts`` is the per-plan allowance baked into the tier
    # (Starter=2,500 / Professional=50,000). ``contacts_used`` is the live
    # tenant count refreshed by the contact pipeline. ``contacts_overage``
    # is the surplus we report to Stripe via the metered subscription item
    # identified by ``stripe_contact_usage_subscription_item_id``.
    included_contacts                          = db.Column(db.Integer, nullable=True)
    contacts_used                              = db.Column(db.Integer, default=0, nullable=False)
    contacts_overage                           = db.Column(db.Integer, default=0, nullable=False)
    stripe_contact_usage_subscription_item_id  = db.Column(db.String(120), nullable=True)
    last_reported_contact_usage                = db.Column(db.Integer, default=0, nullable=False)
    last_usage_reported_at                     = db.Column(db.DateTime, nullable=True)
    onboarding_status          = db.Column(db.String(50), default='pending')
    implementation_status      = db.Column(db.String(50), default='none')
    saas_notes                 = db.Column(Text)

    # ── Team-seat helpers ───────────────────────────────────────────────────
    @property
    def team_member_count(self):
        """Number of distinct users currently attached to this company.

        Counts a union of: users with ``default_company_id == self.id`` and
        users joined via the ``UserCompanyAccess`` table.
        """
        from sqlalchemy import or_
        via_access = db.session.query(UserCompanyAccess.user_id) \
            .filter(UserCompanyAccess.company_id == self.id)
        via_default = db.session.query(User.id) \
            .filter(User.default_company_id == self.id)
        ids = {r[0] for r in via_access.all()} | {r[0] for r in via_default.all()}
        return len(ids)

    @property
    def team_seats_available(self):
        """Remaining seats. ``None`` means unlimited (Professional/Enterprise)."""
        if self.max_team_members is None:
            return None
        return max(0, self.max_team_members - self.team_member_count)

    def can_add_team_member(self) -> bool:
        """True if a new user can be added without exceeding ``max_team_members``."""
        if self.max_team_members is None:
            return True
        return self.team_member_count < self.max_team_members

    # ── Secret helpers ──────────────────────────────────────────────────────
    def set_secret(self, key_or_provider, key_or_value=None, value=None):
        """
        Store/update an encrypted secret.

        Two call styles:
          company.set_secret("OPENAI_API_KEY", "sk-123")          # key, value
          company.set_secret("twilio", "auth_token", "tok123")    # provider, key, value
        """
        from services.secret_vault import vault

        if value is not None:
            full_key   = f"{key_or_provider}_{key_or_value}"
            plain_value = value
        else:
            full_key   = key_or_provider
            plain_value = key_or_value

        if not plain_value:
            return

        try:
            enc_value = vault.encrypt(str(plain_value))
        except Exception:
            enc_value = str(plain_value)

        secret = CompanySecret.query.filter_by(
            company_id=self.id, key=full_key
        ).first()
        if secret:
            secret.value      = enc_value
            secret.updated_at = datetime.utcnow()
        else:
            secret = CompanySecret(
                company_id=self.id, key=full_key, value=enc_value
            )
            db.session.add(secret)
        db.session.commit()

    def get_secret(self, key_or_provider, sub_key=None):
        """
        Retrieve and decrypt a secret. Returns None if not found.

          company.get_secret("OPENAI_API_KEY")
          company.get_secret("twilio", "auth_token")
        """
        from services.secret_vault import vault

        full_key = (f"{key_or_provider}_{sub_key}" if sub_key
                    else key_or_provider)
        secret = CompanySecret.query.filter_by(
            company_id=self.id, key=full_key
        ).first()
        if not secret or not secret.value:
            return None
        try:
            return vault.decrypt(secret.value)
        except Exception:
            return secret.value   # Fallback for legacy unencrypted values

    def delete_secret(self, key_or_provider, sub_key=None):
        """Delete a secret for this company."""
        full_key = (f"{key_or_provider}_{sub_key}" if sub_key
                    else key_or_provider)
        secret = CompanySecret.query.filter_by(
            company_id=self.id, key=full_key
        ).first()
        if secret:
            db.session.delete(secret)
            db.session.commit()


class Contact(db.Model):
    __tablename__ = "contact"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    email = db.Column(db.String(255))
    name = db.Column(db.String(255))
    first_name = db.Column(db.String(120))
    last_name = db.Column(db.String(120))
    company = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    normalized_phone = db.Column(db.String(32), index=True)
    tags = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    is_subscribed = db.Column(db.Boolean, default=True)
    source = db.Column(db.String(100))
    segment = db.Column(db.String(100))
    sms_marketing_opt_in = db.Column(db.Boolean, default=False, nullable=False)
    sms_marketing_opt_in_at = db.Column(db.DateTime)
    sms_marketing_opt_in_source = db.Column(db.String(120))
    sms_opt_out_at = db.Column(db.DateTime)
    sms_consent_status = db.Column(db.String(30), default="unknown", nullable=False)
    do_not_market = db.Column(db.Boolean, default=False, nullable=False)
    do_not_email = db.Column(db.Boolean, default=False, nullable=False)
    do_not_sms = db.Column(db.Boolean, default=False, nullable=False)
    email_unsubscribed = db.Column(db.Boolean, default=False, nullable=False)
    sms_opted_out = db.Column(db.Boolean, default=False, nullable=False)
    marketing_preferences_reason = db.Column(db.String(255))
    marketing_preferences_source = db.Column(db.String(120))
    marketing_preferences_updated_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    marketing_preferences_updated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Campaign(db.Model):
    __tablename__ = "campaign"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    name = db.Column(db.String(255))
    subject = db.Column(db.String(500))
    template_id = db.Column(db.Integer, db.ForeignKey("email_template.id"), nullable=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=True)
    ab_test_id = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(50))
    scheduled_at = db.Column(db.DateTime)
    sent_at = db.Column(db.DateTime)
    revenue_generated = db.Column(db.Float, default=0.0)
    utm_keyword = db.Column(db.String(255))
    ai_generated = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def total_recipients(self):
        from sqlalchemy import func
        return db.session.query(func.count(CampaignRecipient.id)).filter_by(campaign_id=self.id).scalar() or 0

    @property
    def sent_count(self):
        return self.total_recipients if self.status in ('sent', 'active') else 0

    @property
    def failed_count(self):
        return 0


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
    """Encrypted per-company API secrets (multi-tenant safe)."""
    __tablename__ = "company_secret"

    id         = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    key        = db.Column(db.String(255), nullable=False)
    value      = db.Column(db.Text)          # stored encrypted via services.secret_vault
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="secrets")

    __table_args__ = (
        db.UniqueConstraint("company_id", "key", name="uq_company_secret_key"),
    )


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
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True)
    test_type = db.Column(db.String(50))
    variant_a = db.Column(db.Text)
    variant_b = db.Column(db.Text)
    split_ratio = db.Column(db.Float, default=0.5)
    winner = db.Column(db.String(10))
    status = db.Column(db.String(50), default='draft')
    started_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    campaign = db.relationship("Campaign", backref="ab_tests")


class Automation(db.Model):
    __tablename__ = "automation"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(50), default='active')
    trigger_type = db.Column(db.String(100))
    channel_type = db.Column(db.String(50), default='email')
    description = db.Column(db.Text, nullable=True)
    trigger_conditions = db.Column(db.JSON, nullable=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AutomationStep(db.Model):
    __tablename__ = "automation_step"

    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=True)
    step_type = db.Column(db.String(100))
    step_order = db.Column(db.Integer)
    template_id = db.Column(db.Integer)
    delay_hours = db.Column(db.Integer, default=0)
    conditions = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    automation = db.relationship("Automation", backref="steps")


class SMSCampaign(db.Model):
    __tablename__ = "sms_campaign"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    from_phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True)
    from_phone_number = db.Column(db.String(20), nullable=True)
    media_urls = db.Column(JSON, default=list)
    batch_size = db.Column(db.Integer, default=50)
    send_rate_per_minute = db.Column(db.Integer, default=60)
    canceled_at = db.Column(db.DateTime)
    archived_at = db.Column(db.DateTime)
    name = db.Column(db.String(255))
    objective = db.Column(db.Text)
    message = db.Column(db.String(1000))
    segment = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(50), default="draft")
    audience_filter = db.Column(JSON, default=dict)
    estimated_recipient_count = db.Column(db.Integer, default=0)
    test_sent_at = db.Column(db.DateTime)
    scheduled_at = db.Column(db.DateTime)
    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    recipients = db.relationship('SMSRecipient', backref='campaign', lazy='dynamic', foreign_keys='SMSRecipient.campaign_id')


class SMSRecipient(db.Model):
    __tablename__ = "sms_recipient"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("sms_campaign.id"), nullable=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    phone_number = db.Column(db.String(50))
    status = db.Column(db.String(50), default="pending")
    message_sid = db.Column(db.String(100))
    provider_message_sid = db.Column(db.String(255), nullable=True, index=True)
    error_code = db.Column(db.String(50))
    sent_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    replied_at = db.Column(db.DateTime)
    opted_out_at = db.Column(db.DateTime)
    provider_error_code = db.Column(db.String(50))
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("campaign_id", "contact_id", name="uq_sms_recipient_campaign_contact"),
        db.UniqueConstraint("provider_message_sid", name="uq_sms_recipient_provider_message_sid"),
    )

    contact = db.relationship("Contact", backref="sms_recipients")


class SMSTemplate(db.Model):
    __tablename__ = "sms_template"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    name = db.Column(db.String(255))
    message = db.Column(db.Text)
    category = db.Column(db.String(100))
    tone = db.Column(db.String(50))
    has_opt_out = db.Column(db.Boolean, default=True)
    is_compliant = db.Column(db.Boolean, default=True)
    usage_count = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SocialPost(db.Model):
    __tablename__ = "social_post"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    platform = db.Column(db.String(50))
    content = db.Column(db.Text)
    platforms = db.Column(db.JSON)
    media_urls = db.Column(db.JSON)
    image_url = db.Column(db.String(500))
    status = db.Column(db.String(50))
    scheduled_at = db.Column(db.DateTime)
    published_at = db.Column(db.DateTime)
    engagement_data = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Segment(db.Model):
    __tablename__ = "segment"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    segment_type = db.Column(db.String(100), default="behavioral")
    category = db.Column(db.String(120), nullable=True)
    match_mode = db.Column(db.String(20), default="all", nullable=False)
    triggers = db.Column(db.JSON, nullable=True)
    conditions = db.Column(db.JSON, nullable=True)
    actions = db.Column(db.JSON, nullable=True)
    is_dynamic = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = db.relationship("SegmentMember", backref="segment", lazy="dynamic", cascade="all, delete-orphan")


class SegmentMember(db.Model):
    __tablename__ = "segment_member"

    id = db.Column(db.Integer, primary_key=True)
    segment_id = db.Column(db.Integer, db.ForeignKey("segment.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=False)
    source = db.Column(db.String(80), default="manual", nullable=False)
    added_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    removed_at = db.Column(db.DateTime)
    removed_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    exclusion_reason = db.Column(db.String(255))
    is_excluded = db.Column(db.Boolean, default=False, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("segment_id", "contact_id", name="uq_segment_member_contact"),
        db.Index("ix_segment_member_segment_contact", "segment_id", "contact_id"),
    )

    contact = db.relationship("Contact", backref="segment_memberships")


class SMSKeywordRule(db.Model):
    __tablename__ = "sms_keyword_rule"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("sms_campaign.id"), nullable=True)
    keyword = db.Column(db.String(80), nullable=False)
    match_type = db.Column(db.String(30), default="exact")
    reply_message = db.Column(db.Text)
    priority = db.Column(db.Integer, default=100)
    is_active = db.Column(db.Boolean, default=True)
    business_hours_only = db.Column(db.Boolean, default=False)
    after_hours_message = db.Column(db.Text)
    tag_to_add = db.Column(db.String(100))
    segment_to_add = db.Column(db.String(100))
    notify_admin = db.Column(db.Boolean, default=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SMSAutoReplyRule(db.Model):
    __tablename__ = "sms_auto_reply_rule"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("sms_campaign.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    trigger_type = db.Column(db.String(50), default="inbound")
    reply_message = db.Column(db.Text)
    after_hours_message = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MarketingAuditLog(db.Model):
    __tablename__ = "marketing_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    entity_type = db.Column(db.String(80))
    entity_id = db.Column(db.Integer)
    action = db.Column(db.String(80), nullable=False)
    details = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WebForm(db.Model):
    __tablename__ = "web_form"

    id = db.Column(db.Integer, primary_key=True)


class FormSubmission(db.Model):
    __tablename__ = "form_submission"

    id = db.Column(db.Integer, primary_key=True)


class Event(db.Model):
    __tablename__ = "event"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    description = db.Column(db.Text)
    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    location = db.Column(db.String(255))
    max_attendees = db.Column(db.Integer)
    price = db.Column(db.Float, default=0.0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    registrations = db.relationship('EventRegistration', backref='event', lazy='dynamic')
    tickets = db.relationship('EventTicket', backref='event', lazy='dynamic')


class EventRegistration(db.Model):
    __tablename__ = "event_registration"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    status = db.Column(db.String(50), default='registered')
    payment_status = db.Column(db.String(50), default='pending')
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    title = db.Column(db.String(255))
    description = db.Column(db.Text)
    event_type = db.Column(db.String(50))
    channel = db.Column(db.String(50))
    content_type = db.Column(db.String(80))
    content_id = db.Column(db.Integer)
    status = db.Column(db.String(50))
    audience = db.Column(db.String(255))
    estimated_recipient_count = db.Column(db.Integer, default=0)
    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    all_day = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)
    color = db.Column(db.String(40))
    deadline_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "event_type": self.event_type,
            "channel": self.channel,
            "content_type": self.content_type,
            "content_id": self.content_id,
            "status": self.status,
            "audience": self.audience,
            "estimated_recipient_count": self.estimated_recipient_count or 0,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "all_day": bool(self.all_day),
            "color": self.color,
        }


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
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True)
    title = db.Column(db.String(255))
    slug = db.Column(db.String(255))
    html_content = db.Column(db.Text)
    published_at = db.Column(db.DateTime)
    view_count = db.Column(db.Integer, default=0)
    is_public = db.Column(db.Boolean, default=True)


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
    source_url = db.Column(db.Text)
    source_domain = db.Column(db.String(255))
    target_url = db.Column(db.Text)
    anchor_text = db.Column(db.String(500))
    link_type = db.Column(db.String(50))
    status = db.Column(db.String(50), default='active')
    domain_authority = db.Column(db.Float)
    page_authority = db.Column(db.Float)
    spam_score = db.Column(db.Float)
    first_seen = db.Column(db.DateTime)
    last_seen = db.Column(db.DateTime)
    lost_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SEOCompetitor(db.Model):
    __tablename__ = "seo_competitor"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    domain = db.Column(db.String(255))
    organic_traffic = db.Column(db.Integer, default=0)
    organic_keywords = db.Column(db.Integer, default=0)
    backlinks = db.Column(db.Integer, default=0)
    domain_authority = db.Column(db.Float)
    page_authority = db.Column(db.Float)
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    last_analyzed = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    _access_token = db.Column("access_token", db.Text)
    _refresh_token = db.Column("refresh_token", db.Text)
    token_expires_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    is_verified = db.Column(db.Boolean, default=False)
    follower_count = db.Column(db.Integer, default=0)
    last_synced = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="social_media_accounts")

    @staticmethod
    def _encrypt_social_secret(value):
        if not value:
            return None
        try:
            from services.secret_vault import vault
            return vault.encrypt(value)
        except Exception:
            # Preserve operability in development/test environments without a vault key; callers must still avoid rendering/logging raw values.
            return value

    @staticmethod
    def _decrypt_social_secret(value):
        if not value:
            return None
        try:
            from services.secret_vault import vault
            return vault.decrypt(value)
        except Exception:
            return value

    def set_access_token(self, token: str):
        self._access_token = self._encrypt_social_secret(token)

    def get_access_token(self) -> str:
        return self._decrypt_social_secret(self._access_token)

    @property
    def access_token(self):
        return self.get_access_token()

    @access_token.setter
    def access_token(self, token):
        self.set_access_token(token)

    def set_refresh_token(self, token: str):
        self._refresh_token = self._encrypt_social_secret(token)

    def get_refresh_token(self) -> str:
        return self._decrypt_social_secret(self._refresh_token)

    @property
    def refresh_token(self):
        return self.get_refresh_token()

    @refresh_token.setter
    def refresh_token(self, token):
        self.set_refresh_token(token)


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
    lead_score = db.Column(db.Integer, default=0)
    engagement_score = db.Column(db.Integer, default=0)
    behavior_score = db.Column(db.Integer, default=0)
    fit_score = db.Column(db.Integer, default=0)
    last_calculated = db.Column(db.DateTime)
    
    contact = db.relationship("Contact", backref="lead_scores")

    @property
    def score(self):
        return self.lead_score or 0


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
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    name = db.Column(db.String(255))
    segment_criteria = db.Column(db.Text)
    personalization_config = db.Column(db.Text)
    priority = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="personalization_rules")


class KeywordResearch(db.Model):
    __tablename__ = "keyword_research"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    keyword = db.Column(db.String(255))
    search_volume = db.Column(db.Integer, default=0)
    difficulty_score = db.Column(db.Float)
    competition = db.Column(db.String(50))
    seasonal_trend = db.Column(db.Text)
    intent = db.Column(db.String(100))
    related_keywords = db.Column(db.Text)
    status = db.Column(db.String(50), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="keyword_research")


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
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
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
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    agent_type = db.Column(db.String(100), nullable=False, index=True)
    agent_name = db.Column(db.String(200))
    report_type = db.Column(db.String(50), nullable=False)
    report_title = db.Column(db.String(500))
    report_data = db.Column(JSON)
    insights = db.Column(db.Text)
    period_start = db.Column(db.DateTime)
    period_end = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="agent_reports")


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
    agent_type = db.Column(db.String(100), nullable=False, index=True)
    agent_name = db.Column(db.String(200))
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    deliverable_type = db.Column(db.String(100))
    title = db.Column(db.String(500))
    description = db.Column(db.Text)
    content = db.Column(db.Text)
    content_format = db.Column(db.String(50))
    file_path = db.Column(db.String(500))
    extra_data = db.Column(db.Text)
    prompt_used = db.Column(db.Text)
    priority = db.Column(db.String(50), default='normal')
    requested_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    status = db.Column(db.String(50), default='requested')
    is_starred = db.Column(db.Boolean, default=False)
    view_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="agent_deliverables")


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
    source = db.Column(db.String(255))
    category = db.Column(db.String(100))
    key = db.Column(db.String(255))
    value = db.Column(db.Text)
    confidence = db.Column(db.Float)
    usage_count = db.Column(db.Integer, default=0)
    success_rate = db.Column(db.Float)
    expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    company = db.relationship("Company", backref="agent_memories")

    @property
    def memory_key(self):
        return self.key

    @property
    def memory_value(self):
        return self.value


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
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    source = db.Column(db.String(255))
    signal_type = db.Column(db.String(100))
    title = db.Column(db.String(500))
    summary = db.Column(db.Text)
    severity = db.Column(db.String(50))
    signal_date = db.Column(db.DateTime, default=datetime.utcnow)
    raw_data = db.Column(db.Text)
    is_actionable = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="market_signals")


class StrategyRecommendation(db.Model):
    __tablename__ = "strategy_recommendation"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    related_signal_id = db.Column(db.Integer)
    title = db.Column(db.String(500))
    recommendation_type = db.Column(db.String(100))
    priority = db.Column(db.String(50))
    status = db.Column(db.String(50), default='pending')
    rationale = db.Column(db.Text)
    action_steps = db.Column(db.Text)
    generated_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="strategy_recommendations")


class Competitor(db.Model):
    __tablename__ = "competitor"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    name = db.Column(db.String(255))
    website_url = db.Column(db.String(500))
    industry = db.Column(db.String(100))
    status = db.Column(db.String(50), default='active')
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="competitors")


class CompetitorContent(db.Model):
    """Content item captured for a tracked competitor."""
    __tablename__ = "competitor_content"

    id = db.Column(db.Integer, primary_key=True)
    competitor_id = db.Column(db.Integer, db.ForeignKey("competitor.id"), nullable=False, index=True)
    content_type = db.Column(db.String(100))
    title = db.Column(db.String(500))
    url = db.Column(db.String(500))
    summary = db.Column(db.Text)
    published_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    competitor = db.relationship("Competitor", backref=db.backref("content_items", lazy="select"))


class FacebookOAuth(db.Model):
    """
    Stores Facebook OAuth login records per user+company.
    Holds the Facebook App-Scoped User ID (ASID), display identity,
    and an encrypted long-lived user access token (~60 days).
    Page access tokens are stored separately in CompanySecret
    under key "facebook_page_tokens" (also encrypted).
    """
    __tablename__ = "facebook_oauth"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)

    # Facebook identity fields
    facebook_user_id = db.Column(db.String(64), nullable=True)   # App-Scoped User ID (ASID)
    display_name = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(254), nullable=True)
    avatar_url = db.Column(db.Text, nullable=True)

    # Encrypted long-lived user access token
    _access_token = db.Column("access_token", db.Text, nullable=True)

    # Selected Facebook Page (after page picker step)
    page_id = db.Column(db.String(64), nullable=True)
    page_name = db.Column(db.String(255), nullable=True)
    page_avatar_url = db.Column(db.Text, nullable=True)

    # Meta
    status = db.Column(db.String(20), default="active")
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref="facebook_oauths")
    company = db.relationship("Company", backref="facebook_oauths")

    def set_access_token(self, token: str):
        """Encrypt and store the access token."""
        if not token:
            self._access_token = None
            return
        try:
            from services.secret_vault import vault
            self._access_token = vault.encrypt(token)
        except Exception:
            self._access_token = token

    def get_access_token(self) -> str:
        """Decrypt and return the access token."""
        if not self._access_token:
            return None
        try:
            from services.secret_vault import vault
            return vault.decrypt(self._access_token)
        except Exception:
            return self._access_token


class InstagramOAuth(db.Model):
    __tablename__ = "instagram_oauth"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    instagram_account_id = db.Column(db.String(255))
    access_token = db.Column(db.Text)
    expires_at = db.Column(db.DateTime)
    username = db.Column(db.String(255))
    avatar_url = db.Column(db.String(500))
    status = db.Column(db.String(50), default='active')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="instagram_oauths")


class WordPressIntegration(db.Model):
    __tablename__ = "wordpress_integration"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)

    company = db.relationship("Company", backref="wordpress_integrations")


class CompetitorProfile(db.Model):
    __tablename__ = "competitor_profile"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    competitor_name = db.Column(db.String(200))
    website_url = db.Column(db.String(255))
    strengths = db.Column(Text)
    weaknesses = db.Column(Text)
    pricing_model = db.Column(db.String(100))
    market_share = db.Column(db.String(100))
    last_analyzed = db.Column(db.DateTime)
    notes = db.Column(Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def name(self):
        return self.competitor_name or ''


class MultivariateTest(db.Model):
    __tablename__ = "multivariate_test"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True)
    name = db.Column(db.String(255))
    variables = db.Column(db.Text)
    variants = db.Column(db.Text)
    sample_size = db.Column(db.Integer)
    confidence_level = db.Column(db.Float)
    status = db.Column(db.String(50), default='draft')
    winner = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CampaignCost(db.Model):
    __tablename__ = "campaign_cost"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True)
    cost_type = db.Column(db.String(50))
    amount = db.Column(db.Float)
    currency = db.Column(db.String(10))
    cost_date = db.Column(db.DateTime)

    campaign = db.relationship("Campaign", backref="costs")


class AttributionModel(db.Model):
    __tablename__ = "attribution_model"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    revenue = db.Column(db.Float)
    attribution_model = db.Column(db.String(50))
    confidence_score = db.Column(db.Float)

    campaign = db.relationship("Campaign", backref="attributions")


class SurveyResponse(db.Model):
    __tablename__ = "survey_response"

    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    survey_type = db.Column(db.String(50))
    score = db.Column(db.Integer, default=0)
    feedback = db.Column(db.Text)
    sentiment = db.Column(db.String(50))
    sentiment_score = db.Column(db.Float)
    topics = db.Column(db.Text)
    responded_at = db.Column(db.DateTime)


class AgentConfiguration(db.Model):
    __tablename__ = "agent_configuration"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    agent_type = db.Column(db.String(100))
    is_enabled = db.Column(db.Boolean, default=False)
    schedule_frequency = db.Column(db.String(50))
    configuration = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="agent_configurations")


class CompanyIntegrationConfig(db.Model):
    __tablename__ = "company_integration_config"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    service_slug = db.Column(db.String(100), nullable=False)
    is_active = db.Column(db.Boolean, default=False)
    config_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    @property
    def is_enabled(self):
        return self.is_active


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
    agent_type = db.Column(db.String(100))
    name = db.Column(db.String(255))
    description = db.Column(db.Text)
    schedule = db.Column(db.String(100))
    enabled = db.Column(db.Boolean, default=True)
    last_run = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    previous_version_id = db.Column(db.Integer)
    compliance_flags = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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


class FeatureToggle(db.Model):
    __tablename__ = "feature_toggle"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    feature_key = db.Column(db.String(255))
    feature_name = db.Column(db.String(255))
    feature_category = db.Column(db.String(100))
    description = db.Column(db.Text)
    is_enabled = db.Column(db.Boolean, default=False)
    agent_type = db.Column(db.String(100))
    allow_automated_creation = db.Column(db.Boolean, default=False)
    require_approval = db.Column(db.Boolean, default=True)
    confidence_threshold = db.Column(db.Float)
    daily_limit = db.Column(db.Integer)
    budget_ceiling = db.Column(db.Float)
    risk_tolerance = db.Column(db.String(50))
    content_aggressiveness = db.Column(db.String(50))
    brand_strictness = db.Column(db.String(50))
    platform_rules = db.Column(db.JSON)
    schedule_frequency = db.Column(db.String(50))
    active_hours_start = db.Column(db.Integer)
    active_hours_end = db.Column(db.Integer)
    emergency_stop = db.Column(db.Boolean, default=False)
    last_modified_by = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="feature_toggles")

    def to_dict(self):
        return {
            'id': self.id,
            'company_id': self.company_id,
            'feature_key': self.feature_key,
            'feature_name': self.feature_name,
            'feature_category': self.feature_category,
            'description': self.description,
            'is_enabled': self.is_enabled,
            'agent_type': self.agent_type,
            'allow_automated_creation': self.allow_automated_creation,
            'require_approval': self.require_approval,
            'confidence_threshold': self.confidence_threshold,
            'daily_limit': self.daily_limit,
            'budget_ceiling': self.budget_ceiling,
            'risk_tolerance': self.risk_tolerance,
            'content_aggressiveness': self.content_aggressiveness,
            'brand_strictness': self.brand_strictness,
            'schedule_frequency': self.schedule_frequency,
            'active_hours_start': self.active_hours_start,
            'active_hours_end': self.active_hours_end,
            'emergency_stop': self.emergency_stop,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
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
    normalized_phone = db.Column(db.String(32), index=True)
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


class LicenseRequest(db.Model):
    __tablename__ = 'license_request'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), nullable=False, index=True)
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    company_name = db.Column(db.String(200))
    plan = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text)
    status = db.Column(db.String(20), default='new', index=True)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(512))
    source_page = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    admin_notes = db.Column(db.Text)


class UserQuickLink(db.Model):
    __tablename__ = "user_quick_link"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    label = db.Column(db.String(50), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    icon = db.Column(db.String(50), default='link')
    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ActivityLog(db.Model):
    __tablename__ = "activity_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    action = db.Column(db.String(200), nullable=False)
    detail = db.Column(db.String(500))
    icon = db.Column(db.String(50), default='activity')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    __tablename__ = "notification"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True, index=True)
    event_type = db.Column(db.String(50), default='system')
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text)
    category = db.Column(db.String(50), default='system')
    icon = db.Column(db.String(50), default='bell')
    link = db.Column(db.String(500))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('notifications', lazy='dynamic'))
    phone_number = db.relationship("TwilioPhoneNumber", backref="notifications")


# ─────────────────────────────────────────────────────────────────────────────
# Feedback / Bug Reporting
# ─────────────────────────────────────────────────────────────────────────────
class FeedbackTicket(db.Model):
    """A user-submitted feedback / bug / UX / feature item.

    Tenant scoping: ``company_id`` is set from the submitting user's default
    company at submission time. All listing/detail endpoints enforce that
    regular users only see their own submissions; company admins see their
    company's submissions; platform admins (``User.is_admin``) see all.
    """
    __tablename__ = "feedback_ticket"

    # Allowed values — kept loose (strings) to avoid migrations on enum changes.
    TYPES = ("bug", "ux_issue", "feature_request", "general", "confused")
    SEVERITIES = ("low", "medium", "high", "critical")
    STATUSES = (
        "new", "reviewed", "priority_fix", "in_progress",
        "waiting_on_user", "closed", "rejected",
    )

    id = db.Column(db.Integer, primary_key=True)
    ticket_type = db.Column(db.String(30), default="general", nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    page_url = db.Column(db.String(500))
    user_agent = db.Column(db.Text)
    severity = db.Column(db.String(20), default="medium", nullable=False)
    status = db.Column(db.String(30), default="new", nullable=False)
    priority_fix = db.Column(db.Boolean, default=False, nullable=False)
    screenshot_path = db.Column(db.String(500))

    rating = db.Column(db.SmallInteger, nullable=True)
    allow_follow_up = db.Column(db.Boolean, default=True, nullable=False)
    screen_width = db.Column(db.Integer, nullable=True)
    screen_height = db.Column(db.Integer, nullable=True)
    posthog_session_id = db.Column(db.String(100), nullable=True)
    posthog_distinct_id = db.Column(db.String(100), nullable=True)
    posthog_replay_url = db.Column(db.String(500), nullable=True)
    admin_notes = db.Column(db.Text, nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    assigned_to_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id],
                           backref=db.backref("feedback_tickets", lazy="dynamic"))
    company = db.relationship("Company",
                              backref=db.backref("feedback_tickets", lazy="dynamic"))
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_user_id])
    comments = db.relationship(
        "FeedbackTicketComment",
        backref="ticket", cascade="all, delete-orphan",
        order_by="FeedbackTicketComment.created_at",
    )

    __table_args__ = (
        db.Index("ix_feedback_ticket_company_status", "company_id", "status"),
        db.Index("ix_feedback_ticket_user", "user_id"),
    )

    def to_dict(self, include_user=True):
        d = {
            "id": self.id,
            "ticket_type": self.ticket_type,
            "title": self.title,
            "description": self.description,
            "page_url": self.page_url,
            "severity": self.severity,
            "status": self.status,
            "priority_fix": self.priority_fix,
            "screenshot_url": (f"/{self.screenshot_path}" if self.screenshot_path else None),
            "rating": self.rating,
            "allow_follow_up": self.allow_follow_up,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "posthog_session_id": self.posthog_session_id,
            "posthog_distinct_id": self.posthog_distinct_id,
            "posthog_replay_url": self.posthog_replay_url,
            "admin_notes": self.admin_notes,
            "company_id": self.company_id,
            "assigned_to_user_id": self.assigned_to_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }
        if include_user and self.user:
            d["user"] = {
                "id": self.user.id,
                "username": getattr(self.user, "username", None),
                "email": getattr(self.user, "email", None),
            }
        return d


class FeedbackTicketComment(db.Model):
    """A comment on a feedback ticket. ``is_internal`` notes are admin-only."""
    __tablename__ = "feedback_ticket_comment"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("feedback_ticket.id"), nullable=False)
    author_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    author = db.relationship("User")

    __table_args__ = (
        db.Index("ix_feedback_comment_ticket", "ticket_id"),
    )


class InboxMessage(db.Model):
    __tablename__ = "inbox_message"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    sender_name = db.Column(db.String(200), nullable=False)
    sender_email = db.Column(db.String(200))
    subject = db.Column(db.String(300), nullable=False)
    body = db.Column(db.Text)
    category = db.Column(db.String(50), default='general')
    is_read = db.Column(db.Boolean, default=False)
    is_starred = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('inbox_messages', lazy='dynamic'))


class DashboardStickyNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    content = db.Column(db.Text, default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('sticky_notes', lazy='dynamic'))


class DashboardTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    title = db.Column(db.String(500), nullable=False)
    is_completed = db.Column(db.Boolean, default=False)
    due_date = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('dashboard_tasks', lazy='dynamic'))


class AffiliateLink(db.Model):
    __tablename__ = "affiliate_link"

    id = db.Column(db.Integer, primary_key=True)
    tracking_code = db.Column(db.String(100), unique=True)
    affiliate_id = db.Column(db.Integer)
    product_url = db.Column(db.Text)
    campaign_name = db.Column(db.String(200))
    commission_rate = db.Column(db.Float, default=10.0)
    commission_type = db.Column(db.String(20), default='percentage')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Influencer(db.Model):
    __tablename__ = "influencer"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200))
    instagram_handle = db.Column(db.String(100))
    tiktok_handle = db.Column(db.String(100))
    youtube_channel = db.Column(db.String(200))
    twitter_handle = db.Column(db.String(100))
    niche = db.Column(db.String(100))
    follower_count = db.Column(db.Integer, default=0)
    engagement_rate = db.Column(db.Float, default=0.0)
    tier = db.Column(db.String(20), default='micro')
    status = db.Column(db.String(20), default='prospect')
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AffiliateClick(db.Model):
    __tablename__ = "affiliate_click"

    id = db.Column(db.Integer, primary_key=True)
    tracking_code = db.Column(db.String(100))
    ip_address = db.Column(db.String(50))
    user_agent = db.Column(db.Text)
    referrer = db.Column(db.Text)
    clicked_at = db.Column(db.DateTime, default=datetime.utcnow)


class AffiliateConversion(db.Model):
    __tablename__ = "affiliate_conversion"

    id = db.Column(db.Integer, primary_key=True)
    tracking_code = db.Column(db.String(100))
    affiliate_id = db.Column(db.Integer)
    sale_amount = db.Column(db.Float, default=0.0)
    commission_amount = db.Column(db.Float, default=0.0)
    order_id = db.Column(db.String(100))
    status = db.Column(db.String(20), default='pending')
    converted_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime)


class UTMLink(db.Model):
    __tablename__ = "utm_link"

    id = db.Column(db.Integer, primary_key=True)
    campaign_name = db.Column(db.String(200))
    base_url = db.Column(db.Text)
    full_url = db.Column(db.Text)
    short_url = db.Column(db.String(300))
    utm_source = db.Column(db.String(100))
    utm_medium = db.Column(db.String(100))
    utm_campaign = db.Column(db.String(200))
    utm_term = db.Column(db.String(200))
    utm_content = db.Column(db.String(200))
    clicks = db.Column(db.Integer, default=0)
    conversions = db.Column(db.Integer, default=0)
    revenue = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer)


class PressRelease(db.Model):
    __tablename__ = "press_release"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"))
    title = db.Column(db.String(300), nullable=False)
    subtitle = db.Column(db.String(300))
    content = db.Column(db.Text)
    status = db.Column(db.String(20), default='draft')
    release_date = db.Column(db.Date)
    embargo_until = db.Column(db.DateTime)
    contact_info = db.Column(db.Text)
    pickups = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))


class MediaContact(db.Model):
    __tablename__ = "media_contact"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"))
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200))
    outlet = db.Column(db.String(200))
    beat = db.Column(db.String(200))
    relationship_score = db.Column(db.Integer, default=5)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Workflow(db.Model):
    __tablename__ = "workflow"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"))
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default='draft')
    nodes_data = db.Column(JSON)
    connections_data = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))


class HelpContent(db.Model):
    __tablename__ = "help_content"

    id = db.Column(db.Integer, primary_key=True)
    screen_key = db.Column(db.String(100), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    instructions = db.Column(db.Text)
    video_url = db.Column(db.String(500))
    pdf_url = db.Column(db.String(500))
    role_filter = db.Column(db.String(50))
    product_filter = db.Column(db.String(50))
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"))
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WalkthroughDef(db.Model):
    __tablename__ = "walkthrough_def"

    id = db.Column(db.Integer, primary_key=True)
    screen_key = db.Column(db.String(100), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    steps = db.Column(JSON, nullable=False, default=list)
    role_filter = db.Column(db.String(50))
    product_filter = db.Column(db.String(50))
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WalkthroughProgress(db.Model):
    __tablename__ = "walkthrough_progress"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    walkthrough_id = db.Column(db.Integer, db.ForeignKey("walkthrough_def.id"), nullable=False)
    completed_steps = db.Column(JSON, default=list)
    is_complete = db.Column(db.Boolean, default=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)


class XOAuth(db.Model):
    """Stores X (Twitter) OAuth 2.0 PKCE tokens per user/company."""
    __tablename__ = "x_oauth"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)

    x_user_id = db.Column(db.String(255), nullable=False)
    username = db.Column(db.String(255))
    display_name = db.Column(db.String(255))
    profile_image_url = db.Column(db.String(500))

    _access_token = db.Column("access_token", db.Text, nullable=False)
    _refresh_token = db.Column("refresh_token", db.Text)
    expires_at = db.Column(db.DateTime)
    scope = db.Column(db.String(500))
    token_type = db.Column(db.String(50), default="bearer")
    status = db.Column(db.String(50), default="active")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref="x_oauths")
    company = db.relationship("Company", backref="x_oauths")

    def set_access_token(self, token: str):
        if not token:
            self._access_token = None
            return
        try:
            from services.secret_vault import vault
            self._access_token = vault.encrypt(token)
        except Exception:
            self._access_token = token

    def get_access_token(self) -> str:
        if not self._access_token:
            return None
        try:
            from services.secret_vault import vault
            return vault.decrypt(self._access_token)
        except Exception:
            return self._access_token

    def set_refresh_token(self, token: str):
        if not token:
            self._refresh_token = None
            return
        try:
            from services.secret_vault import vault
            self._refresh_token = vault.encrypt(token)
        except Exception:
            self._refresh_token = token

    def get_refresh_token(self) -> str:
        if not self._refresh_token:
            return None
        try:
            from services.secret_vault import vault
            return vault.decrypt(self._refresh_token)
        except Exception:
            return self._refresh_token

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.utcnow() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.utcnow() >= self.expires_at - timedelta(minutes=10)


class OnboardingProgress(db.Model):
    __tablename__ = "onboarding_progress"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    setup_pct = db.Column(db.Integer, default=0)
    training_pct = db.Column(db.Integer, default=0)
    docs_pct = db.Column(db.Integer, default=0)
    go_live_ready = db.Column(db.Boolean, default=False)
    checklist_data = db.Column(JSON, default=dict)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# Password Reset Tokens
# ---------------------------------------------------------------------------

class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_token"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    token      = db.Column(db.String(100), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("reset_tokens", lazy="dynamic"))


# ---------------------------------------------------------------------------
# Twilio Multi-Tenant SMS / Call Platform
# ---------------------------------------------------------------------------

class TwilioAccount(db.Model):
    """Per-company Twilio credentials and global settings."""
    __tablename__ = "twilio_account"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, unique=True)

    _account_sid = db.Column("account_sid", db.Text)
    _auth_token  = db.Column("auth_token",  db.Text)
    messaging_service_sid = db.Column(db.String(60))
    from_phone   = db.Column(db.String(20))
    webhook_base_url  = db.Column(db.String(500))
    sms_fallback_url  = db.Column(db.String(500))
    voice_fallback_url = db.Column(db.String(500))

    is_active            = db.Column(db.Boolean, default=True)
    automation_enabled   = db.Column(db.Boolean, default=True)
    ai_mode              = db.Column(db.String(20), default="off")   # off | assist | auto
    ai_system_prompt     = db.Column(db.Text)
    missed_call_text     = db.Column(db.Text, default="Sorry we missed your call! Reply to schedule a callback.")
    after_hours_text     = db.Column(db.Text, default="Thanks for reaching out. We’re currently closed, but your message has been received. A team member will reply as soon as we’re back during business hours. Reply STOP to opt out.")
    after_hours_cooldown_minutes = db.Column(db.Integer, default=720, server_default="720")
    sms_forward_to       = db.Column(db.String(20))   # Forward all inbound SMS to this number
    call_forward_to      = db.Column(db.String(20))   # Forward all inbound calls to this number

    # Routing feature toggles
    sms_forwarding_enabled       = db.Column(db.Boolean, default=True,  server_default="true")
    voice_forwarding_enabled     = db.Column(db.Boolean, default=True,  server_default="true")
    after_hours_sms_enabled      = db.Column(db.Boolean, default=True,  server_default="true")
    after_hours_voicemail_enabled = db.Column(db.Boolean, default=True, server_default="true")

    # Voicemail
    voicemail_greeting_text      = db.Column(db.Text)
    voicemail_greeting_audio_url = db.Column(db.String(500), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref=db.backref("twilio_account", uselist=False))

    def set_account_sid(self, sid: str):
        if not sid:
            self._account_sid = None
            return
        try:
            from services.secret_vault import vault
            self._account_sid = vault.encrypt(sid)
        except Exception:
            self._account_sid = sid

    def get_account_sid(self) -> str:
        if not self._account_sid:
            return None
        try:
            from services.secret_vault import vault
            return vault.decrypt(self._account_sid)
        except Exception:
            return self._account_sid

    def set_auth_token(self, token: str):
        if not token:
            self._auth_token = None
            return
        try:
            from services.secret_vault import vault
            self._auth_token = vault.encrypt(token)
        except Exception:
            self._auth_token = token

    def get_auth_token(self) -> str:
        if not self._auth_token:
            return None
        try:
            from services.secret_vault import vault
            return vault.decrypt(self._auth_token)
        except Exception:
            return self._auth_token

    @property
    def is_configured(self) -> bool:
        return bool(self._account_sid and self._auth_token and
                    (self.messaging_service_sid or self.from_phone))


class TwilioConversation(db.Model):
    """One thread per (company, external phone number)."""
    __tablename__ = "twilio_conversation"

    id = db.Column(db.Integer, primary_key=True)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True, index=True)
    contact_id      = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    from_number     = db.Column(db.String(20), nullable=False)
    to_number       = db.Column(db.String(20))
    contact_name    = db.Column(db.String(200))
    contact_source  = db.Column(db.String(50), nullable=True)
    is_read         = db.Column(db.Boolean, default=False)
    is_opted_out    = db.Column(db.Boolean, default=False)
    sms_opt_in_at   = db.Column(db.DateTime, nullable=True)
    sms_opt_out_at  = db.Column(db.DateTime, nullable=True)
    is_first_contact = db.Column(db.Boolean, default=True)
    lead_captured   = db.Column(db.Boolean, default=False)
    tags            = db.Column(JSON, default=list)
    notes           = db.Column(db.Text)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    last_message_at = db.Column(db.DateTime)
    last_message_preview = db.Column(db.String(200))
    message_count   = db.Column(db.Integer, default=0)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = db.relationship(
        "TwilioMessage", backref="conversation", lazy="dynamic",
        order_by="TwilioMessage.created_at", cascade="all, delete-orphan"
    )
    company  = db.relationship("Company", backref="twilio_conversations")
    phone_number = db.relationship("TwilioPhoneNumber", backref="conversations")
    contact  = db.relationship("Contact", backref="twilio_conversations")

    __table_args__ = (
        db.UniqueConstraint("company_id", "from_number", name="uq_twilio_conv_company_from"),
    )


class TwilioMessage(db.Model):
    """Individual SMS message (inbound or outbound)."""
    __tablename__ = "twilio_message"

    id              = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("twilio_conversation.id"), nullable=False)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    twilio_sid      = db.Column(db.String(100), unique=True, nullable=True)
    direction       = db.Column(db.String(10), nullable=False)   # inbound | outbound
    from_number     = db.Column(db.String(20))
    to_number       = db.Column(db.String(20))
    body            = db.Column(db.Text)
    status          = db.Column(db.String(50), default="received")
    num_segments    = db.Column(db.Integer, default=1)
    media_urls      = db.Column(JSON)
    is_auto_reply   = db.Column(db.Boolean, default=False)
    rule_id         = db.Column(db.Integer, db.ForeignKey("auto_reply_rule.id"), nullable=True)
    error_code      = db.Column(db.String(20))
    error_message   = db.Column(db.Text)
    raw_payload     = db.Column(JSON)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AutoReplyRule(db.Model):
    """Keyword- and schedule-based auto-reply rules per company."""
    __tablename__ = "auto_reply_rule"

    id           = db.Column(db.Integer, primary_key=True)
    company_id   = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True, index=True)
    name         = db.Column(db.String(200), nullable=False)
    trigger_type = db.Column(db.String(50))
    # keyword_contains | keyword_exact | first_contact | after_hours | always | stop_keyword
    keywords     = db.Column(JSON, default=list)
    response     = db.Column(db.Text)
    is_active    = db.Column(db.Boolean, default=True)
    priority     = db.Column(db.Integer, default=0)
    action       = db.Column(db.String(50), default="reply")  # reply | forward | opt_out | tag
    forward_to   = db.Column(db.String(200))
    tag_value    = db.Column(db.String(100))
    match_count  = db.Column(db.Integer, default=0)
    active_days  = db.Column(JSON)           # [0,1,2,3,4] = Mon-Fri
    active_hours_start = db.Column(db.String(5))   # "09:00"
    active_hours_end   = db.Column(db.String(5))   # "17:00"
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="auto_reply_rules")
    phone_number = db.relationship("TwilioPhoneNumber", backref="auto_reply_rules")


class BusinessHours(db.Model):
    """Per-company business hours (one row per day of week)."""
    __tablename__ = "business_hours"

    id           = db.Column(db.Integer, primary_key=True)
    company_id   = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    day_of_week  = db.Column(db.Integer)    # 0=Mon ... 6=Sun
    is_open      = db.Column(db.Boolean, default=True)
    open_time    = db.Column(db.String(5), default="09:00")
    close_time   = db.Column(db.String(5), default="17:00")
    timezone     = db.Column(db.String(50), default="America/Chicago")

    company = db.relationship("Company", backref="business_hours")

    __table_args__ = (
        db.UniqueConstraint("company_id", "day_of_week", name="uq_biz_hours_company_day"),
    )


class TwilioCallLog(db.Model):
    """Inbound and outbound call records."""
    __tablename__ = "twilio_call_log"

    id           = db.Column(db.Integer, primary_key=True)
    company_id   = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True, index=True)
    twilio_sid   = db.Column(db.String(100))
    parent_call_sid = db.Column(db.String(100), nullable=True)
    direction    = db.Column(db.String(20))     # inbound | outbound
    from_number  = db.Column(db.String(20))
    to_number    = db.Column(db.String(20))
    forwarded_to_number = db.Column(db.String(20), nullable=True)
    contact_id   = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    customer_id  = db.Column(db.Integer, nullable=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    answered_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    status       = db.Column(db.String(50))     # completed | missed | no-answer | busy | failed
    duration     = db.Column(db.Integer, default=0)
    answered_at  = db.Column(db.DateTime, nullable=True)
    ended_at     = db.Column(db.DateTime, nullable=True)
    recording_url = db.Column(db.String(500), nullable=True)
    recording_sid = db.Column(db.String(100), nullable=True)
    voicemail_url = db.Column(db.String(500), nullable=True)
    voicemail_sid = db.Column(db.String(100), nullable=True)
    transcription_text = db.Column(db.Text, nullable=True)
    transcription_status = db.Column(db.String(30), default="not_requested")
    transcription_provider = db.Column(db.String(80), nullable=True)
    transcription_error = db.Column(db.Text, nullable=True)
    transcribed_at = db.Column(db.DateTime, nullable=True)
    caller_name  = db.Column(db.String(200))
    notes        = db.Column(db.Text)
    missed_text_sent = db.Column(db.Boolean, default=False)
    raw_payload  = db.Column(JSON)
    metadata_json = db.Column(JSON)
    is_read      = db.Column(db.Boolean, default=False)
    read_at      = db.Column(db.DateTime, nullable=True)
    read_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    callback_target = db.Column(db.String(20), nullable=True)
    is_archived  = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="twilio_call_logs")
    phone_number = db.relationship("TwilioPhoneNumber", backref="call_logs")


class PhoneSettings(db.Model):
    """Tenant-level PWA phone routing, voicemail, SMS, recording, and transcription settings."""
    __tablename__ = "phone_settings"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, unique=True, index=True)
    business_hours = db.Column(JSON, default=dict)
    timezone = db.Column(db.String(80), default="America/Los_Angeles")
    during_hours_route = db.Column(db.String(30), default="ring_pwa")  # ring_pwa | forward | voicemail | message_then_route
    after_hours_route = db.Column(db.String(30), default="voicemail")
    forward_number = db.Column(db.String(20))
    fallback_forward_number = db.Column(db.String(20))
    after_hours_forward_number = db.Column(db.String(20))
    after_hours_fallback_forward_number = db.Column(db.String(20))
    ring_duration_seconds = db.Column(db.Integer, default=25)
    voicemail_greeting = db.Column(db.Text)
    after_hours_voicemail_greeting = db.Column(db.Text)
    missed_call_sms_enabled = db.Column(db.Boolean, default=False)
    missed_call_sms_body = db.Column(db.Text)
    after_hours_sms_enabled = db.Column(db.Boolean, default=False)
    after_hours_sms_body = db.Column(db.Text)
    recording_enabled = db.Column(db.Boolean, default=False)
    transcription_enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref=db.backref("phone_settings", uselist=False))


class CallEvent(db.Model):
    """Idempotent audit trail for Twilio voice webhook events."""
    __tablename__ = "call_event"

    id = db.Column(db.Integer, primary_key=True)
    call_log_id = db.Column(db.Integer, db.ForeignKey("twilio_call_log.id"), nullable=False, index=True)
    event_type = db.Column(db.String(80), nullable=False)
    provider_event_id = db.Column(db.String(160), nullable=True)
    payload = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    call_log = db.relationship("TwilioCallLog", backref="events")

    __table_args__ = (
        db.UniqueConstraint("call_log_id", "event_type", "provider_event_id", name="uq_call_event_idempotency"),
    )


# ---------------------------------------------------------------------------
# SaaS Command Center
# ---------------------------------------------------------------------------

class SaasLicense(db.Model):
    """One license record per app per company (LUXit, MyPayLink, MyOrder, etc.)."""
    __tablename__ = "saas_license"

    id                = db.Column(db.Integer, primary_key=True)
    company_id        = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    app_name          = db.Column(db.String(100), nullable=False)
    plan              = db.Column(db.String(50))
    status            = db.Column(db.String(50), default="trial")   # trial | active | past_due | suspended | canceled
    start_date        = db.Column(db.DateTime)
    renewal_date      = db.Column(db.DateTime)
    tenant_url        = db.Column(db.String(255))
    enabled_features  = db.Column(JSON)
    stripe_product_id = db.Column(db.String(100))
    stripe_price_id   = db.Column(db.String(100))
    notes             = db.Column(db.Text)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="saas_licenses")


class CustomerOnboardingProject(db.Model):
    """Customer-facing onboarding project created when a deal is won."""
    __tablename__ = "customer_onboarding_project"

    id           = db.Column(db.Integer, primary_key=True)
    company_id   = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    contact_id   = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)
    deal_id      = db.Column(db.Integer, db.ForeignKey("deal.id"),    nullable=True)
    title        = db.Column(db.String(255))
    status       = db.Column(db.String(50), default="pending")   # pending | in_progress | completed | blocked
    due_date     = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company_ref = db.relationship("Company", backref="onboarding_projects")
    contact     = db.relationship("Contact", backref="onboarding_projects")
    deal        = db.relationship("Deal",    backref="onboarding_projects")


class CustomerOnboardingTask(db.Model):
    """Individual checklist task inside a CustomerOnboardingProject."""
    __tablename__ = "customer_onboarding_task"

    id           = db.Column(db.Integer, primary_key=True)
    project_id   = db.Column(db.Integer, db.ForeignKey("customer_onboarding_project.id"), nullable=False)
    company_id   = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    title        = db.Column(db.String(255), nullable=False)
    description  = db.Column(db.Text)
    status       = db.Column(db.String(50), default="pending")
    assigned_to  = db.Column(db.String(100))
    due_date     = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    sort_order   = db.Column(db.Integer, default=0)
    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    project = db.relationship("CustomerOnboardingProject", backref="tasks")


# ============================================================
# Integration Layer Models
# ============================================================

class IntegrationConnection(db.Model):
    """Platform-level or company-level integration connection record."""
    __tablename__ = "integration_connection"

    id             = db.Column(db.Integer, primary_key=True)
    company_id     = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    provider       = db.Column(db.String(50), nullable=False, index=True)
    status         = db.Column(db.String(30), default="unknown")  # connected|missing_config|error|disabled
    enabled        = db.Column(db.Boolean, default=True)
    config_json    = db.Column(db.Text)
    last_tested_at = db.Column(db.DateTime, nullable=True)
    last_success_at= db.Column(db.DateTime, nullable=True)
    last_error     = db.Column(db.Text, nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("company_id", "provider", name="uq_integration_connection_company_provider"),
    )


class IntegrationEvent(db.Model):
    """Log of inbound/outbound integration events (webhooks, API calls)."""
    __tablename__ = "integration_event"

    id           = db.Column(db.Integer, primary_key=True)
    company_id   = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    provider     = db.Column(db.String(50), nullable=False, index=True)
    event_type   = db.Column(db.String(100))
    external_id  = db.Column(db.String(255), nullable=True)
    payload_json = db.Column(db.Text)
    status       = db.Column(db.String(30), default="received")  # received|processed|failed
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)


class IntegrationErrorLog(db.Model):
    """Safe error log — never stores secrets, only sanitised details."""
    __tablename__ = "integration_error_log"

    id               = db.Column(db.Integer, primary_key=True)
    company_id       = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    provider         = db.Column(db.String(50), nullable=False, index=True)
    endpoint         = db.Column(db.String(255))
    error_message    = db.Column(db.Text)
    safe_details_json= db.Column(db.Text)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)


class ExternalSyncRecord(db.Model):
    """Tracks bidirectional sync state between LUXit entities and external systems."""
    __tablename__ = "external_sync_record"

    id                = db.Column(db.Integer, primary_key=True)
    company_id        = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    provider          = db.Column(db.String(50), nullable=False, index=True)
    local_entity_type = db.Column(db.String(100))
    local_entity_id   = db.Column(db.String(100))
    external_entity_id= db.Column(db.String(255))
    last_synced_at    = db.Column(db.DateTime, nullable=True)
    sync_status       = db.Column(db.String(30), default="pending")  # pending|synced|failed
    metadata_json     = db.Column(db.Text)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)


class SaasAutomationLog(db.Model):
    """Audit trail for Stripe webhooks, n8n triggers, and manual actions.

    For Stripe events, `stripe_event_id` is unique and indexed so we can
    reject duplicate webhook deliveries idempotently.
    """
    __tablename__ = "saas_automation_log"

    id              = db.Column(db.Integer, primary_key=True)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True)
    event_type      = db.Column(db.String(100))
    source          = db.Column(db.String(50))   # stripe | n8n | manual | system
    stripe_event_id = db.Column(db.String(120), unique=True, index=True, nullable=True)
    customer_id     = db.Column(db.String(120), index=True, nullable=True)
    subscription_id = db.Column(db.String(120), index=True, nullable=True)
    payload         = db.Column(JSON)
    status          = db.Column(db.String(50), default="success")   # success | failed | skipped | duplicate
    error           = db.Column(db.Text)
    received_at     = db.Column(db.DateTime, default=datetime.utcnow)
    processed_at    = db.Column(db.DateTime, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="automation_logs")


class PushSubscription(db.Model):
    """Web Push API subscription per user/device for inbox notifications."""
    __tablename__ = "push_subscription"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    device_key = db.Column(db.String(120), nullable=True, index=True)
    endpoint   = db.Column(db.Text, nullable=False, unique=True)
    p256dh     = db.Column(db.Text)
    auth_key   = db.Column(db.Text)
    user_agent = db.Column(db.Text)
    device_label = db.Column(db.String(160))
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)

    user    = db.relationship("User",    backref="push_subscriptions")
    company = db.relationship("Company", backref="push_subscriptions")


class GoogleOAuthToken(db.Model):
    """Stores Google OAuth tokens per user for Contacts sync."""
    __tablename__ = "google_oauth_token"

    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    access_token   = db.Column(db.Text)
    refresh_token  = db.Column(db.Text)
    token_expiry   = db.Column(db.DateTime, nullable=True)
    last_sync_at   = db.Column(db.DateTime, nullable=True)
    contacts_synced = db.Column(db.Integer, default=0)
    sync_error     = db.Column(db.Text, nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("google_oauth_token", uselist=False))


# ============================================================
# API Hub — Provider Credentials & Audit Log
# ============================================================

class ProviderCredential(db.Model):
    """
    Encrypted credential storage for all provider API keys / secrets.

    scope values:
        'platform'  — platform-wide (company_id NULL)
        'company'   — per-company override (company_id set)
        'user'      — per-user OAuth token (company_id + actor_user_id set)

    source values:
        'env'       — imported from environment variable
        'manual'    — entered manually via API Hub UI
        'oauth'     — obtained via OAuth redirect
    """
    __tablename__ = "provider_credential"

    id              = db.Column(db.Integer, primary_key=True)
    provider_slug   = db.Column(db.String(80),  nullable=False, index=True)
    scope           = db.Column(db.String(20),  nullable=False, default="platform")
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    key             = db.Column(db.String(255), nullable=False)
    encrypted_value = db.Column(db.Text,        nullable=True)
    source          = db.Column(db.String(30),  nullable=False, default="manual")
    imported_at     = db.Column(db.DateTime,    default=datetime.utcnow)
    last_tested_at  = db.Column(db.DateTime,    nullable=True)
    last_test_status= db.Column(db.String(30),  nullable=True)
    is_active       = db.Column(db.Boolean,     default=True)
    audit_notes     = db.Column(db.Text,        nullable=True)
    created_at      = db.Column(db.DateTime,    default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="provider_credentials")

    __table_args__ = (
        db.UniqueConstraint(
            "provider_slug", "scope", "company_id", "key",
            name="uq_provider_credential_provider_scope_company_key",
        ),
        db.Index("ix_provider_credential_provider_scope", "provider_slug", "scope"),
    )

    def masked_value(self, show_chars: int = 4) -> str:
        """Return masked display of the DECRYPTED plaintext tail (never raw secret)."""
        if not self.encrypted_value:
            return "****"
        try:
            from services.secret_vault import vault
            plain = vault.decrypt(self.encrypted_value)
            if plain and len(plain) > show_chars:
                return f"****{plain[-show_chars:]}"
            return "****"
        except Exception:
            # If decryption fails, fall back to a generic mask — never expose
            # the encrypted ciphertext tail (which is Fernet base64, not meaningful)
            tail = (self.key or "")[-2:] or "??"
            return f"****({tail})"


class ApiHubAuditLog(db.Model):
    """
    Immutable audit trail for all API Hub credential operations.

    action values: created | updated | rotated | tested | disabled |
                   imported | fallback_used | deleted

    NEVER store raw secret values in any column.
    """
    __tablename__ = "api_hub_audit_log"

    id            = db.Column(db.Integer, primary_key=True)
    provider_slug = db.Column(db.String(80),  nullable=False, index=True)
    action        = db.Column(db.String(50),  nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    scope         = db.Column(db.String(20),  nullable=True)
    company_id    = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    timestamp     = db.Column(db.DateTime,    default=datetime.utcnow, index=True)
    result        = db.Column(db.String(50),  nullable=True)
    notes         = db.Column(db.Text,        nullable=True)

    __table_args__ = (
        db.Index("ix_api_hub_audit_provider_ts", "provider_slug", "timestamp"),
    )


# ===========================================================================
# Phase A — Multi-Number VoIP Architecture
# ===========================================================================

class TwilioPhoneNumber(db.Model):
    """One row per Twilio phone number. Single source of truth for routing.
    TwilioAccount holds credentials; TwilioPhoneNumber holds per-number config."""
    __tablename__ = "twilio_phone_number"

    id                = db.Column(db.Integer, primary_key=True)
    company_id        = db.Column(db.Integer, db.ForeignKey("company.id"),        nullable=False)
    twilio_account_id = db.Column(db.Integer, db.ForeignKey("twilio_account.id"), nullable=True)

    phone_number   = db.Column(db.String(20), nullable=False, unique=True)
    friendly_name  = db.Column(db.String(100))
    twilio_sid     = db.Column(db.String(60))

    # luxit | mypaylink | myorder | myreview | unassigned
    app_assignment = db.Column(db.String(50), default="luxit")
    # local | toll_free | short_code | mobile
    number_type    = db.Column(db.String(20), default="local")

    sms_enabled    = db.Column(db.Boolean, default=True)
    voice_enabled  = db.Column(db.Boolean, default=True)
    mms_enabled    = db.Column(db.Boolean, default=False)

    sms_webhook_url   = db.Column(db.String(500))
    voice_webhook_url = db.Column(db.String(500))
    status_callback_webhook_url = db.Column(db.String(500))

    sms_forward_to         = db.Column(db.String(20))
    sms_forwarding_enabled = db.Column(db.Boolean, default=False)
    auto_reply_enabled     = db.Column(db.Boolean, default=True)
    number_auto_reply_text = db.Column(db.Text)
    campaign_sender_enabled = db.Column(db.Boolean, default=True)
    campaign_default_batch_size = db.Column(db.Integer, default=50)
    campaign_send_rate_per_minute = db.Column(db.Integer, default=60)
    allow_global_fallback = db.Column(db.Boolean, default=False)

    call_forward_to          = db.Column(db.String(20))
    voice_forwarding_enabled = db.Column(db.Boolean, default=True)
    ring_timeout             = db.Column(db.Integer, default=25)

    business_hours           = db.Column(JSON, default=dict)
    timezone                 = db.Column(db.String(80), default="America/Los_Angeles")
    during_hours_route       = db.Column(db.String(30), default="ring_pwa")
    after_hours_route        = db.Column(db.String(30), default="voicemail")
    browser_calling_enabled  = db.Column(db.Boolean, default=True)
    cell_callback_enabled    = db.Column(db.Boolean, default=True)
    wifi_only                = db.Column(db.Boolean, default=False)
    mobile_data_allowed      = db.Column(db.Boolean, default=True)
    fallback_behavior        = db.Column(db.String(30), default="cell_callback")
    caller_id_display_name   = db.Column(db.String(120))

    voicemail_greeting_text       = db.Column(db.Text)
    voicemail_greeting_audio_url  = db.Column(db.String(500))
    missed_call_text              = db.Column(db.Text)
    after_hours_text              = db.Column(db.Text)
    after_hours_sms_enabled       = db.Column(db.Boolean, default=True)
    after_hours_voicemail_enabled = db.Column(db.Boolean, default=True)

    is_active  = db.Column(db.Boolean, default=True)
    is_primary = db.Column(db.Boolean, default=False)
    notes      = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company        = db.relationship("Company",       backref="phone_numbers")
    twilio_account = db.relationship("TwilioAccount", backref="phone_numbers")

    def __repr__(self):
        return f"<TwilioPhoneNumber {self.phone_number} co={self.company_id}>"




class PhoneNumberUserPermission(db.Model):
    """Per-user permissions for a specific Twilio phone number/line."""
    __tablename__ = "phone_number_user_permission"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)

    can_access_pwa = db.Column(db.Boolean, default=True)
    can_view_sms = db.Column(db.Boolean, default=True)
    can_send_sms = db.Column(db.Boolean, default=True)
    can_view_calls = db.Column(db.Boolean, default=True)
    can_call = db.Column(db.Boolean, default=True)
    can_view_voicemail = db.Column(db.Boolean, default=True)
    can_manage_number = db.Column(db.Boolean, default=False)
    can_send_campaigns = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    phone_number = db.relationship("TwilioPhoneNumber", backref="user_permissions")
    user = db.relationship("User", backref="phone_number_permissions")
    company = db.relationship("Company", backref="phone_number_user_permissions")

    __table_args__ = (
        db.UniqueConstraint("phone_number_id", "user_id", name="uq_phone_number_user_permission"),
    )

class PWADevice(db.Model):
    """Registered PWA/work-phone device for Communications runtime readiness."""
    __tablename__ = "pwa_device"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True, index=True)
    device_key = db.Column(db.String(120), nullable=False)
    device_name = db.Column(db.String(120))
    browser = db.Column(db.String(120))
    device_type = db.Column(db.String(80))
    user_agent = db.Column(db.Text)
    online_status = db.Column(db.String(20), default="online")
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow)
    push_enabled = db.Column(db.Boolean, default=False)
    microphone_permission = db.Column(db.String(30), default="unknown")
    pwa_installed = db.Column(db.Boolean, default=False)
    wifi_only = db.Column(db.Boolean, default=False)
    cellular_callback_enabled = db.Column(db.Boolean, default=False)
    mobile_data_calling_allowed = db.Column(db.Boolean, default=False)
    default_calling_method = db.Column(db.String(30), default="browser")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("pwa_devices", lazy="dynamic"))
    company = db.relationship("Company", backref=db.backref("pwa_devices", lazy="dynamic"))
    phone_number = db.relationship("TwilioPhoneNumber", backref=db.backref("pwa_devices", lazy="dynamic"))

    __table_args__ = (
        db.UniqueConstraint("company_id", "user_id", "device_key", name="uq_pwa_device_user_key"),
    )


class VoiceVoicemailBox(db.Model):
    """Named voicemail box — attached to a number, extension, or routing rule."""
    __tablename__ = "voice_voicemail_box"

    id              = db.Column(db.Integer, primary_key=True)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"),             nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True)

    name               = db.Column(db.String(100), nullable=False)
    greeting_text      = db.Column(db.Text)
    greeting_audio_url = db.Column(db.String(500))
    email_notify       = db.Column(db.String(200))
    pin                = db.Column(db.String(10))
    max_length_secs    = db.Column(db.Integer, default=180)
    is_active          = db.Column(db.Boolean, default=True)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

    company  = db.relationship("Company", backref="voicemail_boxes")
    messages = db.relationship("VoiceVoicemailMessage", backref="box",
                               cascade="all, delete-orphan")


class VoiceExtension(db.Model):
    """Internal dial-by-extension directory entry."""
    __tablename__ = "voice_extension"

    id              = db.Column(db.Integer, primary_key=True)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"),             nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True)

    extension_number    = db.Column(db.String(10),  nullable=False)
    name                = db.Column(db.String(100), nullable=False)
    description         = db.Column(db.Text)
    # external | user | voicemail
    destination_type    = db.Column(db.String(30), default="external")
    destination_number  = db.Column(db.String(20))
    destination_user_id = db.Column(db.Integer, db.ForeignKey("user.id"),                nullable=True)
    voicemail_box_id    = db.Column(db.Integer, db.ForeignKey("voice_voicemail_box.id"), nullable=True)

    ring_timeout = db.Column(db.Integer, default=20)
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    company          = db.relationship("Company", backref="voice_extensions")
    destination_user = db.relationship("User",    backref="voice_extensions")

    __table_args__ = (
        db.UniqueConstraint("company_id", "extension_number", name="uq_voice_ext_company_num"),
    )


class VoiceIVRMenu(db.Model):
    """IVR menu tree for a phone number."""
    __tablename__ = "voice_ivr_menu"

    id              = db.Column(db.Integer, primary_key=True)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"),             nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True)

    name               = db.Column(db.String(100), nullable=False)
    greeting_text      = db.Column(db.Text)
    greeting_audio_url = db.Column(db.String(500))
    timeout_seconds    = db.Column(db.Integer, default=5)
    max_attempts       = db.Column(db.Integer, default=3)
    invalid_input_text = db.Column(db.Text,    default="Invalid selection. Please try again.")
    is_active          = db.Column(db.Boolean, default=False)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at         = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="ivr_menus")
    options = db.relationship("VoiceIVROption", backref="menu",
                              cascade="all, delete-orphan",
                              order_by="VoiceIVROption.digit",
                              foreign_keys="VoiceIVROption.menu_id")


class VoiceIVROption(db.Model):
    """One keypress -> destination within an IVR menu."""
    __tablename__ = "voice_ivr_option"

    id      = db.Column(db.Integer, primary_key=True)
    menu_id = db.Column(db.Integer, db.ForeignKey("voice_ivr_menu.id"), nullable=False)
    digit   = db.Column(db.String(1), nullable=False)
    label   = db.Column(db.String(100))

    # forward | extension | voicemail | submenu | hang_up
    action           = db.Column(db.String(30), default="forward")
    forward_to       = db.Column(db.String(20))
    extension_id     = db.Column(db.Integer, db.ForeignKey("voice_extension.id"),     nullable=True)
    submenu_id       = db.Column(db.Integer, db.ForeignKey("voice_ivr_menu.id"),      nullable=True)
    voicemail_box_id = db.Column(db.Integer, db.ForeignKey("voice_voicemail_box.id"), nullable=True)
    say_text         = db.Column(db.Text)

    extension = db.relationship("VoiceExtension", backref="ivr_options",
                                foreign_keys=[extension_id])
    submenu   = db.relationship("VoiceIVRMenu",   foreign_keys=[submenu_id])

    __table_args__ = (
        db.UniqueConstraint("menu_id", "digit", name="uq_ivr_option_menu_digit"),
    )


class VoiceRoutingRule(db.Model):
    """Priority-ordered call routing rules for a phone number."""
    __tablename__ = "voice_routing_rule"

    id              = db.Column(db.Integer, primary_key=True)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"),             nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True)

    name              = db.Column(db.String(100), nullable=False)
    priority          = db.Column(db.Integer, default=0)
    # always | business_hours | after_hours | caller_id
    condition_type    = db.Column(db.String(30), default="always")
    caller_id_pattern = db.Column(db.String(50))

    # forward | voicemail | ivr | extension | hang_up
    action           = db.Column(db.String(30), default="voicemail")
    forward_to       = db.Column(db.String(20))
    extension_id     = db.Column(db.Integer, db.ForeignKey("voice_extension.id"),     nullable=True)
    ivr_menu_id      = db.Column(db.Integer, db.ForeignKey("voice_ivr_menu.id"),      nullable=True)
    voicemail_box_id = db.Column(db.Integer, db.ForeignKey("voice_voicemail_box.id"), nullable=True)

    ring_timeout = db.Column(db.Integer, default=25)
    whisper_text = db.Column(db.Text)
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="voice_routing_rules")


class VoiceForwardingRule(db.Model):
    """Sequential or simultaneous ring-group forwarding rules."""
    __tablename__ = "voice_forwarding_rule"

    id              = db.Column(db.Integer, primary_key=True)
    company_id      = db.Column(db.Integer, db.ForeignKey("company.id"),             nullable=False)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True)
    routing_rule_id = db.Column(db.Integer, db.ForeignKey("voice_routing_rule.id"),  nullable=True)

    name            = db.Column(db.String(100))
    # sequential | simultaneous
    ring_strategy   = db.Column(db.String(20), default="sequential")
    # [{number: "+1...", timeout: 20, label: "Luke"}]
    numbers         = db.Column(JSON, default=list)
    fallback_action = db.Column(db.String(20), default="voicemail")
    is_active       = db.Column(db.Boolean,  default=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="forwarding_rules")


class VoiceVoicemailMessage(db.Model):
    """Individual voicemail recording left by a caller."""
    __tablename__ = "voice_voicemail_message"

    id               = db.Column(db.Integer, primary_key=True)
    company_id       = db.Column(db.Integer, db.ForeignKey("company.id"),             nullable=False)
    phone_number_id  = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=True, index=True)
    voicemail_box_id = db.Column(db.Integer, db.ForeignKey("voice_voicemail_box.id"), nullable=True)
    call_log_id      = db.Column(db.Integer, db.ForeignKey("twilio_call_log.id"),     nullable=True)

    from_number   = db.Column(db.String(20))
    to_number     = db.Column(db.String(20))
    call_sid      = db.Column(db.String(100))
    recording_sid = db.Column(db.String(100))
    recording_url = db.Column(db.String(500))
    duration_secs = db.Column(db.Integer, default=0)
    transcript    = db.Column(db.Text)
    transcription_text = db.Column(db.Text)
    transcription_status = db.Column(db.String(30), default="not_requested")
    transcription_provider = db.Column(db.String(80))
    transcription_error = db.Column(db.Text)
    transcribed_at = db.Column(db.DateTime)
    read_at       = db.Column(db.DateTime)
    read_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    is_read       = db.Column(db.Boolean, default=False)
    is_deleted    = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    company  = db.relationship("Company",       backref="voicemail_messages")
    phone_number = db.relationship("TwilioPhoneNumber", backref="voicemail_messages")
    call_log = db.relationship("TwilioCallLog", backref="voicemail_message", uselist=False)


class VoiceGreeting(db.Model):
    """Per-phone-number voicemail greeting asset/configuration."""
    __tablename__ = "voice_greeting"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, index=True)
    phone_number_id = db.Column(db.Integer, db.ForeignKey("twilio_phone_number.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    greeting_type = db.Column(db.String(30), nullable=False, default="standard")
    text_body = db.Column(db.Text)
    audio_url = db.Column(db.String(500))
    storage_path = db.Column(db.String(500))
    voice_name = db.Column(db.String(120))
    is_active = db.Column(db.Boolean, default=False, nullable=False, index=True)
    applies_to = db.Column(db.String(40), default="voicemail_default", nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = db.relationship("Company", backref="voice_greetings")
    phone_number = db.relationship("TwilioPhoneNumber", backref="voice_greetings")

class CallRecording(db.Model):
    """Full call recording (any recorded call leg, not just voicemail)."""
    __tablename__ = "call_recording"

    id          = db.Column(db.Integer, primary_key=True)
    company_id  = db.Column(db.Integer, db.ForeignKey("company.id"),         nullable=False)
    call_log_id = db.Column(db.Integer, db.ForeignKey("twilio_call_log.id"), nullable=True)

    call_sid      = db.Column(db.String(100))
    recording_sid = db.Column(db.String(100), unique=True)
    recording_url = db.Column(db.String(500))
    duration_secs = db.Column(db.Integer, default=0)
    channels      = db.Column(db.Integer, default=1)
    status        = db.Column(db.String(30), default="completed")
    is_deleted    = db.Column(db.Boolean,  default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    company  = db.relationship("Company",       backref="call_recordings")
    call_log = db.relationship("TwilioCallLog", backref="call_recordings")


class PinnedPhoneFavorite(db.Model):
    """User-pinned speed-dial tiles on the PWA phone screen (max 4 per user/company)."""
    __tablename__ = "pinned_phone_favorite"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"),    nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"), nullable=True)

    display_name = db.Column(db.String(100), nullable=False)
    phone_number = db.Column(db.String(20),  nullable=False)
    avatar_url   = db.Column(db.String(500))
    sort_order   = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user    = db.relationship("User",    backref="pinned_favorites")
    company = db.relationship("Company", backref="pinned_favorites")
    contact = db.relationship("Contact", backref="pinned_favorites")

    __table_args__ = (
        db.UniqueConstraint("user_id", "company_id", "sort_order",
                            name="uq_pinned_fav_user_co_order"),
    )
