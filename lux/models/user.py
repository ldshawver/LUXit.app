"""User model."""
import logging

from flask_login import UserMixin
from lux.extensions import db
from lux.models.base import TimestampMixin


class User(UserMixin, TimestampMixin, db.Model):
    """User model for authentication and authorization."""
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    
    def __repr__(self):
        return f'<User {self.username}>'

    def get_default_company(self):
        """
        Backward-compatible shim.
        Prevents production 500s from stale templates.
        Templates must not call model methods long-term.
        """
        logger = logging.getLogger(__name__)
        try:
            if getattr(self, "default_company", None):
                return self.default_company

            default_id = getattr(self, "default_company_id", None)
            if default_id:
                from lux.models.company import Company
                return Company.query.get(default_id)

            from lux.models.company import Company
            return Company.query.filter_by(is_active=True).first()
        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.warning("Default company lookup failed for user %s: %s", self.id, exc)
            return None
