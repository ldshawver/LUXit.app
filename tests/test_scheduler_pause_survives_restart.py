"""A job paused in the jobstore must stay paused across worker restarts.

Regression: init_scheduler re-added recurring jobs with replace_existing=True,
which made APScheduler compute a fresh next_run_time and overwrite the paused
row. The 2026-10-17 host reboot re-armed weekly_contact_dedupe this way and it
ran on 2026-10-18.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text

import scheduler as scheduler_module


def _noop():
    return None


class _StubApp:
    """Only what init_scheduler touches: the jobstore URI and an app context."""

    def __init__(self, url, real_app):
        self.config = {"SQLALCHEMY_DATABASE_URI": url}
        self._real_app = real_app

    @contextmanager
    def app_context(self):
        with self._real_app.app_context():
            yield


@pytest.fixture
def jobstore_url(tmp_path):
    return f"sqlite:///{tmp_path / 'jobs.sqlite'}"


@pytest.fixture
def boot(monkeypatch, jobstore_url):
    from app import app as real_app
    from extensions import db

    # init_scheduler also queries scheduled Campaign rows through the shared
    # app DB, which an earlier test module may have dropped (drop_all).
    with real_app.app_context():
        db.create_all()

    # Never run real job bodies; both refs must stay importable for pickling.
    monkeypatch.setattr(scheduler_module, "run_tuya_reconciliation", _noop)
    monkeypatch.setattr(scheduler_module, "run_weekly_contact_dedupe", _noop)
    monkeypatch.setattr(scheduler_module, "run_crm_task_reminders", _noop)
    monkeypatch.setattr(scheduler_module, "run_crm_inactivity_scan", _noop)
    stub = _StubApp(jobstore_url, real_app)
    started = []

    def _boot():
        """One worker process lifetime: start, then stop like SIGTERM does."""
        scheduler_module.scheduler = None
        sched = scheduler_module.init_scheduler(stub)
        started.append(sched)
        return sched

    yield _boot
    for sched in started:
        if sched.running:
            sched.shutdown(wait=False)
    scheduler_module.scheduler = None


def _next_run(url, job_id):
    engine = create_engine(url)
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT next_run_time FROM apscheduler_jobs WHERE id = :id"),
            {"id": job_id},
        ).scalar()


def _stop(sched):
    sched.shutdown(wait=False)


def test_fresh_jobstore_arms_both_jobs(boot, jobstore_url):
    _stop(boot())
    assert _next_run(jobstore_url, "weekly_contact_dedupe") is not None
    assert _next_run(jobstore_url, "tuya_notification_reconciliation") is not None
    assert _next_run(jobstore_url, "crm_task_reminders") is not None


def test_paused_dedupe_stays_paused_across_restarts(boot, jobstore_url):
    sched = boot()
    sched.pause_job("weekly_contact_dedupe")
    _stop(sched)
    assert _next_run(jobstore_url, "weekly_contact_dedupe") is None

    for _ in range(3):  # reboot, upgrade, restart
        sched = boot()
        assert sched.get_job("weekly_contact_dedupe").next_run_time is None
        _stop(sched)
        assert _next_run(jobstore_url, "weekly_contact_dedupe") is None

    # Pausing one job must not pause the other.
    assert _next_run(jobstore_url, "tuya_notification_reconciliation") is not None


def test_pause_written_directly_to_the_column_is_honoured(boot, jobstore_url):
    """The prod containment nulled the column; the worker must respect that."""
    _stop(boot())
    engine = create_engine(jobstore_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE apscheduler_jobs SET next_run_time = NULL WHERE id = 'weekly_contact_dedupe'")
        )
    _stop(boot())
    assert _next_run(jobstore_url, "weekly_contact_dedupe") is None


def test_explicit_resume_rearms_and_survives_restart(boot, jobstore_url):
    sched = boot()
    sched.pause_job("weekly_contact_dedupe")
    sched.resume_job("weekly_contact_dedupe")
    _stop(sched)
    assert _next_run(jobstore_url, "weekly_contact_dedupe") is not None
    _stop(boot())
    assert _next_run(jobstore_url, "weekly_contact_dedupe") is not None


def test_unreadable_jobstore_fails_startup_instead_of_rearming(monkeypatch, boot):
    def _boom(*_a, **_k):
        raise RuntimeError("jobstore unavailable")

    monkeypatch.setattr(scheduler_module, "_paused_job_ids", _boom)
    with pytest.raises(RuntimeError):
        boot()


def test_paused_crm_reminders_stay_paused(boot, jobstore_url):
    sched = boot()
    sched.pause_job("crm_task_reminders")
    _stop(sched)
    _stop(boot())
    assert _next_run(jobstore_url, "crm_task_reminders") is None
    assert _next_run(jobstore_url, "weekly_contact_dedupe") is not None
