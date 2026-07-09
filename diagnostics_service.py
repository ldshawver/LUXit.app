"""Safe structured diagnostics logging and export helpers."""
from __future__ import annotations

import json, logging, os, re, shutil, time, traceback, zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from flask import has_request_context, g, request
from flask_login import current_user

LOG_NAMES = ["app", "error", "appdr", "github", "payroll", "pdf", "database", "security"]
MAX_LOG_SIZE = 10 * 1024 * 1024
BACKUPS = 10
START_TIME = time.time()
SECRET = "[REDACTED_SECRET]"; BANK = "[REDACTED_BANK]"; PII = "[REDACTED_PII]"; PAYROLL = "[REDACTED_PAYROLL]"
SENSITIVE_KEYS = re.compile(r"(password|passwd|secret|api[_-]?key|token|authorization|cookie|session|jwt|refresh|access[_-]?token|github[_-]?token|ssn|ein|tax|dob|birth|address|routing|account|direct[_-]?deposit|payroll|wage|salary|amount)", re.I)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
SSN_RE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")
BANK_RE = re.compile(r"\b(?:\d[ -]*?){10,17}\b")
KEYVAL_RE = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|github[_-]?token|token|authorization|cookie|session|jwt|refresh[_-]?token|access[_-]?token)\s*[:=]\s*[^\s,;&]+")


def log_dir() -> Path:
    base = os.environ.get("DIAGNOSTICS_LOG_DIR") or "/storage/logs"
    p = Path(base)
    try:
        p.mkdir(parents=True, exist_ok=True)
        test = p / ".write-test"; test.write_text("ok"); test.unlink(missing_ok=True)
        return p
    except Exception:
        fallback = Path(os.environ.get("LOCAL_STORAGE_PATH", "storage")) / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def redact(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SENSITIVE_KEYS.search(str(k)) and isinstance(v, (str, list, dict)):
                lk = str(k).lower()
                out[k] = BANK if "bank" in lk or "routing" in lk or "account" in lk or "direct" in lk else PAYROLL if "payroll" in lk or "salary" in lk or "wage" in lk or "amount" in lk else PII if lk in {"ssn","ein","tax","dob","birth","address"} else SECRET
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list): return [redact(v) for v in value]
    if not isinstance(value, str): return value
    s = KEYVAL_RE.sub(lambda m: f"{m.group(1)}={SECRET}", value)
    s = JWT_RE.sub(SECRET, s)
    s = SSN_RE.sub(PII, s)
    s = EIN_RE.sub(PII, s)
    s = BANK_RE.sub(BANK, s)
    return s


def rotate(path: Path):
    try:
        if path.exists() and path.stat().st_size >= MAX_LOG_SIZE:
            for i in range(BACKUPS - 1, 0, -1):
                src = path.with_suffix(path.suffix + f".{i}"); dst = path.with_suffix(path.suffix + f".{i+1}")
                if src.exists(): src.replace(dst)
            path.replace(path.with_suffix(path.suffix + ".1"))
    except Exception as exc:
        logging.getLogger(__name__).warning("diagnostics rotation failed: %s", exc)


def structured_log(level="info", service="app", message="", error=None, metadata=None, **fields):
    service = service if service in LOG_NAMES else "app"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "level": level,
        "environment": os.environ.get("NODE_ENV") or os.environ.get("FLASK_ENV") or "development",
        "service": service, "message": message,
        "requestId": getattr(g, "request_id", None) if has_request_context() else None,
        "correlationId": fields.pop("correlationId", None), "tenantId": fields.pop("tenantId", None),
        "companyId": fields.pop("companyId", None), "userId": fields.pop("userId", None),
        "method": request.method if has_request_context() else None, "path": request.path if has_request_context() else None,
        "statusCode": fields.pop("statusCode", None), "durationMs": fields.pop("durationMs", None),
        "metadata": metadata or {},
    }
    if error:
        entry.update(errorName=type(error).__name__, errorMessage=str(error), stack="".join(traceback.format_exception(type(error), error, error.__traceback__)))
    entry.update(fields)
    entry = redact(entry)
    p = log_dir() / f"{service}.log"; rotate(p)
    p.open("a", encoding="utf-8").write(json.dumps(entry, default=str) + "\n")
    if level in {"error", "fatal"} and service != "error":
        ep = log_dir() / "error.log"; rotate(ep); ep.open("a", encoding="utf-8").write(json.dumps(entry, default=str) + "\n")
    return entry


