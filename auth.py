import logging
from uuid import uuid4
from urllib.parse import urlparse

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    g,
    current_app,
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import or_
from werkzeug.security import check_password_hash

from models import User
from dotenv import load_dotenv
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, redirect, url_for, request, g, has_request_context
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

# ============================================================
# Environment
# ============================================================
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

CANONICAL_HOST = "luxit.app"
ALLOWED_HOSTS = {"luxit.app", "www.luxit.app"}

    if request.method == "POST":
        username_or_email = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
# ============================================================
# Logging
# ============================================================

LOG_FORMAT = (
    "%(asctime)s %(levelname)s [%(name)s] "
    "[request_id=%(request_id)s] %(message)s"
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
root_logger = logging.getLogger()


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        return True


class RedactionFilter(logging.Filter):
    _nine_digit = re.compile(r"\b\d{9}\b")
    _keys = re.compile(r"\b(tin|ssn|ein)\b", re.IGNORECASE)

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._nine_digit.sub("***REDACTED***", record.msg)
            record.msg = self._keys.sub("[redacted]", record.msg)
        return True


root_logger.addFilter(RequestIdFilter())
root_logger.addFilter(RedactionFilter())

# ============================================================
# Flask Factory
# ============================================================

def create_app():
    app = Flask(__name__)

    # --------------------------------------------------------
    # Core config
    # --------------------------------------------------------

    app.config["SECRET_KEY"] = os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

    # Canonical HTTPS behavior
    app.config.update(
        SERVER_NAME=CANONICAL_HOST,
        PREFERRED_URL_SCHEME="https",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="None",
    )

    # --------------------------------------------------------
    # Proxy (nginx)
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

    from extensions import db, csrf

    db_url = os.getenv("DATABASE_URL", "sqlite:////opt/luxit/email_marketing.db")

    if db_url.startswith("mysql") and importlib.util.find_spec("MySQLdb") is None:
        if importlib.util.find_spec("pymysql"):
            db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    csrf.init_app(app)

    # --------------------------------------------------------
    # Flask-Login
    # --------------------------------------------------------

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.query.get(int(user_id))

    # --------------------------------------------------------
    # Request lifecycle
    # --------------------------------------------------------

    @app.before_request
    def enforce_canonical_host_and_block_unsafe_next():
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

        nxt = request.args.get("next", "")
        if nxt and not _is_safe_next(nxt):
            return redirect(url_for("auth.login", _external=False))

    @app.before_request
    def assign_request_id():
        g.request_id = request.headers.get("X-Request-ID", str(uuid4()))

    @app.after_request
    def attach_request_id(resp):
        resp.headers["X-Request-ID"] = g.request_id
        return resp

    # --------------------------------------------------------
    # Blueprints (imported LAST — critical)
    # --------------------------------------------------------

    from routes import main_bp
    from auth import auth_bp
    from user_management import user_bp
    from advanced_config import advanced_config_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(user_bp, url_prefix="/user")
    app.register_blueprint(advanced_config_bp)

    # --------------------------------------------------------
    # Index
    # --------------------------------------------------------

    @app.route("/")
    def index():
        from flask_login import current_user
        if current_user.is_authenticated:
            return redirect(url_for("main.dashboard", _external=False))
        return redirect(url_for("auth.login", _external=False))

    # --------------------------------------------------------
    # Startup
    # --------------------------------------------------------

    with app.app_context():
        import models
        db.create_all()

    return app


# ============================================================
# Helpers
# ============================================================

# Flask App (SINGLE instance)
# ============================================================

app = Flask(__name__)

# REQUIRED secret
app.config["SECRET_KEY"] = os.getenv("SESSION_SECRET") or os.getenv("SECRET_KEY")
if not app.config["SECRET_KEY"]:
    raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set")

# Trust nginx
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)
from flask_login import (
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import check_password_hash
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import User

logger = logging.getLogger(__name__)

# ============================================================
# Blueprint
# ============================================================

auth_bp = Blueprint("auth", __name__, template_folder="templates")

# ============================================================
# Helpers
# ============================================================

        if not username_or_email or not password:
            flash("Username or email and password are required.", "error")
            return render_template("auth/login.html")

        normalized_email = username_or_email.lower()
        user = User.query.filter(
            or_(
                User.username == username_or_email,
                User.email == normalized_email,
            )
        ).first()

        if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
            flash("Invalid username/email or password.", "error")
            return render_template("auth/login.html")

        login_user(user)
        return redirect(url_for("main.dashboard"))

    return render_template("auth/login.html")
@app.before_request
def enforce_canonical_host_and_block_unsafe_next():
    allowed_hosts = {"luxit.app", "www.luxit.app"}
    if app.testing:
        allowed_hosts.update({"localhost", "127.0.0.1"})

    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()

    if host and host not in allowed_hosts:
        return redirect(f"https://luxit.app{request.full_path.rstrip('?')}", code=301)

    nxt = request.args.get("next", "")
    if nxt and not _is_safe_next(nxt):
        return redirect(url_for("auth.login", _external=False))


@app.before_request
def assign_request_id():
    g.request_id = request.headers.get("X-Request-ID", str(uuid4()))


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

# ============================================================
# Routes
# ============================================================

@auth_bp.before_app_request
def ensure_request_id():
    g.request_id = getattr(g, "request_id", None) or str(uuid4())


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
@app.after_request
def attach_request_id(resp):
    resp.headers["X-Request-ID"] = g.request_id
    return resp


@auth_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
@app.route("/")
def index():
    from flask_login import current_user
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard", _external=False))

    if request.method == "POST":
        username_or_email = (
            request.form.get("username")
            or request.form.get("email")
            or ""
        ).strip()
        password = request.form.get("password") or ""

        if not username_or_email or not password:
            flash("Username/email and password are required.", "error")
            return render_template("auth/login.html")

        try:
            normalized = username_or_email.lower()
            user = User.query.filter(
                or_(
                    User.username == username_or_email,
                    User.email == normalized,
                )
            ).first()
        except SQLAlchemyError:
            logger.exception("Login query failed")
            flash("Login temporarily unavailable.", "error")
            return render_template("auth/login.html")

        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid credentials.", "error")
            return render_template("auth/login.html")

        login_user(user)
        nxt = request.args.get("next")
        if nxt and _is_safe_next(nxt):
            return redirect(nxt)

        return redirect(url_for("main.dashboard", _external=False))

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login", _external=False))
