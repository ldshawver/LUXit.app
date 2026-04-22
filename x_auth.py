"""
X (Twitter) OAuth 2.0 PKCE Integration for LUX Marketing Platform.

Supports:
- Sign in with X (PKCE authorization code flow)
- User-access-token for all posting/account actions
- Automatic token refresh using offline.access scope
- Recent-posts view and tweet deletion
- Robust X API error handling
"""

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import (
    Blueprint, flash, jsonify, redirect, render_template,
    request, session, url_for
)
from flask_login import current_user, login_required

from extensions import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# X API constants
# ---------------------------------------------------------------------------

X_AUTH_URL    = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL   = "https://api.x.com/2/oauth2/token"
X_REVOKE_URL  = "https://api.x.com/2/oauth2/revoke"
X_ME_URL      = "https://api.x.com/2/users/me"
X_TWEETS_URL  = "https://api.x.com/2/tweets"
X_SCOPES      = "tweet.read users.read tweet.write offline.access"

x_bp     = Blueprint("x_auth", __name__, url_prefix="/auth/x")
x_api_bp = Blueprint("x_api",  __name__, url_prefix="/api/x")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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


def _parse_x_error(resp) -> tuple[str, dict]:
    """
    Parse an X API error response into (human_message, raw_data).
    Handles X API v2 format, OAuth error format, and HTTP status fallbacks.
    """
    try:
        data = resp.json()
    except Exception:
        return f"Unexpected response (HTTP {resp.status_code})", {}

    # X API v2 errors array
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        msg = (
            first.get("message")
            or first.get("detail")
            or first.get("title")
            or "Unknown X API error"
        )
        return msg, data

    # OAuth error envelope
    if "error" in data:
        return data.get("error_description") or data.get("error"), data

    # Single-detail field
    if "detail" in data:
        return data["detail"], data

    if "title" in data:
        return data["title"], data

    # HTTP status fallbacks
    status_messages = {
        400: "Bad request — check the content you are trying to post.",
        401: "Authentication failed. Please reconnect your X account.",
        403: "Permission denied. Make sure tweet.write and tweet.read are authorized.",
        404: "Resource not found on X.",
        429: "X rate limit reached. Please wait a moment and try again.",
        500: "X is experiencing an internal error. Please try again later.",
        503: "X is temporarily unavailable. Please try again later.",
    }
    return status_messages.get(resp.status_code, f"HTTP {resp.status_code}"), data


