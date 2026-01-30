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
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            "sqlite:///email_marketing.db",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
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

    # ---- Blueprints ----
    from main import main_bp
    from auth import auth_bp
    from marketing import marketing_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(marketing_bp)

    # ---- Routes ----
    @app.route("/")
    def index():
        return redirect(url_for("auth.login"))

    @app.route("/health")
    def health():
        return {"status": "ok"}, 200

    # ---- Side effects (PROD ONLY) ----
    if False:
        with app.app_context():
            import models
            db.create_all()

    return app


# --------------------------------------------------
# Canonical export (Gunicorn imports THIS)
# --------------------------------------------------

app = create_app(testing=os.getenv("FLASK_ENV") == "testing")
