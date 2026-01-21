from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import or_
from werkzeug.security import check_password_hash

from models import User

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        username_or_email = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

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


@auth_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
