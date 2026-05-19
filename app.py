"""Application entry point."""

import importlib.util
import json
import logging
import os
import re
from datetime import timedelta
from typing import cast
from uuid import uuid4

# Load .env before anything reads os.environ
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)
except ImportError:
    pass

from flask import Flask, g, has_request_context, request
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix
from extensions import db, csrf  # csrf used below to exempt Stripe routes


# ============================================================
# Logging (FIXED PRODUCTION CONFIG)
# ============================================================

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if has_request_context():
            record.request_id = getattr(g, "request_id", "-")
        else:
            record.request_id = "-"
        return True


class RedactionFilter(logging.Filter):
    _nine_digit = re.compile(r"\b\d{9}\b")
    _keys = re.compile(r"\b(tin|ssn|ein)\b", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = self._nine_digit.sub("***REDACTED***", record.msg)
            msg = self._keys.sub("[redacted]", msg)
            record.msg = msg
        return True


def configure_logging():
    log_format = (
        "%(asctime)s %(levelname)s [%(name)s] "
        "[request_id=%(request_id)s] %(message)s"
    )

    formatter = logging.Formatter(log_format)

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactionFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Remove default handlers (important for Gunicorn)
    if root_logger.handlers:
        root_logger.handlers.clear()

    root_logger.addHandler(handler)


configure_logging()


# ============================================================
# Database URL Resolution
# ============================================================

_resolved_db_url_cache: str | None = None


def _resolve_db_url() -> str:
    """Determine the database URL to use at startup.

    Resolution order:
    1. ``DEV_DATABASE_URL`` env var – explicit dev override (highest priority).
    2. ``DATABASE_URL`` env / secret – the configured database.
       * If it's a Postgres URL, we probe the TCP port (2 s timeout).
         - Reachable  → use it as-is.
         - Unreachable + running inside Replit → warn and fall back to SQLite.
         - Unreachable + NOT Replit (VPS/prod) → raise RuntimeError with a
           clear message so the process exits instead of silently corrupting.
    3. Default SQLite (``sqlite:///lux_marketing_dev.db``) when DATABASE_URL is
       not set at all.

    MySQL driver shim is applied before returning.
    Result is cached so repeated calls (e.g. from tests) skip the network probe.
    """
    global _resolved_db_url_cache
    if _resolved_db_url_cache is not None:
        return _resolved_db_url_cache

    import socket
    import urllib.parse

    _SQLITE_FALLBACK = "sqlite:///lux_marketing_dev.db"
    _is_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"))

    # ── 1. Explicit dev override ────────────────────────────────────────────
    dev_override = os.environ.get("DEV_DATABASE_URL", "").strip()
    if dev_override:
        logging.info("DB: using DEV_DATABASE_URL override → %s",
                     dev_override.split("@")[-1] if "@" in dev_override else dev_override)
        return _apply_mysql_shim(dev_override)

    # ── 2. Configured DATABASE_URL ──────────────────────────────────────────
    raw = os.environ.get("DATABASE_URL", "").strip()

    if not raw:
        if _is_replit:
            logging.warning(
                "DATABASE_URL is not set. Falling back to SQLite for "
                "Replit development: %s", _SQLITE_FALLBACK
            )
            return _SQLITE_FALLBACK
        raise RuntimeError(
            "\n\n"
            "  ❌  DATABASE_URL is not configured.\n"
            "  Set DATABASE_URL in your environment to a valid PostgreSQL\n"
            "  connection string, e.g.:\n"
            "    postgresql://user:password@localhost:5432/lux_marketing\n"
        )

    # Probe Postgres/MySQL with a real protocol-level connection attempt.
    # A plain TCP socket check is NOT sufficient — Neon proxies accept TCP
    # but then reject at the Postgres protocol level when compute is disabled.
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.startswith(("postgres", "postgresql", "mysql")):
        host = parsed.hostname or ""
        port = parsed.port or (5432 if "postgres" in parsed.scheme else 3306)
        db_ok = False
        probe_error = ""
        try:
            import psycopg2
            conn = psycopg2.connect(raw, connect_timeout=5)
            conn.close()
            db_ok = True
        except Exception as exc:
            probe_error = str(exc)

        if not db_ok:
            if _is_replit:
                logging.warning(
                    "\n"
                    "  ⚠️  DATABASE_URL points to %s:%s but the database is\n"
                    "     not accepting connections (%s).\n"
                    "  Falling back to SQLite for Replit development: %s\n"
                    "  To connect to your VPS Postgres instead, set:\n"
                    "    DEV_DATABASE_URL=postgresql://user:pass@<vps-ip>:5432/lux_marketing\n"
                    "  in the Replit Secrets panel (Settings → Secrets).\n",
                    host, port, probe_error.split("\n")[0], _SQLITE_FALLBACK
                )
                return _SQLITE_FALLBACK
            raise RuntimeError(
                "\n\n"
                f"  ❌  Database at {host}:{port} is not accepting connections.\n"
                f"  Error: {probe_error.split(chr(10))[0]}\n"
                "  Check that your database server is running and that\n"
                "  DATABASE_URL is correct. On Hostinger VPS:\n"
                "    sudo systemctl status postgresql\n"
                "    sudo systemctl start postgresql\n"
            )

        logging.info("DB: connection probe OK → %s:%s/%s",
                     host, port, parsed.path.lstrip("/"))

    _resolved_db_url_cache = _apply_mysql_shim(raw)
    return _resolved_db_url_cache


def _apply_mysql_shim(url: str) -> str:
    """Swap mysql:// → mysql+pymysql:// when MySQLdb is not installed."""
    if url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
        if importlib.util.find_spec("pymysql") is not None:
            url = url.replace("mysql://", "mysql+pymysql://", 1)
            logging.warning("MySQLdb missing; using PyMySQL driver instead.")
    return url


# ============================================================
# Application Factory
# ============================================================

def create_app() -> Flask:
    app = Flask(__name__)

    # --------------------------------------------------------
    # Secret Key
    # --------------------------------------------------------
    session_secret = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY")

    if not session_secret:
        session_secret = uuid4().hex
        logging.warning("SESSION_SECRET not set — using generated key.")

    app.secret_key = session_secret

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # --------------------------------------------------------
    # Database Configuration
    # --------------------------------------------------------
    db_url = _resolve_db_url()

    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }

    db.init_app(app)

    # --------------------------------------------------------
    # CSRF + Session
    # --------------------------------------------------------
    is_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"))

    app.config.update(
        WTF_CSRF_ENABLED=not is_replit,
        WTF_CSRF_TIME_LIMIT=None,
        # SameSite=None + Secure required so cookies work inside Replit's iframe
        # (parent frame is replit.com; app is replit.dev — treated as cross-site).
        # On VPS keep Lax which is the safe default for a direct browser session.
        SESSION_COOKIE_SAMESITE="None" if is_replit else "Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        REMEMBER_COOKIE_SAMESITE="None" if is_replit else "Lax",
        REMEMBER_COOKIE_SECURE=True,
        REMEMBER_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )

    csrf.init_app(app)

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        from flask import flash as _flash, redirect as _redirect, request as _req, url_for as _url
        _flash("Your session expired. Please try again.", "error")
        referrer = _req.referrer
        if referrer:
            return _redirect(referrer)
        if _req.path.startswith("/auth/"):
            return _redirect(_req.url)
        return _redirect(_url("auth.login"))

    # --------------------------------------------------------
    # Flask-Login
    # --------------------------------------------------------
    login_manager = LoginManager()
    login_manager.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "basic"

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        import logging as _logging
        _log = _logging.getLogger("auth")
        try:
            u = User.query.get(int(user_id))
            _log.info("USER_LOADER: id=%s → user=%s (authenticated=%s)", user_id, u, bool(u))
            return u
        except Exception as _exc:
            _log.warning("USER_LOADER: exception for id=%s: %s", user_id, _exc)
            return None

    # --------------------------------------------------------
    # Request Lifecycle
    # --------------------------------------------------------
    @app.before_request
    def assign_request_id():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid4())
        # Replit's proxy delivers HTTPS externally but passes HTTP internally.
        # Force WSGI to treat requests as HTTPS so Secure cookies are accepted.
        if is_replit and request.environ.get("wsgi.url_scheme") != "https":
            request.environ["wsgi.url_scheme"] = "https"

    @app.after_request
    def attach_request_id(response):
        request_id = getattr(g, "request_id", None)

        if request_id:
            response.headers["X-Request-ID"] = request_id

            if response.mimetype == "application/json" and response.status_code >= 400:
                payload = response.get_json(silent=True) or {}
                payload.setdefault("request_id", request_id)
                response.set_data(json.dumps(payload))

        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response

    # --------------------------------------------------------
    # Blueprints
    # --------------------------------------------------------
    from routes import main_bp
    from auth import auth_bp
    from user_management import user_bp
    from advanced_config import advanced_config_bp
    from marketing import marketing_bp
    from legal import legal_bp
    from x_auth import x_bp, x_api_bp
    from twilio_sms import twilio_bp
    from saas_mgmt import saas_bp, stripe_webhook_bp

    # IMPORTANT: Marketing first if it owns "/"
    app.register_blueprint(marketing_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(user_bp, url_prefix="/user")
    app.register_blueprint(advanced_config_bp)
    app.register_blueprint(x_bp)
    app.register_blueprint(x_api_bp)
    app.register_blueprint(twilio_bp)
    app.register_blueprint(saas_bp)
    app.register_blueprint(stripe_webhook_bp)
    try:
        from feedback import feedback_bp
        app.register_blueprint(feedback_bp)
    except Exception as exc:
        app.logger.warning("Failed to register feedback blueprint: %s", exc)
    try:
        from integrations_bp import integrations_bp
        app.register_blueprint(integrations_bp)
    except Exception as exc:
        app.logger.warning("Failed to register integrations blueprint: %s", exc)
    try:
        from inbox_pwa import inbox_pwa_bp
        app.register_blueprint(inbox_pwa_bp)
        print("✓ PWA Inbox loaded: /app/inbox")
    except Exception as exc:
        app.logger.warning("Failed to register inbox_pwa blueprint: %s", exc)
    # Stripe webhook deliveries from Stripe's edge servers will never carry
    # a CSRF token; the billing endpoints accept JSON from a Fetch call that
    # likewise does not include the form CSRF cookie. Both routes have their
    # own auth/signature/allowlist controls, so we exempt the whole blueprint.
    csrf.exempt(stripe_webhook_bp)
    print("✓ SaaS Command Center routes loaded: /saas, /api/stripe/webhook (CSRF-exempt)")

    from utils import get_campaign_status_color
    app.jinja_env.filters['campaign_status_color'] = get_campaign_status_color

    # --------------------------------------------------------
    # App Context Initialization
    # --------------------------------------------------------
    with app.app_context():
        import models  # noqa

        db.create_all()

        # Apply any missing columns to existing tables (safe migrations)
        try:
            from sqlalchemy import inspect, text
            inspector = inspect(db.engine)
            migrations = {
                "automation_trigger_library": [
                    ("name", "VARCHAR(200)"),
                    ("trigger_type", "VARCHAR(100)"),
                    ("description", "TEXT"),
                    ("category", "VARCHAR(100)"),
                    ("trigger_config", "JSON"),
                    ("steps_template", "JSON"),
                    ("is_active", "BOOLEAN DEFAULT TRUE"),
                    ("created_at", "TIMESTAMP"),
                ],
                "automation_test": [
                    ("automation_id", "INTEGER"),
                    ("test_contact_id", "INTEGER"),
                    ("test_data", "JSON"),
                    ("status", "VARCHAR(50) DEFAULT 'pending'"),
                    ("test_results", "JSON"),
                    ("started_at", "TIMESTAMP"),
                    ("completed_at", "TIMESTAMP"),
                    ("created_at", "TIMESTAMP"),
                ],
                "automation_ab_test": [
                    ("automation_id", "INTEGER"),
                    ("name", "VARCHAR(200)"),
                    ("variant_a", "JSON"),
                    ("variant_b", "JSON"),
                    ("status", "VARCHAR(50) DEFAULT 'running'"),
                    ("winner", "VARCHAR(10)"),
                    ("created_at", "TIMESTAMP"),
                ],
            }
            for table, columns in migrations.items():
                if inspector.has_table(table):
                    existing = {c["name"] for c in inspector.get_columns(table)}
                    for col_name, col_type in columns:
                        if col_name not in existing:
                            try:
                                db.session.execute(text(
                                    f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
                                ))
                                db.session.commit()
                                logging.info(f"Added column {col_name} to {table}")
                            except Exception as col_err:
                                db.session.rollback()
                                logging.warning(f"Could not add {col_name} to {table}: {col_err}")
        except Exception as mig_err:
            logging.warning(f"Migration check failed: {mig_err}")

        try:
            from services.automation_service import AutomationService
            AutomationService.seed_trigger_library()
            logging.info("Automation library seeded")
        except Exception as e:
            logging.error(f"Automation seed failed: {e}")
            db.session.rollback()

        try:
            from error_logger import setup_error_logging_handler
            setup_error_logging_handler()
        except Exception as e:
            logging.error(f"Error logging setup failed: {e}")

        try:
            from agent_scheduler import (
                initialize_agent_scheduler,
                get_agent_scheduler,
            )

            initialize_agent_scheduler()
            app.extensions["agent_scheduler"] = get_agent_scheduler()
            logging.info("Agent scheduler initialized")
        except Exception as e:
            logging.error(f"Agent scheduler failed: {e}")

    return app


# ============================================================
# Gunicorn Entry
# ============================================================

app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)