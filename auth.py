import os
import logging
import re
import importlib.util
from uuid import uuid4

from dotenv import load_dotenv
from urllib.parse import urlparse

from flask import Flask, redirect, url_for, request, g, has_request_context
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# Load environment ONCE
# ============================================================

load_dotenv("/etc/lux-marketing/lux.env")

# ============================================================
# Logging (NO record factory, NO recursion)
# ============================================================

LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
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

# ============================================================
# Flask App (SINGLE instance)
# ============================================================

app = Flask(__name__)

# REQUIRED secret
app.config["SECRET_KEY"] = os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

# Trust nginx
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)

# Canonical HTTPS behavior
app.config.update(
    SERVER_NAME="luxit.app",
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)

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
# Flask-Login
# ============================================================

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"
login_manager.login_message = None

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

# ============================================================
# Request lifecycle
# ============================================================

@app.before_request
def enforce_canonical_host_and_block_unsafe_next():
    allowed_hosts = {"luxit.app", "www.luxit.app"}
    if app.testing:
        allowed_hosts.update({"localhost", "127.0.0.1"})

    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()

    if host and host not in allowed_hosts:
        return redirect(f"https://luxit.app{request.full_path.rstrip('?')}", code=301)

    nxt = request.args.get("next", "")
    if nxt and not _is_safe_next(nxt):
        return redirect(url_for("auth.login"))


@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID", str(uuid4()))


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

@app.after_request
def attach_request_id(resp):
    resp.headers["X-Request-ID"] = g.request_id
    return resp

# ============================================================
# Index route (NO login loop, NO IP)
# ============================================================

@app.route("/")
def index():
    from flask_login import current_user
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("auth.login"))

# ============================================================
# Startup
# ============================================================

with app.app_context():
    import models
    db.create_all()
