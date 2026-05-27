from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from datetime import datetime, timedelta
from extensions import db
import re
import logging

logger = logging.getLogger(__name__)

marketing_bp = Blueprint('marketing', __name__, url_prefix='/m')

VALID_PLANS = {'starter', 'professional', 'enterprise'}


@marketing_bp.context_processor
def inject_now():
    return {'now': datetime.utcnow()}


@marketing_bp.route('/')
def home():
    return render_template('marketing/home.html', active_page='home')


@marketing_bp.route('/features')
def features():
    return render_template('marketing/features.html', active_page='features')


@marketing_bp.route('/solutions')
def solutions():
    return render_template('marketing/solutions.html', active_page='solutions')


@marketing_bp.route('/security')
def security():
    return render_template('marketing/security.html', active_page='security')


@marketing_bp.route('/book-demo', methods=['GET', 'POST'])
def book_demo():
    if request.method == 'POST':
        import re
        from models import DemoRequest

        first_name = request.form.get('first_name', '').strip()[:100]
        last_name = request.form.get('last_name', '').strip()[:100]
        email = request.form.get('email', '').strip()[:200]
        phone = request.form.get('phone', '').strip()[:50]
        company_name = request.form.get('company_name', '').strip()[:200]
        job_title = request.form.get('job_title', '').strip()[:200]
        team_size = request.form.get('team_size', '')[:50]
        message = request.form.get('message', '').strip()[:2000]
        preferred_contact = request.form.get('preferred_contact', 'email')
        source_page = request.form.get('source_page', 'direct')[:100]

        errors = []
        if not first_name:
            errors.append('First name is required.')
        if not last_name:
            errors.append('Last name is required.')
        if not email:
            errors.append('Email is required.')
        elif not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            errors.append('Please enter a valid email address.')
        if preferred_contact not in ('email', 'phone', 'video'):
            preferred_contact = 'email'

        if errors:
            for err in errors:
                flash(err, 'error')
            return render_template('marketing/book_demo.html', active_page='demo')

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
            return render_template('marketing/demo_success.html', active_page='demo', demo=demo)
        except Exception as e:
            db.session.rollback()
            flash('Something went wrong. Please try again or contact us directly.', 'error')
            return redirect(url_for('marketing.book_demo'))

    return render_template('marketing/book_demo.html', active_page='demo')


@marketing_bp.route('/pricing')
def pricing():
    return render_template('marketing/pricing.html', active_page='pricing')


@marketing_bp.route('/api/license-request', methods=['POST'])
def api_license_request():
    from models import LicenseRequest

    data = request.get_json(silent=True) or {}

    email = (data.get('email') or '').strip().lower()[:254]
    first_name = (data.get('first_name') or '').strip()[:100]
    last_name = (data.get('last_name') or '').strip()[:100]
    company_name = (data.get('company_name') or '').strip()[:200]
    plan = (data.get('plan') or '').strip().lower()[:50]
    message = (data.get('message') or '').strip()[:2000]
    source_page = (data.get('source_page') or '').strip()[:200]

    if not email or not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        return jsonify({'success': False, 'message': 'Please enter a valid email address.'}), 400

    if not first_name:
        return jsonify({'success': False, 'message': 'First name is required.'}), 400

    if plan not in VALID_PLANS:
        plan = 'starter'

    ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip_address and ',' in ip_address:
        ip_address = ip_address.split(',')[0].strip()
    ip_address = (ip_address or '')[:45]
    ua = (request.headers.get('User-Agent') or '')[:512]

    try:
        cutoff = datetime.utcnow() - timedelta(minutes=5)
        duplicate = LicenseRequest.query.filter(
            LicenseRequest.email == email,
            LicenseRequest.plan == plan,
            LicenseRequest.created_at >= cutoff
        ).first()

        if duplicate:
            return jsonify({
                'success': True,
                'message': 'Thank you! Your request has been received. Our team will be in touch shortly.'
            })

        lr = LicenseRequest(
            email=email,
            first_name=first_name,
            last_name=last_name,
            company_name=company_name,
            plan=plan,
            message=message,
            ip_address=ip_address,
            user_agent=ua,
            source_page=source_page,
        )
        db.session.add(lr)
        db.session.commit()

        _send_license_notification_email(lr)

        return jsonify({
            'success': True,
            'message': 'Thank you! Your request has been received. Our team will be in touch shortly.'
        })

    except Exception as e:
        db.session.rollback()
        logger.error("License request error: %s", e)
        return jsonify({
            'success': False,
            'message': 'Something went wrong. Please try again or contact us directly.'
        }), 500


def _send_license_notification_email(lr):
    """Best-effort SMTP notification to admin. Failures are logged, not raised."""
    import os
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.environ.get('SMTP_HOST')
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    notify_to = os.environ.get('LICENSE_NOTIFY_EMAIL', 'luke@adiken.com')

    if not all([smtp_host, smtp_user, smtp_pass]):
        logger.debug("SMTP not configured — skipping license notification email")
        return

    try:
        body = (
            f"New License Request\n"
            f"{'='*40}\n"
            f"Plan: {lr.plan}\n"
            f"Name: {lr.first_name} {lr.last_name}\n"
            f"Email: {lr.email}\n"
            f"Company: {lr.company_name or 'N/A'}\n"
            f"Message: {lr.message or 'N/A'}\n"
            f"Source: {lr.source_page or 'N/A'}\n"
            f"IP: {lr.ip_address or 'N/A'}\n"
            f"Time: {lr.created_at}\n"
        )
        msg = MIMEText(body)
        msg['Subject'] = f'[LUX IT] New License Request — {lr.plan.title()}'
        msg['From'] = smtp_user
        msg['To'] = notify_to

        port = int(os.environ.get('SMTP_PORT', 587))
        with smtplib.SMTP(smtp_host, port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        logger.info("License notification sent for %s", lr.email)
    except Exception as e:
        logger.warning("License notification email failed (best-effort): %s", e)
