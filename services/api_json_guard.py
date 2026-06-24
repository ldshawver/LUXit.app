from __future__ import annotations

_ALLOWED_BINARY = {"application/pdf", "application/octet-stream", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def should_warn_api_non_json(path: str, status_code: int, mimetype: str | None) -> bool:
    if not path.startswith("/api/"):
        return False
    if status_code in (204, 304):
        return False
    mt = (mimetype or "").split(";")[0].lower()
    if mt in _ALLOWED_BINARY or mt.startswith("image/") or mt.startswith("audio/"):
        return False
    return mt not in {"application/json", "text/json"}
