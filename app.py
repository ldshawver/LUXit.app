"""Application entry point."""

import importlib.util
import json
import logging
import os
import re
from datetime import timedelta
from typing import cast
from uuid import uuid4

from flask import Flask, g, has_request_context, request
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.orm import DeclarativeBase
from werkzeug.middleware.proxy_fix import ProxyFix


# ============================================================
# Logging
# ============================================================

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = (
            getattr(g, "request_id", "-") if has_request_context() else "-"
        )
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


log_format = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

logging.basicConfig(level=logging.INFO, format=log_format)
root_logger = logging.getLogger()
root_logger.addFilter(RequestIdFilter())
root_logger.addFilter(RedactionFilter())


# ============================================================
# Database
# ============================================================

class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


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

    # Required when behind Nginx / reverse proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # --------------------------------------------------------
    # Database Configuration
    # --------------------------------------------------------
    db_url = os.environ.get("DATABASE_URL", "sqlite:///email_marketing.db")

    if db_url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
        if importlib.util.find_spec("pymysql") is not None:
            db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)
            logging.warning("MySQLdb missing; using PyMySQL.")

    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }

    db.init_app(app)

    # --------------------------------------------------------
    # CSRF + Session
    # --------------------------------------------------------
    app.config.update(
        WTF_CSRF_ENABLED=True,
        WTF_CSRF_TIME_LIMIT=None,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    )

    CSRFProtect(app)

    # --------------------------------------------------------
    # Flask-Login
    # --------------------------------------------------------
    login_manager = LoginManager()
    login_manager.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "info"

    # --------------------------------------------------------
    # Request Lifecycle
    # --------------------------------------------------------
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

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(user_bp, url_prefix="/user")
    app.register_blueprint(advanced_config_bp)
    app.register_blueprint(marketing_bp)

    # --------------------------------------------------------
    # App Context Initialization
    # --------------------------------------------------------
    with app.app_context():
        import models  # noqa

        db.create_all()

        try:
            from services.automation_service import AutomationService
            AutomationService.seed_trigger_library()
            logging.info("Automation library seeded")
        except Exception as e:
            logging.error(f"Automation seed failed: {e}")

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