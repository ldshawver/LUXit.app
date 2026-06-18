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

from flask import Flask, g, has_request_context, jsonify, redirect, request
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
    """Resolve the database URL.

    Resolution order:
    1. Replit managed PostgreSQL — built from PGHOST/PGPORT/PGUSER/PGPASSWORD/
       PGDATABASE env vars when PGHOST is present.  These are set automatically
       by Replit's Database tool and always point to the live managed instance.
    2. DATABASE_URL secret/env — used on VPS / non-Replit environments, or
       when PGHOST is not available.  The legacy ``postgres://`` scheme is
       normalised to ``postgresql://`` for SQLAlchemy compatibility.

    Raises RuntimeError if neither source provides a usable URL so the
    process fails fast instead of silently writing to a wrong database.
    Result is cached so repeated calls skip the URL rewrite.
    """
    global _resolved_db_url_cache

    # Testing shortcut — always wins, bypasses cache so pytest never touches
    # the live database regardless of what PGHOST / DATABASE_URL are set to.
    if os.environ.get("FLASK_ENV") == "testing":
        return os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")

    if _resolved_db_url_cache is not None:
        return _resolved_db_url_cache

    import urllib.parse as _uparse

    def _probe(url: str, timeout: int = 4) -> bool:
        """Return True if a psycopg2 connection to *url* succeeds."""
        try:
            import psycopg2
            conn = psycopg2.connect(url, connect_timeout=timeout)
            conn.close()
            return True
        except Exception:
            return False

    _is_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEV_DOMAIN"))

    # ── 1. Replit managed PostgreSQL (PG* vars) ──────────────────────────────
    # Replit's Database tool sets PGHOST to an internal hostname (not a known
    # external SaaS host).  We probe first to avoid spending time on stale
    # Neon/Supabase credentials that are no longer valid.
    pg_host = os.environ.get("PGHOST", "").strip()
    pg_port = os.environ.get("PGPORT", "5432").strip()
    pg_user = os.environ.get("PGUSER", "").strip()
    pg_password = os.environ.get("PGPASSWORD", "").strip()
    pg_database = os.environ.get("PGDATABASE", "").strip()

    if pg_host and pg_user and pg_database:
        candidate = (
            f"postgresql://{_uparse.quote(pg_user, safe='')}:"
            f"{_uparse.quote(pg_password, safe='')}@"
            f"{pg_host}:{pg_port}/{pg_database}"
        )
        if _probe(candidate):
            os.environ["DATABASE_URL"] = candidate
            _resolved_db_url_cache = candidate
            logging.info(
                "DB: using managed PostgreSQL at %s/%s", pg_host, pg_database
            )
            return _resolved_db_url_cache
        else:
            logging.warning(
                "DB: PG* vars point to %s but connection failed — skipping.", pg_host
            )

    # ── 2. DATABASE_URL (VPS / explicit override) ────────────────────────────
    raw = os.environ.get("DATABASE_URL", "").strip()
    if raw:
        # Normalise postgres:// → postgresql:// (Heroku/Render/Neon legacy scheme)
        if raw.startswith("postgres://"):
            raw = raw.replace("postgres://", "postgresql://", 1)
        url = _apply_mysql_shim(raw)
        if _probe(url):
            _resolved_db_url_cache = url
            _parsed = _uparse.urlparse(url)
            logging.info(
                "DB: using PostgreSQL at %s/%s",
                _parsed.hostname or "?",
                (_parsed.path or "").lstrip("/"),
            )
            return _resolved_db_url_cache
        else:
            logging.warning(
                "DB: DATABASE_URL unreachable (%s) — continuing to local fallback.",
                _uparse.urlparse(raw).hostname or raw[:40],
            )

    # ── 3. Local PostgreSQL via /tmp socket (Replit dev — started by start.sh)
    if _is_replit:
        try:
            import psycopg2 as _pg2
            _local_conn = _pg2.connect(
                host="/tmp",
                port=5432,
                user="luxuser",
                password="LuxPass2024!",
                dbname="lux_marketing",
                connect_timeout=4,
            )
            _local_conn.close()
            _socket_url = (
                "postgresql+psycopg2://luxuser:LuxPass2024%21"
                "@/lux_marketing?host=/tmp"
            )
            os.environ["DATABASE_URL"] = _socket_url
            _resolved_db_url_cache = _socket_url
            logging.info("DB: Connected to local PostgreSQL via /tmp socket.")
            return _resolved_db_url_cache
        except Exception as _local_exc:
            logging.warning("DB: Local /tmp socket also failed: %s", _local_exc)

    raise RuntimeError(
        "\n\n"
        "  ❌  No database connection could be established.\n"
        "  • In Replit: open the Database tool to provision managed PostgreSQL,\n"
        "    then delete the old PGHOST / DATABASE_URL secrets so fresh ones are set.\n"
        "  • On VPS: ensure DATABASE_URL is correct and PostgreSQL is running.\n"
    )


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
            u = db.session.get(User, int(user_id))
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
    from auth import auth_bp, login as auth_login
    from user_management import user_bp
    from advanced_config import advanced_config_bp
    from marketing import marketing_bp
    from legal import legal_bp
    from x_auth import x_bp, x_api_bp
    from twilio_sms import twilio_bp, api_twilio_bp
    from saas_mgmt import saas_bp, stripe_webhook_bp
    from marketing_api import marketing_api_bp

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
    app.register_blueprint(api_twilio_bp)
    csrf.exempt(api_twilio_bp)
    app.register_blueprint(saas_bp)
    app.register_blueprint(stripe_webhook_bp)
    app.register_blueprint(marketing_api_bp)
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
        from admin_communications import admin_communications_bp
        app.register_blueprint(admin_communications_bp)
    except Exception as exc:
        app.logger.warning("Failed to register admin communications blueprint: %s", exc)
    try:
        from inbox_pwa import inbox_pwa_bp
        app.register_blueprint(inbox_pwa_bp)
        # The PWA inbox API endpoints are all session-authenticated via
        # _require_auth().  The service worker caches the page HTML, which
        # means the CSRF token baked into the page can expire before the
        # cached copy is refreshed.  Session auth is sufficient here.
        csrf.exempt(inbox_pwa_bp)
        print("✓ PWA Inbox loaded: /app/inbox (CSRF-exempt, session-auth)")
    except Exception as exc:
        app.logger.warning("Failed to register inbox_pwa blueprint: %s", exc)
    # Stripe webhook deliveries from Stripe's edge servers will never carry
    # a CSRF token; the billing endpoints accept JSON from a Fetch call that
    # likewise does not include the form CSRF cookie. Both routes have their
    # own auth/signature/allowlist controls, so we exempt the whole blueprint.
    csrf.exempt(stripe_webhook_bp)
    print("✓ SaaS Command Center routes loaded: /saas, /api/stripe/webhook (CSRF-exempt)")

    @app.get("/login")
    def login_alias():
        """Public login alias used by deploy smoke tests and legacy links."""
        return auth_login()

    @app.get("/healthz")
    def healthz():
        """Minimal load-balancer health check that never requires auth."""
        return jsonify({"status": "ok"})

    @app.get("/__version")
    def version():
        """Build/version metadata endpoint with safe defaults."""
        return jsonify(
            {
                "app": "luxit",
                "version": os.environ.get("APP_VERSION", "unknown"),
                "git_sha": os.environ.get("GIT_SHA", os.environ.get("COMMIT_SHA", "unknown")),
            }
        )

    @app.get("/communication-hub")
    @app.get("/communications")
    @app.get("/communications-hub")
    def communications_alias():
        """Legacy communications URLs redirect to the tenant-scoped hub."""
        return redirect("/twilio/comms", code=302)

    from utils import get_campaign_status_color
    app.jinja_env.filters['campaign_status_color'] = get_campaign_status_color

    # --------------------------------------------------------
    # App Context Initialization
    # --------------------------------------------------------
    with app.app_context():
        import models  # noqa

        db.create_all()

        # Self-heal guard: ensure at least one company exists and users are linked.
        # This prevents post-sync "no company" outages when ops scripts were skipped.
        try:
            from models import Company, User, UserCompanyAccess

            changed = 0
            company = Company.query.filter_by(is_active=True).order_by(Company.id.asc()).first()
            if not company:
                company = Company.query.order_by(Company.id.asc()).first()
                if company:
                    company.is_active = True
                    logging.warning(
                        "Startup self-heal reactivated fallback company '%s' (id=%s)",
                        company.name,
                        company.id,
                    )
                    changed += 1

            if not company:
                company = Company(
                    name="LUXit Marketing",
                    is_active=True,
                    billing_tier="professional",
                    billing_status="active",
                    subscription_tier="professional",
                    onboarding_status="complete",
                )
                db.session.add(company)
                db.session.flush()
                logging.warning(
                    "Startup self-heal created fallback company '%s' (id=%s)",
                    company.name,
                    company.id,
                )
                changed += 1

            for user in User.query.all():
                if user.is_admin:
                    if user.ensure_default_company_context():
                        changed += 1
                    continue

                acc = UserCompanyAccess.query.filter_by(
                    user_id=user.id, company_id=company.id
                ).first()
                if not acc:
                    acc = UserCompanyAccess(
                        user_id=user.id,
                        company_id=company.id,
                        role="viewer",
                        is_default=True,
                        can_access_full_app=True,
                        can_access_mobile_inbox=False,
                    )
                    db.session.add(acc)
                    changed += 1
                if not user.default_company_id:
                    user.default_company_id = company.id
                    changed += 1

            if changed:
                db.session.commit()
                logging.warning("Startup self-heal updated %s user/company links.", changed)
        except Exception as _self_heal_exc:
            db.session.rollback()
            logging.warning("Startup self-heal skipped due to error: %s", _self_heal_exc)

        # ── Auto-backfill provider credentials (incremental, every cold start) ─
        # Compares every env var in the CREDENTIALS map against existing DB rows.
        # Only imports rows that are missing — already-present rows are skipped.
        # This means:
        #   • Fresh deploy (empty table) → all present env vars are imported.
        #   • Subsequent restarts → no-op if nothing changed (fast set diff).
        #   • New env var added later → picked up automatically on next restart
        #     without needing a manual script run or a full table wipe.
        # The entire block is non-blocking: any error logs a warning and continues.
        # Skipped entirely during test runs so tests never seed live credentials
        # into the ephemeral DB (which would break monkeypatch-based env tests).
        if os.environ.get("FLASK_ENV") != "testing":
            try:
                from models import ProviderCredential
                from scripts.backfill_provider_credentials import CREDENTIALS, run_backfill
                import os as _os
                # Build set of (provider_slug, scope, key) already in DB
                _db_keys = {
                    (r.provider_slug, r.scope, r.key)
                    for r in ProviderCredential.query.with_entities(
                        ProviderCredential.provider_slug,
                        ProviderCredential.scope,
                        ProviderCredential.key,
                    ).all()
                }
                # Check whether any env var is set but has no DB row
                _needs_backfill = any(
                    _os.environ.get(env_key) and (provider, scope, env_key) not in _db_keys
                    for provider, scope, env_key, _field in CREDENTIALS
                )
                if _needs_backfill:
                    logging.info("Auto-backfill: new env credentials detected — importing missing rows")
                    run_backfill()  # already inside app context; idempotent per-row
                else:
                    logging.debug("Auto-backfill: all env credentials already in DB — skipping")
            except Exception as _backfill_exc:
                logging.warning("Auto-backfill skipped (non-fatal): %s", _backfill_exc)

        # ── DB startup report (never logs secrets) ─────────────────────────
        try:
            from models import User, Company
            import re as _re
            _user_count    = User.query.count()
            _company_count = Company.query.count()
            _active_names  = [
                c.name for c in Company.query.filter_by(is_active=True).limit(20).all()
            ]
            # Mask credentials in any connection string
            _safe_url = _re.sub(r"://[^@]+@", "://***@", db_url)
            logging.info(
                "\n"
                "  ── DB STARTUP REPORT ──────────────────────────────────\n"
                "  DB resolved to : %s\n"
                "  Users          : %d\n"
                "  Companies      : %d\n"
                "  Active names   : %s\n"
                "  ───────────────────────────────────────────────────────",
                _safe_url,
                _user_count,
                _company_count,
                ", ".join(_active_names) if _active_names else "(none)",
            )
            if _user_count == 0:
                logging.warning(
                    "\n"
                    "  ⚠️  Zero users found in database.\n"
                    "  Current DATABASE_URL: %s\n"
                    "  If this is a fresh database, run: python3 scripts/bootstrap.py\n"
                    "  If migrating from SQLite, run:   python3 scripts/sqlite_to_postgres.py --help",
                    _safe_url,
                )
        except Exception as _diag_exc:
            logging.warning("DB startup report failed: %s", _diag_exc)

        # PostHog — initialise client eagerly so first events aren't dropped
        try:
            from services.posthog_analytics import _get_client
            _get_client()
        except Exception:
            pass

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
                "sms_campaign": [
                    ("company_id", "INTEGER"),
                    ("created_by_user_id", "INTEGER"),
                    ("objective", "TEXT"),
                    ("segment", "VARCHAR(100)"),
                    ("status", "VARCHAR(50) DEFAULT 'draft'"),
                    ("updated_at", "TIMESTAMP"),
                ],
                "sms_recipient": [
                    ("company_id", "INTEGER"),
                    ("campaign_id", "INTEGER"),
                    ("contact_id", "INTEGER"),
                    ("status", "VARCHAR(50)"),
                    ("provider_message_sid", "VARCHAR(120)"),
                    ("sent_at", "TIMESTAMP"),
                    ("delivered_at", "TIMESTAMP"),
                    ("replied_at", "TIMESTAMP"),
                    ("opted_out_at", "TIMESTAMP"),
                    ("provider_error_code", "VARCHAR(50)"),
                    ("error_message", "TEXT"),
                    ("created_at", "TIMESTAMP"),
                    ("updated_at", "TIMESTAMP"),
                ],
                "social_post": [
                    ("company_id", "INTEGER"),
                    ("user_id", "INTEGER"),
                    ("platforms", "JSON"),
                    ("media_urls", "JSON"),
                    ("updated_at", "TIMESTAMP"),
                ],
                "agent_report": [
                    ("company_id", "INTEGER"),
                ],
                "agent_log": [
                    ("company_id", "INTEGER"),
                ],
                "agent_deliverable": [
                    ("company_id", "INTEGER"),
                    ("priority", "VARCHAR(50) DEFAULT 'normal'"),
                    ("requested_by_id", "INTEGER"),
                ],
                "twilio_conversation": [
                    ("company_id", "INTEGER"),
                    ("phone_number_id", "INTEGER"),
                    ("contact_id", "INTEGER"),
                    ("from_number", "VARCHAR(20)"),
                    ("to_number", "VARCHAR(20)"),
                    ("contact_name", "VARCHAR(200)"),
                    ("contact_source", "VARCHAR(50)"),
                    ("is_read", "BOOLEAN DEFAULT FALSE"),
                    ("is_opted_out", "BOOLEAN DEFAULT FALSE"),
                    ("sms_opt_in_at", "TIMESTAMP"),
                    ("sms_opt_out_at", "TIMESTAMP"),
                    ("is_first_contact", "BOOLEAN DEFAULT TRUE"),
                    ("lead_captured", "BOOLEAN DEFAULT FALSE"),
                    ("tags", "JSON"),
                    ("notes", "TEXT"),
                    ("assigned_user_id", "INTEGER"),
                    ("last_message_at", "TIMESTAMP"),
                    ("last_message_preview", "VARCHAR(200)"),
                    ("message_count", "INTEGER DEFAULT 0"),
                    ("created_at", "TIMESTAMP"),
                    ("updated_at", "TIMESTAMP"),
                ],
                "twilio_message": [
                    ("conversation_id", "INTEGER"),
                    ("company_id", "INTEGER"),
                    ("twilio_sid", "VARCHAR(100)"),
                    ("direction", "VARCHAR(10)"),
                    ("from_number", "VARCHAR(20)"),
                    ("to_number", "VARCHAR(20)"),
                    ("body", "TEXT"),
                    ("status", "VARCHAR(50) DEFAULT 'received'"),
                    ("num_segments", "INTEGER DEFAULT 1"),
                    ("media_urls", "JSON"),
                    ("is_auto_reply", "BOOLEAN DEFAULT FALSE"),
                    ("rule_id", "INTEGER"),
                    ("error_code", "VARCHAR(20)"),
                    ("error_message", "TEXT"),
                    ("raw_payload", "JSON"),
                    ("created_at", "TIMESTAMP"),
                ],
                "twilio_call_log": [
                    ("company_id", "INTEGER"),
                    ("twilio_sid", "VARCHAR(100)"),
                    ("direction", "VARCHAR(20)"),
                    ("from_number", "VARCHAR(20)"),
                    ("to_number", "VARCHAR(20)"),
                    ("status", "VARCHAR(50)"),
                    ("duration", "INTEGER DEFAULT 0"),
                    ("caller_name", "VARCHAR(200)"),
                    ("notes", "TEXT"),
                    ("missed_text_sent", "BOOLEAN DEFAULT FALSE"),
                    ("raw_payload", "JSON"),
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

        if not app.config.get("TESTING"):
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
