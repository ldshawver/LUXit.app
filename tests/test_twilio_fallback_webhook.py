"""P3 regression — /twilio/fallback must always answer Twilio.

Prod defect 2026-09-08: /twilio/fallback was not CSRF-exempt. Twilio's POST hit
Flask-WTF CSRF -> CSRFError -> the app handler 302-redirected -> Twilio followed
with a GET to the POST-only /twilio/voice/inbound -> 405. When the primary voice
webhook timed out, the caller got an unrecoverable failure instead of a graceful
"try again" message.
"""
import pytest

from app import create_app
from extensions import db


@pytest.fixture
def fb_app():
    app = create_app()
    # CSRF stays ENABLED here on purpose — the route's own @csrf.exempt is what
    # must carry it, exactly as Twilio hits it in production.
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=True,
                      SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        yield app, app.test_client()
        db.session.remove(); db.drop_all()


def _assert_valid_voice_twiml(body):
    assert body.strip().startswith("<?xml")
    assert "<Response>" in body and "</Response>" in body
    assert "Traceback" not in body and "<html" not in body.lower()


def test_fallback_post_no_csrf_token_is_not_blocked(fb_app):
    _, client = fb_app
    resp = client.post("/twilio/fallback", data={
        "CallSid": "CA_FB_1", "ErrorCode": "11200",
        "ErrorUrl": "https://luxit.app/twilio/voice/inbound",
        "To": "+18305550123", "From": "+14155551212",
    })
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/xml")
    body = resp.get_data(as_text=True)
    _assert_valid_voice_twiml(body)
    assert "<Say>" in body and "<Hangup/>" in body


def test_fallback_get_does_not_405_loop(fb_app):
    _, client = fb_app
    resp = client.get("/twilio/fallback?CallSid=CA_FB_2&ErrorCode=11200")
    assert resp.status_code == 200
    _assert_valid_voice_twiml(resp.get_data(as_text=True))


def test_fallback_sms_context_returns_empty_response(fb_app):
    _, client = fb_app
    resp = client.post("/twilio/fallback", data={
        "MessageSid": "SM_FB_1", "ErrorCode": "11200",
    })
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<Response></Response>" in body.replace("\n", "")
    assert "<Say>" not in body


def test_fallback_never_leaks_secrets_or_stack(fb_app):
    _, client = fb_app
    resp = client.post("/twilio/fallback", data={"CallSid": "CA_FB_3"})
    body = resp.get_data(as_text=True)
    for needle in ("SECRET", "AUTH_TOKEN", "Traceback", "sqlite", "psycopg"):
        assert needle not in body


def test_primary_voice_route_still_rejects_get(fb_app):
    """The fix is on /twilio/fallback, not by loosening the primary route."""
    _, client = fb_app
    assert client.get("/twilio/voice/inbound").status_code == 405


def test_fallback_route_is_registered_csrf_exempt(fb_app):
    app, _ = fb_app
    assert app.view_functions.get("twilio.twilio_fallback") is not None
    # Flask-WTF records exempt views as "<module>.<func>" strings.
    from extensions import csrf
    assert "twilio_sms.twilio_fallback" in getattr(csrf, "_exempt_views", set()), \
        "twilio_fallback is not registered CSRF-exempt"
