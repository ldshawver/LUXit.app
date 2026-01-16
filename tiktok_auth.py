"""
TikTok OAuth Integration for LUX Marketing Platform
Provides OAuth authentication and API integration with TikTok.
"""

import os
import logging
from datetime import datetime

from flask import Blueprint, redirect, request, url_for, flash, jsonify, session
from flask_login import login_required, current_user

from extensions import db
from models import TikTokOAuth, Company
from services.tiktok_service import TikTokService

logger = logging.getLogger(__name__)

tiktok_bp = Blueprint('tiktok', __name__, url_prefix='/auth/tiktok')

def get_current_company():
    """Get current user's active company"""
    if current_user.is_authenticated:
        return current_user.get_default_company()
    return None

def get_tiktok_service(company=None):
    """Get TikTok service instance with company credentials"""
    if company:
        return TikTokService.from_company(company)
    return TikTokService()


def _ensure_tiktok_configured(service):
    if not service or not service.is_configured():
        logger.warning("TikTok integration disabled: missing credentials.")
        return False
    return True


@tiktok_bp.route('/connect')
@login_required
def connect():
    """Initiate TikTok OAuth flow"""
    company = get_current_company()
    if not company:
        flash('Please select a company first.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        service = get_tiktok_service(company)
        if not _ensure_tiktok_configured(service):
            flash('TikTok API credentials not configured. Please add your TikTok credentials in Settings → API Keys & Secrets.', 'error')
            return redirect(url_for('main.company_settings', company_id=company.id))

        auth_url, state = service.build_auth_url()
        if not auth_url:
            flash('TikTok integration not configured. Please add credentials in Settings → API Keys & Secrets.', 'error')
            return redirect(url_for('main.company_settings', company_id=company.id))
        
        session['tiktok_oauth_state'] = state
        session['tiktok_oauth_company_id'] = company.id
        
        logger.info(f"Initiating TikTok OAuth for user {current_user.id}, company {company.id}")
        return redirect(auth_url)
        
    except Exception as e:
        logger.error(f"TikTok connect error: {e}")
        flash(f'Failed to connect to TikTok: {str(e)}', 'error')
        return redirect(url_for('main.dashboard'))


@tiktok_bp.route('/callback')
@login_required
def callback():
    """Handle TikTok OAuth callback"""
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    error_description = request.args.get('error_description')
    
    if error:
        logger.error(f"TikTok OAuth error: {error} - {error_description}")
        flash(f'TikTok authorization failed: {error_description or error}', 'error')
        return redirect(url_for('main.dashboard'))
    
    stored_state = session.pop('tiktok_oauth_state', None)
    company_id = session.pop('tiktok_oauth_company_id', None)
    
    if not stored_state or state != stored_state:
        logger.error("TikTok OAuth state mismatch - possible CSRF attack")
        flash('Security validation failed. Please try again.', 'error')
        return redirect(url_for('main.dashboard'))
    
    if not code:
        flash('No authorization code received from TikTok.', 'error')
        return redirect(url_for('main.dashboard'))
    
    company = Company.query.get(company_id) if company_id else get_current_company()
    if not company:
        flash('Could not determine company for TikTok connection.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        service = get_tiktok_service(company)
        if not _ensure_tiktok_configured(service):
            flash('TikTok integration not configured. Please add credentials in Settings → API Keys & Secrets.', 'error')
            return redirect(url_for('main.company_settings', company_id=company.id))
        result = service.exchange_code_for_token(code)
        
        if not result.get('success'):
            flash(f'Failed to get TikTok tokens: {result.get("error")}', 'error')
            return redirect(url_for('main.dashboard'))
        
        user_info = service.get_user_info(result['access_token'], result['open_id'])
        
        existing = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            open_id=result['open_id']
        ).first()
        
        if existing:
            existing.set_access_token(result['access_token'])
            existing.set_refresh_token(result.get('refresh_token'))
            existing.expires_at = result.get('expires_at')
            existing.refresh_expires_at = result.get('refresh_expires_at')
            existing.scope = result.get('scope')
            existing.raw_token = result.get('raw_token')
            existing.status = 'active'
            existing.updated_at = datetime.utcnow()
            
            if user_info.get('success'):
                existing.display_name = user_info.get('display_name')
                existing.avatar_url = user_info.get('avatar_url')
            
            db.session.commit()
            flash('TikTok account reconnected successfully!', 'success')
        else:
            oauth_record = TikTokOAuth(
                user_id=current_user.id,
                company_id=company.id,
                open_id=result['open_id'],
                expires_at=result.get('expires_at'),
                refresh_expires_at=result.get('refresh_expires_at'),
                scope=result.get('scope'),
                token_type=result.get('token_type', 'Bearer'),
                raw_token=result.get('raw_token'),
                display_name=user_info.get('display_name') if user_info.get('success') else None,
                avatar_url=user_info.get('avatar_url') if user_info.get('success') else None,
                status='active'
            )
            oauth_record.set_access_token(result['access_token'])
            oauth_record.set_refresh_token(result.get('refresh_token'))
            db.session.add(oauth_record)
            db.session.commit()
            flash('TikTok account connected successfully!', 'success')
        
        logger.info(f"TikTok OAuth completed for user {current_user.id}")
        return redirect(url_for('main.company_settings', company_id=company.id))
        
    except Exception as e:
        logger.error(f"TikTok callback error: {e}")
        flash(f'Failed to complete TikTok connection: {str(e)}', 'error')
        return redirect(url_for('main.dashboard'))


@tiktok_bp.route('/disconnect', methods=['POST'])
@login_required
def disconnect():
    """Disconnect TikTok account"""
    company = get_current_company()
    
    try:
        oauth_record = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None
        ).first()
        
        if oauth_record:
            try:
                service = get_tiktok_service(company)
                if not _ensure_tiktok_configured(service):
                    return jsonify({'success': False, 'error': 'TikTok integration not configured'}), 503
                service.revoke_token(oauth_record.get_access_token(), oauth_record.open_id)
            except Exception as e:
                logger.warning(f"Failed to revoke TikTok token: {e}")
            
            db.session.delete(oauth_record)
            db.session.commit()
            
            return jsonify({'success': True, 'message': 'TikTok account disconnected'})
        else:
            return jsonify({'success': False, 'error': 'No TikTok account connected'})
            
    except Exception as e:
        logger.error(f"TikTok disconnect error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@tiktok_bp.route('/refresh', methods=['POST'])
@login_required
def refresh():
    """Refresh TikTok access token"""
    company = get_current_company()
    
    try:
        oauth_record = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None,
            status='active'
        ).first()
        
        if not oauth_record:
            return jsonify({'success': False, 'error': 'No TikTok account connected'})
        
        if not oauth_record.get_refresh_token():
            return jsonify({'success': False, 'error': 'No refresh token available'})
        
        service = get_tiktok_service(company)
        if not _ensure_tiktok_configured(service):
            return jsonify({'success': False, 'error': 'TikTok integration not configured'}), 503
        result = service.refresh_access_token(oauth_record.get_refresh_token())
        
        if not result.get('success'):
            oauth_record.status = 'expired'
            db.session.commit()
            return jsonify({'success': False, 'error': result.get('error')})
        
        oauth_record.set_access_token(result['access_token'])
        if result.get('refresh_token'):
            oauth_record.set_refresh_token(result['refresh_token'])
        oauth_record.expires_at = result.get('expires_at')
        oauth_record.refresh_expires_at = result.get('refresh_expires_at')
        oauth_record.raw_token = result.get('raw_token')
        oauth_record.last_refreshed_at = datetime.utcnow()
        oauth_record.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Token refreshed successfully',
            'expires_at': oauth_record.expires_at.isoformat() if oauth_record.expires_at else None
        })
        
    except Exception as e:
        logger.error(f"TikTok refresh error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@tiktok_bp.route('/status')
@login_required
def status():
    """Get TikTok connection status"""
    company = get_current_company()
    
    try:
        oauth_record = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None
        ).first()
        
        if not oauth_record:
            return jsonify({
                'connected': False,
                'message': 'No TikTok account connected'
            })
        
        return jsonify({
            'connected': True,
            'status': oauth_record.status,
            'display_name': oauth_record.display_name,
            'avatar_url': oauth_record.avatar_url,
            'open_id': oauth_record.open_id,
            'expires_at': oauth_record.expires_at.isoformat() if oauth_record.expires_at else None,
            'is_expired': oauth_record.is_expired,
            'needs_refresh': oauth_record.needs_refresh,
            'scope': oauth_record.scope
        })
        
    except Exception as e:
        logger.error(f"TikTok status error: {e}")
        return jsonify({'connected': False, 'error': str(e)})


