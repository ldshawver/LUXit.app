"""
Run on the luxit production server:
  python3 /tmp/luxit_legal.py

Adds /sms-consent and /privacy-policy routes to the live app,
writes the HTML templates, and restarts the app process.
"""
import os, re, subprocess
from pathlib import Path

APP_DIR   = Path("/opt/email-marketing")
LEGAL_PY  = APP_DIR / "legal.py"
TMPL_DIR  = APP_DIR / "templates" / "legal"

# ── Colours ───────────────────────────────────────────────────────────────────
OK   = lambda s: print(f"\033[32m  ✓  {s}\033[0m")
ERR  = lambda s: print(f"\033[31m  ✗  {s}\033[0m")
INFO = lambda s: print(f"     {s}")

print("=" * 60)
print("  LUXit — Legal Compliance Patch (luxit production server)")
print("=" * 60)

# ── 1. Verify paths ───────────────────────────────────────────────────────────
if not LEGAL_PY.exists():
    ERR(f"legal.py not found at {LEGAL_PY}")
    raise SystemExit(1)
OK(f"Found {LEGAL_PY}")
TMPL_DIR.mkdir(parents=True, exist_ok=True)
OK(f"Template dir: {TMPL_DIR}")

# ── 2. Patch legal.py ─────────────────────────────────────────────────────────
content = LEGAL_PY.read_text()

NEW_ROUTES = '''

@legal_bp.get("/privacy-policy")
def privacy_policy():
    return render_template("legal/privacy.html", updated=LAST_UPDATED)


@legal_bp.get("/sms-consent")
def sms_consent():
    return render_template("legal/sms_consent.html", updated=LAST_UPDATED)
'''

needs_patch = []
if "/privacy-policy" not in content:
    needs_patch.append("/privacy-policy")
if "/sms-consent" not in content:
    needs_patch.append("/sms-consent")

if needs_patch:
    # Inject after the /terms route definition
    if '@legal_bp.get("/terms")' in content or "@legal_bp.get('/terms')" in content:
        content = re.sub(
            r'(@legal_bp\.(?:get|route)\([\'\"]/terms[\'\"].*?\ndef \w+\(\):.*?\n    return [^\n]+)',
            r'\1' + NEW_ROUTES,
            content, count=1, flags=re.DOTALL
        )
    else:
        # Fall back: append before last function or at end of blueprint section
        content += NEW_ROUTES

    # Make sure render_template is imported
    if "render_template" not in content.split("from flask")[0] if "from flask" in content else True:
        content = content.replace(
            "from flask import Blueprint",
            "from flask import Blueprint, render_template"
        )

    LEGAL_PY.write_text(content)
    OK(f"Patched legal.py — added: {', '.join(needs_patch)}")
else:
    OK("legal.py already has both routes — skipping")

# ── 3. Read LAST_UPDATED from legal.py ───────────────────────────────────────
m = re.search(r'LAST_UPDATED\s*=\s*["\'](.+?)["\']', content)
LAST_UPDATED = m.group(1) if m else "2026"

