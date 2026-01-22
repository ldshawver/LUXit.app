"""LUX package."""
"""Flask application factory."""
import os
import logging
from flask import Flask, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from lux.config import config
from lux.extensions import db, login_manager, csrf, limiter


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def create_app(config_name=None):
    """Create and configure the Flask application."""
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "production")

    app = Flask(__name__, 
                template_folder='templates',
                static_folder='static')
    
    # Load configuration
    app.config.from_object(config[config_name])
    
    # Set up proxy fix for correct URL generation behind reverse proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Configure Flask-Login
    login_manager.login_view = 'auth.login'
    login_manager.login_message = None

    @app.before_request
    def block_next_param():
        if "next" in request.args:
            return redirect(url_for("auth.login", _external=False))

    @login_manager.user_loader
    def load_user(user_id):
        from lux.models.user import User
        return User.query.get(int(user_id))

    # Register blueprints - Import here to avoid circular imports
    from lux.blueprints.auth.routes import auth_bp
    from lux.blueprints.main.routes import main_bp
    from lux.blueprints.user.routes import user_bp
    
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(main_bp)
    app.register_blueprint(user_bp, url_prefix='/user')

    # Create database tables
    with app.app_context():
        import lux.models  # noqa: F401
        db.create_all()

    # Add Jinja2 filters
    @app.template_filter('campaign_status_color')
    def campaign_status_color_filter(status):
        color_mapping = {
            'draft': 'secondary',
            'scheduled': 'warning',
            'sending': 'info',
            'sent': 'success',
            'failed': 'danger',
            'paused': 'dark'
        }
        return color_mapping.get(status, 'secondary')

    # Initialize scheduler
    from scheduler import init_scheduler
    init_scheduler(app)

    # Health check endpoint
    @app.route('/healthz')
    def healthz():
        return {'status': 'ok'}, 200

    logger.info(f"LUX Marketing Platform initialized in {config_name} mode")
    
    return app
