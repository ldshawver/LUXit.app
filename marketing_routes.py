from flask import Blueprint, render_template, request, flash, redirect, url_for
from datetime import datetime
from extensions import db

marketing_bp = Blueprint('marketing', __name__, url_prefix='/m')


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
