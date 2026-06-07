"""
Image Service for Social Media Posts
Handles royalty-free image search, URL import, device upload, and AI generation
"""

import os
import requests
import base64
import uuid
import logging
from typing import Dict, List, Optional, Tuple
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = 'static/uploads/social'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


class ImageService:
    """Unified image handling for social media posts"""
    
    @staticmethod
    def ensure_upload_folder():
        """Ensure upload folder exists"""
        if not os.path.exists(UPLOAD_FOLDER):
            os.makedirs(UPLOAD_FOLDER)
    
    @staticmethod
    def allowed_file(filename: str) -> bool:
        """Check if file extension is allowed"""
        return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
    
    @staticmethod
    def search_unsplash(query: str, page: int = 1, per_page: int = 12) -> Tuple[List[Dict], Optional[str]]:
        """
        Search Unsplash for royalty-free images
        Requires UNSPLASH_ACCESS_KEY in environment
        
        Unsplash API Guidelines Compliance:
        - Photos are hotlinked to original Unsplash URLs (not downloaded)
        - download_location is provided to trigger download events
        - Proper attribution data included (photographer name + Unsplash link)
        """
        try:
            access_key = os.environ.get('UNSPLASH_ACCESS_KEY')
            if not access_key:
                return [], "Unsplash API key not configured. Add UNSPLASH_ACCESS_KEY in Settings → Integrations."
            
            response = requests.get(
                'https://api.unsplash.com/search/photos',
                params={
                    'query': query,
                    'page': page,
                    'per_page': per_page,
                    'orientation': 'landscape'
                },
                headers={'Authorization': f'Client-ID {access_key}'},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                images = []
                for photo in data.get('results', []):
                    user = photo.get('user', {})
                    images.append({
                        'id': photo.get('id'),
                        'url_small': photo.get('urls', {}).get('small'),
                        'url_regular': photo.get('urls', {}).get('regular'),
                        'url_full': photo.get('urls', {}).get('full'),
                        'alt': photo.get('alt_description') or query,
                        'photographer': user.get('name'),
                        'photographer_username': user.get('username'),
                        'photographer_url': f"https://unsplash.com/@{user.get('username')}?utm_source=lux_marketing&utm_medium=referral",
                        'download_location': photo.get('links', {}).get('download_location'),
                        'unsplash_url': f"https://unsplash.com/photos/{photo.get('id')}?utm_source=lux_marketing&utm_medium=referral",
                        'attribution': f"Photo by {user.get('name')} on Unsplash",
                        'source': 'unsplash'
                    })
                return images, None
            else:
                return [], f"Unsplash API error: {response.status_code}"
                
        except Exception as e:
            logger.error(f"Unsplash search error: {e}")
            return [], str(e)
    
    @staticmethod
    def trigger_unsplash_download(download_location: str) -> bool:
        """
        Trigger Unsplash download event as required by API guidelines.
        Must be called when a user actually uses/selects a photo.
        """
        try:
            access_key = os.environ.get('UNSPLASH_ACCESS_KEY')
            if not access_key or not download_location:
                return False
            
            response = requests.get(
                download_location,
                headers={'Authorization': f'Client-ID {access_key}'},
                timeout=10
            )
            logger.info(f"Unsplash download triggered: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Unsplash download trigger error: {e}")
            return False
    
    @staticmethod
    def search_pexels(query: str, page: int = 1, per_page: int = 12) -> Tuple[List[Dict], Optional[str]]:
        """
        Search Pexels for royalty-free images
        Requires PEXELS_API_KEY in environment
        """
        try:
            try:
                from services.provider_config import get_provider_config
                api_key = get_provider_config('pexels', 'platform', 'api_key')
            except Exception:
                api_key = os.environ.get('PEXELS_API_KEY')
            if not api_key:
                return [], "Pexels API key not configured. Add PEXELS_API_KEY in Settings → Integrations."
            
            response = requests.get(
                'https://api.pexels.com/v1/search',
                params={
                    'query': query,
                    'page': page,
                    'per_page': per_page,
                    'orientation': 'landscape'
                },
                headers={'Authorization': api_key},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                images = []
                for photo in data.get('photos', []):
                    images.append({
                        'id': str(photo.get('id')),
                        'url_small': photo.get('src', {}).get('small'),
                        'url_regular': photo.get('src', {}).get('medium'),
                        'url_full': photo.get('src', {}).get('original'),
                        'alt': photo.get('alt') or query,
                        'photographer': photo.get('photographer'),
                        'photographer_url': photo.get('photographer_url'),
                        'source': 'pexels'
                    })
                return images, None
            else:
                return [], f"Pexels API error: {response.status_code}"
                
        except Exception as e:
            logger.error(f"Pexels search error: {e}")
            return [], str(e)
    
    @staticmethod
    def search_images(query: str, source: str = 'all', page: int = 1) -> Tuple[List[Dict], Optional[str]]:
        """Search images from specified source(s)"""
        all_images = []
        errors = []
        
        if source in ['all', 'unsplash']:
            images, error = ImageService.search_unsplash(query, page)
            if images:
                all_images.extend(images)
            if error:
                errors.append(f"Unsplash: {error}")
        
        if source in ['all', 'pexels']:
            images, error = ImageService.search_pexels(query, page)
            if images:
                all_images.extend(images)
            if error:
                errors.append(f"Pexels: {error}")
        
        error_msg = '; '.join(errors) if errors and not all_images else None
        return all_images, error_msg
    
    @staticmethod
    def import_from_url(image_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Import an image from a URL and save locally
        Returns (local_path, error)
        """
        try:
            ImageService.ensure_upload_folder()
            
            response = requests.get(image_url, timeout=30, stream=True)
            if response.status_code != 200:
                return None, f"Failed to fetch image: HTTP {response.status_code}"
            
            content_type = response.headers.get('Content-Type', '')
            if 'image' not in content_type:
                return None, "URL does not point to an image"
            
            ext = 'jpg'
            if 'png' in content_type:
                ext = 'png'
            elif 'gif' in content_type:
                ext = 'gif'
            elif 'webp' in content_type:
                ext = 'webp'
            
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return f"/static/uploads/social/{filename}", None
            
        except Exception as e:
            logger.error(f"URL import error: {e}")
            return None, str(e)
    
    @staticmethod
    def save_uploaded_file(file) -> Tuple[Optional[str], Optional[str]]:
        """
        Save an uploaded file
        Returns (local_path, error)
        """
        try:
            if not file or not file.filename:
                return None, "No file provided"
            
            if not ImageService.allowed_file(file.filename):
                return None, f"File type not allowed. Use: {', '.join(ALLOWED_EXTENSIONS)}"
            
            ImageService.ensure_upload_folder()
            
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            
            file.save(filepath)
            
            return f"/static/uploads/social/{filename}", None
            
        except Exception as e:
            logger.error(f"File upload error: {e}")
            return None, str(e)
    
    @staticmethod
    def generate_ai_image(prompt: str, size: str = "1024x1024") -> Tuple[Optional[str], Optional[str]]:
        """
        Generate an image using OpenAI DALL-E
        Returns (local_path, error)
        """
        try:
            try:
                from services.provider_config import get_provider_config
                api_key = get_provider_config('openai', 'platform', 'api_key')
            except Exception:
                api_key = os.environ.get('OPENAI_API_KEY')
            if not api_key:
                return None, "OpenAI API key not configured"
            
            response = requests.post(
                'https://api.openai.com/v1/images/generations',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'dall-e-3',
                    'prompt': prompt,
                    'n': 1,
                    'size': size,
                    'quality': 'standard'
                },
                timeout=60
            )
            
            if response.status_code == 200:
                data = response.json()
                image_url = data.get('data', [{}])[0].get('url')
                if image_url:
                    return ImageService.import_from_url(image_url)
                return None, "No image URL in response"
            else:
                error_data = response.json()
                return None, error_data.get('error', {}).get('message', f"API error: {response.status_code}")
                
        except Exception as e:
            logger.error(f"AI image generation error: {e}")
            return None, str(e)
