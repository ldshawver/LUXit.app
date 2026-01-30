"""Marketing site routes."""
import logging

from flask import Blueprint, render_template
from jinja2 import TemplateNotFound

logger = logging.getLogger(__name__)

marketing_bp = Blueprint("marketing", __name__, template_folder="templates")


@marketing_bp.route("/")
def marketing_home():
    """Public marketing homepage."""
    try:
        return render_template("marketing/index.html")
    except TemplateNotFound as exc:
        logger.warning("Marketing index template missing: %s", exc)
        return render_template("marketing/base_marketing.html")
