"""Public email unsubscribe page: GET confirms, POST applies.

POST is CSRF-exempt on purpose: mailbox providers send RFC 8058 one-click
unsubscribes (List-Unsubscribe-Post) with no session or form token. The signed
link itself is the authorization, and it can only ever opt that one contact out.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from extensions import csrf
from services.email_unsubscribe import apply_unsubscribe, resolve_token

email_unsubscribe_bp = Blueprint("email_unsubscribe", __name__)


def _client_ip():
    xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or request.remote_addr


@email_unsubscribe_bp.get("/email/unsubscribe/<token>")
def confirm(token: str):
    contact = resolve_token(token)
    if contact is None:
        return render_template("email_unsubscribe/invalid.html"), 404
    return render_template("email_unsubscribe/confirm.html", token=token, done=False)


@email_unsubscribe_bp.post("/email/unsubscribe/<token>")
@csrf.exempt
def unsubscribe(token: str):
    contact = resolve_token(token)
    if contact is None:
        return render_template("email_unsubscribe/invalid.html"), 404
    one_click = request.form.get("List-Unsubscribe") == "One-Click"
    apply_unsubscribe(contact, source="email_link_one_click" if one_click else "email_link",
                      request_ip=_client_ip())
    return render_template("email_unsubscribe/confirm.html", token=token, done=True)
