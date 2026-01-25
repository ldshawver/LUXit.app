"""Flask application factory."""
import os
import logging
from datetime import datetime
from flask import Flask, redirect, request, url_for, jsonify, current_app
from werkzeug.middleware.proxy_fix import ProxyFix

from lux.config import config
from lux.extensions import db, login_manager, csrf, limiter
from sqlalchemy.exc import SQLAlchemyError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app(config_name=None):
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "production")

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
    )

    # --------------------------------------------------
    # Configuration
    # --------------------------------------------------
    app.config.from_object(config[config_name])

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

    # --------------------------------------------------
    # Extensions
    # --------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    # --------------------------------------------------
    # Auth helpers
    # --------------------------------------------------
    @login_manager.user_loader
    def load_user(user_id):
        from lux.models.user import User
        try:
            return User.query.get(int(user_id))
        except SQLAlchemyError as exc:
            current_app.logger.error("User loader DB error: %s", exc)
            try:
                db.session.rollback()
            except Exception:
                pass
            return None

    @app.before_request
    def block_next_param():
        if "next" in request.args:
            return redirect(url_for("auth.login"))

    @app.route("/login")
    def login_alias():
        return redirect(url_for("auth.login"))

    @app.context_processor
    def inject_company_context():
        from flask_login import current_user
        try:
            if not current_user.is_authenticated:
                return {}
            return {
                "current_company": current_user.get_default_company(),
                "user_companies": current_user.get_companies_safe(),
            }
        except SQLAlchemyError as exc:
            current_app.logger.error("Template context DB error: %s", exc)
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

    # --------------------------------------------------
    # Blueprints
    # --------------------------------------------------
    from lux.blueprints.auth.routes import auth_bp
    from lux.blueprints.main.routes import main_bp
    from lux.blueprints.user.routes import user_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(main_bp)
    app.register_blueprint(user_bp, url_prefix="/user")

    # --------------------------------------------------
    # Database
    # --------------------------------------------------
    with app.app_context():
        import lux.models  # noqa
        db.create_all()

    # --------------------------------------------------
    # Health Endpoints (CANONICAL)
    # --------------------------------------------------
    @app.route("/health", methods=["GET"])
    def health():
        try:
            db.session.execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False

        return jsonify({
            "status": "ok",
            "db_ok": db_ok,
            "version": current_app.config.get("APP_VERSION", "unknown"),
            "timestamp": datetime.utcnow().isoformat(),
        }), 200

    # Alias for load balancers / CI
    @app.route("/healthz", methods=["GET"])
    def healthz():
        return health()

    # Version endpoint
    @app.route("/__version", methods=["GET"])
    def version():
        return jsonify({
            "version": current_app.config.get("APP_VERSION", "unknown"),
            "git_sha": os.getenv("GITHUB_SHA", "unknown"),
        }), 200

    # --------------------------------------------------
    # Scheduler
    # --------------------------------------------------
    if not app.config.get("TESTING"):
        from scheduler import init_scheduler
        init_scheduler(app)

    logger.info("LUXit initialized (%s)", config_name)
    return app
