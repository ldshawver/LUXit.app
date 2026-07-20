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


def test_pwa_keyboard_viewport_contract():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()
    nav = (ROOT / "templates" / "inbox_pwa" / "_bottom_nav.html").read_text()

    assert "window.visualViewport" in html
    assert "--vvh" in html
    assert "100dvh" in html
    assert "env(safe-area-inset-bottom" in html
    assert "scrollIntoView" not in html
    assert "pwa-keyboard-open" in html
    assert "body.pwa-keyboard-open .pwa-bottom-nav" in html
    assert "body.has-pwa-bottom-nav.pwa-keyboard-open #shell" in nav
    assert "body.has-pwa-bottom-nav #shell { bottom:" not in nav


def test_pwa_external_links_render_safely_with_new_tab_warning():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    assert "function renderMessageText" in html
    assert "target=\"_blank\"" in html
    assert "rel=\"noopener noreferrer\"" in html
    assert "External site controls any username or password prompt" in html


def test_pwa_twilio_media_urls_are_not_auto_embedded_from_provider():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    assert "function isProtectedProviderUrl" in html
    assert "host === 'api.twilio.com'" in html
    assert "host.endsWith('.twilio.com')" in html


def test_pwa_luxit_internal_links_use_signed_in_session_hint():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    assert "Open LUXit link with your current signed-in session" in html
    assert "window.location.origin" in html


def test_frontend_does_not_hard_code_twilio_credentials():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    forbidden = ["TWILIO_AUTH_TOKEN", "auth_token", "Account SID", "ACtest", "api.twilio.com/2010"]
    for value in forbidden:
        assert value not in html


def test_pwa_reconciles_granted_notification_permission_with_backend_subscription():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    assert "registerSW().then(() => reconcileGrantedPushPermission())" in html
    assert "async function reconcileGrantedPushPermission()" in html
    assert "!browserSubscription || !matchingBackendSubscription" in html
    assert "subscription.endpoint_tail === endpointTail" in html
    assert "await ensurePushSubscription({sendTest:false})" in html


def test_automatic_push_reconciliation_does_not_send_a_test_alert():
    html = (ROOT / "templates" / "inbox_pwa" / "index.html").read_text()

    start = html.index("async function persistAndVerifyPushSubscription")
    end = html.index("let pushReconciliationPromise", start)
    persistence_body = html[start:end]
    assert "if (sendTest)" in persistence_body
    assert "pushApiJson('/api/pwa/push/test'" in persistence_body
