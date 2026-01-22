from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)
from flask_login import (
    login_user,
    logout_user,
    login_required,
)

# ============================================================
# Blueprint
# ============================================================

auth_bp = Blueprint(
    "auth",
    __name__,
    url_prefix="/auth",
)

# ============================================================
# Routes
# ============================================================

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("auth/login.html")

        # TODO: replace with real user lookup
        # user = User.query.filter_by(username=username).first()
        # if not user or not user.check_password(password):
        #     flash("Invalid credentials", "error")
        #     return render_template("auth/login.html")

        # login_user(user)
        return redirect(url_for("main.dashboard"))

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
