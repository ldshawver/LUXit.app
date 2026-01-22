from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from extensions import db
from models import User
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import or_
import os

auth_bp = Blueprint('auth', __name__)

# Password reset token serializer
def get_serializer():
    """Get URL safe serializer for password reset tokens"""
    secret_key = os.environ.get('SESSION_SECRET') or 'dev-secret-key'
    return URLSafeTimedSerializer(secret_key)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    # Check if Replit Auth is available
    replit_auth_enabled = False
    try:
        from replit_auth import is_replit_auth_enabled
        replit_auth_enabled = is_replit_auth_enabled()
    except ImportError:
        pass
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember') in ['on', 'true', '1', 'yes']
        
        if not username or not password:
            flash('Username and password are required', 'error')
            return render_template('login.html', replit_auth_enabled=replit_auth_enabled)
        
        normalized_email = username.lower() if "@" in username else None
        email_lookup = normalized_email if normalized_email else username

        preferred_match = User.email == email_lookup if normalized_email else User.username == username

        user = User.query.filter(
            or_(User.username == username, User.email == email_lookup)
        ).order_by(preferred_match.desc()).first()

        if user and user.password_hash and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('main.dashboard'))
        elif user and not user.password_hash:
            flash(
                "This account doesn't have a password set. Please sign in using the original login method or reset your password.",
                'error'
            )
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html', replit_auth_enabled=replit_auth_enabled)

@auth_bp.route('/logout')
@login_required
def logout():
    """User logout"""
    logout_user()
    flash('You have been logged out', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """Register a new admin (only allowed when no admin exists)"""
    # Check if any admin users exist
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
        from utils import validate_email
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
        user.is_admin = True  # First user is always admin
        
        db.session.add(user)
        db.session.commit()
        
        # Auto-login the new admin
        login_user(user)
        flash('Admin account created successfully! Welcome to LUX Email Marketing.', 'success')
        return redirect(url_for('main.dashboard'))
    
    return render_template('register.html', is_admin_registration=True)

@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Request password reset"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        
        if not email:
            flash('Email address is required', 'error')
            return render_template('forgot_password.html')
        
        user = User.query.filter_by(email=email).first()
        
        if user:
            # Generate password reset token
            serializer = get_serializer()
            token = serializer.dumps(user.email, salt='password-reset')
            reset_url = url_for('auth.reset_password', token=token, _external=True)
            
            # Try to send password reset email
            email_sent = False
            try:
                from email_service import EmailService
                email_service = EmailService()
                
                html_content = f"""
                <html>
                <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; text-align: center;">
                        <h1 style="color: white; margin: 0;">LUX Email Marketing</h1>
                        <p style="color: white; margin: 10px 0 0 0;">Password Reset Request</p>
                    </div>
                    <div style="padding: 30px; background: #f8f9fa;">
                        <h2 style="color: #333;">Reset Your Password</h2>
                        <p style="color: #666; line-height: 1.6;">
                            You requested a password reset for your LUX Email Marketing account. 
                            Click the button below to reset your password:
                        </p>
                        <div style="text-align: center; margin: 30px 0;">
                            <a href="{reset_url}" 
                               style="background: #667eea; color: white; padding: 15px 30px; 
                                      text-decoration: none; border-radius: 5px; display: inline-block;
                                      font-weight: bold;">Reset Password</a>
                        </div>
                        <p style="color: #666; font-size: 14px;">
                            This link will expire in 1 hour for security purposes.<br>
                            If you didn't request this reset, please ignore this email.
                        </p>
                        <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                        <p style="color: #999; font-size: 12px; text-align: center;">
                            LUX Email Marketing Platform
                        </p>
                    </div>
                </body>
                </html>
                """
                
                # Get the configured from email
                from_email = os.environ.get("MS_FROM_EMAIL", "noreply@luxemail.com")
                
                email_sent = email_service.send_email(
                    to_email=user.email,
                    subject="Password Reset - LUX Email Marketing",
                    html_content=html_content,
                    from_email=from_email
                )
                    
            except Exception as e:
                import logging
                logging.error(f"Password reset email error: {str(e)}")
                email_sent = False
            
            if email_sent:
                flash('Password reset instructions have been sent to your email', 'success')
                return redirect(url_for('auth.login'))
            else:
                # Email failed - show direct reset link as fallback
                return render_template('forgot_password.html', 
                                     reset_link=reset_url,
                                     email_failed=True,
                                     user_email=user.email)
                
        else:
            # Don't reveal if email exists or not for security
            flash('If an account with that email exists, password reset instructions have been sent', 'info')
            return redirect(url_for('auth.login'))
    
    return render_template('forgot_password.html')

@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Reset password with token"""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    try:
        serializer = get_serializer()
        email = serializer.loads(token, salt='password-reset', max_age=3600)  # 1 hour expiry
    except (BadSignature, SignatureExpired):
        flash('Invalid or expired password reset link', 'error')
        return redirect(url_for('auth.forgot_password'))
    
    user = User.query.filter_by(email=email).first()
    if not user:
        flash('Invalid password reset link', 'error')
        return redirect(url_for('auth.forgot_password'))
    
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not password or not confirm_password:
            flash('Both password fields are required', 'error')
            return render_template('reset_password.html', token=token)
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('reset_password.html', token=token)
        
        if len(password) < 8:
            flash('Password must be at least 8 characters long', 'error')
            return render_template('reset_password.html', token=token)
        
        # Update password
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        
        flash('Your password has been reset successfully. You can now log in.', 'success')
        return redirect(url_for('auth.login'))
    
    return render_template('reset_password.html', token=token)
