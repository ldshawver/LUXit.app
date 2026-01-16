import os
import logging
import json
import re
import importlib.util
from uuid import uuid4

from flask import Flask, redirect, url_for, request, g, has_request_context
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix


# ============================================================
# Logging configuration
# ============================================================

class RequestIdFilter(logging.Filter):
    """Inject request IDs into log records when available."""
    def filter(self, record: logging.LogRecord) -> bool:
        if has_request_context():
            record.request_id = getattr(g, "request_id", "-")
        else:
            record.request_id = "-"
        return True


log_format = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)
logging.basicConfig(level=logging.DEBUG, format=log_format)

root_logger = logging.getLogger()
root_logger.addFilter(RequestIdFilter())

_old_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _old_factory(*args, **kwargs)
    if not hasattr(record, "request_id"):
        record.request_id = "-"
    return record


logging.setLogRecordFactory(_record_factory)


class RedactionFilter(logging.Filter):
    """Redact sensitive tax identifiers from logs."""
    _nine_digit = re.compile(r"\b\d{9}\b")
    _keys = re.compile(r"\b(tin|ssn|ein)\b", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            redacted = self._nine_digit.sub("***REDACTED***", record.msg)
            redacted = self._keys.sub("[redacted]", redacted)
            record.msg = redacted
        return True


root_logger.addFilter(RedactionFilter())


# ============================================================
from extensions import db, csrf


# ============================================================
# Create Flask app
# ============================================================

app = Flask(__name__)

# ------------------------------------------------------------
# Session / secret key handling (deterministic & review-safe)
# ------------------------------------------------------------

session_secret = (
    os.environ.get("SESSION_SECRET")
    or os.environ.get("SECRET_KEY")
)

if not session_secret:
    logger = logging.getLogger(__name__)
    if os.environ.get("CODEX_ENV") == "dev":
        session_secret = uuid4().hex
        logger.warning(
            "SESSION_SECRET not set; using a temporary dev secret."
        )
    else:
        session_secret = uuid4().hex
        app.config["STARTUP_ERROR"] = (
            "SESSION_SECRET is missing. Set it in your environment to start the app."
        )
        logger.warning(app.config["STARTUP_ERROR"])

app.secret_key = session_secret

# Trust reverse proxy headers (required on VPS / load balancers)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


# ============================================================
# Database configuration
# ============================================================

db_url = os.environ.get("DATABASE_URL", "sqlite:///email_marketing.db")

if db_url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
    if "pymysql" not in db_url and importlib.util.find_spec("pymysql") is not None:
        db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)
        logging.getLogger(__name__).warning(
            "MySQLdb missing; falling back to PyMySQL driver."
        )
    elif os.environ.get("CODEX_ENV") == "dev":
        db_url = "sqlite:///email_marketing.db"
        logging.getLogger(__name__).warning(
            "MySQLdb missing in dev; falling back to sqlite."
        )

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

# File uploads
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB
app.config["UPLOAD_FOLDER"] = "static/company_logos"

# Microsoft Graph API config
app.config["MS_CLIENT_ID"] = os.environ.get("MS_CLIENT_ID", "")
app.config["MS_CLIENT_SECRET"] = os.environ.get("MS_CLIENT_SECRET", "")
app.config["MS_TENANT_ID"] = os.environ.get("MS_TENANT_ID", "")

db.init_app(app)


# ============================================================
# CSRF configuration
# ============================================================

app.config["WTF_CSRF_ENABLED"] = True
app.config["WTF_CSRF_CHECK_DEFAULT"] = True
app.config["WTF_CSRF_METHODS"] = ["POST", "PUT", "PATCH", "DELETE"]
app.config["WTF_CSRF_FIELD_NAME"] = "csrf_token"
app.config["WTF_CSRF_TIME_LIMIT"] = None
app.config["WTF_CSRF_SSL_STRICT"] = False

csrf.init_app(app)

# Session cookies (iframe + OAuth safe)
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True


# ============================================================
# Flask-Login setup
# ============================================================

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."


@login_manager.user_loader
def load_user(user_id):
    from models import User
    return User.query.get(int(user_id))


# ============================================================
# Context processors / template helpers
# ============================================================

@app.context_processor
def inject_tracking_pixels():
    from flask_login import current_user
    facebook_app_id = None
    tiktok_pixel_id = None

    try:
        if current_user and current_user.is_authenticated:
            company = current_user.get_default_company()
            if company:
                from models import CompanySecret

                fb_secret = CompanySecret.query.filter_by(
                    company_id=company.id,
                    key="facebook_app_id",
                ).first()
                if fb_secret:
                    facebook_app_id = fb_secret.value

                tt_secret = CompanySecret.query.filter_by(
                    company_id=company.id,
                    key="tiktok_pixel_id",
                ).first()
                if tt_secret:
                    tiktok_pixel_id = tt_secret.value
    except Exception:
        pass

    return {
        "facebook_app_id": facebook_app_id,
        "tiktok_pixel_id": tiktok_pixel_id,
    }


