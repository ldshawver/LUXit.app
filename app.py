import logging
import os
from uuid import uuid4
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, g, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db, csrf

load_dotenv("/etc/lux-marketing/lux.env")


class SafeFormatter(logging.Formatter):
    def format(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return super().format(record)


LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

handler = logging.StreamHandler()
handler.setFormatter(SafeFormatter(LOG_FORMAT))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(handler)


def _is_safe_next(value: str) -> bool:
    if not value:
        return False
    if value.startswith("/"):
        return True
    try:
        parsed = urlparse(value)
        return not (parsed.scheme or parsed.netloc)
    except Exception:
        return False


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "dev-secret")
    db_uri = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ.get("DATABASE_URL")
    if db_uri:
        app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

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

    from auth import auth_bp
    from routes import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    @app.before_request
    def bootstrap_request():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid4())

        allowed_hosts = {"luxit.app", "www.luxit.app"}
        if app.testing:
            allowed_hosts.update({"localhost", "127.0.0.1"})

        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
        if host and host not in allowed_hosts:
            return redirect(f"https://luxit.app{request.full_path.rstrip('?')}", code=301)

        nxt = request.args.get("next", "")
        if nxt and not _is_safe_next(nxt):
            return redirect(url_for("auth.login"))

    @app.after_request
    def attach_request_id(response):
        if not hasattr(g, "request_id"):
            g.request_id = "-"
        response.headers["X-Request-ID"] = g.request_id
        return response

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("main.dashboard"))
        return redirect(url_for("auth.login"))

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("errors/500.html"), 500

    if app.config.get("SQLALCHEMY_DATABASE_URI"):
        try:
            with app.app_context():
                db.create_all()
        except Exception:
            root_logger.exception("Database initialization failed")

    return app
