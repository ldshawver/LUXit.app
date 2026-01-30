"""Marketing site routes."""
import logging

from flask import Blueprint, redirect, render_template, url_for

logger = logging.getLogger(__name__)

marketing_bp = Blueprint("marketing", __name__, template_folder="templates")


@marketing_bp.route("/")
def marketing_home():
    """Public marketing homepage."""
    return render_template("marketing/index.html")


@marketing_bp.route("/features")
def marketing_features():
    """Redirect to features section on homepage."""
    return redirect(url_for("marketing.marketing_home") + "#features")


@marketing_bp.route("/pricing")
def marketing_pricing():
    """Redirect to pricing section on homepage."""
    return redirect(url_for("marketing.marketing_home") + "#pricing")


@marketing_bp.route("/about")
def marketing_about():
    """Redirect to homepage (about info is on main page)."""
    return redirect(url_for("marketing.marketing_home"))


@marketing_bp.route("/contact")
def marketing_contact():
    """Redirect to email contact."""
    return redirect("mailto:sales@luxit.app")