tiktok_api_bp = Blueprint('tiktok_api', __name__, url_prefix='/api/tiktok')


@tiktok_api_bp.route('/videos')
@login_required
def list_videos():
    """List user's TikTok videos"""
    company = get_current_company()
    cursor = request.args.get('cursor')
    
    try:
        oauth_record = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None,
            status='active'
        ).first()
        
        if not oauth_record:
            return jsonify({'success': False, 'error': 'TikTok not connected'})
        
        if oauth_record.is_expired:
            return jsonify({'success': False, 'error': 'TikTok token expired. Please refresh or reconnect.'})
        
        service = get_tiktok_service(company)
        if not _ensure_tiktok_configured(service):
            return jsonify({'success': False, 'error': 'TikTok integration not configured'}), 503
        result = service.list_videos(oauth_record.get_access_token(), oauth_record.open_id, cursor=cursor)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"TikTok list videos error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@tiktok_api_bp.route('/publish', methods=['POST'])
@login_required
def publish_video():
    """Publish a video to TikTok (explicit user action required)"""
    company = get_current_company()
    data = request.get_json()
    
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'})
    
    video_url = data.get('video_url')
    title = data.get('title', '')
    description = data.get('description', '')
    privacy_level = data.get('privacy_level', 'PUBLIC_TO_EVERYONE')
    
    if not video_url:
        return jsonify({'success': False, 'error': 'Video URL is required'})
    
    try:
        oauth_record = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None,
            status='active'
        ).first()
        
        if not oauth_record:
            return jsonify({'success': False, 'error': 'TikTok not connected'})
        
        if oauth_record.is_expired:
            return jsonify({'success': False, 'error': 'TikTok token expired. Please refresh or reconnect.'})
        
        service = get_tiktok_service(company)
        if not _ensure_tiktok_configured(service):
            return jsonify({'success': False, 'error': 'TikTok integration not configured'}), 503
        result = service.publish_video(
            access_token=oauth_record.get_access_token(),
            title=title,
            description=description,
            video_url=video_url,
            privacy_level=privacy_level
        )
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"TikTok publish error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@tiktok_api_bp.route('/publish/status/<publish_id>')
@login_required
def check_publish_status(publish_id):
    """Check the status of a video publish request"""
    company = get_current_company()
    
    try:
        oauth_record = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None,
            status='active'
        ).first()
        
        if not oauth_record:
            return jsonify({'success': False, 'error': 'TikTok not connected'})
        
        service = get_tiktok_service(company)
        if not _ensure_tiktok_configured(service):
            return jsonify({'success': False, 'error': 'TikTok integration not configured'}), 503
        result = service.check_publish_status(oauth_record.get_access_token(), publish_id)
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"TikTok publish status error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@tiktok_api_bp.route('/user/info')
@login_required
def get_user_info():
    """Get connected TikTok user info"""
    company = get_current_company()
    
    try:
        oauth_record = TikTokOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None,
            status='active'
        ).first()
        
        if not oauth_record:
            return jsonify({'success': False, 'error': 'TikTok not connected'})
        
        if oauth_record.is_expired:
            return jsonify({'success': False, 'error': 'TikTok token expired'})
        
        service = get_tiktok_service(company)
        if not _ensure_tiktok_configured(service):
            return jsonify({'success': False, 'error': 'TikTok integration not configured'}), 503
        result = service.get_user_info(oauth_record.get_access_token(), oauth_record.open_id)
        
        if result.get('success'):
            oauth_record.display_name = result.get('display_name')
            oauth_record.avatar_url = result.get('avatar_url')
            db.session.commit()
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"TikTok user info error: {e}")
        return jsonify({'success': False, 'error': str(e)})
