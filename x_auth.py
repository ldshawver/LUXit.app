"""
X (Twitter) OAuth 2.0 PKCE Integration for LUX Marketing Platform.
Provides authorization code flow with PKCE, token management, and tweet creation.
"""

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import Blueprint, flash, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_required

from extensions import db

logger = logging.getLogger(__name__)

X_AUTH_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
X_ME_URL = "https://api.x.com/2/users/me"
X_TWEETS_URL = "https://api.x.com/2/tweets"
X_SCOPES = "tweet.read users.read tweet.write offline.access"

x_bp = Blueprint("x_auth", __name__, url_prefix="/auth/x")
x_api_bp = Blueprint("x_api", __name__, url_prefix="/api/x")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _get_client_id() -> str:
    return os.environ.get("X_CLIENT_ID", "")


def _get_redirect_uri() -> str:
    configured = os.environ.get("X_REDIRECT_URI", "")
    if configured:
        return configured
    dev_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
    if dev_domain:
        return f"https://{dev_domain}/auth/x/callback"
    return url_for("x_auth.callback", _external=True)


def _get_current_company():
    if current_user.is_authenticated:
        return current_user.get_default_company()
    return None


def _get_oauth_record(user_id, company_id):
    from models import XOAuth
    return XOAuth.query.filter_by(
        user_id=user_id,
        company_id=company_id,
        status="active",
    ).first()


def _refresh_token_if_needed(oauth_record) -> bool:
    """Attempt a token refresh. Returns True if successful or not needed."""
    if not oauth_record.needs_refresh:
        return True
    refresh_tok = oauth_record.get_refresh_token()
    if not refresh_tok:
        return False
    client_id = _get_client_id()
    if not client_id:
        return False
    try:
        resp = requests.post(
            X_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        data = resp.json()
        if not resp.ok:
            logger.warning("X token refresh failed: %s", data)
            oauth_record.status = "expired"
            db.session.commit()
            return False
        oauth_record.set_access_token(data["access_token"])
        if data.get("refresh_token"):
            oauth_record.set_refresh_token(data["refresh_token"])
        if data.get("expires_in"):
            oauth_record.expires_at = datetime.utcnow() + timedelta(
                seconds=int(data["expires_in"])
            )
        oauth_record.updated_at = datetime.utcnow()
        db.session.commit()
        logger.info("X token refreshed for user %s", oauth_record.user_id)
        return True
    except Exception as exc:
        logger.error("X token refresh error: %s", exc)
        return False


@x_bp.route("/connect")
@login_required
def connect():
    """Initiate X OAuth 2.0 PKCE flow."""
    client_id = _get_client_id()
    if not client_id:
        flash(
            "X Client ID not configured. Add X_CLIENT_ID in Settings → API Keys & Secrets.",
            "error",
        )
        company = _get_current_company()
        if company:
            return redirect(url_for("main.company_settings", company_id=company.id))
        return redirect(url_for("main.dashboard"))

    code_verifier = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))

    session["x_oauth_state"] = state
    session["x_oauth_code_verifier"] = code_verifier
    company = _get_current_company()
    session["x_oauth_company_id"] = company.id if company else None

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _get_redirect_uri(),
        "scope": X_SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{X_AUTH_URL}?{urlencode(params)}"
    logger.info("Initiating X OAuth for user %s", current_user.id)
    return redirect(auth_url)


