import logging
import os
import re
import importlib.util
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, redirect, url_for, request, g
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# Load environment
# ============================================================

load_dotenv("/etc/lux-marketing/lux.env")

# ============================================================
# Flask app
from uuid import uuid4
from urllib.parse import urlparse

from flask import (
    Blueprint,
    render_template,
    request,
    flash,
    redirect,
    url_for,
    session,
    current_app,
    g,
)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

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
# Logging (safe request_id fallback)
# ============================================================

class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

handler = logging.StreamHandler()
handler.setFormatter(SafeFormatter(LOG_FORMAT))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(handler)

# ============================================================
# Logging (safe request_id fallback)
# ============================================================

class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

handler = logging.StreamHandler()
handler.setFormatter(SafeFormatter(LOG_FORMAT))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(handler)

# ============================================================
# Blueprint
# Flask App (SINGLE INSTANCE – GLOBAL CONTROL)
# ============================================================

app = Flask(__name__)

# 🔒 HARD CANONICAL DOMAIN LOCK
app.config.update(
    SERVER_NAME="luxit.app",
    APPLICATION_ROOT="/",
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)

# REQUIRED secret
app.config["SECRET_KEY"] = (
    os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
)
if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET must be set")

# TRUST NGINX — REQUIRED
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

# ============================================================
# Extensions
# ============================================================

from extensions import db, csrf

db.init_app(app)
csrf.init_app(app)

# ============================================================
# Login manager
# ============================================================

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"

@login_manager.user_loader
def load_user(user_id):
    from models import User
    return User.query.get(int(user_id))

# ============================================================
# Blueprints
# ============================================================

from routes import main_bp
from auth import auth_bp

app.register_blueprint(main_bp)
app.register_blueprint(auth_bp, url_prefix="/auth")

# ============================================================
# Request safety net (BLOCK IP HOSTS)
# ============================================================

@app.before_request
def enforce_canonical_host():
    if request.host != "luxit.app":
        return redirect(
            "https://luxit.app" + request.full_path,
            code=301,
        )
    g.request_id = request.headers.get("X-Request-ID", str(uuid4()))

# ============================================================
# Root
# ============================================================


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

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # If already logged in, go straight to dashboard
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard", _external=False))

    if request.method == "POST":
        username_or_email = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

        if not username_or_email or not password:
            flash("Username or email and password are required.", "error")
            return render_template("auth/login.html")

        try:
            normalized_email = username_or_email.lower()
            user = User.query.filter(
                or_(
                    User.username == username_or_email,
                    User.email == normalized_email,
                )
            ).first()
        except SQLAlchemyError:
            flash("Login unavailable. Please try again later.", "error")
            return render_template("auth/login.html")

from extensions import db, csrf

# ============================================================
# Database
# ============================================================

db_url = os.getenv("DATABASE_URL", "sqlite:///email_marketing.db")

if db_url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
    if importlib.util.find_spec("pymysql"):
        db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)

        # 🔒 HARD CANONICAL REDIRECT (NO IP, NO HOST LEAK)
        return redirect(url_for("main.dashboard", _external=False))

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

@auth_bp.before_app_request
def _canonical_host_and_request_id():
    g.request_id = request.headers.get("X-Request-ID") or str(uuid4())


def _is_safe_next(value: str) -> bool:
    if not value:
        return False
    if value.startswith("/"):
        return True
    try:
        parsed = urlparse(value)
        return not (parsed.scheme or parsed.netloc)
    except Exception:
        return False


@auth_bp.before_app_request
def enforce_canonical_host_and_block_unsafe_next():
    allowed_hosts = {"luxit.app", "www.luxit.app"}
    if current_app.testing:
        allowed_hosts.update({"localhost", "127.0.0.1"})

    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
    if host and host not in allowed_hosts:
        return redirect(f"https://luxit.app{request.full_path.rstrip('?')}", code=301)

    nxt = request.args.get("next", "")
    if nxt and not _is_safe_next(nxt):
        return redirect(url_for("auth.login", _external=False))
