"""
AI Keyword/Hashtag Service for Social Media Posts
Generates relevant keywords and hashtags using OpenAI
"""

import os
import json
import requests
import logging
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class KeywordService:
    """AI-powered keyword and hashtag generation for social media"""
    
    @staticmethod
    def generate_hashtags(content: str, platform: str = 'general', max_tags: int = 10) -> Tuple[List[str], Optional[str]]:
        """
        Generate relevant hashtags for social media content
        Returns (list of hashtags, error)
        """
        try:
            from services.provider_config import get_provider_config
            api_key = get_provider_config('openai', 'platform')
            if not api_key:
                return [], "OpenAI API key not configured"
            
            platform_context = {
                'instagram': 'Instagram (use popular, engaging hashtags, mix of broad and niche)',
                'tiktok': 'TikTok (trending hashtags, FYP-focused)',
                'twitter': 'Twitter/X (concise, trending topics)',
                'linkedin': 'LinkedIn (professional, industry-specific)',
                'facebook': 'Facebook (engagement-focused, community tags)',
                'youtube': 'YouTube (SEO-optimized tags)',
                'general': 'social media in general'
            }
            
            prompt = f"""Generate {max_tags} highly relevant and engaging hashtags for the following social media post.
Platform: {platform_context.get(platform, platform_context['general'])}

Post content:
{content}

Requirements:
- Return ONLY hashtags, one per line
- Include # symbol
- Mix of popular and niche tags
- Make them relevant to the content
- No explanations, just the hashtags"""

            response = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o',
                    'messages': [
                        {'role': 'system', 'content': 'You are a social media marketing expert specializing in hashtag optimization.'},
                        {'role': 'user', 'content': prompt}
                    ],
                    'max_tokens': 200,
                    'temperature': 0.7
                },
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                
                hashtags = []
                for line in text.strip().split('\n'):
                    line = line.strip()
                    if line.startswith('#'):
                        tag = line.split()[0]
                        if len(tag) > 1:
                            hashtags.append(tag)
                
                return hashtags[:max_tags], None
            else:
                error_data = response.json()
                return [], error_data.get('error', {}).get('message', f"API error: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Hashtag generation error: {e}")
            return [], str(e)
    
    @staticmethod
    def generate_keywords(content: str, for_seo: bool = False) -> Tuple[List[str], Optional[str]]:
        """
        Generate relevant keywords from content
        Returns (list of keywords, error)
        """
        try:
            from services.provider_config import get_provider_config
            api_key = get_provider_config('openai', 'platform')
            if not api_key:
                return [], "OpenAI API key not configured"
            
            context = "SEO optimization" if for_seo else "social media engagement"
            
            prompt = f"""Extract and generate the most relevant keywords for {context} from this content:

{content}

Requirements:
- Return 10-15 keywords/phrases
- One keyword per line
- Include both single words and short phrases
- Focus on searchable, relevant terms
- No hashtags, just plain keywords"""

            response = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o',
                    'messages': [
                        {'role': 'system', 'content': 'You are an SEO and keyword research expert.'},
                        {'role': 'user', 'content': prompt}
                    ],
                    'max_tokens': 200,
                    'temperature': 0.5
                },
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                
                keywords = []
                for line in text.strip().split('\n'):
                    line = line.strip().strip('-').strip('•').strip('*').strip()
                    if line and len(line) > 1 and len(line) < 50:
                        keywords.append(line)
                
                return keywords[:15], None
            else:
                error_data = response.json()
                return [], error_data.get('error', {}).get('message', f"API error: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Keyword generation error: {e}")
            return [], str(e)
    
    @staticmethod
    def suggest_content_improvements(content: str, platform: str = 'general') -> Tuple[Dict, Optional[str]]:
        """
        Analyze content and suggest improvements
        Returns (suggestions dict, error)
        """
        try:
            from services.provider_config import get_provider_config
            api_key = get_provider_config('openai', 'platform')
            if not api_key:
                return {}, "OpenAI API key not configured"
            
            prompt = f"""Analyze this social media post for {platform} and provide suggestions:

{content}

Return a JSON object with:
{{
  "score": 1-10 rating,
  "improvements": ["list of suggestions"],
  "optimal_length": true/false,
  "has_call_to_action": true/false,
  "recommended_posting_times": ["time suggestions"],
  "emoji_suggestions": ["relevant emojis"]
}}"""

            response = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': 'gpt-4o',
                    'messages': [
                        {'role': 'system', 'content': 'You are a social media optimization expert. Return only valid JSON.'},
                        {'role': 'user', 'content': prompt}
                    ],
                    'max_tokens': 500,
                    'temperature': 0.5
                },
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                
                text = text.strip()
                if text.startswith('```json'):
                    text = text[7:]
                if text.startswith('```'):
                    text = text[3:]
                if text.endswith('```'):
                    text = text[:-3]
                
                try:
                    suggestions = json.loads(text.strip())
                    return suggestions, None
                except json.JSONDecodeError:
                    return {'raw_suggestions': text}, None
            else:
                error_data = response.json()
                return {}, error_data.get('error', {}).get('message', f"API error: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Content analysis error: {e}")
            return {}, str(e)
