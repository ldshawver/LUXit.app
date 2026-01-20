"""Flask configuration."""
import os


class Config:
    """Base configuration."""

    SECRET_KEY = os.environ.get("SESSION_SECRET")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "sqlite:///email_marketing.db")
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Microsoft Graph API
    MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
    MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
    MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "")

    # OpenAI
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

    # Twilio (for SMS)
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "")

    # Email Providers
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASS = os.environ.get("SMTP_PASS", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "")

    MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
    MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN", "")
    MAILGUN_FROM = os.environ.get("MAILGUN_FROM", "")

    # Social Media API Keys
    FACEBOOK_ACCESS_TOKEN = os.environ.get("FACEBOOK_ACCESS_TOKEN", "")
    FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "")
    LINKEDIN_ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")

    # WooCommerce
    WOOCOMMERCE_URL = os.environ.get("WOOCOMMERCE_URL", "")
    WOOCOMMERCE_KEY = os.environ.get("WOOCOMMERCE_KEY", "")
    WOOCOMMERCE_SECRET = os.environ.get("WOOCOMMERCE_SECRET", "")

    # Stripe
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

    # Google Ads
    GOOGLE_ADS_CLIENT_ID = os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
    GOOGLE_ADS_CLIENT_SECRET = os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
    GOOGLE_ADS_DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")


class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG = True
    TESTING = False


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG = False
    TESTING = False
    SERVER_NAME = "luxit.app"
    PREFERRED_URL_SCHEME = "https"
    SESSION_COOKIE_SECURE = True


class TestingConfig(Config):
    """Testing configuration."""

    DEBUG = True
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
