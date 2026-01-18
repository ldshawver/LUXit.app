"""
Replit Auth Integration for LUX Marketing Platform
This module provides OAuth authentication via Replit's OpenID Connect provider.
Users can sign in with Google, GitHub, X, Apple, or email/password through Replit.
"""

import jwt
import os
import uuid
import logging
import time
import requests
from functools import wraps
from urllib.parse import urlencode
from jwt import PyJWKClient

from flask import g, session, redirect, request, url_for, flash
from flask_dance.consumer import (
    OAuth2ConsumerBlueprint,
    oauth_authorized,
    oauth_error,
)
from flask_dance.consumer.storage import BaseStorage
from flask_login import login_user, logout_user, current_user
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
from sqlalchemy.exc import NoResultFound
from werkzeug.local import LocalProxy
from werkzeug.security import generate_password_hash

from app import app, db

logger = logging.getLogger(__name__)

_jwks_client = None
_jwks_cache_time = 0
JWKS_CACHE_DURATION = 3600


def get_jwks_client():
    """Get or create JWKS client with caching for Replit OIDC keys"""
    global _jwks_client, _jwks_cache_time
    
    issuer_url = os.environ.get('ISSUER_URL', "https://replit.com/oidc")
    jwks_url = f"{issuer_url}/.well-known/jwks.json"
    
    current_time = time.time()
    if _jwks_client is None or (current_time - _jwks_cache_time) > JWKS_CACHE_DURATION:
        try:
            _jwks_client = PyJWKClient(jwks_url, cache_keys=True, lifespan=JWKS_CACHE_DURATION)
            _jwks_cache_time = current_time
            logger.info("JWKS client initialized/refreshed successfully")
        except Exception as e:
            logger.error(f"Failed to initialize JWKS client: {e}")
            raise
    
    return _jwks_client


def verify_id_token(id_token, expected_audience):
    """
    Verify and decode the ID token with full security validation.
    Returns the decoded claims if valid, raises an exception otherwise.
    """
    issuer_url = os.environ.get('ISSUER_URL', "https://replit.com/oidc")
    
    try:
        jwks_client = get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=issuer_url,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["sub", "iss", "aud", "exp", "iat"]
            }
        )
        
        if 'sub' not in claims or not claims['sub']:
            raise jwt.InvalidTokenError("Missing or empty 'sub' claim")
        
        return claims
        
    except jwt.ExpiredSignatureError:
        logger.error("ID token has expired")
        raise
    except jwt.InvalidAudienceError:
        logger.error(f"Invalid audience in ID token")
        raise
    except jwt.InvalidIssuerError:
        logger.error(f"Invalid issuer in ID token")
        raise
    except jwt.InvalidSignatureError:
        logger.error("Invalid signature on ID token")
        raise
    except Exception as e:
        logger.error(f"ID token verification failed: {e}")
        raise


class UserSessionStorage(BaseStorage):
    """Storage for OAuth tokens in database, per user session"""
    
    def get(self, blueprint):
        from models import ReplitOAuth
        try:
            token = db.session.query(ReplitOAuth).filter_by(
                user_id=current_user.get_id() if current_user.is_authenticated else None,
                browser_session_key=g.browser_session_key,
                provider=blueprint.name,
            ).one().token
        except NoResultFound:
            token = None
        return token

    def set(self, blueprint, token):
        from models import ReplitOAuth
        user_id = current_user.get_id() if current_user.is_authenticated else None
        
        db.session.query(ReplitOAuth).filter_by(
            user_id=user_id,
            browser_session_key=g.browser_session_key,
            provider=blueprint.name,
        ).delete()
        
        new_model = ReplitOAuth()
        new_model.user_id = user_id
        new_model.browser_session_key = g.browser_session_key
        new_model.provider = blueprint.name
        new_model.token = token
        db.session.add(new_model)
        db.session.commit()

    def delete(self, blueprint):
        from models import ReplitOAuth
        user_id = current_user.get_id() if current_user.is_authenticated else None
        db.session.query(ReplitOAuth).filter_by(
            user_id=user_id,
            browser_session_key=g.browser_session_key,
            provider=blueprint.name
        ).delete()
        db.session.commit()