# ── 4. Write sms_consent.html ─────────────────────────────────────────────────
SMS_CONSENT = (TMPL_DIR / "sms_consent.html")
if not SMS_CONSENT.exists() or "sms-consent" not in SMS_CONSENT.read_text():
    SMS_CONSENT.write_text(f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>SMS Consent | LUXit</title>
  <style>
    :root{{--bg:#0b0d12;--surface:#101522;--border:#1f2633;--text:#f7f7fb;--muted:#9aa4b2;--accent:#7c3aed;}}
    *{{box-sizing:border-box;}} body{{margin:0;font-family:Inter,system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.7;}}
    nav{{display:flex;align-items:center;justify-content:space-between;padding:20px 40px;border-bottom:1px solid var(--border);background:rgba(11,13,18,.95);position:sticky;top:0;z-index:10;backdrop-filter:blur(12px);}}
    nav a{{color:var(--muted);text-decoration:none;font-size:.9rem;}} nav a:hover{{color:var(--text);}}
    .logo{{font-weight:700;font-size:1rem;letter-spacing:.05em;color:var(--text);}}
    .container{{max-width:760px;margin:0 auto;padding:48px 24px 80px;}}
    h1{{font-size:2rem;font-weight:700;margin-bottom:8px;}}
    .meta{{font-size:.85rem;color:var(--muted);margin-bottom:40px;}}
    p{{margin:0 0 20px;color:#c8d0dc;}}
    .card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:32px;margin:36px 0;}}
    .card h2{{font-size:1.1rem;font-weight:600;margin:0 0 24px;}}
    .fg{{margin-bottom:20px;}} label{{display:block;font-size:.875rem;font-weight:500;color:var(--muted);margin-bottom:6px;}}
    input[type=text],input[type=tel]{{width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:.95rem;outline:none;}}
    input[type=text]:focus,input[type=tel]:focus{{border-color:var(--accent);}}
    .cb{{display:flex;align-items:flex-start;gap:12px;margin-bottom:16px;}}
    input[type=checkbox]{{margin-top:4px;width:16px;height:16px;flex-shrink:0;accent-color:var(--accent);cursor:pointer;}}
    .cb-label{{font-size:.875rem;color:#c8d0dc;line-height:1.6;}}
    .disc{{font-size:.8rem;color:var(--muted);margin-bottom:24px;line-height:1.5;}}
    .btn{{display:inline-block;padding:11px 28px;background:var(--accent);color:#fff;border:none;border-radius:6px;font-size:.95rem;font-weight:600;cursor:pointer;}}
    .btn:hover{{opacity:.88;}}
    .conf{{display:none;background:rgba(124,58,237,.12);border:1px solid var(--accent);border-radius:8px;padding:16px 20px;color:var(--text);margin-top:20px;}}
    footer{{border-top:1px solid var(--border);padding:32px 40px;text-align:center;color:var(--muted);font-size:.85rem;}}
    footer a{{color:var(--muted);margin:0 8px;text-decoration:none;}} footer a:hover{{color:var(--text);}}
    @media(max-width:600px){{nav{{padding:16px 20px;}}.container{{padding:32px 16px 60px;}} footer{{padding:24px 20px;}}}}
  </style>
</head>
<body>
  <nav><a class="logo" href="/">LUXit</a><a href="/privacy-policy">Privacy Policy</a></nav>
  <div class="container">
    <h1>SMS Consent</h1>
    <div class="meta">Last Updated: {LAST_UPDATED}</div>
    <p>LUX sends SMS messages for account notifications, customer support, scheduling, reminders, marketing, and service updates to users who have opted in.</p>
    <div class="card">
      <h2>Opt-In Consent Form</h2>
      <form id="f" novalidate>
        <div class="fg"><label for="name">Name</label><input type="text" id="name" placeholder="Your full name" required></div>
        <div class="fg"><label for="phone">Phone Number</label><input type="tel" id="phone" placeholder="+1 (555) 000-0000" required></div>
        <div class="cb">
          <input type="checkbox" id="cb" required>
          <span class="cb-label">I agree to receive SMS messages from LUX. Message frequency varies. Message &amp; data rates may apply. Reply STOP to opt out.</span>
        </div>
        <p class="disc">
          Reply HELP for help. Consent is not a condition of purchase.
          SMS consent is not shared with third parties or affiliates for marketing purposes.<br>
          <a href="/privacy-policy" style="color:var(--accent);font-size:.8rem;">Privacy Policy</a> &nbsp;&bull;&nbsp;
          <a href="/terms" style="color:var(--accent);font-size:.8rem;">Terms &amp; Conditions</a>
        </p>
        <button type="submit" class="btn">Submit Consent</button>
      </form>
      <div class="conf" id="conf">Thank you. Your SMS consent preference has been received.</div>
    </div>
  </div>
  <footer>
    <div style="margin-bottom:12px;">&copy; LUX. All rights reserved.</div>
    <div><a href="/sms-consent">SMS Consent</a><a href="/privacy-policy">Privacy Policy</a><a href="/terms">Terms &amp; Conditions</a></div>
  </footer>
  <script>
    document.getElementById('f').addEventListener('submit',function(e){{
      e.preventDefault();
      if(!document.getElementById('name').value.trim()||!document.getElementById('phone').value.trim()||!document.getElementById('cb').checked){{alert('Please complete all fields and check the consent box.');return;}}
      this.style.display='none'; document.getElementById('conf').style.display='block';
    }});
  </script>
</body>
</html>''')
    OK("Wrote sms_consent.html")
else:
    OK("sms_consent.html already exists — skipping")

# ── 5. Update privacy.html footer + add SMS section ───────────────────────────
PRIVACY = TMPL_DIR / "privacy.html"
if PRIVACY.exists():
    ptxt = PRIVACY.read_text()
    changed = False
    # Add SMS section if missing
    if "SMS Messaging Privacy" not in ptxt and "sms-consent" not in ptxt:
        sms_section = '''
    <h2>SMS Messaging Privacy</h2>
    <p>LUX may collect and use phone numbers to send SMS messages related to account notifications, reminders, service updates, support, and approved marketing communications.</p>
    <p>LUX does not sell, rent, or share SMS opt-in data or consent information with third parties for their marketing purposes. Reply <strong>STOP</strong> to opt out. Reply <strong>HELP</strong> for help. Contact: <a href="mailto:privacy@luxit.app">privacy@luxit.app</a></p>
'''
        ptxt = re.sub(r'(</div>\s*<footer)', sms_section + r'\1', ptxt, count=1)
        changed = True
    # Update footer links
    if "sms-consent" not in ptxt:
        ptxt = re.sub(
            r'(<footer[^>]*>.*?</footer>)',
            '''<footer>
    <div style="margin-bottom:12px;">&copy; LUX. All rights reserved.</div>
    <div>
      <a href="/sms-consent">SMS Consent</a>
      <a href="/privacy-policy">Privacy Policy</a>
      <a href="/terms">Terms &amp; Conditions</a>
      <a href="/data-deletion">Data Deletion</a>
    </div>
  </footer>''',
            ptxt, count=1, flags=re.DOTALL
        )
        changed = True
    if changed:
        PRIVACY.write_text(ptxt)
        OK("Updated privacy.html (SMS section + footer links)")
    else:
        OK("privacy.html already patched — skipping")
else:
    INFO("privacy.html not found — skipping (route still works, page may be minimal)")

# ── 6. Update terms.html footer + add SMS Terms section ───────────────────────
TERMS = TMPL_DIR / "terms.html"
if TERMS.exists():
    ttxt = TERMS.read_text()
    changed = False
    if "SMS Terms" not in ttxt:
        sms_terms = '''
    <h2>SMS Terms</h2>
    <p><strong>Program name:</strong> LUX SMS Notifications</p>
    <p><strong>Program description:</strong> LUX sends account, support, scheduling, reminder, marketing, and service-related messages.</p>
    <p><strong>Message frequency:</strong> Message frequency varies.</p>
    <p><strong>Costs:</strong> Message and data rates may apply.</p>
    <p><strong>Opt out:</strong> Reply STOP to opt out.</p>
    <p><strong>Help:</strong> Reply HELP for help.</p>
    <p><strong>Carrier disclaimer:</strong> Carriers are not liable for delayed or undelivered messages.</p>
    <p>See our <a href="/privacy-policy">Privacy Policy</a> for more information.</p>
'''
        ttxt = re.sub(r'(</div>\s*<footer)', sms_terms + r'\1', ttxt, count=1)
        changed = True
    if "sms-consent" not in ttxt:
        ttxt = re.sub(
            r'(<footer[^>]*>.*?</footer>)',
            '''<footer>
    <div style="margin-bottom:12px;">&copy; LUX. All rights reserved.</div>
    <div>
      <a href="/sms-consent">SMS Consent</a>
      <a href="/privacy-policy">Privacy Policy</a>
      <a href="/terms">Terms &amp; Conditions</a>
    </div>
  </footer>''',
            ttxt, count=1, flags=re.DOTALL
        )
        changed = True
    if changed:
        TERMS.write_text(ttxt)
        OK("Updated terms.html (SMS Terms + footer links)")
    else:
        OK("terms.html already patched — skipping")
else:
    INFO("terms.html not found — skipping")

# ── 7. Restart the app ────────────────────────────────────────────────────────
print("\n── Restarting app ──")
for cmd in [
    ["supervisorctl", "restart", "all"],
    ["supervisorctl", "restart", "email-marketing"],
    ["systemctl", "restart", "email-marketing"],
]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            OK(f"Restarted via: {' '.join(cmd)}")
            break
        else:
            INFO(f"Tried {cmd[0]}: {r.stderr.strip()[:80]}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

# ── 8. Quick self-test ────────────────────────────────────────────────────────
import time, urllib.request
time.sleep(3)
print("\n── Verifying routes (direct to Flask) ──")
for path in ["/sms-consent", "/privacy-policy", "/terms"]:
    try:
        code = urllib.request.urlopen(f"http://127.0.0.1:8001{path}", timeout=5).status
    except Exception as e:
        code = str(e)
    sym = "✓" if code == 200 else "✗"
    print(f"  {sym}  http://127.0.0.1:8001{path} → {code}")

print("\nDone. If all routes show 200, visit https://luxit.app/sms-consent to confirm.")
