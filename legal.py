"""Legal pages blueprint - Privacy, Terms, Data Deletion."""
import hmac
import hashlib
import base64
import json
import uuid
import os
import logging
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, current_app
from extensions import csrf

legal_bp = Blueprint("legal", __name__)

logger = logging.getLogger(__name__)

LAST_UPDATED = "April 26, 2026"


@legal_bp.get("/privacy")
def privacy():
    return render_template("legal/privacy.html", updated=LAST_UPDATED)


@legal_bp.get("/privacy-policy")
def privacy_policy():
    return render_template("legal/privacy.html", updated=LAST_UPDATED)


@legal_bp.get("/terms")
def terms():
    return render_template("legal/terms.html", updated=LAST_UPDATED)


@legal_bp.get("/sms-consent")
def sms_consent():
    return render_template("legal/sms_consent.html", updated=LAST_UPDATED)


@legal_bp.route("/data-deletion", methods=["GET", "POST"])
def data_deletion():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        details = (request.form.get("details") or "").strip()
        request_id = str(uuid.uuid4())

        try:
            from extensions import db
            from models import DeletionRequest, User, FacebookOAuth, CompanySecret

            # Record the request
            record = DeletionRequest(
                request_id=request_id,
                email=email,
                details=details,
            )
            db.session.add(record)

            # Delete Facebook OAuth records tied to this email
            user = User.query.filter_by(email=email).first()
            if user:
                deleted_fb = FacebookOAuth.query.filter_by(user_id=user.id).all()
                for fb in deleted_fb:
                    company_id = fb.company_id
                    db.session.delete(fb)
                    # Remove stored page tokens for all companies this user belonged to
                    page_token_secret = CompanySecret.query.filter_by(
                        company_id=company_id,
                        key="facebook_page_tokens"
                    ).first()
                    if page_token_secret:
                        db.session.delete(page_token_secret)

            db.session.commit()
            logger.info("Deletion request %s: purged Facebook data for %s", request_id, email)
        except Exception as e:
            logger.warning("Could not complete deletion for %s: %s", email, e)

        return render_template(
            "legal/data_deletion_submitted.html",
            updated=LAST_UPDATED,
            request_id=request_id,
            email=email,
            details=details,
            submitted_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    return render_template("legal/data_deletion.html", updated=LAST_UPDATED)


@legal_bp.post("/meta/data-deletion")
@csrf.exempt
def meta_data_deletion_callback():
    """Meta signed-request data deletion callback."""
    signed_request = request.form.get("signed_request", "")
    if not signed_request:
        return jsonify({"error": "missing signed_request"}), 400

    try:
        encoded_sig, payload = signed_request.split(".", 1)
        sig = base64.urlsafe_b64decode(encoded_sig + "==")
        expected = hmac.new(
            current_app.config.get("META_APP_SECRET", "").encode("utf-8"),
            msg=payload.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()

        if not hmac.compare_digest(sig, expected):
            return jsonify({"error": "invalid signature"}), 403

        # Decode payload and extract Meta user ID (ASID)
        import json as _json
        payload_data = _json.loads(base64.urlsafe_b64decode(payload + "=="))
        meta_user_id = str(payload_data.get("user_id", ""))

        confirmation_code = str(uuid.uuid4())

        # Delete all Facebook OAuth data stored for this ASID
        if meta_user_id:
            try:
                from extensions import db
                from models import FacebookOAuth, CompanySecret, DeletionRequest
                records = FacebookOAuth.query.filter_by(facebook_user_id=meta_user_id).all()
                for fb in records:
                    page_tokens = CompanySecret.query.filter_by(
                        company_id=fb.company_id,
                        key="facebook_page_tokens"
                    ).first()
                    if page_tokens:
                        db.session.delete(page_tokens)
                    db.session.delete(fb)
                dr = DeletionRequest(
                    request_id=confirmation_code,
                    email=f"meta_asid:{meta_user_id}",
                    details="Triggered by Meta signed data deletion callback",
                    status="completed",
                )
                db.session.add(dr)
                db.session.commit()
                logger.info("Meta callback: deleted data for ASID %s", meta_user_id)
            except Exception as e:
                logger.error("Meta callback DB error: %s", e)

        status_url = f"https://luxit.app/data-deletion/status/{confirmation_code}"
        return jsonify({
            "url": status_url,
            "confirmation_code": confirmation_code,
        })
    except Exception:
        return jsonify({"error": "bad signed_request"}), 400


@legal_bp.get("/data-deletion/status/<code>")
def data_deletion_status(code):
    return render_template("legal/data_deletion_status.html", code=code, updated=LAST_UPDATED)


# ---------------------------------------------------------------------------
# Meta Webhooks endpoint
# GET  /webhooks/meta  — hub verification challenge (required by Meta)
# POST /webhooks/meta  — receive real-time event payloads
# ---------------------------------------------------------------------------

@legal_bp.route("/webhooks/meta", methods=["GET", "POST"])
@csrf.exempt
def meta_webhook():
    """
    Meta webhook receiver.
    GET  → hub.challenge verification (required when setting up in Meta dashboard)
    POST → incoming event payloads (page events, leads, etc.)
    """
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        # Fail closed: with no verify token configured the endpoint cannot be
        # verified. Never fall back to a guessable in-source constant.
        expected_token = (
            os.environ.get("META_WEBHOOK_VERIFY_TOKEN")
            or os.environ.get("META_VERIFY_TOKEN")
            or ""
        ).strip()
        if not expected_token:
            logger.error("Meta webhook GET rejected: no verify token configured")
            return jsonify({"error": "verification not configured"}), 503

        if mode == "subscribe" and token and hmac.compare_digest(token, expected_token):
            logger.info("Meta webhook verified successfully")
            return challenge, 200, {"Content-Type": "text/plain"}

        logger.warning("Meta webhook verification failed: token mismatch or bad mode")
        return jsonify({"error": "verification failed"}), 403

    # POST — incoming event payload.
    # Machine-to-machine: X-Hub-Signature-256 verification is MANDATORY and
    # fail-closed. A missing app secret is a configuration error (503), never
    # "auth disabled"; a missing or invalid signature is rejected (403) BEFORE
    # the payload is parsed or routed to any handler.
    app_secret = (os.environ.get("META_APP_SECRET") or "").strip()
    if not app_secret:
        logger.error("Meta webhook POST rejected: META_APP_SECRET is not configured")
        return jsonify({"error": "webhook authentication is not configured"}), 503

    raw_body = request.get_data()
    sig_header = request.headers.get("X-Hub-Signature-256", "") or ""
    expected_sig = "sha256=" + hmac.new(
        app_secret.encode(),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    if not sig_header or not hmac.compare_digest(sig_header, expected_sig):
        logger.warning("Meta webhook POST rejected: missing or invalid X-Hub-Signature-256")
        return jsonify({"error": "invalid signature"}), 403

    try:
        payload = json.loads(raw_body or b"{}") or {}
        logger.info("Meta webhook event received: object=%s", payload.get("object"))
        # TODO: route payload to relevant handlers (lead gen, page events, etc.)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("Meta webhook error: %s", e)
        return jsonify({"error": "internal error"}), 500