@x_bp.route("/callback")
@login_required
def callback():
    """Handle the X OAuth callback, exchange code for tokens."""
    error = request.args.get("error")
    if error:
        flash(f"X authorization failed: {request.args.get('error_description', error)}", "error")
        return redirect(url_for("main.dashboard"))

    state = request.args.get("state")
    code = request.args.get("code")
    stored_state = session.pop("x_oauth_state", None)
    code_verifier = session.pop("x_oauth_code_verifier", None)
    company_id = session.pop("x_oauth_company_id", None)

    if not stored_state or state != stored_state:
        logger.error("X OAuth state mismatch — possible CSRF")
        flash("Security validation failed. Please try again.", "error")
        return redirect(url_for("main.dashboard"))

    if not code:
        flash("No authorization code received from X.", "error")
        return redirect(url_for("main.dashboard"))

    client_id = _get_client_id()
    try:
        token_resp = requests.post(
            X_TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": _get_redirect_uri(),
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        token_data = token_resp.json()
        if not token_resp.ok:
            logger.error("X token exchange failed: %s", token_data)
            flash("Failed to exchange X authorization code for tokens.", "error")
            return redirect(url_for("main.dashboard"))
    except Exception as exc:
        logger.error("X token request error: %s", exc)
        flash("Connection to X failed. Please try again.", "error")
        return redirect(url_for("main.dashboard"))

    access_token = token_data.get("access_token")
    try:
        me_resp = requests.get(
            f"{X_ME_URL}?user.fields=profile_image_url,name,username",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        me_data = me_resp.json()
        if not me_resp.ok:
            logger.error("X /users/me failed: %s", me_data)
            flash("Could not retrieve your X profile. Please try again.", "error")
            return redirect(url_for("main.dashboard"))
    except Exception as exc:
        logger.error("X /users/me error: %s", exc)
        flash("Failed to fetch X user profile.", "error")
        return redirect(url_for("main.dashboard"))

    user_info = me_data.get("data", {})
    from models import XOAuth, Company
    company = Company.query.get(company_id) if company_id else _get_current_company()

    expires_at = None
    if token_data.get("expires_in"):
        expires_at = datetime.utcnow() + timedelta(seconds=int(token_data["expires_in"]))

    existing = XOAuth.query.filter_by(
        user_id=current_user.id,
        x_user_id=user_info.get("id"),
    ).first()

    if existing:
        existing.set_access_token(access_token)
        existing.set_refresh_token(token_data.get("refresh_token"))
        existing.expires_at = expires_at
        existing.scope = token_data.get("scope", X_SCOPES)
        existing.token_type = token_data.get("token_type", "bearer")
        existing.username = user_info.get("username")
        existing.display_name = user_info.get("name")
        existing.profile_image_url = user_info.get("profile_image_url")
        existing.status = "active"
        existing.company_id = company.id if company else None
        existing.updated_at = datetime.utcnow()
        db.session.commit()
        flash(f"X account @{existing.username} reconnected successfully!", "success")
    else:
        record = XOAuth(
            user_id=current_user.id,
            company_id=company.id if company else None,
            x_user_id=user_info.get("id"),
            username=user_info.get("username"),
            display_name=user_info.get("name"),
            profile_image_url=user_info.get("profile_image_url"),
            expires_at=expires_at,
            scope=token_data.get("scope", X_SCOPES),
            token_type=token_data.get("token_type", "bearer"),
            status="active",
        )
        record.set_access_token(access_token)
        record.set_refresh_token(token_data.get("refresh_token"))
        db.session.add(record)
        db.session.commit()
        flash(f"X account @{record.username} connected successfully!", "success")

    logger.info("X OAuth completed for user %s", current_user.id)
    if company:
        return redirect(url_for("main.company_settings", company_id=company.id))
    return redirect(url_for("main.dashboard"))


@x_bp.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    """Revoke X token and remove the stored OAuth record."""
    company = _get_current_company()
    try:
        from models import XOAuth
        record = XOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None,
        ).first()

        if not record:
            return jsonify({"success": False, "error": "No X account connected"})

        access_token = record.get_access_token()
        client_id = _get_client_id()
        if access_token and client_id:
            try:
                requests.post(
                    X_REVOKE_URL,
                    data={"token": access_token, "client_id": client_id},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("X token revoke error (non-fatal): %s", exc)

        db.session.delete(record)
        db.session.commit()
        logger.info("X account disconnected for user %s", current_user.id)
        return jsonify({"success": True, "message": "X account disconnected"})

    except Exception as exc:
        logger.error("X disconnect error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@x_bp.route("/status")
@login_required
def status():
    """Return the current X connection status for the active company."""
    company = _get_current_company()
    try:
        from models import XOAuth
        record = XOAuth.query.filter_by(
            user_id=current_user.id,
            company_id=company.id if company else None,
        ).first()

        if not record:
            return jsonify({"connected": False, "message": "No X account connected"})

        return jsonify({
            "connected": True,
            "status": record.status,
            "username": record.username,
            "display_name": record.display_name,
            "profile_image_url": record.profile_image_url,
            "x_user_id": record.x_user_id,
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "is_expired": record.is_expired,
            "needs_refresh": record.needs_refresh,
            "scope": record.scope,
        })

    except Exception as exc:
        logger.error("X status error: %s", exc)
        return jsonify({"connected": False, "error": str(exc)})


@x_bp.route("/refresh", methods=["POST"])
@login_required
def refresh():
    """Manually refresh the X access token."""
    company = _get_current_company()
    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "No active X account connected"})

        ok = _refresh_token_if_needed(record)
        if ok:
            return jsonify({"success": True, "message": "X token refreshed"})
        return jsonify({"success": False, "error": "Token refresh failed. Please reconnect."})

    except Exception as exc:
        logger.error("X refresh error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@x_api_bp.route("/tweet", methods=["POST"])
@login_required
def create_tweet():
    """Create a tweet on behalf of the connected X account."""
    company = _get_current_company()
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()

    if not text:
        return jsonify({"success": False, "error": "Tweet text is required"})
    if len(text) > 280:
        return jsonify({"success": False, "error": "Tweet text exceeds 280 characters"})

    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "X account not connected"})

        _refresh_token_if_needed(record)
        if record.is_expired:
            return jsonify({"success": False, "error": "X token expired. Please reconnect."})

        access_token = record.get_access_token()
        resp = requests.post(
            X_TWEETS_URL,
            json={"text": text},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        result = resp.json()
        if not resp.ok:
            logger.error("X create tweet failed: %s", result)
            return jsonify({"success": False, "error": result.get("detail", "Tweet creation failed"), "details": result})

        tweet_id = result.get("data", {}).get("id")
        tweet_text = result.get("data", {}).get("text", text)
        logger.info("X tweet created (id=%s) for user %s", tweet_id, current_user.id)
        return jsonify({
            "success": True,
            "tweet_id": tweet_id,
            "text": tweet_text,
            "url": f"https://x.com/{record.username}/status/{tweet_id}",
        })

    except Exception as exc:
        logger.error("X create tweet error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@x_api_bp.route("/user")
@login_required
def get_user():
    """Return the connected X user info."""
    company = _get_current_company()
    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "X account not connected"})

        return jsonify({
            "success": True,
            "x_user_id": record.x_user_id,
            "username": record.username,
            "display_name": record.display_name,
            "profile_image_url": record.profile_image_url,
        })

    except Exception as exc:
        logger.error("X get user error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})
