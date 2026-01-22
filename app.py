# ============================================================
# app.py — Canonical, Compile-Safe Application Entry
# ============================================================

import logging
import os
import re
from uuid import uuid4
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    redirect,
    url_for,
    request,
    g,
    flash,
    render_template,
    current_app,
)
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

# ============================================================
# Environment
# ============================================================

load_dotenv("/etc/lux-marketing/lux.env")

CANONICAL_HOST = "luxit.app"
ALLOWED_HOSTS = {"luxit.app", "www.luxit.app"}

# ============================================================
# Logging
# ============================================================

LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


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

from dotenv import load_dotenv
from flask import (
    Flask,
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
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

# ============================================================
# Environment
# ============================================================

load_dotenv("/etc/lux-marketing/lux.env")

CANONICAL_HOST = "luxit.app"

# ============================================================
# Logging
# ============================================================

LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)
"""Application entry point."""
import os

from flask import redirect, request

from lux import create_app as _create_app


def create_app():
    """Create the Flask app using the lux factory."""
    config_name = os.environ.get("FLASK_ENV")
    app = _create_app(config_name)

    @app.before_request
    def enforce_canonical_host():
        allowed_hosts = {"luxit.app", "www.luxit.app"}
        if app.testing:
            allowed_hosts.update({"localhost", "127.0.0.1"})

        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
        if host and host not in allowed_hosts:
            return redirect(f"https://luxit.app{request.full_path.rstrip('?')}", code=301)

    return app



class RedactionFilter(logging.Filter):
    _nine_digit = re.compile(r"\b\d{9}\b")
    _keys = re.compile(r"\b(tin|ssn|ein)\b", re.IGNORECASE)

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._nine_digit.sub("***REDACTED***", record.msg)
            record.msg = self._keys.sub("[redacted]", record.msg)
        return True


handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(LOG_FORMAT))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(handler)
root_logger.addFilter(RequestIdFilter())
root_logger.addFilter(RedactionFilter())

logger = logging.getLogger(__name__)

# ============================================================
# Flask Factory
# ============================================================

def create_app():
    app = Flask(__name__)

    # --------------------------------------------------------
    # Core config
    # --------------------------------------------------------

    app.config.update(
        SERVER_NAME=CANONICAL_HOST,
        APPLICATION_ROOT="/",
        PREFERRED_URL_SCHEME="https",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="None",
    )

    app.config["SECRET_KEY"] = (
        os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
    )
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

    # --------------------------------------------------------
    # Proxy trust (nginx)
    # --------------------------------------------------------

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

    # --------------------------------------------------------
    # Extensions
    # --------------------------------------------------------

    from extensions import db, csrf
    db.init_app(app)
    csrf.init_app(app)

    # --------------------------------------------------------
    # Login manager
    # --------------------------------------------------------

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.query.get(int(user_id))

    # --------------------------------------------------------
    # Request hooks
    # --------------------------------------------------------

    @app.before_request
    def enforce_canonical_host_and_request_id():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid4())

        if current_app.testing:
            return

        host = (
            request.headers.get("X-Forwarded-Host")
            or request.host
            or ""
        ).split(":")[0].lower()

        if host not in ALLOWED_HOSTS:
            return redirect(
                f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}",
                code=301,
            )

    # --------------------------------------------------------
    # Blueprints
    # --------------------------------------------------------

    from routes import main_bp
    from auth import auth_bp
    from user_management import user_bp
    from advanced_config import advanced_config_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(user_bp, url_prefix="/user")
    app.register_blueprint(advanced_config_bp)

    # Optional OAuth integrations
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
            logger.warning("%s not loaded", module)

    # --------------------------------------------------------
    # Routes
    # --------------------------------------------------------

    @app.route("/")
    def index():
        return redirect(url_for("auth.login", _external=False))

    # --------------------------------------------------------
    # Database bootstrap
    # --------------------------------------------------------

    with app.app_context():
        import models
        db.create_all()

    return app

logger = logging.getLogger(__name__)

# ============================================================
# Flask App
# ============================================================

app = Flask(__name__)

app.config.update(
    SERVER_NAME=CANONICAL_HOST,
    APPLICATION_ROOT="/",
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)

# ------------------------------------------------------------
# Secrets
# ------------------------------------------------------------

app.config["SECRET_KEY"] = (
    os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
)

if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

# ------------------------------------------------------------
# Proxy / Nginx trust
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

db_url = os.getenv("DATABASE_URL", "sqlite:///email_marketing.db")

if db_url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
    if importlib.util.find_spec("pymysql"):
        db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
csrf.init_app(app)

# ============================================================
# Login Manager
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

app.register_blueprint(main_bp)
app.register_blueprint(auth_bp, url_prefix="/auth")

# ============================================================
# Canonical Host Enforcement
# ============================================================

@app.before_request
def enforce_canonical_host_and_request_id():
    g.request_id = request.headers.get("X-Request-ID", str(uuid4()))

    allowed_hosts = {CANONICAL_HOST, f"www.{CANONICAL_HOST}"}
    if current_app.testing:
        allowed_hosts.update({"localhost", "127.0.0.1"})

    host = (
        request.headers.get("X-Forwarded-Host")
        or request.host
        or ""
    ).split(":")[0].lower()

    if host and host not in allowed_hosts:
        return redirect(
            f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}",
            code=301,
        )

# ============================================================
# Auth Routes (LOGIN)
# ============================================================

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    from models import User

    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard", _external=False))

    if request.method == "POST":
        username_or_email = (
            request.form.get("username")
            or request.form.get("email")
            or ""
        ).strip()
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
            flash("Login unavailable. Try again later.", "error")
            return render_template("auth/login.html")

        if not user or not check_password_hash(
            user.password_hash, password
        ):
            flash("Invalid credentials.", "error")
            return render_template("auth/login.html")

        login_user(user)
        return redirect(url_for("main.dashboard", _external=False))

    return render_template("auth/login.html")

# ============================================================
# Root Route
# ============================================================

@app.route("/")
def index():
    return redirect(url_for("auth.login", _external=False))

# ============================================================
# Safe next parameter validation
# ============================================================

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

# ============================================================
# Startup
# ============================================================

with app.app_context():
    import models
    db.create_all()
if __name__ == "__main__":
    app = create_app()
    app.run()
