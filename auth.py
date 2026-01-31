import logging
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from jinja2 import TemplateNotFound
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

from models import User

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth", template_folder="templates")


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


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard", _external=False))

    if request.method == "POST":
        identifier = (request.form.get("username") or request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

        if not identifier or not password:
            flash("Username/email and password are required.", "error")
            try:
                return render_template("auth/login.html")
            except TemplateNotFound as exc:
                logger.warning("Auth login template missing: %s", exc)
                return render_template("login.html")

        try:
            user = User.query.filter(
                or_(
                    User.username == identifier,
                    User.email == identifier.lower(),
                )
            ).first()
        except SQLAlchemyError:
            logger.exception("Login query failed")
            flash("Login temporarily unavailable.", "error")
            try:
                return render_template("auth/login.html")
            except TemplateNotFound as exc:
                logger.warning("Auth login template missing: %s", exc)
                return render_template("login.html")

        if not user or not user.password_hash:
            flash("Invalid credentials.", "error")
            try:
                return render_template("auth/login.html")
            except TemplateNotFound as exc:
                logger.warning("Auth login template missing: %s", exc)
                return render_template("login.html")

        if not check_password_hash(user.password_hash, password):
            flash("Invalid credentials.", "error")
            try:
                return render_template("auth/login.html")
            except TemplateNotFound as exc:
                logger.warning("Auth login template missing: %s", exc)
                return render_template("login.html")

        login_user(user)

        nxt = request.args.get("next")
        if nxt and _is_safe_next(nxt):
            return redirect(nxt)

        return redirect(url_for("main.dashboard", _external=False))

    try:
        return render_template("auth/login.html")
    except TemplateNotFound as exc:
        logger.warning("Auth login template missing: %s", exc)
        return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login", _external=False))
