import os
from flask import (
    Blueprint,
    render_template,
    request,
    flash,
    redirect,
    url_for,
    session,
)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
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
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("auth/login.html")

        try:
            user = User.query.filter(User.email == email).first()
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
        return redirect(url_for("main.dashboard"))

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
