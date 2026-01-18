import os
import logging
import json
import re
import importlib.util
from uuid import uuid4
from urllib.parse import urlparse, urljoin

from dotenv import load_dotenv
from flask import Flask, redirect, url_for, request, g, has_request_context
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# Load environment FIRST (ONCE)
# ============================================================
load_dotenv("/etc/lux-marketing/lux.env")

# ============================================================
# Logging (SAFE – NO custom record factory)
# ============================================================
LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=LOG_FORMAT)
root_logger = logging.getLogger()


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        return True


class RedactionFilter(logging.Filter):
    _nine_digit = re.compile(r"\b\d{9}\b")
    _keys = re.compile(r"\b(tin|ssn|ein)\b", re.IGNORECASE)

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._nine_digit.sub("***REDACTED***", record.msg)
            record.msg = self._keys.sub("[redacted]", record.msg)
        return True


root_logger.addFilter(RequestIdFilter())
root_logger.addFilter(RedactionFilter())

logger = logging.getLogger(__name__)

# ============================================================
# Flask App (SINGLE INSTANCE)
# ============================================================
app = Flask(__name__)

# REQUIRED secret
app.config["SECRET_KEY"] = os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

# IMPORTANT: make Flask respect Nginx reverse proxy headers
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)

# Cookie + URL behavior
app.config.update(
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)

# If you want to force host correctness, uncomment these:
# app.config["SERVER_NAME"] = "luxit.app"

# ============================================================
# Safe redirect helper (prevents redirects to IP / external domains)
# ============================================================
def is_safe_url(target: str) -> bool:
    if not target:
        return False

    # Convert relative to absolute based on current host
    host_url = request.host_url  # e.g. https://luxit.app/
    ref = urlparse(host_url)
    test = urlparse(urljoin(host_url, target))

    # Only allow same scheme+netloc as current request host
    if test.scheme not in ("http", "https"):
        return False

    # Block raw IP redirects explicitly (your symptom)
    # (still also blocked by netloc check below, but this is extra safety)
    ip_like = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
    if test.hostname and ip_like.match(test.hostname):
        return False

    return ref.netloc == test.netloc


def safe_next(default_endpoint: str = "main.dashboard"):
    nxt = request.args.get("next") or request.form.get("next")
    if nxt and is_safe_url(nxt):
        return redirect(nxt)
    return redirect(url_for(default_endpoint))

# ============================================================
# Extensions
# ============================================================
from extensions import db, csrf

# ============================================================
# Database
# ============================================================
db_url = os.getenv("DATABASE_URL", "sqlite:///email_marketing.db")

if db_url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
    if importlib.util.find_spec("pymysql"):
        db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
csrf.init_app(app)

# ============================================================
# Auth (Flask-Login)
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

# Optional integrations (match your real blueprint names)
try:
    from tiktok_auth import tiktok_bp, tiktok_api_bp
    app.register_blueprint(tiktok_bp)
    app.register_blueprint(tiktok_api_bp)
    logger.info("TikTok OAuth enabled")
except Exception as e:
    logger.warning("TikTok OAuth not available: %s", e)

try:
    from facebook_auth import facebook_auth_bp
    app.register_blueprint(facebook_auth_bp)
    logger.info("Facebook OAuth enabled")
except Exception as e:
    logger.warning("Facebook OAuth not available: %s", e)

try:
    from instagram_auth import instagram_auth_bp
    app.register_blueprint(instagram_auth_bp)
    logger.info("Instagram OAuth enabled")
except Exception as e:
    logger.warning("Instagram OAuth not available: %s", e)

try:
    from fb_webhook import fb_webhook
    app.register_blueprint(fb_webhook)
    csrf.exempt(fb_webhook)
    logger.info("Facebook webhook enabled")
except Exception as e:
    logger.warning("Facebook webhook not available: %s", e)

# ============================================================
# Request lifecycle
# ============================================================
@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID") or str(uuid4())

@app.after_request
def attach_request_id(resp):
    resp.headers["X-Request-ID"] = getattr(g, "request_id", "-")
    return resp

# ============================================================
# Root route (your requested behavior)
# ============================================================
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("auth.login"))

# ============================================================
# Startup
# ============================================================
with app.app_context():
    import models  # noqa: F401
    db.create_all()
    logger.info("DB initialized (create_all complete)")
