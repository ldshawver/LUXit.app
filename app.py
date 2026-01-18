import os
import logging
import re
import importlib.util
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, redirect, url_for, request, g
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# Load environment
# ============================================================

load_dotenv("/etc/lux-marketing/lux.env")

# ============================================================
# Flask app
# ============================================================

app = Flask(__name__)

# 🔒 HARD CANONICAL DOMAIN LOCK
app.config.update(
    SERVER_NAME="luxit.app",
    APPLICATION_ROOT="/",
    PREFERRED_URL_SCHEME="https",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="None",
)

# REQUIRED secret
app.config["SECRET_KEY"] = (
    os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
)
if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET must be set")

# TRUST NGINX — REQUIRED
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)

# ============================================================
# Extensions
# ============================================================

from extensions import db, csrf

db.init_app(app)
csrf.init_app(app)

# ============================================================
# Login manager
# ============================================================

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"

@login_manager.user_loader
def load_user(user_id):
    from models import User
    return User.query.get(int(user_id))

# ============================================================
# Blueprints
# ============================================================

from routes import main_bp
from auth import auth_bp

app.register_blueprint(main_bp)
app.register_blueprint(auth_bp, url_prefix="/auth")

# ============================================================
# Request safety net (BLOCK IP HOSTS)
# ============================================================

@app.before_request
def enforce_canonical_host():
    if request.host != "luxit.app":
        return redirect(
            "https://luxit.app" + request.full_path,
            code=301,
        )
    g.request_id = request.headers.get("X-Request-ID", str(uuid4()))

# ============================================================
# Root
# ============================================================

@app.route("/")
def index():
    return redirect(url_for("auth.login"))

# ============================================================
# Startup
# ============================================================

with app.app_context():
    import models
    db.create_all()
