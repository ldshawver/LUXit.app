"""
GitHub integration service.

Uses GITHUB_PERSONAL_ACCESS_TOKEN (40 chars, set in Replit secrets).
All mutating endpoints are restricted to platform admins only (enforced in blueprint).
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def health_check() -> dict:
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"status": "missing_config", "detail": "GitHub token not configured"}
    try:
        r = _get("/user", token)
        if r.status_code == 200:
            data = r.json()
            return {"status": "connected", "login": data.get("login")}
        return {"status": "error", "detail": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Read functions
# ---------------------------------------------------------------------------

def list_repos() -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = _get("/user/repos?per_page=50&sort=updated", token)
        r.raise_for_status()
        repos = [
            {"full_name": x["full_name"], "description": x.get("description"),
             "private": x["private"], "updated_at": x["updated_at"],
             "default_branch": x["default_branch"]}
            for x in r.json()
        ]
        return {"ok": True, "repos": repos}
    except Exception as exc:
        _log_error("list_repos", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def get_repo(owner: str, repo: str) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = _get(f"/repos/{owner}/{repo}", token)
        r.raise_for_status()
        return {"ok": True, "repo": r.json()}
    except Exception as exc:
        _log_error("get_repo", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def list_issues(owner: str, repo: str, state: str = "open") -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = _get(f"/repos/{owner}/{repo}/issues?state={state}&per_page=50", token)
        r.raise_for_status()
        issues = [
            {"number": x["number"], "title": x["title"], "state": x["state"],
             "html_url": x["html_url"], "created_at": x["created_at"],
             "labels": [l["name"] for l in x.get("labels", [])]}
            for x in r.json() if "pull_request" not in x
        ]
        return {"ok": True, "issues": issues}
    except Exception as exc:
        _log_error("list_issues", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def create_issue(owner: str, repo: str, title: str, body: str,
                 labels: list | None = None) -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    # Sanitise content
    title = _sanitise(title)[:256]
    body  = _sanitise(body)[:65535]
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = [str(l)[:50] for l in labels[:10]]
    try:
        r = _post(f"/repos/{owner}/{repo}/issues", token, payload)
        r.raise_for_status()
        data = r.json()
        return {"ok": True, "number": data["number"], "html_url": data["html_url"]}
    except Exception as exc:
        _log_error("create_issue", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def list_pull_requests(owner: str, repo: str, state: str = "open") -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = _get(f"/repos/{owner}/{repo}/pulls?state={state}&per_page=30", token)
        r.raise_for_status()
        prs = [
            {"number": x["number"], "title": x["title"], "state": x["state"],
             "html_url": x["html_url"], "created_at": x["created_at"],
             "user": x["user"]["login"]}
            for x in r.json()
        ]
        return {"ok": True, "pull_requests": prs}
    except Exception as exc:
        _log_error("list_prs", exc)
        return {"ok": False, "reason": str(exc)[:200]}


def get_latest_commit(owner: str, repo: str, branch: str = "main") -> dict:
    token = _token()
    if not token:
        return {"ok": False, "reason": "missing_config"}
    try:
        r = _get(f"/repos/{owner}/{repo}/commits/{branch}", token)
        if r.status_code == 404:
            r = _get(f"/repos/{owner}/{repo}/commits/master", token)
        r.raise_for_status()
        data = r.json()
        commit = data.get("commit", {})
        return {
            "ok": True,
            "sha": data.get("sha", "")[:12],
            "message": commit.get("message", "")[:200],
            "author": commit.get("author", {}).get("name"),
            "date": commit.get("author", {}).get("date"),
            "html_url": data.get("html_url"),
        }
    except Exception as exc:
        _log_error("get_latest_commit", exc)
        return {"ok": False, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _token():
    return os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, token: str):
    return requests.get(_BASE + path, headers=_headers(token), timeout=15)


def _post(path: str, token: str, json_body: dict):
    return requests.post(_BASE + path, headers=_headers(token), json=json_body, timeout=15)


def _sanitise(text: str) -> str:
    import re
    return re.sub(r"[<>]", "", text or "")


def _log_error(endpoint: str, exc: Exception):
    logger.error("GitHub %s error: %s", endpoint, exc)
    try:
        from extensions import db
        from models import IntegrationErrorLog
        el = IntegrationErrorLog(
            provider="github",
            endpoint=endpoint,
            error_message=str(exc)[:500],
        )
        db.session.add(el)
        db.session.commit()
    except Exception:
        pass
