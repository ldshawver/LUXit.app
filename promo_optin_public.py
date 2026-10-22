"""Public, unauthenticated hosted promotional opt-in consent page.

A signed per-contact link (``/promo-optin/<token>``) renders the MyOrder.fun
"Text Specials" consent page with an initially-unchecked disclosure box. An
affirmative submission grants promotional SMS consent through the existing
service (services.promotional_optin.record_web_optin) — recording immutable
evidence, the exact disclosure version/text, and request context — and makes
the customer promotional-campaign eligible immediately.

No SMS is ever sent to deliver this link. The link is generated for an operator
to share out-of-band (checkout, QR, printed material, customer account, other
independently permitted channels).
"""
from __future__ import annotations

import logging

from flask import Blueprint, render_template, request, abort

from extensions import db, csrf
from services.promotional_optin import (
    get_web_optin_context, record_web_optin, WEB_OPTIN_DISCLOSURE_VERSION,
)

logger = logging.getLogger(__name__)

promo_optin_public_bp = Blueprint("promo_optin_public", __name__)


def _client_ip() -> str | None:
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or request.remote_addr


@promo_optin_public_bp.get("/promo-optin/<token>")
def consent_page(token: str):
    ctx = get_web_optin_context(token)
    if not ctx.get("ok"):
        # Do not leak why (unknown vs tampered vs phone-changed) to the public.
        return render_template("promo_optin/invalid.html"), 404
    if ctx.get("closed"):
        # The link's opportunity was closed (STOP, operator cancel, superseded).
        # Never offer a consent control on a stale link.
        return render_template("promo_optin/invalid.html"), 404
    return render_template(
        "promo_optin/consent.html",
        token=token,
        brand=ctx["brand"],
        disclosure_text=ctx["disclosure_text"],
        disclosure_version=ctx["disclosure_version"],
        first_name=ctx.get("first_name"),
        phone_hint=ctx.get("phone_hint"),
        already_consented=ctx.get("already_consented"),
        suppressed=ctx.get("suppressed"),
        submitted=False,
    )


@promo_optin_public_bp.post("/promo-optin/<token>")
@csrf.exempt
def consent_submit(token: str):
    ctx = get_web_optin_context(token)
    if not ctx.get("ok") or ctx.get("closed"):
        return render_template("promo_optin/invalid.html"), 404

    agreed = (request.form.get("agree") or "").strip().lower() in ("on", "true", "yes", "1")
    posted_version = (request.form.get("disclosure_version") or "").strip() or None

    if not agreed:
        return render_template(
            "promo_optin/consent.html",
            token=token, brand=ctx["brand"],
            disclosure_text=ctx["disclosure_text"],
            disclosure_version=ctx["disclosure_version"],
            first_name=ctx.get("first_name"), phone_hint=ctx.get("phone_hint"),
            already_consented=ctx.get("already_consented"),
            suppressed=ctx.get("suppressed"),
            submitted=False,
            error="Please check the box to agree before submitting.",
        ), 400

    result = record_web_optin(
        token,
        disclosure_version=posted_version,
        consent_ip=_client_ip(),
        user_agent=request.headers.get("User-Agent"),
        page_url=request.url,
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("promo web opt-in commit failed")
        abort(500)

    if not result.get("ok"):
        if result.get("error") == "stale_disclosure":
            # Re-render with the current disclosure so the customer re-affirms.
            fresh = get_web_optin_context(token)
            return render_template(
                "promo_optin/consent.html",
                token=token, brand=fresh["brand"],
                disclosure_text=fresh["disclosure_text"],
                disclosure_version=fresh["disclosure_version"],
                first_name=fresh.get("first_name"), phone_hint=fresh.get("phone_hint"),
                already_consented=fresh.get("already_consented"),
                suppressed=fresh.get("suppressed"),
                submitted=False,
                error="Our terms were updated. Please review and agree again.",
            ), 409
        return render_template("promo_optin/invalid.html"), 400

    if result.get("closed"):
        # Link closed between page load and submit (race with STOP / cancel).
        return render_template("promo_optin/invalid.html"), 404

    state = "suppressed" if result.get("suppressed") else (
        "already" if result.get("already") else (
            "duplicate" if result.get("duplicate") else "granted"))
    return render_template(
        "promo_optin/consent.html",
        token=token, brand=ctx["brand"],
        disclosure_text=ctx["disclosure_text"],
        disclosure_version=ctx["disclosure_version"],
        first_name=ctx.get("first_name"), phone_hint=ctx.get("phone_hint"),
        submitted=True, result_state=state,
    )
