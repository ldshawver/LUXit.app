"""Application entry point."""
import logging
import os
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, g, redirect, render_template, request, url_for
from flask_login import LoginManager
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, csrf

CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "app.luxit.app")
ALLOWED_HOSTS = {"luxit.app", "www.luxit.app", "app.luxit.app", "api.luxit.app"}

load_dotenv("/etc/lux-marketing/lux.env")


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    secret_key = os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
    if not secret_key:
        raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

    app.config.update(
        SECRET_KEY=secret_key,
        SERVER_NAME=CANONICAL_HOST,
        PREFERRED_URL_SCHEME="https",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="None",
        WTF_CSRF_TIME_LIMIT=3600,
    )

    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL",
        "sqlite:///email_marketing.db",
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    db.init_app(app)
    csrf.init_app(app)

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        try:
            return User.query.get(int(user_id))
        except SQLAlchemyError as exc:
            app.logger.error("User loader DB error: %s", exc)
            try:
                db.session.rollback()
            except Exception:
                pass
            return None

    @app.before_request
    def canonical_and_request_id():
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
    from auth import auth_bp
    from marketing import marketing_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(marketing_bp)

    @app.route("/")
    def marketing_home():
        return render_template("marketing/index.html")

    @app.route("/login")
    def login():
        from auth import login as auth_login
        return auth_login()

    @app.route("/logout")
    def logout():
        from auth import logout as auth_logout
        return auth_logout()

    return app


app = create_app()


if __name__ == "__main__":
    application = create_app()
    application.run()
