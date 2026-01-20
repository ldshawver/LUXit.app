import os
from urllib.parse import urlparse

from flask import (
    Blueprint,
    render_template,
    request,
    flash,
    redirect,
    url_for,
    session,
    current_app,
)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import User

# ============================================================
# Blueprint
# ============================================================

auth_bp = Blueprint("auth", __name__)

# ============================================================
# Login
# ============================================================

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # If already logged in, go straight to dashboard
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard", _external=False))

    if request.method == "POST":
        username_or_email = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

        if not username_or_email or not password:
            flash("Username or email and password are required.", "error")
            return render_template("auth/login.html")

        try:
            normalized_email = username_or_email.lower()
            user = User.query.filter(
                or_(
                    User.username == username_or_email,
                    User.email == normalized_email,
                )
            ).first()
        except SQLAlchemyError:
            flash("Login unavailable. Please try again later.", "error")
            return render_template("auth/login.html")

        if not user or not user.password_hash:
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html")

        if not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html")

        # ✅ LOGIN USER
        login_user(user)

        # 🔥 CRITICAL FIX:
        # Flask-Login stores a poisoned redirect in session["next"]
        # We MUST destroy it or it will redirect to the IP
        session.pop("next", None)

        # 🔒 HARD CANONICAL REDIRECT (NO IP, NO HOST LEAK)
        return redirect(url_for("main.dashboard", _external=False))

    return render_template("auth/login.html")

# ============================================================
# Logout
# ============================================================

@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
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


@auth_bp.before_app_request
def enforce_canonical_host_and_block_unsafe_next():
    allowed_hosts = {"luxit.app", "www.luxit.app"}
    if current_app.testing:
        allowed_hosts.update({"localhost", "127.0.0.1"})

    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
    if host and host not in allowed_hosts:
        return redirect(f"https://luxit.app{request.full_path.rstrip('?')}", code=301)

    nxt = request.args.get("next", "")
    if nxt and not _is_safe_next(nxt):
        return redirect(url_for("auth.login", _external=False))