def make_replit_blueprint():
    """Create the Replit OAuth blueprint"""
    try:
        repl_id = os.environ.get('REPL_ID')
        if not repl_id:
            logger.warning("REPL_ID environment variable not set - Replit Auth will be disabled")
            return None
    except KeyError:
        logger.warning("REPL_ID environment variable not set - Replit Auth will be disabled")
        return None

    issuer_url = os.environ.get('ISSUER_URL', "https://replit.com/oidc")

    replit_bp = OAuth2ConsumerBlueprint(
        "replit_auth",
        __name__,
        client_id=repl_id,
        client_secret=None,
        base_url=issuer_url,
        authorization_url_params={
            "prompt": "login consent",
        },
        token_url=issuer_url + "/token",
        token_url_params={
            "auth": (),
            "include_client_id": True,
        },
        auto_refresh_url=issuer_url + "/token",
        auto_refresh_kwargs={
            "client_id": repl_id,
        },
        authorization_url=issuer_url + "/auth",
        use_pkce=True,
        code_challenge_method="S256",
        scope=["openid", "profile", "email", "offline_access"],
        storage=UserSessionStorage(),
    )

    @replit_bp.before_app_request
    def set_applocal_session():
        if '_browser_session_key' not in session:
            session['_browser_session_key'] = uuid.uuid4().hex
        session.modified = True
        g.browser_session_key = session['_browser_session_key']
        g.flask_dance_replit = replit_bp.session

    @replit_bp.route("/replit-logout")
    def replit_logout():
        """Log out from Replit Auth"""
        try:
            del replit_bp.token
        except Exception:
            pass
        logout_user()

        end_session_endpoint = issuer_url + "/session/end"
        encoded_params = urlencode({
            "client_id": repl_id,
            "post_logout_redirect_uri": request.url_root,
        })
        logout_url = f"{end_session_endpoint}?{encoded_params}"

        return redirect(logout_url)

    @replit_bp.route("/error")
    def error():
        flash('Authentication failed. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    return replit_bp


def save_or_update_user(user_claims):
    """
    Save or update user from Replit Auth claims.
    Links to existing user by email if found, otherwise creates new user.
    """
    from models import User
    
    replit_id = user_claims.get('sub')
    email = user_claims.get('email')
    first_name = user_claims.get('first_name')
    last_name = user_claims.get('last_name')
    profile_image = user_claims.get('profile_image_url')
    
    user = User.query.filter_by(replit_id=replit_id).first()
    
    if user:
        user.first_name = first_name or user.first_name
        user.last_name = last_name or user.last_name
        if profile_image:
            user.avatar_path = profile_image
        db.session.commit()
        return user
    
    if email:
        user = User.query.filter_by(email=email).first()
        if user:
            user.replit_id = replit_id
            user.first_name = first_name or user.first_name
            user.last_name = last_name or user.last_name
            if profile_image and not user.avatar_path:
                user.avatar_path = profile_image
            db.session.commit()
            logger.info(f"Linked existing user {email} to Replit account")
            return user
    
    username = email.split('@')[0] if email else f"replit_{replit_id}"
    base_username = username
    counter = 1
    while User.query.filter_by(username=username).first():
        username = f"{base_username}{counter}"
        counter += 1
    
    user = User()
    user.username = username
    user.email = email or f"{replit_id}@replit.user"
    user.replit_id = replit_id
    user.first_name = first_name
    user.last_name = last_name
    user.avatar_path = profile_image
    user.password_hash = generate_password_hash(uuid.uuid4().hex)
    user.is_admin = False
    
    db.session.add(user)
    db.session.commit()
    
    logger.info(f"Created new user {username} from Replit Auth")
    return user


@oauth_authorized.connect
def logged_in(blueprint, token):
    """Handle successful OAuth authorization with full JWT verification"""
    if blueprint.name != "replit_auth":
        return
    
    try:
        repl_id = os.environ.get('REPL_ID')
        if not repl_id:
            logger.error("REPL_ID not set - cannot validate token")
            flash('Authentication configuration error.', 'error')
            return redirect(url_for('auth.login'))
        
        user_claims = verify_id_token(token['id_token'], expected_audience=repl_id)
        
        user = save_or_update_user(user_claims)
        login_user(user, remember=True)
        blueprint.token = token
        
        flash(f'Welcome, {user.full_name}!', 'success')
        
        next_url = session.pop("next_url", None)
        if next_url:
            return redirect(next_url)
        return redirect(url_for('main.dashboard'))
        
    except jwt.ExpiredSignatureError:
        flash('Authentication failed: session expired.', 'error')
        return redirect(url_for('auth.login'))
    except jwt.InvalidAudienceError:
        flash('Authentication failed: invalid token audience.', 'error')
        return redirect(url_for('auth.login'))
    except jwt.InvalidIssuerError:
        flash('Authentication failed: invalid token issuer.', 'error')
        return redirect(url_for('auth.login'))
    except jwt.InvalidSignatureError:
        flash('Authentication failed: invalid token signature.', 'error')
        return redirect(url_for('auth.login'))
    except jwt.DecodeError as e:
        logger.error(f"JWT decode error: {e}")
        flash('Authentication failed: invalid token format.', 'error')
        return redirect(url_for('auth.login'))
    except Exception as e:
        logger.error(f"Error processing Replit Auth login: {e}")
        flash('Authentication failed. Please try again.', 'error')
        return redirect(url_for('auth.login'))


@oauth_error.connect
def handle_error(blueprint, error, error_description=None, error_uri=None):
    """Handle OAuth errors"""
    if blueprint.name != "replit_auth":
        return
    
    logger.error(f"Replit Auth error: {error} - {error_description}")
    flash('Authentication failed. Please try again.', 'error')
    return redirect(url_for('auth.login'))


def get_next_navigation_url(req):
    """Get the URL to redirect to after login"""
    is_navigation_url = (
        req.headers.get('Sec-Fetch-Mode') == 'navigate' and
        req.headers.get('Sec-Fetch-Dest') == 'document'
    )
    if is_navigation_url:
        return req.url
    return req.url


def require_replit_login(f):
    """Decorator to require Replit Auth login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            session["next_url"] = get_next_navigation_url(request)
            return redirect(url_for('replit_auth.login'))

        if hasattr(g, 'flask_dance_replit') and g.flask_dance_replit.token:
            expires_in = g.flask_dance_replit.token.get('expires_in', 0)
            if expires_in < 0:
                try:
                    issuer_url = os.environ.get('ISSUER_URL', "https://replit.com/oidc")
                    refresh_token_url = issuer_url + "/token"
                    repl_id = os.getenv('REPL_ID')
                    if not repl_id:
                        logger.warning("REPL_ID missing; skipping Replit token refresh.")
                        session["next_url"] = get_next_navigation_url(request)
                        return redirect(url_for('replit_auth.login'))
                    token = g.flask_dance_replit.refresh_token(
                        token_url=refresh_token_url,
                        client_id=repl_id
                    )
                    g.flask_dance_replit.token_updater(token)
                except InvalidGrantError:
                    session["next_url"] = get_next_navigation_url(request)
                    return redirect(url_for('replit_auth.login'))
                except Exception as e:
                    logger.error(f"Token refresh failed: {e}")
                    session["next_url"] = get_next_navigation_url(request)
                    return redirect(url_for('replit_auth.login'))

        return f(*args, **kwargs)
    return decorated_function


replit = LocalProxy(lambda: g.flask_dance_replit if hasattr(g, 'flask_dance_replit') else None)


def is_replit_auth_enabled():
    """Check if Replit Auth is available"""
    return bool(os.environ.get('REPL_ID'))
