"""TikTok OAuth Login Kit and Content Posting API routes."""
import os
from datetime import datetime

from flask import Blueprint, jsonify, redirect, request, session, render_template
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from extensions import db
from models import Company, TikTokOAuth, TikTokPost, TikTokPostStatusHistory
from services.tiktok_service import TikTokService, chunk_plan, is_allowed_media_url


tiktok_bp = Blueprint("tiktok", __name__)


def get_current_company():
    return current_user.get_default_company() if current_user.is_authenticated else None


def get_tiktok_service(company=None):
    service = TikTokService.from_company(company) if company else TikTokService()
    if not service.client_key:
        service.client_key = os.getenv("TIKTOK_CLIENT_KEY")
    if not service.client_secret:
        service.client_secret = os.getenv("TIKTOK_CLIENT_SECRET")
    return service


def _account(account_id=None):
    company = get_current_company()
    q = TikTokOAuth.query.filter_by(user_id=current_user.id, company_id=company.id, status="active")
    if account_id:
        q = q.filter_by(id=account_id)
    return company, q.first()


def _safe_error(message):
    return str(message or "TikTok request failed")[:500]


def _token_json(account):
    return {"id": account.id, "open_id": account.open_id, "display_name": account.display_name, "creator_username": account.creator_username, "creator_nickname": account.creator_nickname, "avatar_url": account.avatar_url, "scopes": account.scope, "expires_at": account.expires_at.isoformat() if account.expires_at else None}


def _ensure_fresh(account, company):
    if not account.needs_refresh:
        return True, None
    refresh = account.get_refresh_token()
    if not refresh:
        account.status = "expired"; db.session.commit()
        return False, "TikTok authorization expired. Please reconnect."
    result = get_tiktok_service(company).refresh_access_token(refresh)
    if not result.get("success"):
        account.status = "expired"; db.session.commit()
        return False, "TikTok authorization expired or revoked. Please reconnect."
    account.set_access_token(result["access_token"])
    if result.get("refresh_token"):
        account.set_refresh_token(result["refresh_token"])
    account.expires_at = result.get("expires_at")
    account.refresh_expires_at = result.get("refresh_expires_at")
    account.scope = result.get("scope") or account.scope
    db.session.commit()
    return True, None


@tiktok_bp.get("/api/oauth/tiktok/start")
@login_required
def oauth_start():
    company = get_current_company()
    service = get_tiktok_service(company)
    ok, error = service.validate_configuration()
    if not ok:
        return jsonify({"success": False, "error": error, "diagnostics": service.configuration_diagnostics()}), 503
    auth_url, state, verifier = service.build_auth_url()
    session["tiktok_oauth_state"] = state
    session["tiktok_pkce_verifier"] = verifier
    session["tiktok_oauth_company_id"] = company.id if company else None
    return redirect(auth_url)


@tiktok_bp.get("/api/oauth/tiktok/callback")
@login_required
def oauth_callback():
    error = request.args.get("error")
    if error:
        return jsonify({"success": False, "error": error, "error_description": request.args.get("error_description")}), 400
    if request.args.get("state") != session.pop("tiktok_oauth_state", None):
        return jsonify({"success": False, "error": "state_mismatch"}), 400
    code = request.args.get("code")
    if not code:
        return jsonify({"success": False, "error": "missing_code"}), 400
    company = db.session.get(Company, session.pop("tiktok_oauth_company_id", None)) or get_current_company()
    verifier = session.pop("tiktok_pkce_verifier", None)
    service = get_tiktok_service(company)
    result = service.exchange_code_for_token(code, code_verifier=verifier)
    if not result.get("success"):
        return jsonify({"success": False, "error": _safe_error(result.get("error"))}), 502
    info = service.get_user_info(result["access_token"], result.get("open_id"))
    account = TikTokOAuth.query.filter_by(company_id=company.id, user_id=current_user.id, open_id=result["open_id"]).first() or TikTokOAuth(company_id=company.id, user_id=current_user.id, open_id=result["open_id"])
    account.set_access_token(result["access_token"]); account.set_refresh_token(result.get("refresh_token"))
    account.expires_at = result.get("expires_at"); account.refresh_expires_at = result.get("refresh_expires_at")
    account.scope = result.get("scope"); account.status = "active"; account.disconnected_at = None
    if info.get("success"):
        account.display_name = info.get("display_name"); account.avatar_url = info.get("avatar_url")
    db.session.add(account); db.session.commit()
    return redirect("/integrations/tiktok")


