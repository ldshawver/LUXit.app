from flask import (
    Blueprint,
    render_template,
    request,
    flash,
    redirect,
    url_for,
    current_app,
)
from flask_login import (
    login_user,
    logout_user,
    login_required,
    current_user,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import or_
from itsdangerous import URLSafeTimedSerializer
import os

from extensions import db
from models import User

# ============================================================
# Blueprint
# ============================================================

auth_bp = Blueprint("auth", __name__)

# ============================================================
# Helpers
# ============================================================

def get_serializer():
    secret_key = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY")
    return URLSafeTimedSerializer(secret_key)

# ============================================================
# Login
# ============================================================

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    replit_auth_enabled = False
    try:
        from replit_auth import is_replit_auth_enabled
        replit_auth_enabled = is_replit_auth_enabled()
    except Exception:
        pass

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") in ("on", "true", "1", "yes")

        if not username or not password:
            flash("Username and password are required", "error")
            return render_template(
                "auth/login.html",
                replit_auth_enabled=replit_auth_enabled,
            )

        email_lookup = username.lower() if "@" in username else None

        try:
            user = (
                User.query.filter(
                    or_(
                        User.username == username,
                        User.email == email_lookup,
                    )
                )
                .order_by(User.email == email_lookup)
                .first()
            )
        except SQLAlchemyError:
            current_app.logger.exception("Login lookup failed")
            flash("Unable to sign in right now.", "error")
            return render_template(
                "auth/login.html",
                replit_auth_enabled=replit_auth_enabled,
            )

        if not user:
            flash("Invalid credentials", "error")
            return render_template(
                "auth/login.html",
                replit_auth_enabled=replit_auth_enabled,
            )

        if not user.password_hash:
            flash(
                "This account does not have a password set. "
                "Please use the original login method or reset your password.",
                "error",
            )
            return render_template(
                "auth/login.html",
                replit_auth_enabled=replit_auth_enabled,
            )

        if not user.check_password(password):
            flash("Invalid credentials", "error")
            return render_template(
                "auth/login.html",
                replit_auth_enabled=replit_auth_enabled,
            )

        # ✅ AUTH SUCCESS
        login_user(user, remember=remember)

        # 🔒 HARD LOCK — NO next, NO IP, NO HOST
        return redirect(url_for("main.dashboard"))

    return render_template(
        "auth/login.html",
        replit_auth_enabled=replit_auth_enabled,
    )

# ============================================================
# Logout
# ============================================================

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
