import logging
import os
import re
from uuid import uuid4
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    g,
    redirect,
    render_template,
    request,
    url_for,
    current_app,
)
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix

# Local extension imports (must exist in your project)
from extensions import db, csrf

# Load environment (system-wide .env for production)
load_dotenv("/etc/lux-marketing/lux.env")

# ---------------------------------------------------------------------
# Logging: safe request-id + redaction
# ---------------------------------------------------------------------
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
        # Only redact textual messages
        try:
            if isinstance(record.msg, str):
                record.msg = self._nine_digit.sub("***REDACTED***", record.msg)
                record.msg = self._keys.sub("[redacted]", record.msg)
        except Exception:
            # never break logging if redact fails
            pass
        return True


# Configure root logger once
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(SafeFormatter(LOG_FORMAT))
root_logger.addHandler(stream_handler)
root_logger.addFilter(RequestIdFilter())
root_logger.addFilter(RedactionFilter())


# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------
def _is_safe_next(value: str) -> bool:
    """
    Determine if `next` is a safe internal path. Accept only relative paths.
    """
    if not value:
        return False
    if value.startswith("/"):
        return True
    try:
        parsed = urlparse(value)
        return not (parsed.scheme or parsed.netloc)
    except Exception:
        return False


# ---------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------
def create_app(config: dict | None = None) -> Flask:
    """
    Create and configure the Flask application.
    - Uses environment variables where appropriate.
    - Initializes extensions: db, csrf, login_manager.
    - Registers blueprints if they exist; otherwise registers a minimal auth blueprint.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Load basic configuration from environment
    app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET") or os.environ.get(
        "SECRET_KEY"
    )
    if not app.config["SECRET_KEY"]:
        # In development you may want a default, but production MUST set the secret
        raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set in environment")

    db_uri = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ.get("DATABASE_URL")
    if db_uri:
        app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Canonical host (used for redirects). Provide an env override if needed.
    CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "luxit.app")
    app.config.update(
        SERVER_NAME=CANONICAL_HOST,
        PREFERRED_URL_SCHEME="https",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="None",
    )

    # Trust proxy headers (nginx / load balancer)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # Initialize extensions
    db.init_app(app)
    csrf.init_app(app)

    # Login manager
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    @login_manager.user_loader
    def load_user(user_id):
        from models import User

        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # Register blueprints. If your modules exist, import them; otherwise create a small fallback auth blueprint.
    try:
        from routes import main_bp
        from auth import auth_bp

        app.register_blueprint(main_bp)
        app.register_blueprint(auth_bp, url_prefix="/auth")
    except Exception:
        # Minimal fallback auth blueprint (keeps app usable in dev)
        from flask import Blueprint, flash, render_template, request
        from sqlalchemy import or_
        from sqlalchemy.exc import SQLAlchemyError
        from flask_login import login_user

        auth_bp = Blueprint("auth", __name__, template_folder="templates")

        @auth_bp.route("/login", methods=["GET", "POST"])
        def login():
            if current_user.is_authenticated:
                return redirect(url_for("main.dashboard"))

            if request.method == "POST":
                username_or_email = (
                    (request.form.get("username") or request.form.get("email") or "")
                    .strip()
                    .lower()
                )
                password = request.form.get("password") or ""
                if not username_or_email or not password:
                    flash("Username/email and password are required.", "error")
                    return render_template("auth/login.html")

                try:
                    from models import User

                    user = User.query.filter(
                        or_(
                            User.username == username_or_email,
                            User.email == username_or_email,
                        )
                    ).first()
                    if user and getattr(user, "check_password", None) and user.check_password(
                        password
                    ):
                        login_user(user)
                        return redirect(url_for("main.dashboard"))

                    flash("Invalid credentials", "error")
                except SQLAlchemyError:
                    flash("Login unavailable. Please try again later.", "error")
                    return render_template("auth/login.html")

            return render_template("auth/login.html")

        app.register_blueprint(auth_bp, url_prefix="/auth")

        # Also register a minimal main blueprint so redirects work
        from flask import Blueprint

        main_bp = Blueprint("main", __name__)

        @main_bp.route("/dashboard")
        def dashboard():
            return "Dashboard (placeholder)"

        app.register_blueprint(main_bp)

    # -----------------------------------------------------------------
    # Request-level protections and helpers
    # -----------------------------------------------------------------
    @app.before_request
    def enforce_canonical_host_and_set_request_id():
        # Set request id early so logging filters can access it
        g.request_id = request.headers.get("X-Request-ID") or str(uuid4())

        # Enforce canonical host to avoid host header confusion/poisoning
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
        allowed_hosts = {CANONICAL_HOST, f"www.{CANONICAL_HOST}"}
        if app.testing:
            allowed_hosts.update({"localhost", "127.0.0.1"})

        if host and host not in allowed_hosts:
            # Redirect to canonical host (preserve path)
            return redirect(f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}", code=301)

        # Safe-next protection (prevent open redirects)
        nxt = request.args.get("next", "")
        if nxt and not _is_safe_next(nxt):
            return redirect(url_for("auth.login"))

    @app.after_request
    def attach_request_id(response):
        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        return response

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("main.dashboard"))
        return redirect(url_for("auth.login"))

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_error):
        # Keep errors from raising additional exceptions in the handler
        try:
            return render_template("errors/500.html"), 500
        except Exception:
            root_logger.exception("Error rendering 500 template")
            return "Internal Server Error", 500

    # Initialize DB (best-effort)
    try:
        with app.app_context():
            db.create_all()
    except Exception:
        root_logger.exception("Database initialization failed")

    return app
