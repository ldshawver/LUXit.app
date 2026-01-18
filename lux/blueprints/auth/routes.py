"""Authentication blueprint routes."""
from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import os

from lux.extensions import db
from lux.models.user import User

auth_bp = Blueprint('auth', __name__, template_folder='../../templates')


def get_serializer():
    """Get URL safe serializer for password reset tokens."""
    secret_key = os.environ.get('SESSION_SECRET') or 'dev-secret-key'
    return URLSafeTimedSerializer(secret_key)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard', _external=False))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        
        if not username or not password:
            flash('Username and password are required', 'error')
            return render_template('login.html')
        
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            return redirect(url_for('main.dashboard', _external=False))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    """User logout."""
    logout_user()
    flash('You have been logged out', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """Register a new admin (only allowed when no admin exists)."""
    admin_exists = User.query.filter_by(is_admin=True).first() is not None
    
    if admin_exists:
        flash('Admin registration is not allowed - an admin already exists', 'error')
        return redirect(url_for('auth.login'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not all([username, email, password, confirm_password]):
            flash('All fields are required', 'error')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('register.html')
        
        if len(password) < 8:
            flash('Password must be at least 8 characters long', 'error')
            return render_template('register.html')
        
        # Validate email format
        from lux.core.utils import validate_email
        if not validate_email(email):
            flash('Please enter a valid email address', 'error')
            return render_template('register.html')
        
        # Check if user already exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'error')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('Email already exists', 'error')
            return render_template('register.html')
        
        # Create new admin user
        user = User()
        user.username = username
        user.email = email
        user.password_hash = generate_password_hash(password)
        user.is_admin = True
        
        db.session.add(user)
        db.session.commit()
        
        login_user(user)
        flash('Admin account created successfully! Welcome to LUX Marketing.', 'success')
        return redirect(url_for('main.dashboard', _external=False))
    
    return render_template('register.html', is_admin_registration=True)
