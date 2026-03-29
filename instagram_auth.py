"""
Instagram OAuth 2.0 Integration for LUX Marketing Platform
Uses Facebook Graph API for Instagram Business/Creator accounts
"""

import os
import secrets
import logging
from datetime import datetime, timedelta
from flask import Blueprint, redirect, url_for, request, jsonify, flash, session
from flask_login import login_required, current_user
from models import db, InstagramOAuth, CompanySecret
from services.secret_vault import SecretVault

logger = logging.getLogger(__name__)

instagram_auth_bp = Blueprint('instagram_auth', __name__, url_prefix='/auth/instagram')

class InstagramService:
    """Instagram OAuth and Graph API service"""
    
    AUTHORIZATION_URL = "https://www.facebook.com/v18.0/dialog/oauth"
    TOKEN_URL = "https://graph.facebook.com/v18.0/oauth/access_token"
    GRAPH_API_URL = "https://graph.facebook.com/v18.0"
    
    SCOPES = [
        'instagram_basic',
        'instagram_content_publish',
        'instagram_manage_comments',
        'instagram_manage_insights',
        'pages_show_list',
        'pages_read_engagement',
        'business_management'
    ]
    
    @staticmethod
    def get_credentials(company_id):
        """Get Instagram/Facebook App credentials from company secrets"""
        app_id = CompanySecret.query.filter_by(
            company_id=company_id, 
            key='instagram_app_id'
        ).first()
        app_secret = CompanySecret.query.filter_by(
            company_id=company_id, 
            key='instagram_app_secret'
        ).first()
        
        if not app_id:
            app_id = CompanySecret.query.filter_by(
                company_id=company_id, 
                key='facebook_app_id'
            ).first()
        if not app_secret:
            app_secret = CompanySecret.query.filter_by(
                company_id=company_id, 
                key='facebook_app_secret'
            ).first()
        
        return (
            app_id.value if app_id else None,
            app_secret.value if app_secret else None
        )
    
    @staticmethod
    def get_redirect_uri():
        """Get OAuth redirect URI"""
        import os
        base = os.environ.get("APP_BASE_URL", "https://luxit.app").rstrip("/")
        return f"{base}/auth/instagram/callback"
    
    @staticmethod
    def get_authorization_url(company_id):
        """Generate Instagram OAuth authorization URL"""
        app_id, app_secret = InstagramService.get_credentials(company_id)
        
        if not app_id or not app_secret:
            return None, "Instagram/Facebook App credentials not configured"
        
        state = secrets.token_urlsafe(32)
        session['instagram_oauth_state'] = state
        session['instagram_oauth_company_id'] = company_id
        
        params = {
            'client_id': app_id,
            'redirect_uri': InstagramService.get_redirect_uri(),
            'state': state,
            'scope': ','.join(InstagramService.SCOPES),
            'response_type': 'code'
        }
        
        query_string = '&'.join(f"{k}={v}" for k, v in params.items())
        auth_url = f"{InstagramService.AUTHORIZATION_URL}?{query_string}"
        
        return auth_url, None
    
    @staticmethod
    def exchange_code_for_token(code, company_id):
        """Exchange authorization code for access token"""
        import requests
        
        app_id, app_secret = InstagramService.get_credentials(company_id)
        
        if not app_id or not app_secret:
            return None, "Instagram App credentials not configured"
        
        try:
            response = requests.get(
                InstagramService.TOKEN_URL,
                params={
                    'client_id': app_id,
                    'client_secret': app_secret,
                    'redirect_uri': InstagramService.get_redirect_uri(),
                    'code': code
                },
                timeout=30
            )
            
            if response.status_code != 200:
                error_data = response.json()
                error_msg = error_data.get('error', {}).get('message', 'Unknown error')
                return None, f"Token exchange failed: {error_msg}"
            
            data = response.json()
            return data, None
            
        except Exception as e:
            logger.error(f"Instagram token exchange error: {e}")
            return None, str(e)
    
    @staticmethod
    def get_long_lived_token(short_token, company_id):
        """Exchange short-lived token for long-lived token (60 days)"""
        import requests
        
        app_id, app_secret = InstagramService.get_credentials(company_id)
        
        try:
            response = requests.get(
                InstagramService.TOKEN_URL,
                params={
                    'grant_type': 'fb_exchange_token',
                    'client_id': app_id,
                    'client_secret': app_secret,
                    'fb_exchange_token': short_token
                },
                timeout=30
            )
            
            if response.status_code != 200:
                return None, "Failed to get long-lived token"
            
            return response.json(), None
            
        except Exception as e:
            logger.error(f"Instagram long-lived token error: {e}")
            return None, str(e)
    
    @staticmethod
    def get_instagram_accounts(access_token):
        """Get Instagram Business accounts linked to Facebook pages"""
        import requests
        
        try:
            response = requests.get(
                f"{InstagramService.GRAPH_API_URL}/me/accounts",
                params={
                    'access_token': access_token,
                    'fields': 'id,name,instagram_business_account{id,username,profile_picture_url,followers_count}'
                },
                timeout=30
            )
            
            if response.status_code != 200:
                return None, "Failed to get Instagram accounts"
            
            pages = response.json().get('data', [])
            instagram_accounts = []
            
            for page in pages:
                if 'instagram_business_account' in page:
                    ig_account = page['instagram_business_account']
                    ig_account['page_id'] = page['id']
                    ig_account['page_name'] = page['name']
                    instagram_accounts.append(ig_account)
            
            return instagram_accounts, None
            
        except Exception as e:
            logger.error(f"Instagram accounts error: {e}")
            return None, str(e)
    
    @staticmethod
    def get_user_media(instagram_account_id, access_token, limit=20):
        """Get user's Instagram media"""
        import requests
        
        try:
            response = requests.get(
                f"{InstagramService.GRAPH_API_URL}/{instagram_account_id}/media",
                params={
                    'access_token': access_token,
                    'fields': 'id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count',
                    'limit': limit
                },
                timeout=30
            )
            
            if response.status_code != 200:
                return None, "Failed to get media"
            
            return response.json().get('data', []), None
            
        except Exception as e:
            logger.error(f"Instagram media error: {e}")
            return None, str(e)
    
    @staticmethod
    def publish_photo(instagram_account_id, access_token, image_url, caption=None):
        """Publish a photo to Instagram"""
        import requests
        
        try:
            container_response = requests.post(
                f"{InstagramService.GRAPH_API_URL}/{instagram_account_id}/media",
                data={
                    'image_url': image_url,
                    'caption': caption or '',
                    'access_token': access_token
                },
                timeout=60
            )
            
            if container_response.status_code != 200:
                error_data = container_response.json()
                error_msg = error_data.get('error', {}).get('message', 'Unknown error')
                return None, f"Container creation failed: {error_msg}"
            
            container_id = container_response.json().get('id')
            
            publish_response = requests.post(
                f"{InstagramService.GRAPH_API_URL}/{instagram_account_id}/media_publish",
                data={
                    'creation_id': container_id,
                    'access_token': access_token
                },
                timeout=60
            )
            
            if publish_response.status_code != 200:
                error_data = publish_response.json()
                error_msg = error_data.get('error', {}).get('message', 'Unknown error')
                return None, f"Publish failed: {error_msg}"
            
            return publish_response.json(), None
            
        except Exception as e:
            logger.error(f"Instagram publish error: {e}")
            return None, str(e)


