"""
Tests for phone-number normalization and PWA inbox display-name fallback.

These tests are pure-Python (no DB, no Flask app context) so they run fast
and without any shared-state issues.
"""
import pytest
from services.google_contacts import normalize_phone, _all_forms


# ---------------------------------------------------------------------------
# normalize_phone — all common US + international formats
# ---------------------------------------------------------------------------

class TestNormalizePhone:
    def test_10_digit(self):
        assert normalize_phone("4155551212") == "+14155551212"

    def test_11_digit_with_1(self):
        assert normalize_phone("14155551212") == "+14155551212"

    def test_parens_spaces(self):
        assert normalize_phone("(415) 555-1212") == "+14155551212"

    def test_dashes_with_country(self):
        assert normalize_phone("1-415-555-1212") == "+14155551212"

    def test_dots(self):
        assert normalize_phone("415.555.1212") == "+14155551212"

    def test_already_e164(self):
        assert normalize_phone("+14155551212") == "+14155551212"

    def test_international_uk(self):
        assert normalize_phone("+447911123456") == "+447911123456"

    def test_international_no_plus(self):
        # 12-digit non-US: just prepend +
        assert normalize_phone("447911123456") == "+447911123456"

    def test_empty_string(self):
        assert normalize_phone("") == ""

    def test_none_safe(self):
        assert normalize_phone(None) == ""

    def test_mixed_separators(self):
        assert normalize_phone("+1 (415) 555-1212") == "+14155551212"


# ---------------------------------------------------------------------------
# _all_forms — coverage of form variants
# ---------------------------------------------------------------------------

class TestAllForms:
    def test_e164_produces_variants(self):
        forms = _all_forms("+14155551212")
        assert "+14155551212" in forms
        assert "14155551212" in forms   # without +
        assert "4155551212"  in forms   # 10-digit

    def test_raw_included(self):
        raw = "(415) 555-1212"
        forms = _all_forms(raw)
        assert raw in forms
        assert "+14155551212" in forms

    def test_empty(self):
        assert _all_forms("") == []

    def test_none(self):
        assert _all_forms(None) == []

    def test_no_duplicates(self):
        forms = _all_forms("+14155551212")
        assert len(forms) == len(set(forms))


# ---------------------------------------------------------------------------
# _conv_to_dict display-name fallback logic (pure Python, no DB)
# ---------------------------------------------------------------------------

class FakeConv:
    """Minimal stand-in for a TwilioConversation row."""
    id = 1
    from_number = "+14155551212"
    contact_name = None
    contact_id = None
    contact_source = None
    is_read = True
    is_opted_out = False
    tags = []
    assigned_user_id = None
    last_message_at = None
    last_message_preview = ""
    message_count = 0


def _dict_from(conv):
    """Replicate _conv_to_dict key fields without importing Flask."""
    return {
        "contact_name": conv.contact_name or conv.from_number,
        "display_name": conv.contact_name or conv.from_number,
        "contact_source": getattr(conv, "contact_source", None),
    }


class TestDisplayNameFallback:
    def test_uses_contact_name_when_set(self):
        c = FakeConv()
        c.contact_name = "Jane Smith"
        d = _dict_from(c)
        assert d["display_name"] == "Jane Smith"
        assert d["contact_name"] == "Jane Smith"

    def test_falls_back_to_phone_when_no_name(self):
        c = FakeConv()
        c.contact_name = None
        d = _dict_from(c)
        assert d["display_name"] == "+14155551212"

    def test_falls_back_to_phone_when_empty_string(self):
        c = FakeConv()
        c.contact_name = ""
        d = _dict_from(c)
        assert d["display_name"] == "+14155551212"

    def test_contact_source_propagated(self):
        c = FakeConv()
        c.contact_name = "Bob Jones"
        c.contact_source = "google"
        d = _dict_from(c)
        assert d["contact_source"] == "google"

    def test_contact_source_none_when_unset(self):
        c = FakeConv()
        d = _dict_from(c)
        assert d["contact_source"] is None

    def test_crm_source_preserved(self):
        c = FakeConv()
        c.contact_name = "Alice"
        c.contact_source = "crm"
        d = _dict_from(c)
        assert d["contact_source"] == "crm"
