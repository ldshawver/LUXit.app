"""Alert logic of deploy/monitoring/luxit_healthcheck.py (no network, no systemd)."""
import importlib.util
import os
from datetime import datetime, timezone

_PATH = os.path.join(os.path.dirname(__file__), '..', 'deploy', 'monitoring', 'luxit_healthcheck.py')
_spec = importlib.util.spec_from_file_location('luxit_healthcheck', _PATH)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

NOW = datetime(2026, 10, 31, 1, 20, 0, tzinfo=timezone.utc)


def _line(ts, path, status):
    return f'1.2.3.4 - - [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "GET {path} HTTP/1.1" {status} 10 "-" "ua"\n'


def test_error_rate_counts_only_window_and_ignores_sse_streams():
    old = NOW.replace(minute=0)
    lines = [_line(NOW, '/app', 200), _line(NOW, '/app', 502), _line(NOW, '/api/inbox/stream', 504),
             _line(old, '/app', 500), 'garbage line\n']
    assert hc.error_rate(lines, NOW, window_min=10) == (2, 1)


def test_error_rate_check_needs_minimum_traffic(tmp_path, monkeypatch):
    log = tmp_path / 'access.log'
    log.write_text(''.join(_line(NOW, '/x', 500) for _ in range(5)))
    monkeypatch.setattr(hc, 'ERROR_RATE_MIN_REQUESTS', 20)
    assert hc.check_error_rate(str(log), now=NOW)[0] is True
    log.write_text(''.join(_line(NOW, '/x', 500 if i % 4 == 0 else 200) for i in range(40)))
    ok, detail, action = hc.check_error_rate(str(log), now=NOW)
    assert not ok and '10/40' in detail and action


def _status(tmp_path, result, age_h, detail='file=x offsite=yes'):
    p = tmp_path / 'last-backup.status'
    p.write_text(f'result={result}\nfinished_epoch={1_000_000 - age_h * 3600}\ndetail={detail}\n')
    return str(p)


def test_backup_check_states(tmp_path):
    assert hc.check_backup(str(tmp_path / 'missing'), now_epoch=1_000_000)[0] is False
    assert hc.check_backup(_status(tmp_path, 'ok', 2), now_epoch=1_000_000)[0] is True
    assert hc.check_backup(_status(tmp_path, 'fail', 1), now_epoch=1_000_000)[0] is False
    ok, detail, _ = hc.check_backup(_status(tmp_path, 'ok', 30), now_epoch=1_000_000)
    assert not ok and '30.0 h' in detail
    ok, detail, action = hc.check_backup(_status(tmp_path, 'ok', 2, 'file=x offsite=no'), now_epoch=1_000_000)
    assert not ok and 'off-site' in detail and 'OFFSITE_CMD' in action


def test_alerts_fire_on_transition_repeat_and_recovery():
    failing = {'backup': (False, 'old', 'run it')}
    msgs, state = hc.decide_alerts(failing, {}, now_epoch=1000, repeat_minutes=60)
    assert msgs == ['FAILING backup: old\n  -> run it']
    msgs, state = hc.decide_alerts(failing, state, now_epoch=1000 + 600, repeat_minutes=60)
    assert msgs == []  # still failing, inside the repeat window
    msgs, state = hc.decide_alerts(failing, state, now_epoch=1000 + 3600, repeat_minutes=60)
    assert len(msgs) == 1
    msgs, state = hc.decide_alerts({'backup': (True, 'fine', '')}, state, now_epoch=5000)
    assert msgs == ['RECOVERED backup: fine'] and state['backup']['failing'] is False
    msgs, _ = hc.decide_alerts({'backup': (True, 'fine', '')}, state, now_epoch=6000)
    assert msgs == []


def test_unhealthy_http_body_is_a_failure(monkeypatch):
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"status": "degraded", "db": "error"}'
    monkeypatch.setattr(hc.urllib.request, 'urlopen', lambda *a, **k: Resp())
    ok, detail, action = hc.check_http('http://x/health')
    assert not ok and 'db=error' in detail and action
