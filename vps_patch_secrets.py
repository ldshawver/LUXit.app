"""
VPS patch — adds set_secret / get_secret / delete_secret to Company model
and ensures services/secret_vault.py exists.

Run from /root/lux-email-bot:
    python3 vps_patch_secrets.py

Safe to run multiple times — all operations are idempotent.
"""
import os
import sys
import re
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).parent
MODELS   = APP_DIR / "models.py"
SVC_DIR  = APP_DIR / "services"
VAULT_PY = SVC_DIR / "secret_vault.py"
INIT_PY  = SVC_DIR / "__init__.py"

# ── 1. Ensure services/secret_vault.py ───────────────────────────────────────
VAULT_SRC = '''\
"""
SecretVault - Encryption service for secure storage of API keys and secrets.
"""
import os
import base64
from cryptography.fernet import Fernet
import logging

logger = logging.getLogger(__name__)


class SecretVault:
    """Handles encryption and decryption of sensitive data."""

    def __init__(self):
        self._cipher = None
        self._initialize_cipher()

    def _initialize_cipher(self):
        master_key = os.environ.get("ENCRYPTION_MASTER_KEY")
        if not master_key:
            logger.warning("ENCRYPTION_MASTER_KEY not set — generating ephemeral key")
            key = Fernet.generate_key()
            logger.info("Add to environment: ENCRYPTION_MASTER_KEY=%s", key.decode())
        else:
            key = master_key.encode() if isinstance(master_key, str) else master_key
        try:
            self._cipher = Fernet(key)
        except Exception as exc:
            logger.error("Bad master key (%s) — generating fallback key", exc)
            self._cipher = Fernet(Fernet.generate_key())

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        encrypted_bytes = self._cipher.encrypt(plaintext.encode())
        return base64.b64encode(encrypted_bytes).decode()

    def decrypt(self, encrypted_text: str) -> str:
        if not encrypted_text:
            return ""
        try:
            encrypted_bytes = base64.b64decode(encrypted_text.encode())
            return self._cipher.decrypt(encrypted_bytes).decode()
        except Exception:
            raise

    def mask_secret(self, secret: str, show_chars: int = 4) -> str:
        if not secret or len(secret) <= show_chars:
            return "****"
        return f"{secret[:2]}***{secret[-show_chars:]}"


# Global singleton
vault = SecretVault()
'''

print("\n── Step 1: services/secret_vault.py ──")
SVC_DIR.mkdir(exist_ok=True)
if not INIT_PY.exists():
    INIT_PY.write_text("")
    print("  Created services/__init__.py")

if not VAULT_PY.exists():
    VAULT_PY.write_text(VAULT_SRC)
    print("  Created services/secret_vault.py  ✓")
else:
    # Check if our vault singleton is there; if not, overwrite
    content = VAULT_PY.read_text()
    if "vault = SecretVault()" not in content:
        VAULT_PY.write_text(VAULT_SRC)
        print("  Replaced services/secret_vault.py (was incomplete) ✓")
    else:
        print("  services/secret_vault.py already OK")


# ── 2. Patch models.py — inject methods into Company class ───────────────────
METHODS_SRC = '''\

    # ── Secret helpers ──────────────────────────────────────────────────────
    def set_secret(self, key_or_provider, key_or_value=None, value=None):
        """
        Store/update an encrypted secret.

        Two call styles:
          company.set_secret("OPENAI_API_KEY", "sk-123")
          company.set_secret("twilio", "auth_token", "tok123")
        """
        from services.secret_vault import vault

        if value is not None:
            full_key    = f"{key_or_provider}_{key_or_value}"
            plain_value = value
        else:
            full_key    = key_or_provider
            plain_value = key_or_value

        if not plain_value:
            return

        try:
            enc_value = vault.encrypt(str(plain_value))
        except Exception:
            enc_value = str(plain_value)

        secret = CompanySecret.query.filter_by(
            company_id=self.id, key=full_key
        ).first()
        if secret:
            secret.value      = enc_value
            secret.updated_at = datetime.utcnow()
        else:
            secret = CompanySecret(
                company_id=self.id, key=full_key, value=enc_value
            )
            db.session.add(secret)
        db.session.commit()

    def get_secret(self, key_or_provider, sub_key=None):
        """Retrieve and decrypt a secret. Returns None if not found."""
        from services.secret_vault import vault

        full_key = (f"{key_or_provider}_{sub_key}" if sub_key
                    else key_or_provider)
        secret = CompanySecret.query.filter_by(
            company_id=self.id, key=full_key
        ).first()
        if not secret or not secret.value:
            return None
        try:
            return vault.decrypt(secret.value)
        except Exception:
            return secret.value

    def delete_secret(self, key_or_provider, sub_key=None):
        """Delete a stored secret for this company."""
        full_key = (f"{key_or_provider}_{sub_key}" if sub_key
                    else key_or_provider)
        secret = CompanySecret.query.filter_by(
            company_id=self.id, key=full_key
        ).first()
        if secret:
            db.session.delete(secret)
            db.session.commit()

'''

