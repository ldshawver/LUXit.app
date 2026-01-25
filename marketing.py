"""Marketing site routes."""
from flask import Blueprint, render_template

marketing_bp = Blueprint("marketing", __name__)


@marketing_bp.route("/")
def marketing_home():
    """Public marketing homepage."""
    return render_template("marketing/index.html")