@app.template_filter("campaign_status_color")
def campaign_status_color(status):
    colors = {
        "draft": "secondary",
        "scheduled": "info",
        "sending": "warning",
        "sent": "success",
        "partial": "warning",
        "failed": "danger",
        "paused": "secondary",
        "completed": "success",
        "active": "primary",
    }
    return colors.get(status, "secondary")


# ============================================================
# Blueprints
# ============================================================

from routes import main_bp
from auth import auth_bp
from user_management import user_bp
from advanced_config import advanced_config_bp

app.register_blueprint(main_bp)
app.register_blueprint(auth_bp, url_prefix="/auth")
app.register_blueprint(user_bp, url_prefix="/user")
app.register_blueprint(advanced_config_bp)


# Optional / external integrations

try:
    from replit_auth import make_replit_blueprint, is_replit_auth_enabled
    if is_replit_auth_enabled():
        bp = make_replit_blueprint()
        if bp:
            app.register_blueprint(bp, url_prefix="/replit-auth")
            logging.info("Replit Auth blueprint registered")
except Exception as e:
    logging.warning(f"Replit Auth not available: {e}")

try:
    from tiktok_auth import tiktok_bp, tiktok_api_bp
    app.register_blueprint(tiktok_bp)
    app.register_blueprint(tiktok_api_bp)
    logging.info("TikTok OAuth blueprint registered")
except Exception as e:
    logging.warning(f"TikTok OAuth not available: {e}")

try:
    from facebook_auth import facebook_auth_bp
    app.register_blueprint(facebook_auth_bp)
    logging.info("Facebook OAuth blueprint registered")
except Exception as e:
    logging.warning(f"Facebook OAuth not available: {e}")

try:
    from instagram_auth import instagram_auth_bp
    app.register_blueprint(instagram_auth_bp)
    logging.info("Instagram OAuth blueprint registered")
except Exception as e:
    logging.warning(f"Instagram OAuth not available: {e}")

try:
    from fb_webhook import fb_webhook
    app.register_blueprint(fb_webhook)
    csrf.exempt(fb_webhook)
    logging.info("Facebook webhook blueprint registered")
except Exception as e:
    logging.warning(f"Facebook webhook not available: {e}")


# ============================================================
# Request lifecycle helpers
# ============================================================

def _log_startup_feature_summary():
    logger = logging.getLogger(__name__)
    feature_flags = {
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "replit_auth": bool(os.getenv("REPL_ID")),
        "tiktok": bool(os.getenv("TIKTOK_CLIENT_KEY") and os.getenv("TIKTOK_CLIENT_SECRET")),
        "microsoft_graph": bool(os.getenv("MS_CLIENT_ID") and os.getenv("MS_CLIENT_SECRET") and os.getenv("MS_TENANT_ID")),
        "twilio": bool(
            os.getenv("TWILIO_ACCOUNT_SID")
            and os.getenv("TWILIO_AUTH_TOKEN")
            and os.getenv("TWILIO_PHONE_NUMBER")
        ),
        "stripe": bool(os.getenv("STRIPE_SECRET_KEY")),
        "woocommerce": bool(
            os.getenv("WC_STORE_URL")
            and os.getenv("WC_CONSUMER_KEY")
            and os.getenv("WC_CONSUMER_SECRET")
        ),
        "ga4": bool(os.getenv("GA4_PROPERTY_ID")),
    }
    logger.info("Startup feature summary: %s", feature_flags)


_log_startup_feature_summary()

@app.route("/")
def index():
    return redirect(url_for("auth.login"))


@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID") or str(uuid4())


@app.after_request
def attach_request_id(response):
    request_id = getattr(g, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
        if response.mimetype == "application/json" and response.status_code >= 400:
            payload = response.get_json(silent=True) or {}
            payload.setdefault("request_id", request_id)
            response.set_data(json.dumps(payload))
    return response


# ============================================================
# App initialization
# ============================================================

with app.app_context():
    import models
    from error_logger import ErrorLog

    db.create_all()

    try:
        from services.automation_service import AutomationService
        AutomationService.seed_trigger_library()
        logging.info("Automation trigger library seeded")
    except Exception as e:
        logging.error(f"Error seeding trigger library: {e}")

    try:
        from error_logger import setup_error_logging_handler
        setup_error_logging_handler()
        logging.info("Error logging initialized")
    except Exception as e:
        logging.error(f"Error initializing error logging: {e}")

    try:
        from agent_scheduler import (
            initialize_agent_scheduler,
            get_agent_scheduler,
        )
        initialize_agent_scheduler()
        app.agent_scheduler = get_agent_scheduler()
        logging.info(
            f"AI Agent Scheduler initialized with "
            f"{len(app.agent_scheduler.agents)} agents"
        )
    except Exception as e:
        logging.error(f"Error initializing AI Agent Scheduler: {e}")
