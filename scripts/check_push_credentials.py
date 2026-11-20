#!/usr/bin/env python3
"""Offline check of LUX Connect native push credentials, before they go live.

Reads the same environment variables as ``services/native_push.py`` and checks
that each provider would actually authenticate, without contacting Apple or
Google and without printing any secret:

  APNs  LUXIT_APNS_KEY_FILE is an EC P-256 private key (.p8), the key id and
        team id are 10-character Apple ids, the topic looks like a bundle id,
        the ``h2`` package is importable, and an ES256 provider token can be
        signed and verified with the key.
  FCM   LUXIT_FCM_SERVICE_ACCOUNT_FILE is a service-account JSON with an RSA
        private key that can sign the RS256 OAuth assertion.

Both credential files must not be readable by other local users.

Exit 0 when every configured provider passes and at least one is configured
(``--require apns,fcm`` makes specific providers mandatory). Prints one
``RESULT:`` line.

Usage:
    env $(cat /etc/lux-email-bot-staging.env | grep LUXIT_) \
        python scripts/check_push_credentials.py --require apns,fcm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time

APPLE_ID = re.compile(r"^[A-Z0-9]{10}$")
BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def _file_problems(path: str | None, label: str) -> tuple[list[str], str | None]:
    if not path:
        return [f"{label} is not set"], None
    try:
        st = os.stat(path)
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
    except OSError as exc:
        return [f"{label} cannot be read: {exc.strerror}"], None
    problems = []
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        problems.append(f"{label} is readable by group/other (mode {stat.S_IMODE(st.st_mode):o}); chmod 600")
    return problems, body


def check_apns(env) -> list[str]:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    import jwt

    problems, pem = _file_problems(env.get("LUXIT_APNS_KEY_FILE"), "LUXIT_APNS_KEY_FILE")
    key_id = env.get("LUXIT_APNS_KEY_ID") or ""
    team_id = env.get("LUXIT_APNS_TEAM_ID") or ""
    topic = env.get("LUXIT_APNS_TOPIC") or ""
    if not APPLE_ID.match(key_id):
        problems.append("LUXIT_APNS_KEY_ID must be the 10-character key id from the Apple developer portal")
    if not APPLE_ID.match(team_id):
        problems.append("LUXIT_APNS_TEAM_ID must be the 10-character Apple team id")
    if not BUNDLE_ID.match(topic) or topic.endswith(".voip"):
        problems.append("LUXIT_APNS_TOPIC must be the app bundle id (alert pushes; not the .voip topic)")
    try:
        import h2  # noqa: F401
    except ImportError:
        problems.append("the h2 package is missing from this venv; APNs needs HTTP/2 (pip install h2==4.3.0)")
    if pem is not None:
        try:
            key = load_pem_private_key(pem.encode(), password=None)
        except ValueError:
            problems.append("LUXIT_APNS_KEY_FILE is not a PEM private key (expected the AuthKey_XXXX.p8 file)")
        else:
            if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
                problems.append("LUXIT_APNS_KEY_FILE must be an EC P-256 key (APNs auth key)")
            elif APPLE_ID.match(key_id) and APPLE_ID.match(team_id):
                token = jwt.encode({"iss": team_id, "iat": int(time.time())}, pem,
                                   algorithm="ES256", headers={"kid": key_id})
                jwt.decode(token, key.public_key(), algorithms=["ES256"])
    return problems


def check_fcm(env) -> list[str]:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    import jwt

    problems, raw = _file_problems(env.get("LUXIT_FCM_SERVICE_ACCOUNT_FILE"), "LUXIT_FCM_SERVICE_ACCOUNT_FILE")
    if raw is None:
        return problems
    try:
        info = json.loads(raw)
    except ValueError:
        return problems + ["LUXIT_FCM_SERVICE_ACCOUNT_FILE is not JSON"]
    if info.get("type") != "service_account":
        problems.append('service-account JSON must have "type": "service_account"')
    missing = [k for k in ("project_id", "client_email", "private_key") if not info.get(k)]
    if missing:
        return problems + [f"service-account JSON is missing {', '.join(missing)}"]
    try:
        key = load_pem_private_key(info["private_key"].encode(), password=None)
    except ValueError:
        return problems + ["service-account private_key is not a PEM private key"]
    if not isinstance(key, rsa.RSAPrivateKey):
        return problems + ["service-account private_key must be RSA"]
    now = int(time.time())
    token = jwt.encode({"iss": info["client_email"], "aud": "https://oauth2.googleapis.com/token",
                        "iat": now, "exp": now + 60}, info["private_key"], algorithm="RS256")
    jwt.decode(token, key.public_key(), algorithms=["RS256"], audience="https://oauth2.googleapis.com/token")
    return problems


CHECKS = {"apns": ("LUXIT_APNS_KEY_FILE", check_apns), "fcm": ("LUXIT_FCM_SERVICE_ACCOUNT_FILE", check_fcm)}


def run(env, require: set[str]) -> tuple[bool, dict]:
    report = {}
    for name, (marker, check) in CHECKS.items():
        configured = any(k.startswith(f"LUXIT_{name.upper()}_") and env.get(k) for k in env) or bool(env.get(marker))
        if not configured and name not in require:
            report[name] = "not configured"
            continue
        problems = check(env)
        report[name] = problems or "ok"
    ok = all(v in ("ok", "not configured") for v in report.values()) and any(v == "ok" for v in report.values())
    return ok, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--require", default="", help="comma-separated providers that must pass (apns,fcm)")
    args = ap.parse_args(argv)
    require = {p.strip() for p in args.require.split(",") if p.strip()}
    unknown = require - set(CHECKS)
    if unknown:
        ap.error(f"unknown provider(s): {', '.join(sorted(unknown))}")
    ok, report = run(os.environ, require)
    for name, result in report.items():
        if isinstance(result, list):
            for problem in result:
                print(f"{name}: FAIL {problem}")
        else:
            print(f"{name}: {result}")
    print("RESULT:", json.dumps({"pass": ok, **{k: (v if isinstance(v, str) else "fail") for k, v in report.items()}}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