COMPANY_SECRET_CLASS = '''\

class CompanySecret(db.Model):
    """Encrypted per-company API secrets (multi-tenant safe)."""
    __tablename__ = "company_secret"

    id         = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=True, index=True)
    key        = db.Column(db.String(255), nullable=False)
    value      = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

'''

print("\n── Step 2: models.py — Company secret helpers ──")
if not MODELS.exists():
    sys.exit(f"  ERROR: {MODELS} not found — are you in the right directory?")

src = MODELS.read_text()

# Check if methods already present
if "def set_secret" in src:
    print("  set_secret already present — skipping method injection")
else:
    # Find the end of the Company class body.
    # Strategy: find 'class Company' then locate the next top-level class definition.
    company_match = re.search(r'^class Company\b', src, re.MULTILINE)
    if not company_match:
        sys.exit("  ERROR: 'class Company' not found in models.py")

    # Find where the NEXT top-level class starts after Company
    next_class_match = re.search(r'^class \w+', src[company_match.end():], re.MULTILINE)
    if next_class_match:
        insert_pos = company_match.end() + next_class_match.start()
    else:
        insert_pos = len(src)  # Company is the last class

    # Insert methods just before the next class
    src = src[:insert_pos] + METHODS_SRC + src[insert_pos:]
    MODELS.write_text(src)
    print("  Injected set_secret / get_secret / delete_secret into Company ✓")

# Check if CompanySecret model exists
if "class CompanySecret" not in src:
    # Append at end of file
    src = src + COMPANY_SECRET_CLASS
    MODELS.write_text(src)
    print("  Appended CompanySecret model to models.py ✓")
else:
    print("  CompanySecret model already present")


# ── 3. Create company_secret table in DB ─────────────────────────────────────
print("\n── Step 3: DB migration — company_secret table ──")

SQL = """
CREATE TABLE IF NOT EXISTS company_secret (
    id         SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES company(id) ON DELETE CASCADE,
    key        VARCHAR(255) NOT NULL,
    value      TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_company_secret_key
    ON company_secret (company_id, key);
"""

# Try to find DATABASE_URL
db_url = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("DB_URL")
)

if not db_url:
    # Check .env file
    env_file = APP_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                db_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

if not db_url:
    # Check systemd service file
    result = subprocess.run(
        ["grep", "-r", "DATABASE_URL", "/etc/systemd/system/"],
        capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        if "DATABASE_URL=" in line:
            db_url = line.split("DATABASE_URL=", 1)[1].strip().strip('"').strip("'")
            break

if not db_url:
    print("  ⚠  Could not find DATABASE_URL — skipping DB migration")
    print("     Run manually:")
    print(f"     psql $DATABASE_URL -c \"{SQL.strip()}\"")
else:
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(SQL)
        conn.close()
        print("  company_secret table created / verified ✓")
    except ImportError:
        # Fallback: use psql CLI
        result = subprocess.run(
            ["psql", db_url, "-c", SQL],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("  company_secret table created via psql ✓")
        else:
            print(f"  ⚠  psql error: {result.stderr.strip()}")
    except Exception as exc:
        print(f"  ⚠  DB error: {exc}")


# ── 4. Verify syntax of patched models.py ────────────────────────────────────
print("\n── Step 4: Verify models.py syntax ──")
result = subprocess.run(
    [sys.executable, "-m", "py_compile", str(MODELS)],
    capture_output=True, text=True
)
if result.returncode == 0:
    print("  models.py syntax OK ✓")
else:
    print(f"  SYNTAX ERROR in models.py:\n{result.stderr}")
    sys.exit(1)


print("\n══ Patch complete. Restart the service: ══")
print("   sudo systemctl restart luxit")
print("   sudo journalctl -u luxit -n 30 --no-pager")
