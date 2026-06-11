---
name: Flask 3.x g is per-AppContext, not per-RequestContext
description: Flask-Login's g._login_user persists across requests when a module-scoped app fixture keeps one AppContext alive; must clear it before each test.
---

## Rule
In Flask 3.x (3.0+), `g` lives on the `AppContext`, NOT the `RequestContext`.
A module-scoped pytest fixture that does `with a.app_context(): yield a` keeps one `AppContext` alive for the entire module, so `g._login_user` set by Flask-Login during one request persists into every subsequent request.

**Why:** Flask-Login's `_get_user()` checks `"_login_user" not in g` before calling `_load_user()`. If `g._login_user` is already set to `AnonymousUser` from a previous unauthenticated request, every subsequent authenticated request also gets `AnonymousUser`, producing unexpected 302 redirects.

**How to apply:** In any autouse fixture that wraps each test (e.g. `_db_rollback` in `tests/conftest.py`), clear `g._login_user` at setup time:

```python
try:
    from flask.globals import _cv_app as _flask_cv_app
    _app_ctx = _flask_cv_app.get(None)
    if _app_ctx is not None and hasattr(_app_ctx.g, "_login_user"):
        del _app_ctx.g._login_user
except Exception:
    pass
```

This is already in `tests/conftest.py` → `_db_rollback` fixture setup block.
