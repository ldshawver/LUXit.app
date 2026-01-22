"""
LUX package
Flask application factory
"""

import os
import logging

from flask import Flask, redirect, request, url_for, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from lux.config import config
from lux.extensions import db, login_manager, csrf, limiter

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# Application Factory
# ------------------------------------------------------------

def create_app(config_name: str | None = None) -> Flask:
    """
    Create and configure the Flask application.
    """

    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "production")

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # --------------------------------------------------------
    # Configuration
    # --------------------------------------------------------

    app.config.from_object(config[config_name])

    # --------------------------------------------------------
    # Reverse proxy support (nginx / load balancer)
    # --------------------------------------------------------

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

    # --------------------------------------------------------
    # Extensions
    # --------------------------------------------------------

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # --------------------------------------------------------
    # Flask-Login
    # --------------------------------------------------------

    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    @login_manager.user_loader
    def load_user(user_id):
        from lux.models.user import User
        return User.query.get(int(user_id))

    # --------------------------------------------------------
    # Request Guards
    # --------------------------------------------------------

    @app.before_request
    def guard_next_param():
        """
        Prevent open redirects via ?next=
        Allow health/version endpoints to pass untouched.
        """
        if request.endpoint in {"healthz", "version"}:
            return None

        if "next" in request.args:
            return redirect(url_for("auth.login", _external=False))

    # --------------------------------------------------------
    # Blueprints
    # --------------------------------------------------------

    from lux.blueprints.auth.routes import auth_bp
    from lux.blueprints.main.routes import main_bp
    from lux.blueprints.user.routes import user_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(main_bp)
    app.register_blueprint(user_bp, url_prefix="/user")

    # --------------------------------------------------------
    # Database bootstrap
    # --------------------------------------------------------

    with app.app_context():
        import lux.models  # noqa
        db.create_all()

    # --------------------------------------------------------
    # Template filters
    # --------------------------------------------------------

    @app.template_filter("campaign_status_color")
    def campaign_status_color(status: str) -> str:
        return {
            "draft": "secondary",
            "scheduled": "warning",
            "sending": "info",
            "sent": "success",
            "failed": "danger",
            "paused": "dark",
        }.get(status, "secondary")

    # --------------------------------------------------------
    # Scheduler
    # --------------------------------------------------------

    try:
        from scheduler import init_scheduler
        init_scheduler(app)
    except Exception:
        logger.warning("Scheduler not initialized", exc_info=True)

    # --------------------------------------------------------
    # Health & Version Endpoints (ROOT LEVEL)
    # --------------------------------------------------------

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"}), 200

    @app.route("/__version")
    def version():
        return jsonify({
            "version": app.config.get("APP_VERSION", "unknown"),
            "git_sha": os.getenv("GITHUB_SHA", "unknown"),
            "environment": config_name,
        }), 200

    # --------------------------------------------------------
    # Startup Log
    # --------------------------------------------------------

    logger.info("LUX Marketing Platform initialized (%s)", config_name)

    return app
