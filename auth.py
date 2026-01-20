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
