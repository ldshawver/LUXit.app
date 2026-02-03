"""Marketing site routes."""
import logging
from datetime import datetime

from flask import Blueprint, redirect, render_template, url_for, make_response, request

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


@marketing_bp.route("/robots.txt")
def robots_txt():
    """Serve robots.txt for SEO."""
    content = """User-agent: *
Allow: /
Disallow: /dashboard/
Disallow: /admin/
Disallow: /api/

Sitemap: {}/sitemap.xml
""".format(request.host_url.rstrip('/'))
    response = make_response(content)
    response.headers["Content-Type"] = "text/plain"
    return response


@marketing_bp.route("/sitemap.xml")
def sitemap_xml():
    """Generate XML sitemap for SEO."""
    base_url = request.host_url.rstrip('/')
    pages = [
        {"loc": "/", "priority": "1.0", "changefreq": "weekly"},
        {"loc": "/#features", "priority": "0.8", "changefreq": "monthly"},
        {"loc": "/#pricing", "priority": "0.8", "changefreq": "monthly"},
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
