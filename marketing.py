"""Marketing site routes."""
import logging
import re
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, url_for, make_response, request
from flask_login import current_user

logger = logging.getLogger(__name__)

marketing_bp = Blueprint("marketing", __name__, template_folder="marketing/templates")


@marketing_bp.context_processor
def inject_now():
    return {'now': datetime.utcnow()}


@marketing_bp.route("/")
def marketing_home():
    if current_user.is_authenticated:
        hub = getattr(current_user, 'default_hub', 'sales')
        if hub == 'marketing':
            return redirect(url_for("main.marketing_hub"))
        return redirect(url_for("main.dashboard"))
    return render_template("marketing/index.html")


@marketing_bp.route("/features")
def marketing_features():
    return render_template("marketing/features.html", active_page="features")


@marketing_bp.route("/solutions")
def marketing_solutions():
    return render_template("marketing/solutions.html", active_page="solutions")


@marketing_bp.route("/security")
def marketing_security():
    return render_template("marketing/security.html", active_page="security")


@marketing_bp.route("/pricing")
def marketing_pricing():
    return render_template("marketing/pricing.html", active_page="pricing")


@marketing_bp.route("/about")
def marketing_about():
    return render_template("marketing/about.html", active_page="about")


@marketing_bp.route("/contact")
def marketing_contact():
    return render_template("marketing/contact.html", active_page="contact")


@marketing_bp.route("/book-demo", methods=["GET", "POST"])
def book_demo():
    if request.method == "POST":
        from extensions import db
        from models import DemoRequest

        first_name = request.form.get("first_name", "").strip()[:100]
        last_name = request.form.get("last_name", "").strip()[:100]
        email = request.form.get("email", "").strip()[:200]
        phone = request.form.get("phone", "").strip()[:50]
        company_name = request.form.get("company_name", "").strip()[:200]
        job_title = request.form.get("job_title", "").strip()[:200]
        team_size = request.form.get("team_size", "")[:50]
        message = request.form.get("message", "").strip()[:2000]
        preferred_contact = request.form.get("preferred_contact", "email")
        source_page = request.form.get("source_page", "direct")[:100]

        errors = []
        if not first_name:
            errors.append("First name is required.")
        if not last_name:
            errors.append("Last name is required.")
        if not email:
            errors.append("Email is required.")
        elif not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
            errors.append("Please enter a valid email address.")
        if preferred_contact not in ("email", "phone", "video"):
            preferred_contact = "email"

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("marketing/book_demo.html", active_page="demo")

        try:
            demo = DemoRequest(
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                company_name=company_name,
                job_title=job_title,
                team_size=team_size,
                message=message,
                preferred_contact=preferred_contact,
                source_page=source_page,
            )
            db.session.add(demo)
            db.session.commit()
            return render_template("marketing/demo_success.html", active_page="demo", demo=demo)
        except Exception as e:
            logger.error("Demo request save failed: %s", e)
            db.session.rollback()
            flash("Something went wrong. Please try again or contact us directly.", "error")
            return redirect(url_for("marketing.book_demo"))

    return render_template("marketing/book_demo.html", active_page="demo")


@marketing_bp.route("/robots.txt")
def robots_txt():
    content = """User-agent: *
Allow: /
Disallow: /dashboard/
Disallow: /admin/
Disallow: /api/

Sitemap: {}/sitemap.xml
""".format(request.host_url.rstrip("/"))
    response = make_response(content)
    response.headers["Content-Type"] = "text/plain"
    return response


@marketing_bp.route("/sitemap.xml")
def sitemap_xml():
    base_url = request.host_url.rstrip("/")
    pages = [
        {"loc": "/", "priority": "1.0", "changefreq": "weekly"},
        {"loc": "/features", "priority": "0.8", "changefreq": "monthly"},
        {"loc": "/solutions", "priority": "0.8", "changefreq": "monthly"},
        {"loc": "/pricing", "priority": "0.8", "changefreq": "monthly"},
        {"loc": "/security", "priority": "0.7", "changefreq": "monthly"},
        {"loc": "/book-demo", "priority": "0.9", "changefreq": "weekly"},
        {"loc": "/auth/login", "priority": "0.6", "changefreq": "monthly"},
        {"loc": "/auth/register", "priority": "0.6", "changefreq": "monthly"},
    ]

    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml_content += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'

    for page in pages:
        xml_content += "  <url>\n"
        xml_content += f"    <loc>{base_url}{page['loc']}</loc>\n"
        xml_content += f"    <lastmod>{datetime.now().strftime('%Y-%m-%d')}</lastmod>\n"
        xml_content += f"    <changefreq>{page['changefreq']}</changefreq>\n"
        xml_content += f"    <priority>{page['priority']}</priority>\n"
        xml_content += "  </url>\n"

    xml_content += "</urlset>"

    response = make_response(xml_content)
    response.headers["Content-Type"] = "application/xml"
    return response
