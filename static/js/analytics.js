(() => {
  const consent = localStorage.getItem("lux_consent");
  if (consent !== "true") {
    return;
  }

  const sessionKey = "lux_session_id";
  let sessionId = localStorage.getItem(sessionKey);
  if (!sessionId) {
    sessionId = `sess_${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(sessionKey, sessionId);
  }

  const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
  const orientation =
    window.matchMedia && window.matchMedia("(orientation: portrait)").matches ? "portrait" : "landscape";

  const payload = {
    company_id: window.LUX_COMPANY_ID,
    event_name: "page_view",
    consent: true,
    session_id: sessionId,
    page_url: window.location.href,
    referrer: document.referrer || null,
    device_type: viewportWidth < 768 ? "mobile" : viewportWidth < 1024 ? "tablet" : "desktop",
    viewport_width: viewportWidth,
    orientation,
  };

  if (!payload.company_id) {
    return;
  }

  fetch("/e", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
    keepalive: true,
  }).catch(() => {});
})();
