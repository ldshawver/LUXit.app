"""
Patch script: injects A2P/SMS compliance footer links into VPS marketing templates.
Run on VPS: python3 /root/lux-email-bot/vps_patch_marketing_footer.py
"""
import re
from pathlib import Path

APP_DIR = Path("/root/lux-email-bot")
TEMPLATES_DIR = APP_DIR / "templates"

COMPLIANCE_BLOCK = """
      <!-- A2P/SMS Compliance Links -->
      <div style="margin-top:16px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.08);text-align:center;font-size:0.8rem;color:rgba(255,255,255,0.35);">
        &copy; LUX. All rights reserved. &nbsp;&bull;&nbsp;
        <a href="/sms-consent" style="color:rgba(255,255,255,0.4);text-decoration:none;margin:0 6px;">SMS Consent</a>
        <a href="/privacy-policy" style="color:rgba(255,255,255,0.4);text-decoration:none;margin:0 6px;">Privacy Policy</a>
        <a href="/terms" style="color:rgba(255,255,255,0.4);text-decoration:none;margin:0 6px;">Terms &amp; Conditions</a>
      </div>"""

ALREADY_PATCHED_MARKER = "sms-consent"


def patch_file(path: Path) -> str:
    content = path.read_text(encoding="utf-8")

    if ALREADY_PATCHED_MARKER in content:
        return f"  SKIP  {path.relative_to(APP_DIR)} (already has sms-consent link)"

    # Strategy 1: insert just before </footer>
    if "</footer>" in content.lower():
        patched = re.sub(
            r"(</footer>)",
            COMPLIANCE_BLOCK + r"\n\1",
            content,
            count=1,
            flags=re.IGNORECASE,
        )
        path.write_text(patched, encoding="utf-8")
        return f"  OK    {path.relative_to(APP_DIR)} (injected before </footer>)"

    # Strategy 2: insert before </body>
    if "</body>" in content.lower():
        patched = re.sub(
            r"(</body>)",
            COMPLIANCE_BLOCK + r"\n\1",
            content,
            count=1,
            flags=re.IGNORECASE,
        )
        path.write_text(patched, encoding="utf-8")
        return f"  OK    {path.relative_to(APP_DIR)} (injected before </body> — no <footer> tag found)"

    return f"  WARN  {path.relative_to(APP_DIR)} (no </footer> or </body> found — skipped)"


def main():
    print("=" * 60)
    print("  LUXit VPS — Marketing Footer Compliance Patch")
    print("=" * 60)

    # Candidate templates to patch (order matters — base first)
    candidates = [
        TEMPLATES_DIR / "marketing" / "base.html",
        TEMPLATES_DIR / "marketing" / "home.html",
        TEMPLATES_DIR / "marketing" / "pricing.html",
        TEMPLATES_DIR / "marketing" / "features.html",
        TEMPLATES_DIR / "marketing" / "solutions.html",
        TEMPLATES_DIR / "marketing" / "security.html",
        TEMPLATES_DIR / "marketing" / "book_demo.html",
    ]

    # Also auto-discover any other marketing/*.html files
    marketing_dir = TEMPLATES_DIR / "marketing"
    if marketing_dir.exists():
        for f in sorted(marketing_dir.glob("*.html")):
            if f not in candidates:
                candidates.append(f)

    # Only patch the base/layout template if it exists (avoids duplicating
    # the block on every page that extends it)
    base = TEMPLATES_DIR / "marketing" / "base.html"
    if base.exists():
        print(f"\nFound marketing/base.html — patching base only (all pages inherit it)\n")
        print(patch_file(base))
    else:
        print(f"\nNo marketing/base.html — patching individual page templates\n")
        for path in candidates:
            if path.exists() and path != base:
                print(patch_file(path))

    print("\nDone. Restart service: systemctl restart luxit")
    print("Then visit https://luxit.app to verify the footer links appear.")


if __name__ == "__main__":
    main()
