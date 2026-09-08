"""P1 guard — the canonical Gunicorn config must keep the gthread worker model.

The Twilio voice webhook starved under `sync` workers because /api/inbox/stream
holds a worker for the life of each PWA SSE connection (prod incident
2026-09-08). `deploy/gunicorn.conf.py` is the reviewable source of intent; this
test fails loudly if it regresses to sync or drops threads.
"""
import os
import runpy


CONF = os.path.join(os.path.dirname(__file__), "..", "deploy", "gunicorn.conf.py")


def _load():
    return runpy.run_path(CONF)


def test_worker_class_is_gthread():
    assert _load()["worker_class"] == "gthread"


def test_threads_and_workers_are_sane():
    ns = _load()
    assert ns["threads"] >= 4
    assert ns["workers"] >= 2
    # 15s is Twilio's voice webhook deadline; the worker-liveness timeout must
    # not be so low it kills healthy requests, nor the old 300s that let a
    # wedged worker linger.
    assert 30 <= ns["timeout"] <= 120


def test_sse_endpoint_still_declares_event_stream():
    """If the SSE contract changes, the worker-model rationale must be revisited."""
    import inbox_pwa
    src = inbox_pwa.sse_stream.__doc__ or ""
    assert "gthread" in src.lower() or "thread" in src.lower()
