""Application entry point."""
import os
from uuid import uuid4

from flask import Flask, g, request, redirect, url_for
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, csrf


def create_app(testing: bool = False):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.testing = testing

    # --------------------------------------------------
    # Secrets
    # --------------------------------------------------
    app.secret_key = os.environ.get("SESSION_SECRET")
    if not app.secret_key and testing:
        app.secret_key = "luxit-test-secret"

    # --------------------------------------------------
    # Core config (sessions + CSRF)
    # --------------------------------------------------
    app.config.update(
        SECRET_KEY=app.secret_key,
        SESSION_COOKIE_SECURE=True,      # HTTPS only
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",   # REQUIRED for login CSRF
        WTF_CSRF_TIME_LIMIT=3600,
    )

    # 🚫 DO NOT set SERVER_NAME (breaks cookies behind Nginx)
    # app.config["SERVER_NAME"] = ...

    # --------------------------------------------------
    # Database
    # --------------------------------------------------
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL",
        "sqlite:///email_marketing.db",
    )
   # --------------------------------------------------
    # Proxy (Nginx → Flask)
    # --------------------------------------------------
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
    )

    # --------------------------------------------------
    # Extensions
    # --------------------------------------------------
    db.init_app(app)
    csrf.init_app(app)

    # --------------------------------------------------
    # Request lifecycle hooks
    # --------------------------------------------------

    @app.before_request
    def assign_request_id():
        g.request_id = request.headers.get(
            "X-Request-ID",
            str(uuid4())
        )

    @app.before_request
    def enforce_auth_boundary():
        path = request.path

        # ---------------------------
        # Public routes (NO AUTH)
        # ---------------------------
        if (
            path == "/"
            or path.startswith("/features")
            or path.startswith("/pricing")
            or path.startswith("/static")
            or path.startswith("/health")
            or path.startswith("/auth")
        ):
            return None

        # ---------------------------
        # Locked app + API
        # ---------------------------
        if path.startswith("/app") or path.startswith("/api"):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login"))

    @app.teardown_request
    def rollback_on_error(exc=None):
        if exc:
            try:
                db.session.rollback()
            except Exception:
                pass

    # --------------------------------------------------
    # Blueprints
    # --------------------------------------------------
    from routes import main_bp
    from auth import auth_bp
    from marketing import marketing_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(marketing_bp)

    return app
