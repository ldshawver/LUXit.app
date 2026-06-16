from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_admin_communications_route_is_additive_and_backend_connected():
    route = (ROOT / "admin_communications.py").read_text()
    template = (ROOT / "templates" / "admin" / "communications.html").read_text()

    assert '/admin/communications' in route
    assert 'PhoneSettings' in route
    assert 'TwilioCallLog' in route
    assert 'VoiceVoicemailMessage' in route
    assert '/api/phone/settings' in template
    assert '/api/calls/${id}/${action}' in template
    assert "url_for('inbox_pwa.pwa_calls')" in template


def test_global_admin_integrations_alias_and_sidebar_links_exist():
    integrations = (ROOT / "integrations_bp.py").read_text()
    sidebar = (ROOT / "templates" / "base.html").read_text()

    assert '/global-admin/integrations' in integrations
    assert 'SMS &amp; Calls Admin' in sidebar
    assert "admin_communications.communications_admin" in sidebar
    assert '/global-admin/integrations' in sidebar


def test_reconciled_admin_template_contains_requested_sections():
    template = (ROOT / "templates" / "admin" / "communications.html").read_text()

    for label in [
        'Auto Reply Rules',
        'SMS Campaigns',
        'Routing &amp; timed forwarding',
        'Missed-call and after-hours SMS',
        'Multi-number management',
        'Voicemail Inbox Controls',
        'Filter',
    ]:
        assert label in template