def _refresh_token(oauth_record) -> bool:
    """
    Exchange a refresh token for a fresh access token.
    Updates the record in-place and commits. Returns True on success.
    """
    refresh_tok = oauth_record.get_refresh_token()
    if not refresh_tok:
        logger.warning("X refresh: no refresh token stored for user %s", oauth_record.user_id)
        return False

    client_id = _get_client_id()
    if not client_id:
        logger.error("X refresh: X_CLIENT_ID not configured")
        return False

    try:
        resp = requests.post(
            X_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_tok,
                "client_id":     client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.error("X refresh network error: %s", exc)
        return False

    if not resp.ok:
        msg, _ = _parse_x_error(resp)
        logger.warning("X token refresh failed for user %s: %s", oauth_record.user_id, msg)
        oauth_record.status = "expired"
        db.session.commit()
        return False

    data = resp.json()
    oauth_record.set_access_token(data["access_token"])
    if data.get("refresh_token"):
        oauth_record.set_refresh_token(data["refresh_token"])
    if data.get("expires_in"):
        oauth_record.expires_at = datetime.utcnow() + timedelta(
            seconds=int(data["expires_in"])
        )
    oauth_record.status = "active"
    oauth_record.updated_at = datetime.utcnow()
    db.session.commit()
    logger.info("X token refreshed for user %s", oauth_record.user_id)
    return True


def _refresh_token_if_needed(oauth_record) -> bool:
    """Refresh only when the token is near expiry or already expired."""
    if not oauth_record.needs_refresh:
        return True
    return _refresh_token(oauth_record)


def _make_user_request(oauth_record, method: str, url: str, **kwargs) -> requests.Response:
    """
    Make an authenticated X API request using the user's access token.
    Automatically refreshes and retries once on HTTP 401.
    Raises requests.RequestException on network failure.
    """
    def _do_request():
        token = oauth_record.get_access_token()
        hdrs = kwargs.pop("headers", {})
        hdrs["Authorization"] = f"Bearer {token}"
        return requests.request(method, url, headers=hdrs, timeout=15, **kwargs)

    resp = _do_request()

    if resp.status_code == 401:
        logger.info("X API 401 — attempting token refresh for user %s", oauth_record.user_id)
        if _refresh_token(oauth_record):
            resp = _do_request()
        else:
            logger.warning("X API 401 — refresh failed, staying on original response")

    return resp


# ---------------------------------------------------------------------------
# Auth routes  (/auth/x/…)
# ---------------------------------------------------------------------------

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

    code_verifier  = _b64url(secrets.token_bytes(32))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    state          = _b64url(secrets.token_bytes(24))

    session["x_oauth_state"]        = state
    session["x_oauth_code_verifier"] = code_verifier
    company = _get_current_company()
    session["x_oauth_company_id"]   = company.id if company else None

    params = {
        "response_type":         "code",
        "client_id":             client_id,
        "redirect_uri":          _get_redirect_uri(),
        "scope":                 X_SCOPES,
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    logger.info("Initiating X OAuth PKCE for user %s", current_user.id)
    return redirect(f"{X_AUTH_URL}?{urlencode(params)}")


@x_bp.route("/callback")
@login_required
def callback():
    """Handle the X OAuth callback — exchange code for tokens, store record."""
    error = request.args.get("error")
    if error:
        flash(
            f"X authorization failed: {request.args.get('error_description', error)}",
            "error",
        )
        return redirect(url_for("main.dashboard"))

    state        = request.args.get("state")
    code         = request.args.get("code")
    stored_state = session.pop("x_oauth_state",        None)
    code_verifier = session.pop("x_oauth_code_verifier", None)
    company_id   = session.pop("x_oauth_company_id",   None)

    if not stored_state or state != stored_state:
        logger.error("X OAuth state mismatch — possible CSRF from user %s", current_user.id)
        flash("Security validation failed. Please try connecting again.", "error")
        return redirect(url_for("main.dashboard"))

    if not code:
        flash("No authorization code received from X.", "error")
        return redirect(url_for("main.dashboard"))

    client_id = _get_client_id()
    try:
        token_resp = requests.post(
            X_TOKEN_URL,
            data={
                "code":          code,
                "grant_type":    "authorization_code",
                "client_id":     client_id,
                "redirect_uri":  _get_redirect_uri(),
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    except requests.RequestException as exc:
        logger.error("X token exchange network error: %s", exc)
        flash("Could not reach X. Please check your connection and try again.", "error")
        return redirect(url_for("main.dashboard"))

    if not token_resp.ok:
        msg, _ = _parse_x_error(token_resp)
        logger.error("X token exchange failed: %s", msg)
        flash(f"Failed to connect X account: {msg}", "error")
        return redirect(url_for("main.dashboard"))

    token_data   = token_resp.json()
    access_token = token_data.get("access_token")

    try:
        me_resp = requests.get(
            f"{X_ME_URL}?user.fields=profile_image_url,name,username",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error("X /users/me network error: %s", exc)
        flash("Connected but failed to fetch your X profile. Please try again.", "error")
        return redirect(url_for("main.dashboard"))

    if not me_resp.ok:
        msg, _ = _parse_x_error(me_resp)
        logger.error("X /users/me failed: %s", msg)
        flash(f"Could not retrieve your X profile: {msg}", "error")
        return redirect(url_for("main.dashboard"))

    user_info = me_resp.json().get("data", {})

    from models import XOAuth, Company
    company = (
        Company.query.get(company_id) if company_id else _get_current_company()
    )

    expires_at = None
    if token_data.get("expires_in"):
        expires_at = datetime.utcnow() + timedelta(
            seconds=int(token_data["expires_in"])
        )

    existing = XOAuth.query.filter_by(
        user_id=current_user.id,
        x_user_id=user_info.get("id"),
    ).first()

    if existing:
        existing.set_access_token(access_token)
        existing.set_refresh_token(token_data.get("refresh_token"))
        existing.expires_at       = expires_at
        existing.scope            = token_data.get("scope", X_SCOPES)
        existing.token_type       = token_data.get("token_type", "bearer")
        existing.username         = user_info.get("username")
        existing.display_name     = user_info.get("name")
        existing.profile_image_url = user_info.get("profile_image_url")
        existing.status           = "active"
        existing.company_id       = company.id if company else None
        existing.updated_at       = datetime.utcnow()
        db.session.commit()
        flash(f"X account @{existing.username} reconnected successfully!", "success")
        account_username = existing.username
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
        account_username = record.username

    logger.info("X OAuth completed — user %s connected @%s", current_user.id, account_username)
    return redirect(url_for("x_auth.manage"))


@x_bp.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    """Revoke the X token and remove the stored OAuth record."""
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
        client_id    = _get_client_id()
        if access_token and client_id:
            try:
                requests.post(
                    X_REVOKE_URL,
                    data={"token": access_token, "client_id": client_id},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("X token revoke non-fatal error: %s", exc)

        db.session.delete(record)
        db.session.commit()
        logger.info("X account disconnected for user %s", current_user.id)
        return jsonify({"success": True, "message": "X account disconnected"})

    except Exception as exc:
        logger.error("X disconnect error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@x_bp.route("/refresh", methods=["POST"])
@login_required
def refresh():
    """Manually force a token refresh."""
    company = _get_current_company()
    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "No active X account connected"})

        if _refresh_token(record):
            return jsonify({"success": True, "message": "X token refreshed successfully"})
        return jsonify({
            "success": False,
            "error": "Token refresh failed. Please disconnect and reconnect your X account.",
        })

    except Exception as exc:
        logger.error("X manual refresh error: %s", exc)
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
            "connected":         True,
            "status":            record.status,
            "username":          record.username,
            "display_name":      record.display_name,
            "profile_image_url": record.profile_image_url,
            "x_user_id":         record.x_user_id,
            "expires_at":        record.expires_at.isoformat() if record.expires_at else None,
            "is_expired":        record.is_expired,
            "needs_refresh":     record.needs_refresh,
            "scope":             record.scope,
        })

    except Exception as exc:
        logger.error("X status error: %s", exc)
        return jsonify({"connected": False, "error": str(exc)})


@x_bp.route("/manage")
@login_required
def manage():
    """Render the X account management page."""
    company = _get_current_company()
    from models import XOAuth
    record = XOAuth.query.filter_by(
        user_id=current_user.id,
        company_id=company.id if company else None,
    ).first()
    return render_template(
        "x_integration.html",
        x_record=record,
        company=company,
    )


# ---------------------------------------------------------------------------
# API routes  (/api/x/…)
# ---------------------------------------------------------------------------

@x_api_bp.route("/tweet", methods=["POST"])
@login_required
def create_tweet():
    """Post a tweet on behalf of the connected X account (user token)."""
    company = _get_current_company()
    payload = request.get_json() or {}
    text    = (payload.get("text") or "").strip()

    if not text:
        return jsonify({"success": False, "error": "Tweet text is required"})
    if len(text) > 280:
        return jsonify({"success": False, "error": "Tweet text exceeds 280 characters"})

    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "X account not connected. Please connect first."})

        _refresh_token_if_needed(record)
        if record.is_expired:
            return jsonify({
                "success": False,
                "error":   "X token expired. Please reconnect your account.",
                "action":  "reconnect",
            })

        resp = _make_user_request(
            record, "POST", X_TWEETS_URL,
            json={"text": text},
            headers={"Content-Type": "application/json"},
        )

        if not resp.ok:
            msg, raw = _parse_x_error(resp)
            logger.error("X create tweet failed (HTTP %s): %s", resp.status_code, raw)
            result = {"success": False, "error": msg}
            if resp.status_code == 429:
                result["action"] = "rate_limited"
            elif resp.status_code == 401:
                result["action"] = "reconnect"
            return jsonify(result)

        data      = resp.json().get("data", {})
        tweet_id  = data.get("id")
        tweet_text = data.get("text", text)
        logger.info("X tweet created (id=%s) for user %s", tweet_id, current_user.id)
        return jsonify({
            "success":  True,
            "tweet_id": tweet_id,
            "text":     tweet_text,
            "url":      f"https://x.com/{record.username}/status/{tweet_id}",
        })

    except requests.RequestException as exc:
        logger.error("X create tweet network error: %s", exc)
        return jsonify({"success": False, "error": "Network error reaching X. Please try again."})
    except Exception as exc:
        logger.error("X create tweet unexpected error: %s", exc)
        return jsonify({"success": False, "error": "An unexpected error occurred."})


@x_api_bp.route("/tweet/<tweet_id>", methods=["DELETE"])
@login_required
def delete_tweet(tweet_id):
    """Delete a tweet by ID (must be owned by the connected account)."""
    company = _get_current_company()
    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "X account not connected"})

        _refresh_token_if_needed(record)
        if record.is_expired:
            return jsonify({
                "success": False,
                "error":   "X token expired. Please reconnect.",
                "action":  "reconnect",
            })

        resp = _make_user_request(
            record, "DELETE", f"{X_TWEETS_URL}/{tweet_id}"
        )

        if not resp.ok:
            msg, raw = _parse_x_error(resp)
            logger.error("X delete tweet %s failed (HTTP %s): %s", tweet_id, resp.status_code, raw)
            result = {"success": False, "error": msg}
            if resp.status_code == 403:
                result["error"] = "You can only delete your own tweets."
            elif resp.status_code == 404:
                result["error"] = "Tweet not found — it may have already been deleted."
            elif resp.status_code == 429:
                result["action"] = "rate_limited"
            return jsonify(result)

        deleted = resp.json().get("data", {}).get("deleted", False)
        logger.info("X tweet %s deleted by user %s", tweet_id, current_user.id)
        return jsonify({"success": True, "deleted": deleted})

    except requests.RequestException as exc:
        logger.error("X delete tweet network error: %s", exc)
        return jsonify({"success": False, "error": "Network error reaching X. Please try again."})
    except Exception as exc:
        logger.error("X delete tweet unexpected error: %s", exc)
        return jsonify({"success": False, "error": "An unexpected error occurred."})


