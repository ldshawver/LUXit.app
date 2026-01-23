import os
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, g, jsonify, redirect, request, url_for
from flask_login import LoginManager
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
        return User.query.get(int(user_id))

    @app.before_request
    def canonical_and_request_id():
        g.request_id = request.headers.get("X-Request-ID", str(uuid4()))
        if app.testing:
            return None
        if request.host.startswith("127.0.0.1"):
            return None
        if request.path in {"/health", "/healthz", "/health/config", "/health/deep", "/__version"}:
            return None
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
        if host and host not in ALLOWED_HOSTS:
            return redirect(f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}", 301)
        return None

    from routes import main_bp
    from auth import auth_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")

    @app.route("/healthz")
    def healthz():
        return app.view_functions["main.health_check"]()

    @app.route("/__version")
    def version():
        return (
            jsonify(
                {
                    "app": "luxit",
                    "version": os.getenv("APP_VERSION", "unknown"),
                    "git_sha": os.getenv("GIT_SHA", "unknown"),
                }
            ),
            200,
        )

    @app.route("/")
    def index():
        return redirect(url_for("auth.login"))

    return app


if __name__ == "__main__":
    application = create_app()
    application.run()
