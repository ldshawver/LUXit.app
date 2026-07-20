from services.contact_identity import extract_identity, should_request_identity
from services.phone_normalization import normalize_phone_e164


def test_identity_parser_extracts_email_before_conservative_name():
    assert extract_identity("Jane Q. Public, jane.public@example.com") == (
        "Jane", "Q Public", "jane.public@example.com", []
    )


def test_identity_parser_reports_each_missing_component():
    first, last, email, missing = extract_identity("Jane")
    assert (first, last, email) == (None, None, None)
    assert missing == ["first and last name", "valid email address"]


def test_canonical_phone_rejects_impossible_number_and_accepts_reserved_example():
    assert normalize_phone_e164("(202) 555-0123") == "+12025550123"
    assert normalize_phone_e164("+1 555 000 0000") == ""


class _Contact:
    identity_status = "pending_identity"
    google_match_status = "not_checked"
    identity_request_count = 0
    identity_requested_at = None
    sms_opted_out = False
    do_not_sms = False
    do_not_contact = False


def test_identity_request_suppression_and_attempt_limit():
    contact = _Contact()
    assert should_request_identity(contact)
    contact.sms_opted_out = True
    assert not should_request_identity(contact)
    contact.sms_opted_out = False
    contact.identity_request_count = 3
    assert not should_request_identity(contact)