@tiktok_bp.get("/api/integrations/tiktok/accounts")
@login_required
def accounts():
    company = get_current_company()
    rows = TikTokOAuth.query.filter_by(company_id=company.id, user_id=current_user.id, status="active").all() if company else []
    return jsonify({"connected": bool(rows), "accounts": [_token_json(r) for r in rows]})


@tiktok_bp.post("/api/integrations/tiktok/disconnect")
@login_required
def disconnect():
    company, account = _account(request.json.get("account_id") if request.is_json and request.json else None)
    if not account:
        return jsonify({"success": False, "error": "TikTok account not connected"}), 404
    try:
        get_tiktok_service(company).revoke_token(account.get_access_token(), account.open_id)
    except Exception:
        pass
    account.status = "disconnected"; account.disconnected_at = datetime.utcnow(); db.session.commit()
    return jsonify({"success": True})


@tiktok_bp.get("/api/integrations/tiktok/creator-info")
@login_required
def creator_info():
    company, account = _account(request.args.get("account_id"))
    if not account: return jsonify({"success": False, "error": "Missing TikTok token"}), 404
    ok, err = _ensure_fresh(account, company)
    if not ok: return jsonify({"success": False, "error": err}), 401
    result = get_tiktok_service(company).query_creator_info(account.get_access_token())
    if result.get("success"):
        data = result.get("data", {})
        account.creator_info = data; account.creator_username = data.get("creator_username") or data.get("username"); account.creator_nickname = data.get("creator_nickname") or data.get("nickname"); account.avatar_url = data.get("creator_avatar_url") or data.get("avatar_url") or account.avatar_url
        db.session.commit()
    return jsonify(result)


def _validate_post(account, privacy):
    if not account.has_scope("video.publish"):
        return "TikTok account is missing required video.publish scope."
    if not account.creator_info:
        return "Creator info must be queried before posting."
    opts = account.creator_info.get("privacy_level_options") or account.creator_info.get("privacy_options") or []
    allowed = [o.get("privacy_level") if isinstance(o, dict) else o for o in opts]
    if allowed and privacy not in allowed:
        return "Invalid privacy level for this TikTok creator."
    return None


@tiktok_bp.post("/api/integrations/tiktok/posts/video")
@login_required
def post_video():
    company, account = _account(request.form.get("account_id") or (request.json or {}).get("account_id") if request.is_json else None)
    if not account: return jsonify({"success": False, "error": "Missing TikTok token"}), 404
    ok, err = _ensure_fresh(account, company)
    if not ok: return jsonify({"success": False, "error": err}), 401
    data = request.form if request.form else (request.json or {})
    privacy = data.get("privacy_level", "SELF_ONLY")
    invalid = _validate_post(account, privacy)
    if invalid: return jsonify({"success": False, "error": invalid}), 400
    post_info = {"title": data.get("title", "")[:150], "privacy_level": privacy, "disable_comment": str(data.get("disable_comment", False)).lower() == "true", "disable_duet": str(data.get("disable_duet", False)).lower() == "true", "disable_stitch": str(data.get("disable_stitch", False)).lower() == "true", "video_cover_timestamp_ms": int(data.get("video_cover_timestamp_ms", 1000))}
    source_type = data.get("source", "PULL_FROM_URL")
    if source_type == "FILE_UPLOAD":
        f = request.files.get("video")
        if not f or f.mimetype not in ("video/mp4", "application/octet-stream") or not secure_filename(f.filename).lower().endswith(".mp4"):
            return jsonify({"success": False, "error": "Only MP4/H.264 video uploads are supported."}), 400
        blob = f.read(); size = len(blob); chunk_size, count = chunk_plan(size)
        source_info = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}
    else:
        video_url = data.get("video_url")
        if not video_url or not is_allowed_media_url(video_url, company): return jsonify({"success": False, "error": "Video URL domain is not verified/allowed."}), 400
        source_info = {"source": "PULL_FROM_URL", "video_url": video_url}
    result = get_tiktok_service(company).init_video(account.get_access_token(), post_info, source_info)
    if not result.get("success"): return jsonify(result), 502
    d = result.get("data", {})
    if source_type == "FILE_UPLOAD" and d.get("upload_url"):
        svc = get_tiktok_service(company)
        for i in range(count):
            start = i * chunk_size; end = min(start + chunk_size, size) - 1
            svc.upload_video_chunk(d["upload_url"], blob[start:end+1], start, end, size)
    post = TikTokPost(company_id=company.id, user_id=current_user.id, tiktok_account_id=account.id, publish_id=d.get("publish_id"), media_type="video", source_type=source_type.lower(), title=post_info["title"], privacy_level=privacy, status="initialized", status_payload=d)
    db.session.add(post); db.session.commit()
    return jsonify({"success": True, "publish_id": post.publish_id, "post_id": post.id, "warning": "Unaudited TikTok clients may be restricted to private posting until audit approval."})


