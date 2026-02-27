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

auth_bp = Blueprint(
    "auth",
    __name__,
    url_prefix="/auth",
    template_folder="templates",
)

# --------------------------------------------------
# Helpers
# --------------------------------------------------

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

# --------------------------------------------------
# Routes
# --------------------------------------------------

def _hub_redirect(user):
    """Return a redirect to the user's preferred hub after login."""
    if getattr(user, 'default_hub', 'sales') == 'marketing':
        return redirect("/marketing-hub")
    return redirect("/dashboard")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _hub_redirect(current_user)

    if request.method == "POST":
        identifier = (
            request.form.get("username")
            or request.form.get("email")
            or ""
        ).strip()
        password = request.form.get("password") or ""

        if not identifier or not password:
            flash("Username/email and password are required.", "error")
            try:
                return render_template("auth/login.html")
            except TemplateNotFound as exc:
                logger.warning("Auth login template missing: %s", exc)
                return render_template("login.html")

        user = None
        for _attempt in range(3):
            try:
                from extensions import db as _db
                user = User.query.filter(
                    or_(
                        User.username == identifier,
                        User.email == identifier.lower(),
                    )
                ).first()
                break
            except SQLAlchemyError:
                logger.warning("Login query attempt %d failed, retrying...", _attempt + 1)
                try:
                    from extensions import db as _db
                    _db.session.rollback()
                    _db.session.remove()
                except Exception:
                    pass
        else:
            logger.exception("Login query failed after retries")
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

        login_user(user, remember=True)

        nxt = request.args.get("next")
        hub_roots = {"/dashboard", "/marketing-hub"}
        if nxt and _is_safe_next(nxt) and nxt.rstrip("/") not in {h.rstrip("/") for h in hub_roots}:
            return redirect(nxt)

        return _hub_redirect(user)

    try:
        return render_template("auth/login.html")
    except TemplateNotFound as exc:
        logger.warning("Auth login template missing: %s", exc)
        return render_template("login.html")


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    sent = False
    not_found = False
    reset_url = None

    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()
        if identifier:
            from extensions import db as _db
            import secrets, datetime
            try:
                user = User.query.filter(
                    or_(User.email == identifier.lower(), User.username == identifier)
                ).first()
                if user:
                    token = secrets.token_urlsafe(32)
                    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                    _db.session.execute(
                        "INSERT INTO password_reset_token (user_id, token, expires_at) "
                        "VALUES (:uid, :tok, :exp) ON CONFLICT (token) DO NOTHING",
                        {"uid": user.id, "tok": token, "exp": expires}
                    )
                    _db.session.commit()
                    reset_url = url_for("auth.reset_password", token=token, _external=True)
                    sent = True
                else:
                    not_found = True
            except SQLAlchemyError:
                _db.session.rollback()
                not_found = True

    return render_template(
        "auth/forgot_password.html",
        sent=sent,
        not_found=not_found,
        reset_url=reset_url,
        admin_email="luke@adiken.com",
    )


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    import datetime
    from extensions import db as _db

    error = None
    success = False

    try:
        row = _db.session.execute(
            "SELECT prt.id, prt.user_id, prt.expires_at, prt.used "
            "FROM password_reset_token prt WHERE prt.token = :tok",
            {"tok": token}
        ).fetchone()
    except SQLAlchemyError:
        _db.session.rollback()
        row = None

    if not row or row.used or row.expires_at < datetime.datetime.utcnow():
        return render_template("auth/reset_password.html", invalid=True, success=False, error=None)

    if request.method == "POST":
        from werkzeug.security import generate_password_hash
        new_pw = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if len(new_pw) < 8:
            error = "Password must be at least 8 characters."
        elif new_pw != confirm:
            error = "Passwords do not match."
        else:
            try:
                user = User.query.get(row.user_id)
                if user:
                    user.password_hash = generate_password_hash(new_pw)
                    _db.session.execute(
                        "UPDATE password_reset_token SET used = TRUE WHERE token = :tok",
                        {"tok": token}
                    )
                    _db.session.commit()
                    success = True
                else:
                    error = "User not found."
            except SQLAlchemyError:
                _db.session.rollback()
                error = "Something went wrong. Please try again."

    return render_template("auth/reset_password.html", invalid=False, success=success, error=error)


@auth_bp.route("/forgot-username", methods=["GET", "POST"])
def forgot_username():
    ADMIN_EMAIL = "luke@adiken.com"
    found_username = None
    not_found = False

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if email:
            from extensions import db as _db
            try:
                user = User.query.filter(User.email == email).first()
                if user:
                    found_username = user.username
                else:
                    not_found = True
            except SQLAlchemyError:
                _db.session.rollback()
                not_found = True

    return render_template(
        "auth/forgot_username.html",
        found_username=found_username,
        not_found=not_found,
        admin_email="luke@adiken.com",
    )


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login", _external=False))


# --------------------------------------------------
# Template helper
# --------------------------------------------------

def _render_login():
    try:
        return render_template("auth/login.html")
    except TemplateNotFound as exc:
        logger.warning("Auth login template missing: %s", exc)
        return render_template("login.html")
