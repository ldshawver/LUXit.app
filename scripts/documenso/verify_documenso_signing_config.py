#!/usr/bin/env python3
"""Validate LUXit/Documenso signing URL configuration before emails are sent.

This intentionally does not call MyPayLink or mutate Documenso. It validates local
compose/env/template files and, when DOCUMENSO_PUBLIC_URL is set, verifies that the
public Documenso URL resolves.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BROKEN_PATH = "/app/contractor-hub/contracts/"
DOCUMENSO_URL_KEYS = {
    "APP_URL",
    "NEXT_PUBLIC_WEBAPP_URL",
    "NEXTAUTH_URL",
    "WEBAPP_URL",
    "DOCUMENSO_PUBLIC_URL",
    "DOCUMENSO_WEBAPP_URL",
    "DOCUMENSO_APP_URL",
    "DOCUMENSO_BASE_URL",
    "DOCUMENSO_WEBHOOK_URL",
    "WEBHOOK_URL",
}
MY_PAY_LINK_HOSTS = {"app.mypaylink.app", "mypaylink.app", "www.mypaylink.app"}
URL_RE = re.compile(r"https?://[^\s\"'<>)}]+")


def iter_files(root: Path):
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    for path in root.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if "tests" in path.parts:
            continue
        if path.is_file() and path.suffix.lower() in {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".txt", ".env", ".yml", ".yaml", ".json"}:
            yield path


def parse_env_like(path: Path):
    values = {}
    try:
        for raw in path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("-"):
                line = line[1:].strip()
            key, value = line.split("=", 1)
            key = key.strip().lstrip("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key in DOCUMENSO_URL_KEYS or key.startswith("DOCUMENSO_"):
                values.setdefault(key, set()).add(value)
    except OSError:
        pass
    return values


def validate_url(name: str, value: str, errors: list[str], warnings: list[str]):
    if not value or value.startswith("${"):
        warnings.append(f"{name} is templated or empty: {value!r}")
        return
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https":
        errors.append(f"{name} must use https in production: {value}")
    if not parsed.netloc:
        errors.append(f"{name} is not an absolute URL: {value}")
    if parsed.netloc.lower() in MY_PAY_LINK_HOSTS and parsed.path.startswith("/app/contractor-hub/contracts"):
        errors.append(f"{name} points at known-broken MyPayLink deep link: {value}")


def check_public_url(url: str, errors: list[str], warnings: list[str]):
    if not url:
        warnings.append("DOCUMENSO_PUBLIC_URL not set; skipping live reachability check")
        return
    req = urllib.request.Request(url.rstrip("/") + "/", method="GET", headers={"User-Agent": "luxit-documenso-config-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 400:
                errors.append(f"Documenso public URL returned HTTP {resp.status}: {url}")
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            errors.append(f"Documenso public URL returned HTTP {exc.code}: {url}")
        else:
            warnings.append(f"Documenso public URL returned HTTP {exc.code}; verify manually: {url}")
    except Exception as exc:  # noqa: BLE001 - CLI reports operator-facing validation failure
        errors.append(f"Documenso public URL is not reachable: {url} ({exc})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository/deployment root to scan")
    parser.add_argument("--documenso-public-url", default=os.getenv("DOCUMENSO_PUBLIC_URL", ""))
    parser.add_argument("--skip-network", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    found_values: dict[str, set[str]] = {}

    for path in iter_files(root):
        rel = path.relative_to(root)
        text = path.read_text(errors="ignore")
        if BROKEN_PATH in text and "verify_documenso_signing_config.py" not in str(rel):
            errors.append(f"known-broken path found in {rel}: {BROKEN_PATH}")
        for url in URL_RE.findall(text):
            parsed = urllib.parse.urlparse(url)
            if parsed.netloc.lower() in MY_PAY_LINK_HOSTS and parsed.path.startswith(BROKEN_PATH):
                errors.append(f"known-broken MyPayLink signing URL found in {rel}: {url}")
        if "verify_documenso_signing_config.py" not in str(rel):
            for key, vals in parse_env_like(path).items():
                found_values.setdefault(key, set()).update(vals)

    for key, vals in sorted(found_values.items()):
        for value in sorted(vals):
            validate_url(key, value, errors, warnings)

    public_url = args.documenso_public_url or next(iter(found_values.get("DOCUMENSO_PUBLIC_URL", [])), "")
    if not args.skip_network:
        check_public_url(public_url, errors, warnings)

    for warning in warnings:
        print(f"WARNING: {warning}")
    if found_values:
        print("Documenso URL-related config keys found:")
        for key, vals in sorted(found_values.items()):
            print(f"  {key}=" + ", ".join(sorted(vals)))
    else:
        print("No Documenso URL-related config keys found in scanned files.")

    if errors:
        print("FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("PASS: no known-broken signing links or invalid Documenso URL config found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