@tiktok_bp.post("/api/integrations/tiktok/posts/photo")
@login_required
def post_photo():
    company, account = _account((request.json or {}).get("account_id"))
    if not account: return jsonify({"success": False, "error": "Missing TikTok token"}), 404
    ok, err = _ensure_fresh(account, company)
    if not ok: return jsonify({"success": False, "error": err}), 401
    data = request.json or {}; privacy = data.get("privacy_level", "SELF_ONLY")
    invalid = _validate_post(account, privacy)
    if invalid: return jsonify({"success": False, "error": invalid}), 400
    images = data.get("photo_images") or []
    if not images or any(not is_allowed_media_url(u, company) for u in images): return jsonify({"success": False, "error": "Photo URL domain is not verified/allowed."}), 400
    result = get_tiktok_service(company).init_photo(account.get_access_token(), {"title": data.get("title", "")[:150], "description": data.get("description", ""), "privacy_level": privacy, "disable_comment": bool(data.get("disable_comment")), "auto_add_music": bool(data.get("auto_add_music"))}, {"source": "PULL_FROM_URL", "photo_cover_index": int(data.get("photo_cover_index", 0)), "photo_images": images})
    if not result.get("success"): return jsonify(result), 502
    d = result.get("data", {})
    post = TikTokPost(company_id=company.id, user_id=current_user.id, tiktok_account_id=account.id, publish_id=d.get("publish_id"), media_type="photo", source_type="pull_from_url", title=data.get("title"), description=data.get("description"), privacy_level=privacy, status="initialized", status_payload=d)
    db.session.add(post); db.session.commit()
    return jsonify({"success": True, "publish_id": post.publish_id, "post_id": post.id})


@tiktok_bp.get("/api/integrations/tiktok/posts/<publish_id>/status")
@login_required
def post_status(publish_id):
    company, account = _account(request.args.get("account_id"))
    post = TikTokPost.query.filter_by(company_id=company.id, publish_id=publish_id).first() if company else None
    if not account or not post or post.tiktok_account_id != account.id: return jsonify({"success": False, "error": "Post not found"}), 404
    ok, err = _ensure_fresh(account, company)
    if not ok: return jsonify({"success": False, "error": err}), 401
    result = get_tiktok_service(company).fetch_status(account.get_access_token(), publish_id)
    if result.get("success"):
        data = result.get("data", {}); post.status = data.get("status", post.status); post.status_payload = data; db.session.add(TikTokPostStatusHistory(tiktok_post_id=post.id, status=post.status, payload=data)); db.session.commit()
    return jsonify(result)


@tiktok_bp.get("/integrations/tiktok")
@login_required
def tiktok_page():
    return render_template("tiktok_integration.html")

# Backward-compatible aliases
@tiktok_bp.get("/auth/tiktok/connect")
@login_required
def connect_alias(): return redirect("/api/oauth/tiktok/start")
@tiktok_bp.get("/auth/tiktok/status")
@login_required
def status_alias(): return accounts()
@tiktok_bp.post("/auth/tiktok/disconnect")
@login_required
def disconnect_alias(): return disconnect()
