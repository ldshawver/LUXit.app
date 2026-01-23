import hashlib
import logging
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from error_logger import log_application_error
from extensions import db
from models import User

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


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


def _hash_identifier(identifier: str) -> str:
    if not identifier:
        return "missing"
    return hashlib.sha256(identifier.strip().lower().encode("utf-8")).hexdigest()


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        identifier = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

        if not identifier or not password:
            flash("Username/email and password are required.", "error")
            return render_template("auth/login.html")

        normalized_identifier = identifier.lower()
        identifier_hash = _hash_identifier(identifier)
        try:
            user = User.query.filter(
                or_(
                    User.username == identifier,
                    User.email == normalized_identifier,
                )
            ).first()
        except SQLAlchemyError:
            logger.exception("Login query failed")
            log_application_error(
                error_type="AuthQueryError",
                error_message=f"Login query failed for identifier hash={identifier_hash}",
                endpoint="auth.login",
                method=request.method,
                user_id=None,
                severity="error",
            )
            flash("Login temporarily unavailable.", "error")
            return render_template("auth/login.html")

        if not user or not user.password_hash:
            log_application_error(
                error_type="AuthFailure",
                error_message=f"Invalid credentials for identifier hash={identifier_hash}",
                endpoint="auth.login",
                method=request.method,
                user_id=None,
                severity="warning",
            )
            flash("Invalid credentials.", "error")
            return render_template("auth/login.html")

        if not check_password_hash(user.password_hash, password):
            log_application_error(
                error_type="AuthFailure",
                error_message=f"Invalid password for identifier hash={identifier_hash}",
                endpoint="auth.login",
                method=request.method,
                user_id=user.id,
                severity="warning",
            )
            flash("Invalid credentials.", "error")
            return render_template("auth/login.html")

        login_user(user)

        nxt = request.args.get("next")
        if nxt and _is_safe_next(nxt):
            return redirect(nxt)

        return redirect(url_for("main.dashboard"))

    return render_template("auth/login.html")


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            flash("Email is required.", "error")
            return render_template("forgot_password.html")

        flash(
            "If an account exists for that email, reset instructions have been sent.",
            "success",
        )
        return render_template("forgot_password.html")

    return render_template("forgot_password.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    admin_exists = User.query.filter_by(is_admin=True).first() is not None
    if admin_exists:
        flash("Admin registration is not allowed once an admin exists.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not all([username, email, password, confirm_password]):
            flash("All fields are required.", "error")
            return render_template("register.html", is_admin_registration=True)

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html", is_admin_registration=True)

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "error")
            return render_template("register.html", is_admin_registration=True)

        if "@" not in email or "." not in email:
            flash("Please enter a valid email address.", "error")
            return render_template("register.html", is_admin_registration=True)

        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "error")
            return render_template("register.html", is_admin_registration=True)

        if User.query.filter_by(email=email).first():
            flash("Email already exists.", "error")
            return render_template("register.html", is_admin_registration=True)

        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            is_admin=True,
        )
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash("Admin account created successfully.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("register.html", is_admin_registration=True)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
