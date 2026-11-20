#!/usr/bin/env python3
"""LUXit production health check with actionable alerts.

Runs every few minutes from a systemd timer (luxit-healthcheck.timer). Each
check returns (ok, detail, action); a failing check names what an operator
should do next. Alerts fire on state change (ok -> failing, failing -> ok) and
re-fire for a check that stays failing every REPEAT_MINUTES, so the channel is
not flooded.

Standard library only; reads nothing secret. Alert channels (any or none):
  ALERT_WEBHOOK_URL   POST JSON {"text": ...} (Slack / Teams / generic)
  ALERT_EMAIL_TO      piped to `sendmail -t` when sendmail exists
With no channel configured, alerts go to stderr (the journal) and the exit
code is non-zero, so `systemctl --failed` still shows them.

Read-only: makes HTTP GETs, reads log/status files, runs `systemctl is-active`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HEALTH_URLS = [u for u in os.environ.get(
    'HEALTH_URLS', 'http://127.0.0.1:8001/health https://luxit.app/health').split() if u]
UNITS = os.environ.get(
    'UNITS', 'lux-email-bot.service luxit-tuya-notification-worker.service postgresql@17-main.service').split()
ACCESS_LOG = os.environ.get('ACCESS_LOG', '/var/log/nginx/access.log')
ERROR_RATE_WINDOW_MIN = int(os.environ.get('ERROR_RATE_WINDOW_MIN', '10'))
ERROR_RATE_MAX = float(os.environ.get('ERROR_RATE_MAX', '0.05'))
ERROR_RATE_MIN_REQUESTS = int(os.environ.get('ERROR_RATE_MIN_REQUESTS', '20'))
BACKUP_STATUS = os.environ.get('BACKUP_STATUS', '/var/backups/luxit/last-backup.status')
BACKUP_MAX_AGE_H = float(os.environ.get('BACKUP_MAX_AGE_H', '26'))
DISK_PATHS = os.environ.get('DISK_PATHS', '/ /var/lib/postgresql').split()
DISK_MAX_PCT = float(os.environ.get('DISK_MAX_PCT', '85'))
MEM_MIN_AVAIL_MB = int(os.environ.get('MEM_MIN_AVAIL_MB', '400'))
TLS_HOSTS = os.environ.get('TLS_HOSTS', 'luxit.app app.luxit.app').split()
TLS_MIN_DAYS = int(os.environ.get('TLS_MIN_DAYS', '14'))
STATE_FILE = os.environ.get('STATE_FILE', '/var/lib/luxit-healthcheck/state.json')
REPEAT_MINUTES = int(os.environ.get('REPEAT_MINUTES', '60'))
HOSTNAME = socket.gethostname()

_LOG_RE = re.compile(r'\[(?P<ts>[^\]]+)\] "(?P<req>[^"]*)" (?P<status>\d{3}) ')


def check_http(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or '{}')
            code = resp.status
    except Exception as exc:  # noqa: BLE001 - any failure is the signal
        return False, f'{url} unreachable: {exc.__class__.__name__}: {exc}', \
            'check `systemctl status lux-email-bot` and `journalctl -u lux-email-bot -n 100`; nginx if only the public URL fails'
    bad = [k for k in ('status', 'db') if body.get(k) not in ('ok', 'connected')]
    if code != 200 or bad:
        return False, f'{url} HTTP {code} status={body.get("status")} db={body.get("db")}', \
            'db!=connected: check postgresql@17-main and DATABASE_URL; otherwise read the app journal'
    return True, f'{url} ok scheduler={body.get("scheduler")}', ''


def check_unit(unit):
    try:
        state = subprocess.run(['systemctl', 'is-active', unit], capture_output=True,
                               text=True, timeout=10).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        state = f'error: {exc}'
    if state == 'active':
        return True, f'{unit} active', ''
    return False, f'{unit} is {state}', f'`journalctl -u {unit} -n 200`; restart only per the runbook'


def error_rate(lines, now, window_min=ERROR_RATE_WINDOW_MIN):
    """Return (total, five_xx) for log lines inside the last window_min minutes."""
    since = now - timedelta(minutes=window_min)
    total = errors = 0
    for line in lines:
        m = _LOG_RE.search(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group('ts'), '%d/%b/%Y:%H:%M:%S %z')
        except ValueError:
            continue
        if ts < since:
            continue
        # long-lived SSE streams end with 499/504 by design; not server errors
        if '/stream' in m.group('req'):
            continue
        total += 1
        if m.group('status').startswith('5'):
            errors += 1
    return total, errors


def check_error_rate(path=ACCESS_LOG, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        with open(path, errors='replace') as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4_000_000))
            lines = fh.readlines()
    except OSError as exc:
        return False, f'cannot read {path}: {exc}', 'fix log path/permissions (monitor runs as root)'
    total, errors = error_rate(lines, now)
    if total >= ERROR_RATE_MIN_REQUESTS and errors / total > ERROR_RATE_MAX:
        return False, f'5xx {errors}/{total} in last {ERROR_RATE_WINDOW_MIN} min', \
            f'`grep \'" 5[0-9][0-9] \' {path} | tail`, then the app journal for tracebacks'
    return True, f'5xx {errors}/{total} in last {ERROR_RATE_WINDOW_MIN} min', ''


def check_backup(path=BACKUP_STATUS, now_epoch=None):
    now_epoch = now_epoch or time.time()
    try:
        with open(path) as fh:
            status = dict(line.rstrip('\n').split('=', 1) for line in fh if '=' in line)
    except OSError:
        return False, f'no backup status at {path}', 'backup timer not installed or never ran: see deploy/backup/README.md'
    age_h = (now_epoch - float(status.get('finished_epoch', 0))) / 3600
    if status.get('result') != 'ok':
        return False, f'last backup FAILED: {status.get("detail")}', '`journalctl -u luxit-db-backup -n 50`'
    if age_h > BACKUP_MAX_AGE_H:
        return False, f'last good backup {age_h:.1f} h old', '`systemctl list-timers luxit-db-backup.timer`'
    if 'offsite=no' in status.get('detail', ''):
        return False, f'backup {age_h:.1f} h old but NOT copied off-site', 'set OFFSITE_CMD in the backup drop-in'
    return True, f'backup ok {age_h:.1f} h old', ''


def check_disk(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return False, f'{path}: {exc}', ''
    pct = usage.used / usage.total * 100
    if pct > DISK_MAX_PCT:
        return False, f'{path} {pct:.0f}% full', 'prune old dumps/logs (`du -xh --max-depth=2 / | sort -h | tail`)'
    return True, f'{path} {pct:.0f}% used', ''


def check_memory(meminfo='/proc/meminfo'):
    with open(meminfo) as fh:
        avail = next(int(l.split()[1]) // 1024 for l in fh if l.startswith('MemAvailable'))
    if avail < MEM_MIN_AVAIL_MB:
        return False, f'MemAvailable {avail} MB', 'host has no swap: find the RSS hog (`ps aux --sort=-rss | head`) before the OOM killer picks prod'
    return True, f'MemAvailable {avail} MB', ''


def check_tls(host, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=8) as sock, \
                ctx.wrap_socket(sock, server_hostname=host) as tls:
            not_after = tls.getpeercert()['notAfter']
    except Exception as exc:  # noqa: BLE001
        return False, f'TLS {host}: {exc}', 'check nginx and certbot'
    expires = datetime.strptime(not_after, '%b %d %H:%M:%S %Y %Z').replace(tzinfo=timezone.utc)
    days = (expires - now).days
    if days < TLS_MIN_DAYS:
        return False, f'TLS {host} expires in {days} d', '`certbot renew --dry-run`; check certbot.timer'
    return True, f'TLS {host} {days} d left', ''


def run_checks():
    results = {}
    for url in HEALTH_URLS:
        results[f'http:{url}'] = check_http(url)
    for unit in UNITS:
        results[f'unit:{unit}'] = check_unit(unit)
    results['nginx:5xx'] = check_error_rate()
    results['backup'] = check_backup()
    for path in DISK_PATHS:
        if os.path.exists(path):
            results[f'disk:{path}'] = check_disk(path)
    results['memory'] = check_memory()
    for host in TLS_HOSTS:
        results[f'tls:{host}'] = check_tls(host)
    return results


def decide_alerts(results, state, now_epoch, repeat_minutes=REPEAT_MINUTES):
    """Return (messages, new_state). state maps check -> {"failing": bool, "alerted": epoch}."""
    messages, new_state = [], {}
    for name, (ok, detail, action) in results.items():
        prev = state.get(name, {'failing': False, 'alerted': 0})
        if ok:
            if prev['failing']:
                messages.append(f'RECOVERED {name}: {detail}')
            new_state[name] = {'failing': False, 'alerted': 0}
            continue
        due = not prev['failing'] or now_epoch - prev['alerted'] >= repeat_minutes * 60
        if due:
            messages.append(f'FAILING {name}: {detail}' + (f'\n  -> {action}' if action else ''))
        new_state[name] = {'failing': True, 'alerted': now_epoch if due else prev['alerted']}
    return messages, new_state


def send(messages):
    text = f'[LUXit {HOSTNAME}] ' + '\n'.join(messages)
    print(text, file=sys.stderr)
    url = os.environ.get('ALERT_WEBHOOK_URL')
    if url:
        req = urllib.request.Request(url, data=json.dumps({'text': text}).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:  # noqa: BLE001
            print(f'alert webhook failed: {exc}', file=sys.stderr)
    to = os.environ.get('ALERT_EMAIL_TO')
    sendmail = shutil.which('sendmail')
    if to and sendmail:
        subject = 'LUXit ALERT' if any(m.startswith('FAILING') for m in messages) else 'LUXit recovered'
        mail = f'To: {to}\nSubject: [{HOSTNAME}] {subject}\n\n{text}\n'
        subprocess.run([sendmail, '-t'], input=mail, text=True, timeout=30, check=False)


def main(argv):
    results = run_checks()
    if '--print' in argv:
        for name, (ok, detail, _) in results.items():
            print(f'{"OK  " if ok else "FAIL"} {name}: {detail}')
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    messages, new_state = decide_alerts(results, state, time.time())
    if messages and '--no-alert' not in argv:
        send(messages)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + '.tmp', 'w') as fh:
        json.dump(new_state, fh)
    os.replace(STATE_FILE + '.tmp', STATE_FILE)
    return 1 if any(not ok for ok, _, _ in results.values()) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
