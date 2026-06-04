"""
Regression tests for Communications Hub access control and auto-reply safety.

Covers:
- PWA settings sheet must not expose Twilio admin credentials
- comms_settings.html must not expose raw Twilio credentials
- UserCompanyAccess model has the new feature-toggle columns
- has_mobile_inbox_access() / has_comms_hub_access() logic
- comms_settings route requires admin (403 for non-admin)
- auto-reply skips when automation_enabled=False
- PWA template structure is correct (no admin routes)
"""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

ROOT = Path(__file__).resolve().parents[1]


# ────────────────────────────────────────────────────────────────────────────
# 1. Template safety: PWA settings must not expose Twilio admin fields
# ────────────────────────────────────────────────────────────────────────────

TWILIO_ADMIN_STRINGS = [
    "account_sid",
    "auth_token",
    "Account SID",
    "Auth Token",
    "Twilio Phone Number",
    "call_forward_to",
    "sms_forward_to",
    "Messaging Service SID",
    "auto-reply",
    "Auto-Reply",
    "business hours",
    "Business Hours",
    "voicemail routing",
    "Voicemail Routing",
    "AI routing",
]


def test_pwa_settings_sheet_has_no_twilio_admin_fields():
    """PWA settings sheet must never expose Twilio admin credentials or routing."""
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    # Find the settings sheet section
    start = html.find('id="settingsSheet"')
    assert start != -1, "settingsSheet element not found in PWA template"

    # Extract just the settings sheet div (rough slice — enough to detect leaks)
    end   = html.find('id="renameModal"', start)
    sheet = html[start:end] if end != -1 else html[start:start + 5000]

    for bad in TWILIO_ADMIN_STRINGS:
        assert bad not in sheet, (
            f"PWA settings sheet must not contain admin string: {bad!r}"
        )


def test_pwa_settings_has_account_logout_notifications_app():
    """PWA settings must contain exactly the four allowed sections."""
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()
    start = html.find('id="settingsSheet"')
    end   = html.find('id="renameModal"', start)
    sheet = html[start:end] if end != -1 else html[start:start + 5000]

    assert "Account" in sheet,       "Settings must have Account section"
    assert "Notifications" in sheet, "Settings must have Notifications section"
    assert "App" in sheet,           "Settings must have App section"
    # Sound/vibrate/push toggles
    assert "toggleSound"   in sheet, "Must have sound toggle"
    assert "toggleVibrate" in sheet, "Must have vibrate toggle"
    assert "pushToggleBtn" in sheet, "Must have push toggle"
    # Sign-out link
    assert "/logout" in sheet or "logout" in sheet, "Must have sign-out link"


# ────────────────────────────────────────────────────────────────────────────
# 2. comms_settings.html must not expose raw credentials
# ────────────────────────────────────────────────────────────────────────────

def test_comms_settings_template_has_no_raw_credentials():
    """comms_settings.html must never print raw auth tokens."""
    html = (ROOT / "templates" / "twilio" / "comms_settings.html").read_text()

    # auth_token must never be printed — _account_sid only as masked partial
    assert "auth_token" not in html,       "comms_settings must not expose auth_token field"
    assert "get_auth_token" not in html,   "comms_settings must not call get_auth_token()"
    # Should delegate credential entry to the existing settings page
    assert "comms_settings" in html or "Communications Settings" in html


def test_comms_settings_template_has_user_access_table():
    """comms_settings.html should render a user access section."""
    html = (ROOT / "templates" / "twilio" / "comms_settings.html").read_text()
    assert "users_with_access" in html or "User Access" in html, (
        "comms_settings must include user access/license management"
    )


# ────────────────────────────────────────────────────────────────────────────
# 3. Model: new feature-toggle columns exist on UserCompanyAccess
# ────────────────────────────────────────────────────────────────────────────

def test_user_company_access_has_comms_feature_flag_columns():
    """All new comms feature-toggle columns must be declared in the model."""
    src = (ROOT / "models.py").read_text()

    expected_columns = [
        "comms_hub_enabled",
        "pwa_access_enabled",
        "calls_enabled",
        "sms_enabled",
        "voicemail_enabled",
        "ai_comms_enabled",
        "forwarding_enabled",
        "communications_license",
        "assigned_number",
        "number_type",
    ]
    for col in expected_columns:
        assert col in src, f"UserCompanyAccess must declare column: {col}"


