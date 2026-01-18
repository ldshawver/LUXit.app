import os
import logging
import re
import json
from uuid import uuid4
import importlib.util

from dotenv import load_dotenv
from flask import Flask, redirect, url_for, request, g
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# Environment
# ============================================================

load_dotenv("/etc/lux-marketing/lux.env")

CANONICAL_HOST = "luxit.app"

# ============================================================
# Logging (SAFE – no custom record factory)
# ============================================================

LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)
root_logger = logging.getLogger()


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, "request_id", "-")
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

# ============================================================
# Flask App (SINGLE INSTANCE – GLOBAL CONTROL)
# ============================================================

app = Flask(__name__)

# ------------------------------------------------------------
# Secrets (REQUIRED)
# ------------------------------------------------------------

app.config["SECRET_KEY"] = (
    os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
)

if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

# ------------------------------------------------------------
# Trust Nginx reverse proxy
# ------------------------------------------------------------

app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)

# ------------------------------------------------------------
# URL + Cookie Security (CRITICAL)
# ------------------------------------------------------------

app.config.update(
    SERVER_NAME=CANONICAL_HOST,
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)

# ============================================================
# 🔒 CANONICAL HOST ENFORCEMENT (THE FIX)
# ============================================================

@app.before_request
def enforce_canonical_host():
    # Allow internal health checks / CLI if needed
    if not request.host:
        return None

    if request.host != CANONICAL_HOST:
        target = f"https://{CANONICAL_HOST}{request.full_path}"
        return redirect(target, code=301)


@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID", str(uuid4()))


@app.after_request
def attach_request_id(response):
    response.headers["X-Request-ID"] = g.request_id
    return response

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
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
csrf.init_app(app)

# ============================================================
# Authentication
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
# Routes / Blueprints
# ============================================================

from routes import main_bp
from auth import auth_bp
from user_management import user_bp
from advanced_config import advanced_config_bp

app.register_blueprint(main_bp)
app.register_blueprint(auth_bp, url_prefix="/auth")
app.register_blueprint(user_bp, url_prefix="/user")
app.register_blueprint(advanced_config_bp)

# Optional OAuth integrations (safe)
for module, bp_name in [
    ("tiktok_auth", "tiktok_bp"),
    ("facebook_auth", "facebook_auth_bp"),
    ("instagram_auth", "instagram_auth_bp"),
]:
    try:
        mod = __import__(module)
        app.register_blueprint(getattr(mod, bp_name))
        logger.info("%s enabled", module)
    except Exception:
        pass

# ============================================================
# Root Route
# ============================================================

@app.route("/")
def index():
    return redirect(url_for("auth.login"))

# ============================================================
# Startup
# ============================================================

with app.app_context():
    import models
    db.create_all()
    logger.info("Application startup complete")
