"""Application entry point."""
import os
from uuid import uuid4

from flask import (
    Flask,
    request,
    redirect,
    g,
    current_app,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from extensions import db
from auth import auth_bp

# ============================================================
# Constants
# ============================================================

CANONICAL_HOST = "luxit.app"
ALLOWED_HOSTS = {"luxit.app", "www.luxit.app"}

# ============================================================
# App Factory
# ============================================================

def create_app():
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # --------------------------------------------------------
    # Core config
    # --------------------------------------------------------

    app.config.update(
        SECRET_KEY=os.environ.get("SESSION_SECRET", os.urandom(32)),
        PREFERRED_URL_SCHEME="https",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="None",
    )

    # --------------------------------------------------------
    # Reverse proxy awareness (NGINX)
    # --------------------------------------------------------

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
    )

    # --------------------------------------------------------
    # Database
    # --------------------------------------------------------

    db.init_app(app)

    # --------------------------------------------------------
    # Request bootstrap
    # --------------------------------------------------------

    @app.before_request
    def _request_bootstrap():
        g.request_id = request.headers.get(
            "X-Request-ID",
            str(uuid4()),
        )

        host = (
            request.headers.get("X-Forwarded-Host")
            or request.host
            or ""
        ).split(":")[0].lower()

        if not app.testing and host and host not in ALLOWED_HOSTS:
            return redirect(
                f"https://{CANONICAL_HOST}{request.full_path.rstrip('?')}",
                code=301,
            )

    @app.after_request
    def _attach_request_id(resp):
        resp.headers["X-Request-ID"] = g.get("request_id", "-")
        return resp

    # --------------------------------------------------------
    # Blueprints (REGISTER LAST)
    # --------------------------------------------------------

    app.register_blueprint(auth_bp)

    return app
