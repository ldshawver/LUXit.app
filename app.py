import os
import logging
import json
import re
import importlib.util
from uuid import uuid4

from dotenv import load_dotenv

# ============================================================
# Load environment FIRST
# ============================================================

load_dotenv("/etc/lux-marketing/lux.env")

from flask import Flask, redirect, url_for, request, g, has_request_context
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# Logging configuration (SAFE – no LogRecordFactory)
# ============================================================

class RequestIdFilter(logging.Filter):
    """Inject request IDs into log records when available."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        return True


class RedactionFilter(logging.Filter):
    """Redact sensitive tax identifiers from logs."""
    _nine_digit = re.compile(r"\b\d{9}\b")
    _keys = re.compile(r"\b(tin|ssn|ein)\b", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._keys.sub(
                "[redacted]",
                self._nine_digit.sub("***REDACTED***", record.msg),
            )
        return True


LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

root_logger = logging.getLogger()
root_logger.addFilter(RequestIdFilter())
root_logger.addFilter(RedactionFilter())

logger = logging.getLogger(__name__)

# ============================================================
# Flask app creation
# ============================================================

app = Flask(__name__)

# ------------------------------------------------------------
# Secret key (REQUIRED)
# ------------------------------------------------------------

app.config["SECRET_KEY"] = (
    os.environ.get("SESSION_SECRET")
    or os.environ.get("SECRET_KEY")
)

if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

# ------------------------------------------------------------
# Reverse proxy trust (CRITICAL for HTTPS + cookies)
# ------------------------------------------------------------

app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)

app.config.update(
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)

# ============================================================
# Extensions
# ============================================================

from extensions import db, csrf

# ============================================================
# Database configuration
# ============================================================

db_url = os.environ.get("DATABASE_URL", "sqlite:///email_marketing.db")

if db_url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
    if importlib.util.find_spec("pymysql"):
        db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)
        logger.warning("MySQLdb missing; falling back to PyMySQL")
    elif os.environ.get("CODEX_ENV") == "dev":
        db_url = "sqlite:///email_marketing.db"
        logger.warning("MySQLdb missing in dev; using sqlite")

app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_ENGINE_OPTIONS={
        "pool_recycle": 300,
        "pool_pre_ping": True,
    },
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

db.init_app(app)

# ============================================================
# CSRF
# ============================================================

app.config.update(
    WTF_CSRF_ENABLED=True,
    WTF_CSRF_CHECK_DEFAULT=True,
    WTF_CSRF_METHODS=["POST", "PUT", "PATCH", "DELETE"],
    WTF_CSRF_TIME_LIMIT=None,
    WTF_CSRF_SSL_STRICT=False,
)

csrf.init_app(app)

# ============================================================
# Flask-Login
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

# Optional integrations

def _safe_register(msg, fn):
    try:
        fn()
        logger.info(msg)
    except Exception as e:
        logger.warning("%s: %s", msg, e)

_safe_register(
    "Replit Auth available",
    lambda: (
        __import__("replit_auth").make_replit_blueprint()
        and app.register_blueprint(
            __import__("replit_auth").make_replit_blueprint(),
            url_prefix="/replit-auth",
        )
    ),
)

_safe_register(
    "TikTok OAuth available",
    lambda: (
        app.register_blueprint(__import__("tiktok_auth").tiktok_bp),
        app.register_blueprint(__import__("tiktok_auth").tiktok_api_bp),
    ),
)

_safe_register(
    "Facebook OAuth available",
    lambda: app.register_blueprint(__import__("facebook_auth").facebook_auth_bp),
)

_safe_register(
    "Instagram OAuth available",
    lambda: app.register_blueprint(__import__("instagram_auth").instagram_auth_bp),
)

_safe_register(
    "Facebook webhook available",
    lambda: (
        app.register_blueprint(__import__("fb_webhook").fb_webhook),
        csrf.exempt(__import__("fb_webhook").fb_webhook),
    ),
)

# ============================================================
# Request lifecycle
# ============================================================

@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID") or str(uuid4())


@app.after_request
def attach_request_id(response):
    if hasattr(g, "request_id"):
        response.headers["X-Request-ID"] = g.request_id
    return response


@app.route("/")
def index():
    return redirect(url_for("auth.login"))

# ============================================================
# Startup diagnostics
# ============================================================

def _log_startup_feature_summary():
    features = {
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "replit_auth": bool(os.getenv("REPL_ID")),
        "tiktok": bool(os.getenv("TIKTOK_CLIENT_KEY")),
        "microsoft_graph": bool(os.getenv("MS_CLIENT_ID")),
        "twilio": bool(os.getenv("TWILIO_ACCOUNT_SID")),
        "stripe": bool(os.getenv("STRIPE_SECRET_KEY")),
        "woocommerce": bool(os.getenv("WC_STORE_URL")),
        "ga4": bool(os.getenv("GA4_PROPERTY_ID")),
    }
    logger.info("Startup feature summary: %s", features)

_log_startup_feature_summary()

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
        logger.info("Automation triggers seeded")
    except Exception as e:
        logger.error("Automation seed error: %s", e)

    try:
        from error_logger import setup_error_logging_handler
        setup_error_logging_handler()
        logger.info("Error logging initialized")
    except Exception as e:
        logger.error("Error logger init failed: %s", e)

    try:
        from agent_scheduler import initialize_agent_scheduler, get_agent_scheduler
        initialize_agent_scheduler()
        app.agent_scheduler = get_agent_scheduler()
        logger.info("AI Agent Scheduler initialized")
    except Exception as e:
        logger.error("Agent scheduler error: %s", e)

    try:
        from scheduler import init_scheduler
        init_scheduler(app)
        logger.info("Email scheduler initialized")
    except Exception as e:
        logger.error("Email scheduler error: %s", e)
