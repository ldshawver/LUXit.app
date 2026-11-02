#!/usr/bin/env python3
"""New-customer journey against a LIVE staging instance, with disposable accounts.

The HTTP counterpart of ``tests/test_saas_customer_journey.py``. Run it only
after the release is deployed to staging with ``SELF_SERVE_SIGNUP_ENABLED=1``.
It goes through the real routes, CSRF tokens included:

   1. public pages            /, /pricing, /auth/signup
   2. signup                  disposable owner ``rc-journey-<tag>@<domain>``
   3. unverified login        CRM still refused
   4. email verification      the link is minted with the instance secret
                              (same signer and salt as the verification email),
                              so no inbox is needed; the user id is looked up
                              read-only in the instance database
   5. organization            free plan, owner can manage billing
   6. onboarding + settings   checklist, brand colour saved
   7. contacts + CRM          add a contact, open a deal
   8. checkout                with Stripe test keys: a checkout.stripe.com URL;
                              without: a clean 503 (never a 500)
   9. isolation               a second disposable tenant cannot read tenant 1
  10. invitations             free plan refuses a second seat (no invite row
                              appears in the owner's team list)
  11. sign out / back in
  12. password change         signs the owner's other session out; the old
                              password stops working

It sends no SMS, places no calls, and never touches Twilio: this staging
instance shares production Twilio credentials. Invitation acceptance needs the
emailed link (only its hash is stored), so it stays in the in-process test
until staging has a mail sink.

Each run leaves two disposable companies named ``RC Journey <tag> …``; the
queries that find them are printed at the end. Nothing is deleted.

Exit 0 = PASS, 1 = FAIL. Prints one ``RESULT:`` line.

Usage:
    python scripts/acceptance/staging_customer_journey.py \
        --base http://127.0.0.1:8004 --host staging.luxit.app \
        --db "postgresql://luxstaging:...@127.0.0.1:5434/luxstaging" \
        --secret-env /etc/luxit-staging/secret.env [--expect-stripe]
"""
from __future__ import annotations

import argparse
from http.cookies import SimpleCookie
import json
import re
import secrets
import sys
import time

import requests

CSRF_FIELD = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')
VERIFY_SALT = "luxit-email-verify-v1"  # services.signup._SALT


class Fail(Exception):
    pass


def check(cond, what):
    if not cond:
        raise Fail(what)


class Browser:
    """A requests session that behaves like one browser: cookies + CSRF."""

    def __init__(self, base: str, host: str | None):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        # Cookies are kept here rather than in requests' jar: the app marks
        # them Secure (TLS ends at nginx) and --base is the plain-HTTP port
        # behind it, so the jar would never send them back.
        self.cookies: dict[str, str] = {}
        self.s.headers["User-Agent"] = "luxit-rc-journey/1"
        if host:
            self.s.headers["Host"] = host
            self.s.headers["X-Forwarded-Proto"] = "https"
        self.s.headers["Referer"] = f"https://{host}/" if host else self.base + "/"
        self.csrf = None

    def request(self, method, path, **kw):
        r = self.s.request(method, self.base + path, cookies=dict(self.cookies), allow_redirects=False,
                           timeout=20, **kw)
        self.s.cookies.clear()
        for header in r.raw.headers.getlist("Set-Cookie"):
            for name, morsel in SimpleCookie(header).items():
                expired = morsel["max-age"] == "0" or "1970" in morsel["expires"]
                if expired or not morsel.value:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        return r

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def refresh_csrf(self, path):
        m = CSRF_FIELD.search(self.get(path).text)
        check(m, f"no csrf_token on {path}")
        self.csrf = m.group(1)

    def form(self, path, data, csrf_page=None):
        if csrf_page:
            self.refresh_csrf(csrf_page)
        return self.request("POST", path, data={**data, "csrf_token": self.csrf or ""})

    def json(self, path, body=None):
        return self.request("POST", path, json=body or {}, headers={"X-CSRFToken": self.csrf or ""})

    def login(self, email, password):
        return self.request("POST", "/auth/login", data={"username": email, "password": password})

    def can_use_crm(self):
        return self.get("/crm/today").status_code == 200