@instagram_auth_bp.route('/connect')
@login_required
def connect():
    """Initiate Instagram OAuth flow"""
    company = current_user.get_default_company()
    if not company:
        flash('No company selected', 'error')
        return redirect(url_for('main.settings_integrations'))
    
    auth_url, error = InstagramService.get_authorization_url(company.id)
    
    if error:
        flash(f'Instagram API credentials not configured. Please add your Facebook App ID and App Secret in Settings → API Keys & Secrets (Instagram uses Facebook Login).', 'error')
        return redirect(url_for('main.company_settings', id=company.id))
    
    return redirect(auth_url)


@instagram_auth_bp.route('/callback')
def callback():
    """Handle Instagram OAuth callback"""
    error = request.args.get('error')
    error_description = request.args.get('error_description')
    
    if error:
        flash(f'Instagram authorization failed: {error_description or error}', 'error')
        return redirect(url_for('main.settings_integrations'))
    
    code = request.args.get('code')
    state = request.args.get('state')
    
    stored_state = session.pop('instagram_oauth_state', None)
    company_id = session.pop('instagram_oauth_company_id', None)
    
    if not state or state != stored_state:
        flash('Invalid OAuth state. Please try again.', 'error')
        return redirect(url_for('main.settings_integrations'))
    
    if not code:
        flash('No authorization code received', 'error')
        return redirect(url_for('main.settings_integrations'))
    
    if not company_id:
        flash('Session expired. Please try again.', 'error')
        return redirect(url_for('main.settings_integrations'))
    
    token_data, error = InstagramService.exchange_code_for_token(code, company_id)
    
    if error:
        flash(f'Token exchange failed: {error}', 'error')
        return redirect(url_for('main.settings_integrations'))
    
    short_token = token_data.get('access_token')
    
    long_token_data, error = InstagramService.get_long_lived_token(short_token, company_id)
    
    if error:
        access_token = short_token
        expires_in = token_data.get('expires_in', 3600)
    else:
        access_token = long_token_data.get('access_token', short_token)
        expires_in = long_token_data.get('expires_in', 5184000)
    
    ig_accounts, error = InstagramService.get_instagram_accounts(access_token)
    
    if error or not ig_accounts:
        flash('No Instagram Business accounts found. Make sure your Instagram account is connected to a Facebook Page.', 'warning')
        ig_account_id = None
        username = 'Unknown'
        profile_picture = None
    else:
        ig_account = ig_accounts[0]
        ig_account_id = ig_account.get('id')
        username = ig_account.get('username', 'Unknown')
        profile_picture = ig_account.get('profile_picture_url')
    
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    
    oauth_record = InstagramOAuth.query.filter_by(
        user_id=current_user.id,
        company_id=company_id
    ).first()
    
    if oauth_record:
        oauth_record.set_access_token(access_token)
        oauth_record.instagram_account_id = ig_account_id or oauth_record.instagram_account_id
        oauth_record.username = username
        oauth_record.avatar_url = profile_picture
        oauth_record.expires_at = expires_at
        oauth_record.updated_at = datetime.utcnow()
    else:
        oauth_record = InstagramOAuth(
            user_id=current_user.id,
            company_id=company_id,
            instagram_account_id=ig_account_id or 'pending',
            username=username,
            avatar_url=profile_picture,
            expires_at=expires_at
        )
        oauth_record.set_access_token(access_token)
        db.session.add(oauth_record)
    
    db.session.commit()
    
    flash('Instagram connected successfully!', 'success')
    return redirect(url_for('main.company_settings', id=company_id))


