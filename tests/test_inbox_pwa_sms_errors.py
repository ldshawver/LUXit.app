from types import SimpleNamespace

from flask import Flask

from inbox_pwa import _is_mobile_request, _twilio_send_error_message


def test_twilio_send_error_message_maps_opt_out_code():
    exc = SimpleNamespace(code=21610)

    assert _twilio_send_error_message(exc) == (
        "This customer has opted out. They must reply START before texts can be sent again."
    )


def test_twilio_send_error_message_explains_trial_unverified_numbers():
    exc = Exception("The number +15551234567 is unverified. Trial accounts cannot message it.")

    assert _twilio_send_error_message(exc) == (
        "Twilio trial accounts can only text verified recipient numbers. "
        "Verify this customer in Twilio or upgrade the account."
    )


def test_twilio_send_error_message_explains_http_403():
    exc = SimpleNamespace(code=None, status=403)

    assert _twilio_send_error_message(exc).startswith(
        "Twilio rejected the request with HTTP 403."
    )


def test_twilio_send_error_message_explains_forbidden_record_text():
    exc = Exception("Unable to create record: Forbidden")

    assert _twilio_send_error_message(exc).startswith(
        "Twilio rejected the request with HTTP 403."
    )


def test_mobile_request_detection_accepts_phone_user_agent():
    app = Flask(__name__)

    with app.test_request_context(headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}):
        assert _is_mobile_request() is True


def test_mobile_request_detection_rejects_desktop_user_agent():
    app = Flask(__name__)

    with app.test_request_context(headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}):
        assert _is_mobile_request() is False