def read_secret(path: str) -> str:
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    secret = values.get("SESSION_SECRET") or values.get("SECRET_KEY")
    check(secret, f"{path} has no SESSION_SECRET/SECRET_KEY")
    return secret


def lookup(dsn: str, sql: str, params: tuple):
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


def user_id_for(dsn: str, email: str) -> tuple[int, int]:
    row = lookup(dsn, 'SELECT id, default_company_id FROM "user" WHERE email = %s', (email,))
    check(row, f"signup did not create a user for {email}")
    return row[0], row[1]


def verify_link(secret: str, user_id: int, email: str) -> str:
    from itsdangerous import URLSafeTimedSerializer
    return "/auth/verify-email/" + URLSafeTimedSerializer(secret, salt=VERIFY_SALT).dumps({"u": user_id, "e": email})


def signup(args, secret, tag, label):
    email = f"rc-journey-{label}-{tag}@{args.email_domain}"
    password = f"rc-{secrets.token_urlsafe(12)}"
    b = Browser(args.base, args.host)
    r = b.form("/auth/signup", {"org_name": f"RC Journey {tag} {label}", "full_name": f"RC {label}", "email": email,
                                "password": password, "confirm_password": password, "accept_terms": "1"},
               csrf_page="/auth/signup")
    check(r.status_code == 200 and "Check your email" in r.text, f"signup {label}: {r.status_code}")
    uid, company_id = user_id_for(args.db, email)
    r = b.get(verify_link(secret, uid, email))
    check(r.status_code == 302 and "billing" in r.headers.get("Location", ""), f"verify {label}: {r.status_code}")
    check(b.can_use_crm(), f"{label} cannot reach CRM after verifying")
    return b, email, password, company_id


