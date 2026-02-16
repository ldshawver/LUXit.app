"""User management blueprint routes."""
import logging

from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from lux.extensions import db
from lux.models.user import User
from lux.core.utils import validate_email

logger = logging.getLogger(__name__)

user_bp = Blueprint('user', __name__, template_folder='../../templates')


@user_bp.route('/profile')
@login_required
def profile():
    """User profile page."""
    user = current_user
    try:
        company = user.get_default_company()
    except Exception as exc:
        db.session.rollback()
        company = None
        logger.warning("User profile company lookup failed: %s", exc)
    try:
        all_companies = user.get_all_companies()
    except Exception as exc:
        db.session.rollback()
        all_companies = []
        logger.warning("User profile company list lookup failed: %s", exc)
    company_roles = {}
    for comp in all_companies:
        try:
            company_roles[comp.id] = user.get_company_role(comp.id)
        except Exception as exc:
            db.session.rollback()
            company_roles[comp.id] = "viewer"
            logger.warning("User profile role lookup failed for company %s: %s", comp.id, exc)
    return render_template(
        'user_profile.html',
        user=user,
        company=company,
        all_companies=all_companies,
        company_roles=company_roles,
    )


@user_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Change user password."""
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not all([current_password, new_password, confirm_password]):
            flash('All fields are required', 'error')
            return render_template('change_password.html')
        
        try:
            if not check_password_hash(current_user.password_hash, current_password):
                flash('Current password is incorrect', 'error')
                return render_template('change_password.html')
            
            if len(new_password) < 6:
                flash('New password must be at least 6 characters long', 'error')
                return render_template('change_password.html')
            
            if new_password != confirm_password:
                flash('New passwords do not match', 'error')
                return render_template('change_password.html')
            
            current_user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            
            flash('Password updated successfully', 'success')
            return redirect(url_for('user.profile'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating password: {str(e)}', 'error')
            return render_template('change_password.html')
    
    return render_template('change_password.html')


@user_bp.route('/manage-users')
@login_required
def manage_users():
    """Manage all users (admin function)."""
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('manage_users.html', users=users)
