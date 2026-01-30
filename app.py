"""
Application entry point.
"""
print("🔥 LOADED app.py FROM:", __file__)

import os
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, g, redirect, render_template, request
from flask_login import LoginManager
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, csrf

# --------------------------------------------------
# Environment
# --------------------------------------------------

load_dotenv("/etc/lux-marketing/lux.env")

CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "luxit.app")
ALLOWED_HOSTS = {
    "luxit.app",
    "www.luxit.app",
    "app.luxit.app",
    "api.luxit.app",
}

# --------------------------------------------------
# Application factory
# --------------------------------------------------

def create_app(testing: bool = False):
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # ---- Secret handling (CI SAFE) ----
    secret_key = (
        os.getenv("SESSION_SECRET")
        or os.getenv("SECRET_KEY")
        or ("ci-test-secret" if testing else None)
    )

    if not secret_key:
        raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

    app.config.update(
        SECRET_KEY=secret_key,
        TESTING=testing,
        PREFERRED_URL_SCHEME="https",
        SESSION_COOKIE_SECURE=not testing,
        SESSION_COOKIE_SAMESITE="None",
        WTF_CSRF_TIME_LIMIT=3600,
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            "sqlite:///email_marketing.db",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    # ---- Proxy awareness ----
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

    # ---- Extensions ----
    db.init_app(app)
    csrf.init_app(app)

    # ---- Login manager ----
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        try:
            return User.query.get(int(user_id))
        except SQLAlchemyError:
            db.session.rollback()
            return None

    # ---- Request hooks ----
    @app.before_request
    def canonical_and_request_id():
        g.request_id = request.headers.get("X-Request-ID", str(uuid4()))

        if app.testing:
            return None

        host = (
            request.headers.get("X-Forwarded-Host")
            or request.host
            or ""
        ).split(":")[0].lower()

        if host and host not in ALLOWED_HOSTS:
            return redirect(
                f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}",
                301,
            )

    # ---- Blueprints ----
    from routes import main_bp
    from auth import auth_bp
    from marketing import marketing_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(marketing_bp)

    # ---- Public root ----
    @app.route("/")
    def marketing_home():
        return render_template("marketing/index.html")

    return app


# --------------------------------------------------
# 🔑 REQUIRED EXPORT (CI + GUNICORN)
# --------------------------------------------------

app = create_app(testing=os.getenv("FLASK_ENV") == "testing")

# --------------------------------------------------
# Local dev
# --------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
