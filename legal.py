"""Legal pages blueprint - Privacy, Terms, Data Deletion."""
import hmac
import hashlib
import base64
import uuid
import logging
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, current_app

legal_bp = Blueprint("legal", __name__)

logger = logging.getLogger(__name__)

LAST_UPDATED = "2026-02-27"


@legal_bp.get("/privacy")
def privacy():
    return render_template("legal/privacy.html", updated=LAST_UPDATED)


@legal_bp.get("/terms")
def terms():
    return render_template("legal/terms.html", updated=LAST_UPDATED)


@legal_bp.route("/data-deletion", methods=["GET", "POST"])
def data_deletion():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        details = (request.form.get("details") or "").strip()
        request_id = str(uuid.uuid4())

        try:
            from extensions import db
            from models import DeletionRequest
            record = DeletionRequest(
                request_id=request_id,
                email=email,
                details=details,
            )
            db.session.add(record)
            db.session.commit()
        except Exception as e:
            logger.warning("Could not store deletion request in DB: %s", e)

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

        confirmation_code = str(uuid.uuid4())
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