def test_user_company_access_has_comms_hub_access_method():
    """has_comms_hub_access() must be defined."""
    src = (ROOT / "models.py").read_text()
    assert "has_comms_hub_access" in src, "UserCompanyAccess must define has_comms_hub_access()"


# ────────────────────────────────────────────────────────────────────────────
# 4. Model: access-control logic is correct (no DB needed — pure Python)
# ────────────────────────────────────────────────────────────────────────────

def _make_access(role="viewer", **kwargs):
    """Build a lightweight UserCompanyAccess-like object without a real DB."""
    # We import models to get the real class but don't need a live DB session
    # because we set attributes directly on the instance.
    from models import UserCompanyAccess
    acc = UserCompanyAccess.__new__(UserCompanyAccess)
    acc.role                   = role
    acc.can_access_mobile_inbox = kwargs.get("can_access_mobile_inbox", False)
    acc.can_access_full_app     = kwargs.get("can_access_full_app", True)
    acc.comms_hub_enabled       = kwargs.get("comms_hub_enabled", False)
    acc.pwa_access_enabled      = kwargs.get("pwa_access_enabled", False)
    acc.communications_license  = kwargs.get("communications_license", False)
    acc.calls_enabled           = kwargs.get("calls_enabled", True)
    acc.sms_enabled             = kwargs.get("sms_enabled", True)
    acc.voicemail_enabled       = kwargs.get("voicemail_enabled", False)
    acc.ai_comms_enabled        = kwargs.get("ai_comms_enabled", False)
    acc.forwarding_enabled      = kwargs.get("forwarding_enabled", False)
    return acc


def test_admin_always_has_pwa_access():
    acc = _make_access(role="admin")
    assert acc.has_mobile_inbox_access() is True


def test_owner_always_has_pwa_access():
    acc = _make_access(role="owner")
    assert acc.has_mobile_inbox_access() is True


def test_viewer_without_flags_has_no_pwa_access():
    acc = _make_access(role="viewer")
    assert acc.has_mobile_inbox_access() is False


def test_viewer_with_pwa_access_enabled():
    acc = _make_access(role="viewer", pwa_access_enabled=True)
    assert acc.has_mobile_inbox_access() is True


def test_viewer_with_legacy_can_access_mobile_inbox():
    acc = _make_access(role="viewer", can_access_mobile_inbox=True)
    assert acc.has_mobile_inbox_access() is True


def test_admin_always_has_comms_hub_access():
    acc = _make_access(role="admin")
    assert acc.has_comms_hub_access() is True


def test_viewer_without_comms_flag_has_no_hub_access():
    acc = _make_access(role="viewer")
    assert acc.has_comms_hub_access() is False


def test_viewer_with_comms_hub_enabled():
    acc = _make_access(role="viewer", comms_hub_enabled=True)
    assert acc.has_comms_hub_access() is True


def test_viewer_with_communications_license():
    acc = _make_access(role="viewer", communications_license=True)
    assert acc.has_comms_hub_access() is True


def test_inbox_only_role_has_no_hub_access_by_default():
    acc = _make_access(role="inbox_only")
    assert acc.has_comms_hub_access() is False


def test_inbox_only_with_pwa_flag_gets_pwa_access():
    acc = _make_access(role="inbox_only", pwa_access_enabled=True)
    assert acc.has_mobile_inbox_access() is True


# ────────────────────────────────────────────────────────────────────────────
# 5. comms_settings route requires admin (template-level smoke check)
# ────────────────────────────────────────────────────────────────────────────

def test_comms_settings_route_guards_in_blueprint():
    """Verify the comms_settings route in twilio_sms.py has an admin guard."""
    src = (ROOT / "twilio_sms.py").read_text()
    # The route definition must exist
    assert '"/comms/settings"' in src or "comms_settings" in src, (
        "/twilio/comms/settings route must be defined"
    )
    # Must contain a 403 abort for non-admins
    assert "abort(403)" in src or "403" in src, (
        "comms_settings must abort(403) for non-admin users"
    )