@instagram_auth_bp.route('/disconnect', methods=['POST'])
@login_required
def disconnect():
    """Disconnect Instagram account"""
    company = current_user.get_default_company()
    if not company:
        return jsonify({'success': False, 'error': 'No company selected'}), 400
    
    oauth_record = InstagramOAuth.query.filter_by(
        user_id=current_user.id,
        company_id=company.id
    ).first()
    
    if oauth_record:
        db.session.delete(oauth_record)
        db.session.commit()
    
    return jsonify({'success': True, 'message': 'Instagram disconnected'})


@instagram_auth_bp.route('/status')
@login_required
def status():
    """Check Instagram connection status"""
    company = current_user.get_default_company()
    if not company:
        return jsonify({'connected': False})
    
    oauth_record = InstagramOAuth.query.filter_by(
        user_id=current_user.id,
        company_id=company.id
    ).first()
    
    if not oauth_record:
        return jsonify({'connected': False})
    
    needs_refresh = oauth_record.expires_at and oauth_record.expires_at < datetime.utcnow()
    
    return jsonify({
        'connected': True,
        'instagram_account_id': oauth_record.instagram_account_id,
        'username': oauth_record.username,
        'avatar_url': oauth_record.avatar_url,
        'expires_at': oauth_record.expires_at.isoformat() if oauth_record.expires_at else None,
        'needs_refresh': needs_refresh
    })


@instagram_auth_bp.route('/media')
@login_required
def get_media():
    """Get user's Instagram media"""
    company = current_user.get_default_company()
    if not company:
        return jsonify({'success': False, 'error': 'No company selected'}), 400
    
    oauth_record = InstagramOAuth.query.filter_by(
        user_id=current_user.id,
        company_id=company.id
    ).first()
    
    if not oauth_record:
        return jsonify({'success': False, 'error': 'Instagram not connected'}), 401
    
    access_token = oauth_record.get_access_token()
    media, error = InstagramService.get_user_media(
        oauth_record.instagram_account_id, 
        access_token
    )
    
    if error:
        return jsonify({'success': False, 'error': error}), 500
    
    return jsonify({'success': True, 'media': media})


@instagram_auth_bp.route('/publish', methods=['POST'])
@login_required
def publish_photo():
    """Publish a photo to Instagram"""
    company = current_user.get_default_company()
    if not company:
        return jsonify({'success': False, 'error': 'No company selected'}), 400
    
    oauth_record = InstagramOAuth.query.filter_by(
        user_id=current_user.id,
        company_id=company.id
    ).first()
    
    if not oauth_record:
        return jsonify({'success': False, 'error': 'Instagram not connected'}), 401
    
    data = request.get_json()
    image_url = data.get('image_url')
    caption = data.get('caption')
    
    if not image_url:
        return jsonify({'success': False, 'error': 'image_url is required'}), 400
    
    access_token = oauth_record.get_access_token()
    result, error = InstagramService.publish_photo(
        oauth_record.instagram_account_id,
        access_token,
        image_url,
        caption
    )
    
    if error:
        return jsonify({'success': False, 'error': error}), 500
    
    return jsonify({'success': True, 'post_id': result.get('id')})