@x_api_bp.route("/tweets")
@login_required
def get_recent_tweets():
    """Fetch the authenticated user's recent tweets (up to 10)."""
    company = _get_current_company()
    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "X account not connected"})

        _refresh_token_if_needed(record)
        if record.is_expired:
            return jsonify({
                "success": False,
                "error":   "X token expired. Please reconnect.",
                "action":  "reconnect",
            })

        params = {
            "max_results":   10,
            "tweet.fields":  "created_at,public_metrics",
            "exclude":       "retweets,replies",
        }
        resp = _make_user_request(
            record,
            "GET",
            f"https://api.x.com/2/users/{record.x_user_id}/tweets",
            params=params,
        )

        if not resp.ok:
            msg, raw = _parse_x_error(resp)
            logger.error("X get tweets failed (HTTP %s): %s", resp.status_code, raw)
            result = {"success": False, "error": msg}
            if resp.status_code == 429:
                result["action"] = "rate_limited"
            elif resp.status_code == 401:
                result["action"] = "reconnect"
            return jsonify(result)

        data   = resp.json()
        tweets = data.get("data") or []
        formatted = []
        for t in tweets:
            metrics = t.get("public_metrics", {})
            formatted.append({
                "id":            t["id"],
                "text":          t["text"],
                "created_at":    t.get("created_at"),
                "likes":         metrics.get("like_count", 0),
                "retweets":      metrics.get("retweet_count", 0),
                "replies":       metrics.get("reply_count", 0),
                "url":           f"https://x.com/{record.username}/status/{t['id']}",
            })

        return jsonify({
            "success": True,
            "tweets":  formatted,
            "count":   len(formatted),
        })

    except requests.RequestException as exc:
        logger.error("X get tweets network error: %s", exc)
        return jsonify({"success": False, "error": "Network error reaching X. Please try again."})
    except Exception as exc:
        logger.error("X get tweets unexpected error: %s", exc)
        return jsonify({"success": False, "error": "An unexpected error occurred."})


@x_api_bp.route("/user")
@login_required
def get_user():
    """Return the stored connected X user info."""
    company = _get_current_company()
    try:
        record = _get_oauth_record(current_user.id, company.id if company else None)
        if not record:
            return jsonify({"success": False, "error": "X account not connected"})

        return jsonify({
            "success":          True,
            "x_user_id":        record.x_user_id,
            "username":         record.username,
            "display_name":     record.display_name,
            "profile_image_url": record.profile_image_url,
            "status":           record.status,
            "is_expired":       record.is_expired,
            "needs_refresh":    record.needs_refresh,
        })

    except Exception as exc:
        logger.error("X get user error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})
