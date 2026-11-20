"""Read-only scan of /storage/logs/app.log* (environment=production) for use of
the routes fixed in 212ae1d. Usage: python3 security_log_scan.py out.json"""
import json, re, glob, collections, sys
PATS = {
 "reset_request":   r"^/auth/forgot-(password|username)$",
 "reset_use":       r"^/auth/reset-password/",
 "legacy_user":     r"^/user/(add-user|edit-user/\d+)$",
 "user_mgmt":       r"^/user/(add|edit/\d+|delete/\d+|restore/\d+)$|^/api/user/\d+/access$",
 "profile_write":   r"^/api/user/profile$|^/user/profile/edit$",
 "saas":            r"^/saas(/|$)",
 "campaign_obj":    r"^/campaigns/\d+(/|$)|^/email-builder/\d+",
 "automation_obj":  r"^/automations/\d+/",
 "contact_act":     r"^/(api/)?contacts/\d+/activities",
 "sms_analytics":   r"^/sms/\d+/analytics",
 "calendar_obj":    r"^/api/calendar/events/[a-z]+_\d+",
 "deal_obj":        r"^/crm/deals/\d+",
 "blog_seo":        r"^/blog/\d+/edit|^/seo/audit/\d+",
}
rows = collections.defaultdict(list); first=None; last=None; n=0
for f in sorted(glob.glob("/storage/logs/app.log*")):
    for line in open(f, errors="replace"):
        try: e = json.loads(line)
        except Exception: continue
        if e.get("environment") != "production": continue
        n += 1; ts = e.get("timestamp","")
        first = ts if first is None or ts < first else first; last = ts if last is None or ts > last else last
        p = e.get("path") or ""
        for k, rx in PATS.items():
            if re.search(rx, p):
                if k=="reset_use": p = "/auth/reset-password/<token>"
                rows[k].append((ts[:19], e.get("method"), p, e.get("statusCode"), e.get("userId"), e.get("companyId")))
print(f"production entries={n} span={first}..{last}")
for k in PATS:
    r = rows.get(k, [])
    c = collections.Counter((m, re.sub(r"\d+", "#", p), s, u, co) for _, m, p, s, u, co in r)
    print(f"\n[{k}] {len(r)} requests")
    for (m,p,s,u,co),cnt in sorted(c.items(), key=lambda x:-x[1])[:12]:
        print(f"   {cnt:4} {m} {p} {s} user={u} company={co}")
json.dump({k:v for k,v in rows.items()}, open(sys.argv[1],"w"))
