from datetime import datetime
import logging

from flask_login import UserMixin
from sqlalchemy import JSON, Text

from extensions import db


# ============================================================
# Association table (legacy / simple access)
# ============================================================

user_company = db.Table(
    'user_company',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('company_id', db.Integer, db.ForeignKey('company.id'), primary_key=True),
    db.Column('is_default', db.Boolean, default=False),
    db.Column('created_at', db.DateTime, default=datetime.utcnow),
)


# ============================================================
# User ↔ Company access with roles
# ============================================================

class UserCompanyAccess(db.Model):
    __tablename__ = 'user_company_access'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False)

    role = db.Column(db.String(20), default='viewer')
    is_default = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'company_id', name='uq_user_company_access'),
        db.Index('ix_user_company_access_user', 'user_id'),
        db.Index('ix_user_company_access_company', 'company_id'),
    )

    user = db.relationship('User', backref=db.backref('company_access', lazy='dynamic'))
    company = db.relationship('Company', backref=db.backref('user_access', lazy='dynamic'))

    ROLE_OWNER = 'owner'
    ROLE_ADMIN = 'admin'
    ROLE_EDITOR = 'editor'
    ROLE_VIEWER = 'viewer'

    ROLE_HIERARCHY = {
        'owner': 4,
        'admin': 3,
        'editor': 2,
        'viewer': 1,
    }

    def __repr__(self):
        return f'<UserCompanyAccess user={self.user_id} company={self.company_id} role={self.role}>'

    def can_edit(self):
        return self.role in ('owner', 'admin', 'editor')

    def can_admin(self):
        return self.role in ('owner', 'admin')

    def can_own(self):
        return self.role == 'owner'


# ============================================================
# User model
# ============================================================

class User(UserMixin, db.Model):
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    is_admin = db.Column(db.Boolean, default=False)

    # Replit Auth
    replit_id = db.Column(db.String(64), unique=True, nullable=True)

    # Default company
    default_company_id = db.Column(
        db.Integer,
        db.ForeignKey('company.id'),
        nullable=True,
    )

    # Profile fields
    first_name = db.Column(db.String(64))
    last_name = db.Column(db.String(64))
    phone = db.Column(db.String(20))
    avatar_path = db.Column(db.String(255))
    tags = db.Column(db.String(255))
    segment = db.Column(db.String(100), default='user')
    custom_fields = db.Column(JSON)
    engagement_score = db.Column(db.Float, default=0.0)
    last_activity = db.Column(db.DateTime)
    bio = db.Column(Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    companies = db.relationship(
        'Company',
        secondary=user_company,
        backref=db.backref('users', lazy='dynamic'),
    )

    default_company = db.relationship(
        'Company',
        foreign_keys=[default_company_id],
        backref='default_users',
    )

    replit_oauth = db.relationship(
        'ReplitOAuth',
        backref='user',
        lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<User {self.username}>'

    @property
    def full_name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name or self.last_name or self.username

    @property
    def is_admin_user(self):
        return (
            self.is_admin
            or self.segment == 'admin'
            or (self.tags and 'admin' in self.tags.lower())
        )

    # --------------------------------------------------------
    # ✅ FIXED: this is now a REAL instance method
    # --------------------------------------------------------

    def get_default_company(self):
        """
        Get the user's default company safely.
        NEVER poisons the DB session.
        """
        logger = logging.getLogger(__name__)

        try:
            # 1) Explicit default_company_id
            if self.default_company_id:
                if self.default_company is not None:
                    return self.default_company
                return Company.query.get(self.default_company_id)

            # 2) UserCompanyAccess marked default
            access = (
                UserCompanyAccess.query
                .filter_by(user_id=self.id, is_default=True)
                .join(Company, Company.id == UserCompanyAccess.company_id)
                .filter(Company.is_active.is_(True))
                .first()
            )
            if access:
                return access.company

            # 3) Fallback: first active company
            return (
                Company.query
                .filter_by(is_active=True)
                .order_by(Company.id.asc())
                .first()
            )

        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.warning(
                "Default company lookup failed for user %s: %s",
                self.id,
                exc,
            )
            return None

    # --------------------------------------------------------

    def set_default_company(self, company_id):
        self.default_company_id = company_id
        db.session.commit()

    def get_all_companies(self):
        return (
            Company.query
            .filter_by(is_active=True)
            .order_by(Company.name)
            .all()
        )

    def get_company_access(self, company_id):
        return UserCompanyAccess.query.filter_by(
            user_id=self.id,
            company_id=company_id,
        ).first()

    def get_company_role(self, company_id):
        access = self.get_company_access(company_id)
        return access.role if access else 'viewer'

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


# ============================================================
# OAuth models
# ============================================================

class ReplitOAuth(db.Model):
    __tablename__ = 'replit_oauth'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    browser_session_key = db.Column(db.String(255), nullable=False)
    provider = db.Column(db.String(50), default='replit_auth')
    token = db.Column(JSON)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'user_id',
            'browser_session_key',
            'provider',
            name='uq_replit_oauth_user_session_provider',
        ),
    )

    def __repr__(self):
        return f'<ReplitOAuth user_id={self.user_id}>'


class TikTokOAuth(db.Model):
    __tablename__ = 'tiktok_oauth'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))

    open_id = db.Column(db.String(255), nullable=False)
    access_token = db.Column(Text, nullable=False)
    refresh_token = db.Column(Text)
    expires_at = db.Column(db.DateTime)
    refresh_expires_at = db.Column(db.DateTime)
    scope = db.Column(db.String(500))
    token_type = db.Column(db.String(50), default='Bearer')

    display_name = db.Column(db.String(255))
    avatar_url = db.Column(db.String(500))
    raw_token = db.Column(JSON)
    status = db.Column(db.String(50), default='active')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<TikTokOAuth user_id={self.user_id} open_id={self.open_id}>'
