from flask import Blueprint, render_template
from flask_login import login_required

main_bp = Blueprint("main", __name__)  # 🔥 REMOVE url_prefix

@main_bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")
