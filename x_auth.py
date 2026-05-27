"""
X (Twitter) OAuth 2.0 PKCE Integration — LUX Marketing Platform.

Multi-account support:
- Each X account goes through its own Sign in with X consent flow.
- Tokens are stored per XOAuth record (user × x_user_id).
- Every posting/read/delete action requires an explicit account_id,
  verified against the requesting user before use.
- Auto-refresh per account using offline.access refresh tokens.
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

X_AUTH_URL   = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL  = "https://api.x.com/2/oauth2/token"
X_REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
X_ME_URL     = "https://api.x.com/2/users/me"
X_TWEETS_URL = "https://api.x.com/2/tweets"
X_SCOPES     = "tweet.read users.read tweet.write offline.access"

x_bp     = Blueprint("x_auth", __name__, url_prefix="/auth/x")
x_api_bp = Blueprint("x_api",  __name__, url_prefix="/api/x")


# ---------------------------------------------------------------------------
# Private helpers
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


def _get_all_records(user_id: int, company_id) -> list:
    """Return every XOAuth record that belongs to this user+company."""
    from models import XOAuth
    return (
        XOAuth.query
        .filter_by(user_id=user_id, company_id=company_id)
        .order_by(XOAuth.created_at)
        .all()
    )


def _get_account(account_id: int, user_id: int, company_id) -> tuple:
    """
    Fetch a specific XOAuth record and verify ownership.
    Returns (record, error_message). error_message is None on success.
    """
    from models import XOAuth
    record = XOAuth.query.get(account_id)
    if not record:
        return None, "X account not found."
    if record.user_id != user_id:
        return None, "X account does not belong to your account."
    if company_id is not None and record.company_id != company_id:
        return None, "X account belongs to a different workspace."
    return record, None


def _parse_x_error(resp) -> tuple:
    """
    Parse an X API error response into (human_message, raw_data).
    Handles X API v2 errors array, OAuth envelopes, and HTTP status fallbacks.
    """
    try:
        data = resp.json()
    except Exception:
        return f"Unexpected response (HTTP {resp.status_code})", {}

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

    if "error" in data:
        return data.get("error_description") or data.get("error"), data

    if "detail" in data:
        return data["detail"], data
    if "title" in data:
        return data["title"], data

    fallback = {
        400: "Bad request — check the content you are trying to post.",
        401: "Authentication failed. Please reconnect this X account.",
        403: "Permission denied. Make sure tweet.write and tweet.read are authorized.",
        404: "Resource not found on X.",
        429: "X rate limit reached. Please wait a moment and try again.",
        500: "X is experiencing an internal error. Please try again later.",
        503: "X is temporarily unavailable. Please try again later.",
    }
    return fallback.get(resp.status_code, f"HTTP {resp.status_code}"), data


def _do_refresh(record) -> bool:
    """
    Exchange the stored refresh token for a fresh access token.
    Updates the record in-place, commits, returns True on success.
    """
    refresh_tok = record.get_refresh_token()
    if not refresh_tok:
        logger.warning("X refresh: no refresh token for account id=%s", record.id)
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
        logger.error("X refresh network error (account id=%s): %s", record.id, exc)
        return False

    if not resp.ok:
        msg, _ = _parse_x_error(resp)
        logger.warning("X token refresh rejected (account id=%s): %s", record.id, msg)
        record.status = "expired"
        db.session.commit()
        return False

    data = resp.json()
    record.set_access_token(data["access_token"])
    if data.get("refresh_token"):
        record.set_refresh_token(data["refresh_token"])
    if data.get("expires_in"):
        record.expires_at = datetime.utcnow() + timedelta(seconds=int(data["expires_in"]))
    record.status      = "active"
    record.updated_at  = datetime.utcnow()
    db.session.commit()
    logger.info("X token refreshed (account id=%s @%s)", record.id, record.username)
    return True


def _refresh_if_needed(record) -> bool:
    """Refresh only when the token is within 10 min of expiry or already expired."""
    if not record.needs_refresh:
        return True
    return _do_refresh(record)


def _make_user_request(record, method: str, url: str, **kwargs) -> requests.Response:
    """
    Make an authenticated X API request using the account's access token.
    Auto-refreshes and retries once on HTTP 401.
    Raises requests.RequestException on network failure.
    """
    def _call():
        token = record.get_access_token()
        hdrs  = kwargs.pop("headers", {})
        hdrs["Authorization"] = f"Bearer {token}"
        return requests.request(method, url, headers=hdrs, timeout=15, **kwargs)

    resp = _call()

    if resp.status_code == 401:
        logger.info("X API 401 — attempting refresh for account id=%s", record.id)
        if _do_refresh(record):
            resp = _call()

    return resp


def _record_to_dict(r) -> dict:
    """Serialise an XOAuth record for JSON / template use."""
    return {
        "id":                r.id,
        "x_user_id":         r.x_user_id,
        "username":          r.username,
        "display_name":      r.display_name,
        "profile_image_url": r.profile_image_url,
        "status":            r.status,
        "is_expired":        r.is_expired,
        "needs_refresh":     r.needs_refresh,
        "scope":             r.scope,
        "expires_at":        r.expires_at.isoformat() if r.expires_at else None,
        "created_at":        r.created_at.isoformat() if r.created_at else None,
    }


# ---------------------------------------------------------------------------
# Auth routes  (/auth/x/…)
# ---------------------------------------------------------------------------

@x_bp.route("/connect")
@login_required
def connect():
    """Initiate a new X OAuth 2.0 PKCE consent flow for a fresh account."""
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

    session["x_oauth_state"]         = state
    session["x_oauth_code_verifier"] = code_verifier
    company = _get_current_company()
    session["x_oauth_company_id"]    = company.id if company else None

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
    """Exchange the auth code for tokens and store/update the XOAuth record."""
    error = request.args.get("error")
    if error:
        flash(
            f"X authorization failed: {request.args.get('error_description', error)}",
            "error",
        )
        return redirect(url_for("x_auth.manage"))

    state         = request.args.get("state")
    code          = request.args.get("code")
    stored_state  = session.pop("x_oauth_state",         None)
    code_verifier = session.pop("x_oauth_code_verifier", None)
    company_id    = session.pop("x_oauth_company_id",    None)

    if not stored_state or state != stored_state:
        logger.error("X OAuth state mismatch — possible CSRF from user %s", current_user.id)
        flash("Security validation failed. Please try connecting again.", "error")
        return redirect(url_for("x_auth.manage"))

    if not code:
        flash("No authorization code received from X.", "error")
        return redirect(url_for("x_auth.manage"))

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
        return redirect(url_for("x_auth.manage"))

    if not token_resp.ok:
        msg, _ = _parse_x_error(token_resp)
        logger.error("X token exchange failed: %s", msg)
        flash(f"Failed to connect X account: {msg}", "error")
        return redirect(url_for("x_auth.manage"))

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
        return redirect(url_for("x_auth.manage"))

    if not me_resp.ok:
        msg, _ = _parse_x_error(me_resp)
        logger.error("X /users/me failed: %s", msg)
        flash(f"Could not retrieve your X profile: {msg}", "error")
        return redirect(url_for("x_auth.manage"))

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

    # Upsert: if this exact X account was already connected, refresh its tokens.
    existing = XOAuth.query.filter_by(
        user_id=current_user.id,
        x_user_id=user_info.get("id"),
    ).first()

    if existing:
        existing.set_access_token(access_token)
        existing.set_refresh_token(token_data.get("refresh_token"))
        existing.expires_at        = expires_at
        existing.scope             = token_data.get("scope", X_SCOPES)
        existing.token_type        = token_data.get("token_type", "bearer")
        existing.username          = user_info.get("username")
        existing.display_name      = user_info.get("name")
        existing.profile_image_url = user_info.get("profile_image_url")
        existing.status            = "active"
        existing.company_id        = company.id if company else None
        existing.updated_at        = datetime.utcnow()
        db.session.commit()
        flash(f"X account @{existing.username} reconnected successfully!", "success")
        logger.info(
            "X OAuth re-connected: user=%s @%s (record id=%s)",
            current_user.id, existing.username, existing.id,
        )
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
        logger.info(
            "X OAuth connected: user=%s @%s (record id=%s)",
            current_user.id, record.username, record.id,
        )

    return redirect(url_for("x_auth.manage"))


@x_bp.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    """Revoke and delete one specific X account (identified by account_id)."""
    payload    = request.get_json() or {}
    account_id = payload.get("account_id")
    company    = _get_current_company()

    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"})

    record, err = _get_account(
        int(account_id), current_user.id, company.id if company else None
    )
    if err:
        return jsonify({"success": False, "error": err})

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

    username = record.username
    db.session.delete(record)
    db.session.commit()
    logger.info(
        "X account @%s (id=%s) disconnected by user %s",
        username, account_id, current_user.id,
    )
    return jsonify({"success": True, "message": f"@{username} disconnected"})


@x_bp.route("/refresh", methods=["POST"])
@login_required
def refresh():
    """Force a token refresh for one specific account."""
    payload    = request.get_json() or {}
    account_id = payload.get("account_id")
    company    = _get_current_company()

    if not account_id:
        return jsonify({"success": False, "error": "account_id is required"})

    record, err = _get_account(
        int(account_id), current_user.id, company.id if company else None
    )
    if err:
        return jsonify({"success": False, "error": err})

    if _do_refresh(record):
        return jsonify({
            "success":   True,
            "message":   f"Token refreshed for @{record.username}",
            "is_expired": record.is_expired,
        })
    return jsonify({
        "success": False,
        "error":   "Token refresh failed. Please disconnect and reconnect this account.",
        "action":  "reconnect",
    })


@x_bp.route("/status")
@login_required
def status():
    """Return the list of all connected X accounts for the current user/company."""
    company = _get_current_company()
    try:
        records = _get_all_records(
            current_user.id, company.id if company else None
        )
        return jsonify({
            "connected": len(records) > 0,
            "count":     len(records),
            "accounts":  [_record_to_dict(r) for r in records],
        })
    except Exception as exc:
        logger.error("X status error: %s", exc)
        return jsonify({"connected": False, "error": str(exc)})


@x_bp.route("/manage")
@login_required
def manage():
    """Render the X accounts management page."""
    company = _get_current_company()
    records = _get_all_records(
        current_user.id, company.id if company else None
    )
    return render_template(
        "x_integration.html",
        x_records=records,
        company=company,
    )


# ---------------------------------------------------------------------------
# API routes  (/api/x/…)
# ---------------------------------------------------------------------------

@x_api_bp.route("/accounts")
@login_required
def list_accounts():
    """Return all connected X accounts as JSON."""
    company = _get_current_company()
    try:
        records = _get_all_records(
            current_user.id, company.id if company else None
        )
        return jsonify({
            "success":  True,
            "accounts": [_record_to_dict(r) for r in records],
            "count":    len(records),
        })
    except Exception as exc:
        logger.error("X list_accounts error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@x_api_bp.route("/tweet", methods=["POST"])
@login_required
def create_tweet():
    """
    Post a tweet on behalf of a specific connected X account.
    Required JSON fields: text (str), account_id (int)
    """
    company = _get_current_company()
    payload    = request.get_json() or {}
    text       = (payload.get("text") or "").strip()
    account_id = payload.get("account_id")

    if not text:
        return jsonify({"success": False, "error": "Tweet text is required"})
    if len(text) > 280:
        return jsonify({"success": False, "error": "Tweet text exceeds 280 characters"})
    if not account_id:
        return jsonify({"success": False, "error": "account_id is required — select which X account to post from"})

    try:
        record, err = _get_account(
            int(account_id), current_user.id, company.id if company else None
        )
        if err:
            return jsonify({"success": False, "error": err})

        _refresh_if_needed(record)
        if record.is_expired:
            return jsonify({
                "success":  False,
                "error":    f"Token expired for @{record.username}. Please reconnect this account.",
                "action":   "reconnect",
                "account_id": record.id,
            })

        resp = _make_user_request(
            record, "POST", X_TWEETS_URL,
            json={"text": text},
            headers={"Content-Type": "application/json"},
        )

        if not resp.ok:
            msg, raw = _parse_x_error(resp)
            logger.error(
                "X create tweet failed (account @%s, HTTP %s): %s",
                record.username, resp.status_code, raw,
            )
            result = {"success": False, "error": msg}
            if resp.status_code == 429:
                result["action"] = "rate_limited"
            elif resp.status_code == 401:
                result["action"]     = "reconnect"
                result["account_id"] = record.id
            return jsonify(result)

        tweet_data = resp.json().get("data", {})
        tweet_id   = tweet_data.get("id")
        tweet_text = tweet_data.get("text", text)
        logger.info(
            "X tweet created (id=%s, account @%s, user %s)",
            tweet_id, record.username, current_user.id,
        )
        return jsonify({
            "success":       True,
            "tweet_id":      tweet_id,
            "text":          tweet_text,
            "account":       record.username,
            "url":           f"https://x.com/{record.username}/status/{tweet_id}",
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
    """
    Delete a tweet that belongs to a specific connected account.
    Requires query param: account_id
    """
    company    = _get_current_company()
    account_id = request.args.get("account_id")

    if not account_id:
        return jsonify({"success": False, "error": "account_id query param is required"})

    try:
        record, err = _get_account(
            int(account_id), current_user.id, company.id if company else None
        )
        if err:
            return jsonify({"success": False, "error": err})

        _refresh_if_needed(record)
        if record.is_expired:
            return jsonify({
                "success": False,
                "error":   f"Token expired for @{record.username}. Please reconnect.",
                "action":  "reconnect",
            })

        resp = _make_user_request(
            record, "DELETE", f"{X_TWEETS_URL}/{tweet_id}"
        )

        if not resp.ok:
            msg, raw = _parse_x_error(resp)
            logger.error(
                "X delete tweet %s failed (account @%s, HTTP %s): %s",
                tweet_id, record.username, resp.status_code, raw,
            )
            if resp.status_code == 403:
                msg = "You can only delete your own tweets."
            elif resp.status_code == 404:
                msg = "Tweet not found — it may have already been deleted."
            result = {"success": False, "error": msg}
            if resp.status_code == 429:
                result["action"] = "rate_limited"
            return jsonify(result)

        deleted = resp.json().get("data", {}).get("deleted", False)
        logger.info(
            "X tweet %s deleted (account @%s, user %s)",
            tweet_id, record.username, current_user.id,
        )
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
    """
    Fetch recent tweets for a specific connected account.
    Requires query param: account_id
    """
    company    = _get_current_company()
    account_id = request.args.get("account_id")

    if not account_id:
        return jsonify({"success": False, "error": "account_id query param is required"})

    try:
        record, err = _get_account(
            int(account_id), current_user.id, company.id if company else None
        )
        if err:
            return jsonify({"success": False, "error": err})

        _refresh_if_needed(record)
        if record.is_expired:
            return jsonify({
                "success": False,
                "error":   f"Token expired for @{record.username}. Please reconnect.",
                "action":  "reconnect",
            })

        params = {
            "max_results":  10,
            "tweet.fields": "created_at,public_metrics",
            "exclude":      "retweets,replies",
        }
        resp = _make_user_request(
            record, "GET",
            f"https://api.x.com/2/users/{record.x_user_id}/tweets",
            params=params,
        )

        if not resp.ok:
            msg, raw = _parse_x_error(resp)
            logger.error(
                "X get tweets failed (account @%s, HTTP %s): %s",
                record.username, resp.status_code, raw,
            )
            result = {"success": False, "error": msg}
            if resp.status_code in (401, 429):
                result["action"] = "reconnect" if resp.status_code == 401 else "rate_limited"
            return jsonify(result)

        raw_tweets = resp.json().get("data") or []
        tweets = []
        for t in raw_tweets:
            m = t.get("public_metrics", {})
            tweets.append({
                "id":         t["id"],
                "text":       t["text"],
                "created_at": t.get("created_at"),
                "likes":      m.get("like_count", 0),
                "retweets":   m.get("retweet_count", 0),
                "replies":    m.get("reply_count", 0),
                "url":        f"https://x.com/{record.username}/status/{t['id']}",
            })

        return jsonify({
            "success":    True,
            "tweets":     tweets,
            "count":      len(tweets),
            "account_id": record.id,
            "username":   record.username,
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
    """Return stored info for all connected X accounts."""
    company = _get_current_company()
    try:
        records = _get_all_records(
            current_user.id, company.id if company else None
        )
        if not records:
            return jsonify({"success": False, "error": "No X accounts connected"})
        return jsonify({
            "success":  True,
            "accounts": [_record_to_dict(r) for r in records],
        })
    except Exception as exc:
        logger.error("X get_user error: %s", exc)
        return jsonify({"success": False, "error": str(exc)})
