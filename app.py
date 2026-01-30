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
    )

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    db.init_app(app)
    csrf.init_app(app)

    login_manager = LoginManager(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.query.get(int(user_id))

    from main import main_bp
    from auth import auth_bp
    from marketing import marketing_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(marketing_bp)

    return app


# --------------------------------------------------
# 🔑 CANONICAL EXPORTS (THIS IS THE KEY PART)
# --------------------------------------------------

app = create_app(testing=os.getenv("FLASK_ENV") == "testing")
