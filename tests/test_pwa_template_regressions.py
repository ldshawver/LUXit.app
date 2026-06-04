from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pwa_call_button_uses_twilio_api_not_native_tel():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    assert 'id="callBtn"' in html
    assert 'href="tel:' not in html
    assert "/api/inbox/conversations/${state.activeConvId}/call" in html


def test_pwa_message_bubbles_avoid_character_stacking_regression():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    assert "word-break: normal" in html
    assert "overflow-wrap: break-word" in html
    assert "function normalizeSmsBody" in html


def test_desktop_sidebar_shows_communications_hub():
    html = (ROOT / "templates" / "base.html").read_text()

    assert "Mobile Inbox PWA" not in html
    assert "Communications Hub" in html
    assert "comms_hub" in html
