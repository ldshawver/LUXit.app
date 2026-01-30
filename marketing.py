"""Marketing site routes."""
import logging

from flask import Blueprint, render_template
from jinja2 import TemplateNotFound

logger = logging.getLogger(__name__)

marketing_bp = Blueprint("marketing", __name__, template_folder="marketing/templates")


@marketing_bp.route("/")
def marketing_home():
    """Public marketing homepage."""
    try:
        return render_template("marketing/index.html")
    except TemplateNotFound as exc:
        logger.warning("Marketing index template missing: %s", exc)
        return render_template("marketing/base.html")


@marketing_bp.route("/features")
def marketing_features():
    try:
        return render_template("marketing/features.html")
    except TemplateNotFound as exc:
        logger.warning("Marketing features template missing: %s", exc)
        return render_template("marketing/base.html")


@marketing_bp.route("/pricing")
def marketing_pricing():
    try:
        return render_template("marketing/pricing.html")
    except TemplateNotFound as exc:
        logger.warning("Marketing pricing template missing: %s", exc)
        return render_template("marketing/base.html")


@marketing_bp.route("/about")
def marketing_about():
    try:
        return render_template("marketing/about.html")
    except TemplateNotFound as exc:
        logger.warning("Marketing about template missing: %s", exc)
        return render_template("marketing/base.html")


@marketing_bp.route("/contact")
def marketing_contact():
    try:
        return render_template("marketing/contact.html")
    except TemplateNotFound as exc:
        logger.warning("Marketing contact template missing: %s", exc)
        return render_template("marketing/base.html")