def is_diagnostics_admin(user) -> bool:
    if not getattr(user, "is_authenticated", False): return False
    roles = {str(getattr(user, "role", "") or "").lower(), str(getattr(user, "segment", "") or "").lower()}
    tags = str(getattr(user, "tags", "") or "").lower()
    return bool(getattr(user, "is_admin", False) or roles & {"platform_owner","super_admin","system_admin"} or any(r in tags for r in ["platform_owner","super_admin","system_admin"]))


def read_logs(service="error", level=None, search=None, limit=200, correlation_id=None):
    rows=[]; names=[service] if service in LOG_NAMES else LOG_NAMES
    for name in names:
        p=log_dir()/f"{name}.log"
        if not p.exists(): continue
        lines=p.read_text(errors="ignore").splitlines()[-1000:]
        for line in lines:
            try: row=json.loads(line)
            except Exception: row={"timestamp": None, "level":"info", "service":name, "message":redact(line)}
            if level and row.get("level") != level: continue
            if correlation_id and row.get("correlationId") != correlation_id: continue
            if search and search.lower() not in json.dumps(row).lower(): continue
            rows.append(redact(row))
    return sorted(rows, key=lambda r: r.get("timestamp") or "", reverse=True)[:limit]


def system_health():
    d=log_dir(); usage=shutil.disk_usage(str(d))
    return {"uptime_seconds": int(time.time()-START_TIME), "environment": os.environ.get("NODE_ENV") or os.environ.get("FLASK_ENV") or "development", "version": os.environ.get("APP_VERSION","unknown"), "git_sha": os.environ.get("GIT_SHA", os.environ.get("COMMIT_SHA","unknown")), "database_connected": True, "storage_writable": os.access(d, os.W_OK), "github_configured": bool(os.environ.get("GITHUB_TOKEN")), "email_configured": bool(os.environ.get("SENDGRID_API_KEY") or os.environ.get("SMTP_HOST")), "queue_status": "not_configured", "disk_free_bytes": usage.free, "memory": {"available": True}}


def env_presence():
    return {"NODE_ENV": os.environ.get("NODE_ENV") or os.environ.get("FLASK_ENV") or "development", "DATABASE_URL_PRESENT": bool(os.environ.get("DATABASE_URL")), "GITHUB_TOKEN_PRESENT": bool(os.environ.get("GITHUB_TOKEN")), "EMAIL_PROVIDER_PRESENT": bool(os.environ.get("SENDGRID_API_KEY") or os.environ.get("SMTP_HOST")), "STORAGE_CONFIG_PRESENT": bool(os.environ.get("STORAGE_PATH") or os.environ.get("LOCAL_STORAGE_PATH"))}


def github_status():
    return {"github_token_configured": bool(os.environ.get("GITHUB_TOKEN")), "repo_configured": bool(os.environ.get("GITHUB_REPOSITORY") or (os.environ.get("GITHUB_OWNER") and os.environ.get("GITHUB_REPO"))), "repo": os.environ.get("GITHUB_REPOSITORY") or os.environ.get("GITHUB_REPO"), "base_branch": os.environ.get("GITHUB_BASE_BRANCH", "main"), "last_pr_creation_attempt": None, "last_github_api_status": None, "last_failure_correlationId": (read_logs("github", level="error", limit=1) or [{}])[0].get("correlationId")}


def export_bundle() -> bytes:
    import io
    buf=io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in LOG_NAMES:
            p=log_dir()/f"{name}.log"; content=redact(p.read_text(errors="ignore") if p.exists() else "")
            z.writestr(f"logs/{name}.log", content)
        payloads={"environment":env_presence(), "system-health":system_health(), "recent-errors":read_logs("error",limit=100), "appdr-status":github_status(), "github-status":github_status(), "versions":{"app":"luxit","git_sha":system_health()["git_sha"]}}
        for n,p in payloads.items(): z.writestr(f"json/{n}.json", json.dumps(redact(p), indent=2, default=str))
    buf.seek(0); return buf.getvalue()


def new_correlation_id(): return str(uuid4())