# ────────────────────────────────────────────────────────────────────────────
# 6. Auto-reply: automation_enabled=False must skip rules
# ────────────────────────────────────────────────────────────────────────────

def test_auto_reply_skips_when_automation_disabled():
    """_apply_auto_reply_rules must return False when automation_enabled=False."""
    from twilio_sms import _apply_auto_reply_rules

    ta = MagicMock()
    ta.automation_enabled = False
    ta.company_id         = 1
    ta.after_hours_sms_enabled = True

    conv = MagicMock()
    conv.is_opted_out    = False
    conv.is_first_contact = False
    conv.from_number     = "+15551234567"
    conv.id              = 1

    result = _apply_auto_reply_rules(conv, "Hello", ta)
    assert result is False, "Should return False when automation disabled"


def test_auto_reply_skips_when_opted_out():
    """_apply_auto_reply_rules must return False for opted-out contacts."""
    from twilio_sms import _apply_auto_reply_rules

    ta = MagicMock()
    ta.automation_enabled = True
    ta.company_id         = 1

    conv = MagicMock()
    conv.is_opted_out    = True
    conv.from_number     = "+15551234567"
    conv.id              = 1

    result = _apply_auto_reply_rules(conv, "Hello", ta)
    assert result is False, "Should return False when contact is opted out"


def test_auto_reply_skips_when_no_active_rules(monkeypatch):
    """_apply_auto_reply_rules must return False when no active rules exist."""
    from twilio_sms import _apply_auto_reply_rules

    ta = MagicMock()
    ta.automation_enabled      = True
    ta.company_id              = 99
    ta.after_hours_sms_enabled = True

    conv = MagicMock()
    conv.is_opted_out     = False
    conv.is_first_contact = False
    conv.from_number      = "+15551234567"
    conv.id               = 1

    # Patch AutoReplyRule.query so it returns an empty list
    mock_query = MagicMock()
    mock_query.filter_by.return_value.order_by.return_value.all.return_value = []

    import models
    monkeypatch.setattr(models.AutoReplyRule, "query", mock_query)

    result = _apply_auto_reply_rules(conv, "Hello", ta)
    assert result is False


def test_auto_reply_fires_send_for_matching_rule(monkeypatch):
    """_apply_auto_reply_rules must call _send_sms when an 'always' rule matches."""
    from twilio_sms import _apply_auto_reply_rules
    import twilio_sms
    import models

    ta = MagicMock()
    ta.automation_enabled      = True
    ta.company_id              = 1
    ta.after_hours_sms_enabled = True

    conv = MagicMock()
    conv.is_opted_out     = False
    conv.is_first_contact = False
    conv.from_number      = "+15559876543"
    conv.id               = 42
    conv.tags             = []

    # Create a fake always-reply rule
    rule = MagicMock()
    rule.id           = 1
    rule.name         = "Test Always Rule"
    rule.trigger_type = "always"
    rule.action       = "reply"
    rule.response     = "Thanks for texting us!"
    rule.keywords     = []
    rule.active_days  = None
    rule.active_hours_start = None
    rule.active_hours_end   = None
    rule.priority     = 10
    rule.match_count  = 0

    mock_query = MagicMock()
    mock_query.filter_by.return_value.order_by.return_value.all.return_value = [rule]
    monkeypatch.setattr(models.AutoReplyRule, "query", mock_query)

    send_calls = []

    def fake_send(ta, to, body, **kwargs):
        send_calls.append({"to": to, "body": body, **kwargs})
        return {"success": True, "sid": "SMfake123"}

    monkeypatch.setattr(twilio_sms, "_send_sms", fake_send)

    result = _apply_auto_reply_rules(conv, "Hello", ta)

    assert result is True, "Should return True when a reply rule fires"
    assert len(send_calls) == 1, "Should call _send_sms exactly once"
    assert send_calls[0]["to"] == "+15559876543"
    assert send_calls[0]["body"] == "Thanks for texting us!"
    assert send_calls[0].get("is_auto_reply") is True
