import logging
from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from extensions import csrf
from jinja2 import TemplateNotFound
from sqlalchemy import or_, text as db_text
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


@auth_bp.after_request
def no_cache_auth(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@auth_bp.route("/login", methods=["GET", "POST"])
@csrf.exempt
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

        logger.info("LOGIN ATTEMPT: identifier=%r, has_password=%s", identifier, bool(password))

        if not identifier or not password:
            logger.warning("LOGIN FAIL: empty identifier or password")
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

        logger.info("LOGIN: user found=%s, has_hash=%s", user is not None, bool(getattr(user, 'password_hash', None)))

        if not user or not user.password_hash:
            logger.warning("LOGIN FAIL: user not found or no password hash for %r", identifier)
            flash("Invalid credentials.", "error")
            try:
                return render_template("auth/login.html")
            except TemplateNotFound as exc:
                logger.warning("Auth login template missing: %s", exc)
                return render_template("login.html")

        pw_ok = check_password_hash(user.password_hash, password)
        logger.info("LOGIN: password check for %r = %s (pw_len=%d, hash_len=%d)", 
                     identifier, pw_ok, len(password), len(user.password_hash or ''))
        if not pw_ok:
            flash("Invalid credentials.", "error")
            try:
                return render_template("auth/login.html")
            except TemplateNotFound as exc:
                logger.warning("Auth login template missing: %s", exc)
                return render_template("login.html")

        from flask import session as _session
        _session.permanent = True
        login_user(user, remember=True)
        logger.info("LOGIN: after login_user — session keys=%s, _user_id=%s, is_authenticated=%s, user.id=%s, is_secure=%s, scheme=%s",
                    list(_session.keys()), _session.get('_user_id'), current_user.is_authenticated, user.id,
                    request.is_secure, request.environ.get('wsgi.url_scheme'))

        try:
            from models import ActivityLog
            from app import db as _db
            entry = ActivityLog(user_id=user.id, action='Logged in', detail='Session started', icon='log-in')
            _db.session.add(entry)
            _db.session.commit()
        except Exception:
            pass

        # Ensure user is linked to a company (guards against post-DB-reset orphan accounts)
        try:
            from extensions import db as _db
            from models import Company, UserCompanyAccess, user_company as _uc
            from sqlalchemy import text as _sql
            linked = _db.session.execute(
                _sql("SELECT company_id FROM user_company WHERE user_id = :uid LIMIT 1"),
                {"uid": user.id},
            ).fetchone()
            if not linked:
                first_co = Company.query.filter_by(is_active=True).order_by(Company.id.asc()).first()
                if first_co:
                    try:
                        _db.session.execute(
                            _sql("INSERT OR IGNORE INTO user_company (user_id, company_id) VALUES (:uid, :cid)"),
                            {"uid": user.id, "cid": first_co.id},
                        )
                    except Exception:
                        try:
                            _db.session.execute(
                                _sql("INSERT INTO user_company (user_id, company_id) VALUES (:uid, :cid) ON CONFLICT DO NOTHING"),
                                {"uid": user.id, "cid": first_co.id},
                            )
                        except Exception:
                            pass
                    if not user.default_company_id:
                        user.default_company_id = first_co.id
                    if not UserCompanyAccess.query.filter_by(user_id=user.id, company_id=first_co.id).first():
                        _db.session.add(UserCompanyAccess(
                            user_id=user.id, company_id=first_co.id, role='admin', is_default=True
                        ))
                    _db.session.commit()
                    logger.info("LOGIN: auto-linked user %s to company %s (%s)", user.id, first_co.id, first_co.name)
        except Exception as _exc:
            logger.warning("LOGIN: company auto-link failed: %s", _exc)
            try:
                from extensions import db as _db2
                _db2.session.rollback()
            except Exception:
                pass

        try:
            from services.posthog_client import track_event, identify_user, group_company
            company = user.get_default_company() if hasattr(user, 'get_default_company') else None
            identify_user(user.id, {
                'email':      user.email,
                'name':       f"{user.first_name or ''} {user.last_name or ''}".strip(),
                'role':       'admin' if user.is_admin else 'member',
                'company':    company.name if company else None,
                'company_id': company.id   if company else None,
                'tenant_id':  company.id   if company else None,
                'plan':       getattr(company, 'billing_tier', None) if company else None,
            })
            track_event(user.id, 'user_login', {
                'method':     'password',
                'company_id': company.id   if company else None,
                'tenant_id':  company.id   if company else None,
            })
            if company:
                group_company(user.id, company.id, company.name,
                              getattr(company, 'billing_tier', None))
        except Exception:
            pass

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
            from models import PasswordResetToken
            import secrets, datetime
            try:
                user = User.query.filter(
                    or_(
                        User.email == identifier.lower(),
                        User.username == identifier,
                        User.username == identifier.lower(),
                    )
                ).first()
                if user:
                    token = secrets.token_urlsafe(32)
                    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
                    prt = PasswordResetToken(
                        user_id=user.id,
                        token=token,
                        expires_at=expires,
                    )
                    _db.session.add(prt)
                    _db.session.commit()
                    reset_url = url_for("auth.reset_password", token=token, _external=True)
                    sent = True
                else:
                    not_found = True
            except Exception as exc:
                _db.session.rollback()
                logger.error("Forgot password error: %s", exc)
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
    from models import PasswordResetToken

    error = None
    success = False

    prt = PasswordResetToken.query.filter_by(token=token).first()

    if not prt or prt.used or prt.expires_at < datetime.datetime.utcnow():
        return render_template("auth/reset_password.html", invalid=True, success=False, error=None)

    if request.method == "POST":
        from werkzeug.security import generate_password_hash
        new_pw  = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if len(new_pw) < 8:
            error = "Password must be at least 8 characters."
        elif new_pw != confirm:
            error = "Passwords do not match."
        else:
            try:
                user = User.query.get(prt.user_id)
                if user:
                    user.password_hash = generate_password_hash(new_pw)
                    prt.used = True
                    _db.session.commit()
                    success = True
                else:
                    error = "User not found."
            except Exception as exc:
                _db.session.rollback()
                logger.error("Reset password error: %s", exc)
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


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    from extensions import db as _db

    admin_exists = False
    try:
        row = _db.session.execute(
            db_text("SELECT 1 FROM \"user\" WHERE is_admin = TRUE LIMIT 1")
        ).fetchone()
        admin_exists = row is not None
    except SQLAlchemyError:
        _db.session.rollback()

    if admin_exists:
        flash("Admin registration is closed — an admin already exists.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        from werkzeug.security import generate_password_hash

        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        company_name = (request.form.get("company_name") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        if not all([username, email, password, confirm]):
            flash("All fields are required.", "error")
            return render_template("register.html", is_admin_registration=True)
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html", is_admin_registration=True)
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("register.html", is_admin_registration=True)

        try:
            existing = User.query.filter(
                or_(User.username == username, User.email == email)
            ).first()
            if existing:
                flash("Username or email already taken.", "error")
                return render_template("register.html", is_admin_registration=True)

            company_id = None
            if company_name:
                result = _db.session.execute(
                    db_text("INSERT INTO company (name, is_active) VALUES (:n, TRUE) RETURNING id"),
                    {"n": company_name},
                )
                company_id = result.fetchone()[0]

            user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                is_admin=True,
            )
            if company_id is not None:
                user.default_company_id = company_id
            _db.session.add(user)
            _db.session.commit()

            login_user(user, remember=True)
            try:
                from services.posthog_client import track_event, identify_user
                identify_user(user.id, {'email': user.email, 'role': 'admin'})
                track_event(user.id, 'user_registered', {
                    'method': 'password',
                    'company_name': company_name or None,
                })
            except Exception:
                pass
            flash("Admin account created successfully!", "success")
            return redirect(url_for("auth.login"))
        except SQLAlchemyError:
            _db.session.rollback()
            logger.exception("Registration failed")
            flash("Registration failed. Please try again.", "error")
            return render_template("register.html", is_admin_registration=True)

    return render_template("register.html", is_admin_registration=True)


@auth_bp.route("/logout")
@login_required
def logout():
    try:
        from flask_login import current_user as _cu
        if _cu.is_authenticated:
            from services.posthog_client import track_event
            track_event(_cu.id, 'user_logout', {})
    except Exception:
        pass
    logout_user()
    return redirect(url_for("auth.login", _external=False))


# --------------------------------------------------
# Session diagnostics (temp debug endpoint)
# --------------------------------------------------

@auth_bp.route("/debug-session")
@csrf.exempt
def debug_session():
    """Session diagnostics — restricted to local/dev environments only."""
    import os
    from flask import session, jsonify, abort
    from flask_login import current_user
    from models import User

    is_dev = os.environ.get("FLASK_ENV") == "development" or os.environ.get("REPLIT_DEPLOYMENT") or os.environ.get("REPL_ID")
    remote_addr = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    is_local = remote_addr in ("127.0.0.1", "::1", "localhost")

    if not is_dev and not is_local:
        abort(404)

    try:
        uid = session.get('_user_id')
        user_from_loader = None
        if uid:
            try:
                user_from_loader = User.query.get(int(uid))
            except Exception as e:
                user_from_loader = f"error: {e}"
        return jsonify({
            'session_keys': list(session.keys()),
            'user_id_in_session': uid,
            'user_found_by_loader': str(user_from_loader),
            'is_authenticated': current_user.is_authenticated,
            'is_secure': request.is_secure,
            'scheme': request.environ.get('wsgi.url_scheme'),
            'forwarded_proto': request.headers.get('X-Forwarded-Proto'),
            'cookie_names': [c for c in request.cookies],
            'secret_key_prefix': str(current_app.secret_key)[:8] + '...' if current_app.secret_key else None,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# --------------------------------------------------
# Template helper
# --------------------------------------------------

def _render_login():
    try:
        return render_template("auth/login.html")
    except TemplateNotFound as exc:
        logger.warning("Auth login template missing: %s", exc)
        return render_template("login.html")
