from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, make_response
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db
from models import User
from utils import validate_email

user_bp = Blueprint('user', __name__)

@user_bp.route('/profile')
@login_required
def profile():
    """User profile page"""
    return render_template('user_profile.html', user=current_user)

@user_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Change user password"""
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not all([current_password, new_password, confirm_password]):
            flash('All fields are required', 'error')
            return render_template('change_password.html')
        
        # Verify current password
        if not check_password_hash(current_user.password_hash, current_password):
            flash('Current password is incorrect', 'error')
            return render_template('change_password.html')
        
        # Validate new password
        if len(new_password) < 6:
            flash('New password must be at least 6 characters long', 'error')
            return render_template('change_password.html')
        
        if new_password != confirm_password:
            flash('New passwords do not match', 'error')
            return render_template('change_password.html')
        
        # Update password
        current_user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        
        flash('Password updated successfully', 'success')
        return redirect(url_for('user.profile'))
    
    return render_template('change_password.html')

@user_bp.route('/manage-users-legacy')
@login_required
def manage_users():
    """Superseded by main.manage_users (the tenant-scoped Team page with
    availability / Receive Calls / archive-based removal). The endpoint name is
    kept so existing url_for('user.manage_users') links keep working; it now
    lives at a distinct path (no collision with main.manage_users) and always
    forwards to the canonical page."""
    return redirect(url_for('main.manage_users'))

@user_bp.route('/add-user', methods=['GET', 'POST'])
@login_required
def add_user():
    """DEPRECATED unguarded duplicate. It let any signed-in user create a platform
    admin (is_admin from the form). Forward to the canonical tenant-scoped route,
    which checks manage-users permission and seat limits. 307 preserves the POST."""
    return redirect(url_for('main.add_user'), code=307)

@user_bp.route('/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    """DEPRECATED destructive route. Tenant-admin user removal is archive-based
    and tenant-scoped -- it must never physically DELETE a canonical User (that
    would orphan historical message/call/actor references and cross tenant
    boundaries). Forward to the canonical archive route so there is exactly one
    removal path. 307 preserves the POST + body."""
    return redirect(url_for('main.delete_user', user_id=user_id), code=307)

@user_bp.route('/edit-user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    """DEPRECATED unguarded duplicate. It let any signed-in user edit any user in any
    tenant (password, is_admin). Forward to the canonical permission-checked route.
    307 preserves the POST + body."""
    return redirect(url_for('main.edit_user', user_id=user_id), code=307)
