#!/usr/bin/env python3
"""
vps_fix_twilio.py  --  Find the Flask app, patch it, run DB migration.
  python3 /root/lux-email-bot/vps_fix_twilio.py
"""
import os, shutil, sys
from pathlib import Path

ok = []; skip = []; fail = []; info = []

TWILIO_SMS_ORIGIN = Path("/root/lux-email-bot/twilio_sms.py")

# ===== STEP 1: Find real app.py =====
def find_app_py():
    found = []
    for root in ["/var/www", "/opt", "/home", "/srv", "/root"]:
        for p in Path(root).rglob("app.py") if Path(root).exists() else []:
            try:
                t = p.read_text()
                if "create_app" in t and "register_blueprint" in t:
                    found.append(p)
            except Exception:
                pass
    return found

print("Searching for Flask app.py...")
candidates = find_app_py()
for c in candidates:
    print("  Found:", c)

APP_PY = None
APP_DIR = None
if not candidates:
    fail.append("app.py not found")
else:
    preferred = [c for c in candidates if (c.parent/"wsgi.py").exists()]
    APP_PY = preferred[0] if preferred else candidates[0]
    APP_DIR = APP_PY.parent
    info.append("Using: " + str(APP_PY))

# ===== STEP 2: Move twilio_sms.py to correct dir =====
if APP_DIR:
    dest = APP_DIR / "twilio_sms.py"
    if dest.exists():
        skip.append("twilio_sms.py already at " + str(dest))
    elif TWILIO_SMS_ORIGIN.exists():
        shutil.copy(TWILIO_SMS_ORIGIN, dest)
        ok.append("Copied twilio_sms.py -> " + str(dest))
    else:
        fail.append("twilio_sms.py missing; re-run vps_twilio_deploy.py first")

# ===== STEP 3: Patch app.py =====
def patch(p, candidates, suffix, label):
    if p is None: fail.append("skip (no app.py): " + label); return
    src = p.read_text()
    if suffix.strip() in src: skip.append("already applied: " + label); return
    for old in candidates:
        if old in src:
            shutil.copy(p, str(p) + ".bak")
            p.write_text(src.replace(old, old + suffix, 1))
            ok.append("patched: " + label)
            return
    # Fallback: insert before "return app"
    for end in ["    return app", "  return app", "return app"]:
        if end in src:
            shutil.copy(p, str(p) + ".bak")
            p.write_text(src.replace(end, "    " + suffix.strip() + "\n    " + end.strip(), 1))
            ok.append("patched (before return app): " + label)
            return
    fail.append("no anchor matched: " + label)

patch(APP_PY,
    ["    from x_auth import x_bp, x_api_bp",
     "    from advanced_config import advanced_config_bp",
     "    from legal import legal_bp",
     "    from marketing import marketing_bp",
     "    from routes import main_bp"],
    "\n    from twilio_sms import twilio_bp",
    "import twilio_bp")

patch(APP_PY,
    ["    app.register_blueprint(x_api_bp)",
     "    app.register_blueprint(x_bp)",
     "    app.register_blueprint(advanced_config_bp)",
     "    app.register_blueprint(legal_bp)",
     "    app.register_blueprint(marketing_bp)"],
    "\n    app.register_blueprint(twilio_bp)",
    "register twilio_bp")

# ===== STEP 4: Find DATABASE_URL =====
def find_db_url():
    v = os.environ.get("DATABASE_URL", "")
    if v: return v
    dirs = []
    if APP_DIR: dirs.append(APP_DIR)
    dirs += [Path("/root/lux-email-bot"), Path("/root"), Path("/etc")]
    for d in dirs:
        if not d or not d.exists(): continue
        for name in [".env", ".env.production", "config.env"]:
            f = d / name
            if not f.exists(): continue
            for line in f.read_text().splitlines():
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    val = line[len("DATABASE_URL="):].strip()
                    val = val.strip(chr(34)).strip(chr(39))
                    if val: info.append("DB from " + str(f)); return val
    for svc in Path("/etc/systemd/system").glob("*.service"):
        try:
            for line in svc.read_text().splitlines():
                if "DATABASE_URL=" in line:
                    val = line.split("DATABASE_URL=")[1].split()[0]
                    if val: info.append("DB from " + svc.name); return val
        except Exception: pass
    return ""

DB_URL = find_db_url()
if DB_URL:
    masked = DB_URL[:15] + "..." + DB_URL[-8:]
    info.append("DATABASE_URL: " + masked)
else:
    fail.append("DATABASE_URL not found in env, .env files, or systemd")

