"""
LUX package.
Flask application factory.
"""

import os
import logging
from flask import Flask, redirect, request, url_for, current_app, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from lux.config import config
from lux.extensions import db, login_manager, csrf, limiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app(config_name: str | None = None) -> Flask:
    """Create and configure the Flask application."""

    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "production")
from flask import redirect, url_for, current_app, jsonify
import os

# --- Health aliases (keep existing /health canonical) ---

@app.route("/healthz")
def healthz_alias():
    # Redirect to existing health endpoint
    return redirect(url_for("health.health"), code=302)


@app.route("/__version")
def version():
    return jsonify({
        "version": current_app.config.get("APP_VERSION", "unknown"),
        "git_sha": os.getenv("GITHUB_SHA", "unknown"),
    }), 200

    # ------------------------------------------------------------------
    # App bootstrap
    # ------------------------------------------------------------------
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    app.config.from_object(config[config_name])

    # Trust reverse proxy (nginx)
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    # ------------------------------------------------------------------
    # Request guards
    # ------------------------------------------------------------------
    @app.before_request
    def block_unsafe_next_param():
        if "next" in request.args:
            return redirect(url_for("auth.login", _external=False))

    @login_manager.user_loader
    def load_user(user_id):
        from lux.models.user import User
        return User.query.get(int(user_id))

    # ------------------------------------------------------------------
    # Blueprints
    # ------------------------------------------------------------------
    from lux.blueprints.auth.routes import auth_bp
    from lux.blueprints.main.routes import main_bp
    from lux.blueprints.user.routes import user_bp
    from lux.blueprints.health.routes import health_bp  # <-- IMPORTANT

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(main_bp)
    app.register_blueprint(user_bp, url_prefix="/user")
    app.register_blueprint(health_bp)  # provides /health, /health/deep

    # ------------------------------------------------------------------
    # Health aliases (DO NOT duplicate logic)
    # ------------------------------------------------------------------
    @app.route("/healthz")
    def healthz_alias():
        return redirect(url_for("health.health"), code=302)

    @app.route("/__version")
    def version():
        return jsonify({
            "version": current_app.config.get("APP_VERSION", "unknown"),
            "git_sha": os.getenv("GITHUB_SHA", "unknown"),
            "env": config_name,
        }), 200

    # ------------------------------------------------------------------
    # Jinja filters
    # ------------------------------------------------------------------
    @app.template_filter("campaign_status_color")
    def campaign_status_color_filter(status: str) -> str:
        return {
            "draft": "secondary",
            "scheduled": "warning",
            "sending": "info",
            "sent": "success",
            "failed": "danger",
            "paused": "dark",
        }.get(status, "secondary")

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------
    from scheduler import init_scheduler
    init_scheduler(app)

    # ------------------------------------------------------------------
    # DB bootstrap (safe)
    # ------------------------------------------------------------------
    with app.app_context():
        import lux.models  # noqa
        db.create_all()

    logger.info("LUX initialized (%s)", config_name)
    return app
