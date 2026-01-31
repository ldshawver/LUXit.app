"""
Application entry point.
"""
import os
from uuid import uuid4

from flask import Flask, g, redirect, request, url_for
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, csrf

# --------------------------------------------------
# Application factory
# --------------------------------------------------

def create_app(testing: bool = False):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.testing = testing

    secret_key = (
        os.getenv("SESSION_SECRET")
        or os.getenv("SECRET_KEY")
        or ("ci-test-secret" if testing else None)
    )
    if not secret_key:
        if testing:
            secret_key = "luxit-test-secret"
        else:
            raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

    app.config.update(
        SECRET_KEY=secret_key,
        TESTING=testing,
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            "sqlite:///email_marketing.db",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        WTF_CSRF_SSL_STRICT=False,
    )

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    # Extensions
    db.init_app(app)
    csrf.init_app(app)

    # Login
    login_manager = LoginManager(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.query.get(int(user_id))

    # Request ID
    @app.before_request
    def request_id():
        g.request_id = request.headers.get("X-Request-ID", str(uuid4()))
        if app.testing:
            return None
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
        if host and host not in ALLOWED_HOSTS:
            return redirect(f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}", 301)
        return None

    @app.context_processor
    def inject_company_context():
        from flask_login import current_user
        try:
            if not current_user.is_authenticated:
                return {}
            import models
            if not hasattr(models, "Company"):
                return {}
            return {
                "current_company": current_user.get_default_company(),
                "user_companies": current_user.get_companies_safe(),
            }
        except Exception as exc:
            app.logger.error("Template context error: %s", exc)
            try:
                db.session.rollback()
            except Exception:
                pass
            return {}

    @app.teardown_request
    def rollback_on_error(_exception=None):
        if _exception:
            try:
                db.session.rollback()
            except Exception:
                pass

    from routes import main_bp

    # ---- Blueprints ----
    from main import main_bp
    from auth import auth_bp
    from marketing import marketing_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(marketing_bp)

    # ---- Routes ----
    # "/" is handled by marketing_bp for the public marketing homepage

    @app.route("/health")
    def health():
        return {"status": "ok"}, 200

    # ---- Side effects (PROD ONLY) ----
    if False:
        with app.app_context():
            import models
            db.create_all()

    @app.route("/logout")
    def logout():
        from auth import logout as auth_logout
        return auth_logout()

    return app


# --------------------------------------------------
# Canonical export (Gunicorn imports THIS)
# --------------------------------------------------

app = create_app(testing=os.getenv("FLASK_ENV") == "testing")