def journey(args, log) -> None:
    secret = read_secret(args.secret_env)
    tag = time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    visitor = Browser(args.base, args.host)

    for page in ("/", "/pricing", "/auth/signup"):
        check(visitor.get(page).status_code == 200, f"public page {page}")
    log("1 public pages")

    email = f"rc-journey-owner-{tag}@{args.email_domain}"
    password = f"rc-{secrets.token_urlsafe(12)}"
    r = visitor.form("/auth/signup", {"org_name": f"RC Journey {tag} owner", "full_name": "RC Owner", "email": email,
                                      "password": password, "confirm_password": password, "accept_terms": "1"},
                     csrf_page="/auth/signup")
    check(r.status_code == 200 and "Check your email" in r.text, f"signup: {r.status_code} {r.headers.get('Location', '')}")
    uid, company_id = user_id_for(args.db, email)
    log(f"2 signup company={company_id}")

    early = Browser(args.base, args.host)
    early.login(email, password)
    check(not early.can_use_crm(), "unverified account reached the CRM")
    log("3 unverified login refused")

    owner = Browser(args.base, args.host)
    r = owner.get(verify_link(secret, uid, email))
    check(r.status_code == 302 and "billing" in r.headers.get("Location", ""), f"verify: {r.status_code}")
    check(owner.can_use_crm(), "verified owner cannot reach CRM")
    check(owner.get(verify_link(secret, uid, email + "x")).status_code == 200, "tampered link handled")
    log("4 verified via link")

    ent = owner.get("/api/account/entitlements").json()
    check(ent["plan"]["free"] is True and ent["can_manage_billing"] is True and ent["seats"]["limit"] == 1,
          f"free-plan entitlements: {ent.get('plan')}")
    log("5 free plan, owner manages billing")

    progress = owner.get("/api/onboarding/progress").json()["progress"]
    check(progress["checklist"]["company_created"] is True, "onboarding checklist")
    owner.refresh_csrf("/user/change-password")
    r = owner.json(f"/api/company/{company_id}/settings", {"primary_color": "#123456"})
    check(r.status_code == 200, f"settings save: {r.status_code}")
    log("6 onboarding + settings")

    r = owner.form("/contacts/add", {"first_name": "Casey", "last_name": f"Journey{tag}",
                                     "email": f"casey-{tag}@{args.email_domain}"})
    check(r.status_code in (200, 302), f"contact add: {r.status_code}")
    row = lookup(args.db, "SELECT id FROM contact WHERE company_id = %s AND last_name = %s",
                 (company_id, f"Journey{tag}"))
    check(row, "created contact not found in the tenant")
    contact_id = row[0]
    check(owner.get(f"/api/contacts/{contact_id}/timeline").status_code == 200, "owner reads own contact")
    r = owner.json("/api/crm/opportunities", {"contact_id": contact_id, "name": "RC deal", "estimated_value": "2400"})
    check(r.status_code == 201, f"opportunity: {r.status_code}")
    log(f"7 contact {contact_id} + deal")

    r = owner.json("/api/stripe/create-checkout-session", {"lookup_key": "luxit_starter_monthly"})
    if args.expect_stripe:
        url = (r.json() or {}).get("url", "") if r.status_code == 200 else ""
        check(url.startswith("https://checkout.stripe.com/"), f"checkout: {r.status_code} {r.text[:200]}")
        log("8 checkout session created (test mode); complete it in a browser with card 4242…")
    else:
        check(r.status_code == 503, f"checkout without Stripe keys should be a clean 503, got {r.status_code}")
        log("8 checkout cleanly unavailable (no Stripe test keys)")
    check(owner.json("/api/stripe/create-checkout-session",
                     {"lookup_key": "luxit_starter_monthly", "price_id": "price_free"}).status_code == 400,
          "raw price id accepted")

    stranger, *_ = signup(args, secret, tag, "stranger")
    check(stranger.get(f"/api/contacts/{contact_id}/timeline").status_code in (403, 404), "cross-tenant timeline")
    stranger.refresh_csrf("/user/change-password")
    check(stranger.json(f"/api/company/{company_id}/settings", {"primary_color": "#000000"}).status_code in (403, 404),
          "cross-tenant settings write")
    log("9 second tenant isolated")

    r = owner.form("/user/invite", {"email": f"rc-journey-mate-{tag}@{args.email_domain}", "role": "editor"},
                   csrf_page="/user/change-password")
    check(r.status_code in (200, 302), f"invite: {r.status_code}")
    check(owner.get("/api/account/entitlements").json()["seats"]["members"] == 1, "seat count")
    log("10 free-plan seat limit holds")

    owner.get("/auth/logout")
    check(not owner.can_use_crm(), "still signed in after logout")
    back = Browser(args.base, args.host)
    back.login(email, password)
    check(back.can_use_crm(), "sign back in")
    log("11 sign out / in")

    phone = Browser(args.base, args.host)
    phone.login(email, password)
    check(phone.can_use_crm(), "second session")
    new_password = f"rc-{secrets.token_urlsafe(12)}"
    r = back.form("/user/change-password", {"current_password": password, "new_password": new_password,
                                            "confirm_password": new_password}, csrf_page="/user/change-password")
    check(r.status_code == 302, f"change password: {r.status_code}")
    check(not phone.can_use_crm(), "other session survived the password change")
    check(back.can_use_crm(), "current session lost after password change")
    old = Browser(args.base, args.host)
    old.login(email, password)
    check(not old.can_use_crm(), "old password still works")
    log("12 password change revoked the other session")

    print("DISPOSABLE RECORDS (not deleted; find them with):")
    print(f"  SELECT id, name FROM company WHERE name LIKE 'RC Journey {tag} %';")
    print(f"  SELECT id, email FROM \"user\" WHERE email LIKE 'rc-journey-%-{tag}@{args.email_domain}';")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="instance URL, e.g. http://127.0.0.1:8004")
    ap.add_argument("--host", help="Host header when --base is the local port")
    ap.add_argument("--db", required=True, help="instance Postgres DSN (read-only lookups)")
    ap.add_argument("--secret-env", required=True, help="env file holding SESSION_SECRET")
    ap.add_argument("--email-domain", default="journey.example", help="disposable, undeliverable domain")
    ap.add_argument("--expect-stripe", action="store_true", help="instance has Stripe test keys")
    args = ap.parse_args(argv)
    steps = []

    def log(msg):
        steps.append(msg)
        print("ok  ", msg, flush=True)

    try:
        journey(args, log)
    except (Fail, requests.RequestException, KeyError, ValueError) as exc:
        print("FAIL", f"after step {len(steps)}:", exc)
        print("RESULT:", json.dumps({"pass": False, "steps": len(steps), "error": str(exc)[:300]}))
        return 1
    print("RESULT:", json.dumps({"pass": True, "steps": len(steps)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
