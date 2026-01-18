from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
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

@user_bp.route('/manage-users')
@login_required
def manage_users():
    """Manage all users (admin function)"""
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('manage_users.html', users=users)

@user_bp.route('/add-user', methods=['GET', 'POST'])
@login_required
def add_user():
    """Add a new user"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        is_admin = bool(request.form.get('is_admin'))
        
        if not all([username, email, password, confirm_password]):
            flash('All fields are required', 'error')
            return render_template('add_user.html')
        
        # Validate email
        if not validate_email(email):
            flash('Invalid email format', 'error')
            return render_template('add_user.html')
        
        # Validate password
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return render_template('add_user.html')
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('add_user.html')
        
        # Check if user already exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'error')
            return render_template('add_user.html')
        
        if User.query.filter_by(email=email).first():
            flash('Email already exists', 'error')
            return render_template('add_user.html')
        
        # Create new user
        user = User()
        user.username = username
        user.email = email
        user.password_hash = generate_password_hash(password)
        user.is_admin = is_admin
        
        db.session.add(user)
        db.session.commit()
        
        flash(f'User "{username}" created successfully', 'success')
        return redirect(url_for('user.manage_users'))
    
    return render_template('add_user.html')

@user_bp.route('/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    """Delete a user"""
    if user_id == current_user.id:
        flash('Cannot delete your own account', 'error')
        return redirect(url_for('user.manage_users'))
    
    user = User.query.get_or_404(user_id)
    username = user.username
    
    db.session.delete(user)
    db.session.commit()
    
    flash(f'User "{username}" deleted successfully', 'success')
    return redirect(url_for('user.manage_users'))

@user_bp.route('/edit-user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    """Edit user details"""
    user = User.query.get_or_404(user_id)
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        new_password = request.form.get('new_password', '')
        is_admin = bool(request.form.get('is_admin'))
        
        if not all([username, email]):
            flash('Username and email are required', 'error')
            return render_template('edit_user.html', user=user)
        
        # Validate email
        if not validate_email(email):
            flash('Invalid email format', 'error')
            return render_template('edit_user.html', user=user)
        
        # Check for duplicates (excluding current user)
        existing_username = User.query.filter(User.username == username, User.id != user_id).first()
        if existing_username:
            flash('Username already exists', 'error')
            return render_template('edit_user.html', user=user)
        
        existing_email = User.query.filter(User.email == email, User.id != user_id).first()
        if existing_email:
            flash('Email already exists', 'error')
            return render_template('edit_user.html', user=user)
        
        # Update user details
        user.username = username
        user.email = email
        user.is_admin = is_admin
        
        # Update password if provided
        if new_password:
            if len(new_password) < 6:
                flash('Password must be at least 6 characters long', 'error')
                return render_template('edit_user.html', user=user)
            user.password_hash = generate_password_hash(new_password)
        
        db.session.commit()
        
        flash(f'User "{username}" updated successfully', 'success')
        return redirect(url_for('user.manage_users'))
    
    return render_template('edit_user.html', user=user)