# ===== STEP 5: DB migration =====
TABLES = [
("twilio_account", """CREATE TABLE IF NOT EXISTS twilio_account (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL UNIQUE REFERENCES company(id),
    account_sid TEXT, auth_token TEXT,
    messaging_service_sid VARCHAR(60), from_phone VARCHAR(20),
    webhook_base_url VARCHAR(500), is_active BOOLEAN DEFAULT TRUE,
    automation_enabled BOOLEAN DEFAULT TRUE,
    ai_mode VARCHAR(20) DEFAULT 'off', ai_system_prompt TEXT,
    missed_call_text TEXT, after_hours_text TEXT,
    sms_forward_to VARCHAR(20), call_forward_to VARCHAR(20),
    sms_forwarding_enabled BOOLEAN DEFAULT TRUE,
    voice_forwarding_enabled BOOLEAN DEFAULT TRUE,
    after_hours_sms_enabled BOOLEAN DEFAULT TRUE,
    after_hours_voicemail_enabled BOOLEAN DEFAULT TRUE,
    voicemail_greeting_text TEXT,
    voicemail_greeting_audio_url VARCHAR(500),
    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
)"""),
("twilio_conversation", """CREATE TABLE IF NOT EXISTS twilio_conversation (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    contact_id INTEGER REFERENCES contact(id),
    from_number VARCHAR(20) NOT NULL, to_number VARCHAR(20),
    contact_name VARCHAR(200), is_read BOOLEAN DEFAULT FALSE,
    is_opted_out BOOLEAN DEFAULT FALSE, is_first_contact BOOLEAN DEFAULT TRUE,
    lead_captured BOOLEAN DEFAULT FALSE, tags JSONB DEFAULT '[]',
    notes TEXT, assigned_user_id INTEGER REFERENCES "user"(id),
    last_message_at TIMESTAMP, last_message_preview VARCHAR(200),
    message_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_twilio_conv_company_from UNIQUE (company_id, from_number)
)"""),
("twilio_message", """CREATE TABLE IF NOT EXISTS twilio_message (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES twilio_conversation(id),
    company_id INTEGER NOT NULL REFERENCES company(id),
    twilio_sid VARCHAR(100) UNIQUE, direction VARCHAR(10) NOT NULL,
    from_number VARCHAR(20), to_number VARCHAR(20),
    body TEXT, status VARCHAR(50) DEFAULT 'received',
    num_segments INTEGER DEFAULT 1, media_urls JSONB,
    is_auto_reply BOOLEAN DEFAULT FALSE, rule_id INTEGER,
    error_code VARCHAR(20), error_message TEXT, raw_payload JSONB,
    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
)"""),
("auto_reply_rule", """CREATE TABLE IF NOT EXISTS auto_reply_rule (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    name VARCHAR(200) NOT NULL, trigger_type VARCHAR(50),
    keywords JSONB DEFAULT '[]', response TEXT,
    is_active BOOLEAN DEFAULT TRUE, priority INTEGER DEFAULT 0,
    action VARCHAR(50) DEFAULT 'reply', forward_to VARCHAR(200),
    tag_value VARCHAR(100), match_count INTEGER DEFAULT 0,
    active_days JSONB, active_hours_start VARCHAR(5), active_hours_end VARCHAR(5),
    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
)"""),
("business_hours", """CREATE TABLE IF NOT EXISTS business_hours (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    day_of_week INTEGER, is_open BOOLEAN DEFAULT TRUE,
    open_time VARCHAR(5) DEFAULT '11:00',
    close_time VARCHAR(5) DEFAULT '01:00',
    timezone VARCHAR(50) DEFAULT 'America/Los_Angeles',
    CONSTRAINT uq_biz_hours_company_day UNIQUE (company_id, day_of_week)
)"""),
("twilio_call_log", """CREATE TABLE IF NOT EXISTS twilio_call_log (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    twilio_sid VARCHAR(100) UNIQUE, direction VARCHAR(10),
    from_number VARCHAR(20), to_number VARCHAR(20),
    status VARCHAR(50), duration INTEGER DEFAULT 0,
    caller_name VARCHAR(200), missed_text_sent BOOLEAN DEFAULT FALSE,
    notes TEXT, raw_payload JSONB,
    created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
)"""),
]

if DB_URL:
    try:
        import psycopg2
        conn = psycopg2.connect(DB_URL)
        conn.autocommit = True; cur = conn.cursor()
        for tname, sql in TABLES:
            cur.execute(sql)
            ok.append("table ok: " + tname)
        cur.close(); conn.close()
    except Exception as e:
        fail.append("DB error: " + str(e))

# ===== STEP 6: Show blueprint lines from app.py for verification =====
if APP_PY:
    src = APP_PY.read_text()
    print("\n--- Blueprint registrations in", APP_PY, "---")
    for line in src.splitlines():
        if "twilio" in line.lower() or "register_blueprint" in line:
            print(" ", line)

print("\n=== Info ===")
for m in info: print(" ", m)
print("\n=== Results ===")
for m in ok:   print("  OK:", m)
for m in skip: print("  --:", m)
for m in fail: print("  XX:", m)

if not fail:
    print("\nAll done. Restart and verify:")
    print("  sudo systemctl restart luxit")
    print("  curl -s -o /dev/null -w '%{http_code}' -X POST https://luxit.app/twilio/sms/inbound")
else:
    print("\nSome steps FAILED -- paste output here for next steps.")
    sys.exit(1)
