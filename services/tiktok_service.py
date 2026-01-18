"""TikTok OAuth and API Service for LUX Marketing Platform"""
import os
import secrets
import requests
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

class TikTokService:
    """Service for TikTok OAuth flow and API interactions"""
    
    AUTH_URL = 'https://www.tiktok.com/v2/auth/authorize/'
    TOKEN_URL = 'https://open.tiktokapis.com/v2/oauth/token/'
    REVOKE_URL = 'https://open.tiktokapis.com/v2/oauth/revoke/'
    USER_INFO_URL = 'https://open.tiktokapis.com/v2/user/info/'
    VIDEO_LIST_URL = 'https://open.tiktokapis.com/v2/video/list/'
    VIDEO_UPLOAD_INIT_URL = 'https://open.tiktokapis.com/v2/post/publish/inbox/video/init/'
    VIDEO_PUBLISH_URL = 'https://open.tiktokapis.com/v2/post/publish/video/init/'
    
    REDIRECT_URI = 'https://lux.lucifercruz.com/auth/tiktok/callback'
    
    SCOPES = [
        'user.info.basic',
        'video.list'
    ]
    
    def __init__(self, client_key=None, client_secret=None):
        self.client_key = client_key or os.getenv('TIKTOK_CLIENT_KEY')
        self.client_secret = client_secret or os.getenv('TIKTOK_CLIENT_SECRET')

        if not self.client_key or not self.client_secret:
            logger.warning("TikTok credentials missing; TikTok integration will be disabled.")
    
    @classmethod
    def from_company(cls, company):
        """Create service instance from company secrets"""
        client_key = company.get_secret('TIKTOK_CLIENT_KEY') or company.get_secret('TIKTOK_API_KEY')
        client_secret = company.get_secret('TIKTOK_CLIENT_SECRET')
        return cls(client_key=client_key, client_secret=client_secret)

    def is_configured(self) -> bool:
        return bool(self.client_key and self.client_secret)
    
    def generate_state(self):
        """Generate a secure state token for CSRF protection"""
        return secrets.token_urlsafe(32)
    
    def build_auth_url(self, state=None, redirect_uri=None):
        """Build TikTok OAuth authorization URL"""
        if not self.client_key:
            logger.warning("TikTok client_key missing; cannot build auth URL.")
            return None, None
        
        state = state or self.generate_state()
        redirect_uri = redirect_uri or self.REDIRECT_URI
        
        params = {
            'client_key': self.client_key,
            'response_type': 'code',
            'scope': ','.join(self.SCOPES),
            'redirect_uri': redirect_uri,
            'state': state
        }
        
        return f"{self.AUTH_URL}?{urlencode(params)}", state
    
    def exchange_code_for_token(self, code, redirect_uri=None):
        """Exchange authorization code for access token"""
        if not self.client_key or not self.client_secret:
            logger.warning("TikTok credentials missing; cannot exchange code for token.")
            return {'success': False, 'error': 'TikTok integration not configured'}
        
        redirect_uri = redirect_uri or self.REDIRECT_URI
        
        data = {
            'client_key': self.client_key,
            'client_secret': self.client_secret,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri
        }
        
        try:
            response = requests.post(self.TOKEN_URL, data=data)
            response.raise_for_status()
            token_data = response.json()
            
            if 'error' in token_data:
                logger.error(f"TikTok token error: {token_data}")
                return {'success': False, 'error': token_data.get('error_description', token_data.get('error'))}
            
            expires_in = token_data.get('expires_in', 86400)
            refresh_expires_in = token_data.get('refresh_expires_in', 86400 * 365)
            
            return {
                'success': True,
                'access_token': token_data.get('access_token'),
                'refresh_token': token_data.get('refresh_token'),
                'open_id': token_data.get('open_id'),
                'scope': token_data.get('scope'),
                'token_type': token_data.get('token_type', 'Bearer'),
                'expires_at': datetime.utcnow() + timedelta(seconds=expires_in),
                'refresh_expires_at': datetime.utcnow() + timedelta(seconds=refresh_expires_in),
                'raw_token': token_data
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok token exchange error: {e}")
            return {'success': False, 'error': str(e)}
    
    def refresh_access_token(self, refresh_token):
        """Refresh an expired access token"""
        if not self.client_key or not self.client_secret:
            logger.warning("TikTok credentials missing; cannot refresh access token.")
            return {'success': False, 'error': 'TikTok integration not configured'}
        
        data = {
            'client_key': self.client_key,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token
        }
        
        try:
            response = requests.post(self.TOKEN_URL, data=data)
            response.raise_for_status()
            token_data = response.json()
            
            if 'error' in token_data:
                logger.error(f"TikTok refresh error: {token_data}")
                return {'success': False, 'error': token_data.get('error_description', token_data.get('error'))}
            
            expires_in = token_data.get('expires_in', 86400)
            refresh_expires_in = token_data.get('refresh_expires_in', 86400 * 365)
            
            return {
                'success': True,
                'access_token': token_data.get('access_token'),
                'refresh_token': token_data.get('refresh_token'),
                'open_id': token_data.get('open_id'),
                'scope': token_data.get('scope'),
                'token_type': token_data.get('token_type', 'Bearer'),
                'expires_at': datetime.utcnow() + timedelta(seconds=expires_in),
                'refresh_expires_at': datetime.utcnow() + timedelta(seconds=refresh_expires_in),
                'raw_token': token_data
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok token refresh error: {e}")
            return {'success': False, 'error': str(e)}
    
    def revoke_token(self, access_token, open_id):
        """Revoke an access token"""
        if not self.client_key or not self.client_secret:
            logger.warning("TikTok credentials missing; cannot revoke token.")
            return {'success': False, 'error': 'TikTok integration not configured'}
        
        data = {
            'client_key': self.client_key,
            'client_secret': self.client_secret,
            'token': access_token
        }
        
        try:
            response = requests.post(self.REVOKE_URL, data=data)
            response.raise_for_status()
            return {'success': True, 'message': 'Token revoked successfully'}
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok token revoke error: {e}")
            return {'success': False, 'error': str(e)}
    
    def get_user_info(self, access_token, open_id):
        """Get TikTok user profile information"""
        headers = {
            'Authorization': f'Bearer {access_token}'
        }
        
        params = {
            'fields': 'open_id,union_id,avatar_url,display_name'
        }
        
        try:
            response = requests.get(self.USER_INFO_URL, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            if 'error' in data:
                return {'success': False, 'error': data.get('error', {}).get('message', 'Unknown error')}
            
            user_data = data.get('data', {}).get('user', {})
            return {
                'success': True,
                'open_id': user_data.get('open_id'),
                'display_name': user_data.get('display_name'),
                'avatar_url': user_data.get('avatar_url')
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok user info error: {e}")
            return {'success': False, 'error': str(e)}
    
    def list_videos(self, access_token, open_id, cursor=None, max_count=20):
        """List user's TikTok videos"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        params = {
            'fields': 'id,title,video_description,duration,cover_image_url,create_time,share_url,view_count,like_count,comment_count,share_count'
        }
        
        body = {
            'max_count': min(max_count, 20)
        }
        if cursor:
            body['cursor'] = cursor
        
        try:
            response = requests.post(self.VIDEO_LIST_URL, headers=headers, params=params, json=body)
            response.raise_for_status()
            data = response.json()
            
            if 'error' in data and data['error'].get('code') != 'ok':
                return {'success': False, 'error': data.get('error', {}).get('message', 'Unknown error')}
            
            video_data = data.get('data', {})
            videos = video_data.get('videos', [])
            
            return {
                'success': True,
                'videos': videos,
                'cursor': video_data.get('cursor'),
                'has_more': video_data.get('has_more', False)
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok list videos error: {e}")
            return {'success': False, 'error': str(e)}
    
    def init_video_upload(self, access_token, video_size, chunk_size=None, total_chunk_count=None):
        """Initialize a video upload session (Direct Post)"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        body = {
            'post_info': {
                'title': '',
                'privacy_level': 'SELF_ONLY',
                'disable_duet': False,
                'disable_comment': False,
                'disable_stitch': False,
                'video_cover_timestamp_ms': 1000
            },
            'source_info': {
                'source': 'FILE_UPLOAD',
                'video_size': video_size
            }
        }
        
        if chunk_size:
            body['source_info']['chunk_size'] = chunk_size
            body['source_info']['total_chunk_count'] = total_chunk_count
        
        try:
            response = requests.post(self.VIDEO_PUBLISH_URL, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            
            if 'error' in data and data['error'].get('code') != 'ok':
                return {'success': False, 'error': data.get('error', {}).get('message', 'Unknown error')}
            
            return {
                'success': True,
                'publish_id': data.get('data', {}).get('publish_id'),
                'upload_url': data.get('data', {}).get('upload_url')
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok video upload init error: {e}")
            return {'success': False, 'error': str(e)}
    
    def upload_video_chunk(self, upload_url, video_data, content_range=None):
        """Upload a video chunk to TikTok"""
        headers = {
            'Content-Type': 'video/mp4'
        }
        
        if content_range:
            headers['Content-Range'] = content_range
        
        try:
            response = requests.put(upload_url, headers=headers, data=video_data)
            response.raise_for_status()
            return {'success': True, 'message': 'Chunk uploaded successfully'}
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok video chunk upload error: {e}")
            return {'success': False, 'error': str(e)}
    
    def publish_video(self, access_token, title, description, video_url=None, 
                      privacy_level='PUBLIC_TO_EVERYONE', 
                      disable_duet=False, disable_comment=False, disable_stitch=False):
        """Publish a video to TikTok (from URL)"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        body = {
            'post_info': {
                'title': title[:150] if title else '',
                'privacy_level': privacy_level,
                'disable_duet': disable_duet,
                'disable_comment': disable_comment,
                'disable_stitch': disable_stitch
            },
            'source_info': {
                'source': 'PULL_FROM_URL',
                'video_url': video_url
            }
        }
        
        try:
            response = requests.post(self.VIDEO_PUBLISH_URL, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            
            if 'error' in data and data['error'].get('code') != 'ok':
                return {'success': False, 'error': data.get('error', {}).get('message', 'Unknown error')}
            
            return {
                'success': True,
                'publish_id': data.get('data', {}).get('publish_id'),
                'message': 'Video submitted for publishing'
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok publish video error: {e}")
            return {'success': False, 'error': str(e)}
    
    def check_publish_status(self, access_token, publish_id):
        """Check the status of a video publish request"""
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        
        body = {
            'publish_id': publish_id
        }
        
        status_url = 'https://open.tiktokapis.com/v2/post/publish/status/fetch/'
        
        try:
            response = requests.post(status_url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            
            if 'error' in data and data['error'].get('code') != 'ok':
                return {'success': False, 'error': data.get('error', {}).get('message', 'Unknown error')}
            
            status_data = data.get('data', {})
            return {
                'success': True,
                'status': status_data.get('status'),
                'fail_reason': status_data.get('fail_reason'),
                'public_video_id': status_data.get('public_video_id')
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"TikTok publish status error: {e}")
            return {'success': False, 'error': str(e)}
