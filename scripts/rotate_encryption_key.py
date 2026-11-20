#!/usr/bin/env python3
"""Re-encrypt every SecretVault value under a new ENCRYPTION_MASTER_KEY.

Rotation procedure (docs/ops/ENCRYPTION_KEY_ROTATION.md):
  1. Generate a new key; set ENCRYPTION_MASTER_KEY=<new> and
     ENCRYPTION_MASTER_KEY_PREVIOUS=<old> on every service, restart them
     (the app now encrypts with the new key and still reads the old one).
  2. python scripts/rotate_encryption_key.py            # dry run, counts only
     python scripts/rotate_encryption_key.py --apply    # one transaction
  3. Dry run again: `old_key` must be 0 everywhere. Then remove
     ENCRYPTION_MASTER_KEY_PREVIOUS and restart.

Vault values are base64(Fernet token), which always start with "Z0FBQUFB"
(base64 of "gAAAAA"). Rather than trust a hand-kept list of encrypted
columns, every text/varchar/json(b) column in the public schema is scanned,
including JSON documents that hold encrypted values (CompanySecret page
tokens, AdvancedConfig secrets). Values no key can decrypt are counted and
left untouched. Never prints a secret, only table.column counts.

Reads DATABASE_URL and the keys from the environment.
"""
import argparse
import base64
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg2  # noqa: E402
from cryptography.fernet import InvalidToken  # noqa: E402

from services.secret_vault import load_keys  # noqa: E402

MARKER = 'Z0FBQUFB'
TEXT_TYPES = ('text', 'character varying', 'json', 'jsonb')


class Rotator:
    def __init__(self, keys):
        if len(keys) < 2:
            raise SystemExit('need ENCRYPTION_MASTER_KEY (new) and ENCRYPTION_MASTER_KEY_PREVIOUS (old)')
        self.current, self.previous = keys[0], keys[1:]
        self.stats = Counter()

    def value(self, text):
        """Return (new_text, changed) for one stored vault value."""
        try:
            token = base64.b64decode(text.encode(), validate=True)
        except Exception:
            self.stats['not_a_token'] += 1
            return text, False
        try:
            self.current.decrypt(token)
            self.stats['current_key'] += 1
            return text, False
        except InvalidToken:
            pass
        for old in self.previous:
            try:
                plain = old.decrypt(token)
            except InvalidToken:
                continue
            self.stats['old_key'] += 1
            return base64.b64encode(self.current.encrypt(plain)).decode(), True
        self.stats['undecryptable'] += 1
        return text, False

    def walk(self, node):
        """Rotate vault strings anywhere inside a decoded JSON document."""
        if isinstance(node, str):
            return self.value(node) if node.startswith(MARKER) else (node, False)
        if isinstance(node, list):
            out, changed = [], False
            for item in node:
                new, c = self.walk(item)
                out.append(new)
                changed |= c
            return out, changed
        if isinstance(node, dict):
            out, changed = {}, False
            for k, item in node.items():
                new, c = self.walk(item)
                out[k] = new
                changed |= c
            return out, changed
        return node, False

    def cell(self, raw, data_type):
        if data_type in ('json', 'jsonb') and not isinstance(raw, str):
            new, changed = self.walk(raw)
            return (json.dumps(new) if changed else raw), changed
        if raw.startswith(MARKER):
            return self.value(raw)
        try:
            doc = json.loads(raw)
        except ValueError:
            self.stats['marker_in_plain_text'] += 1
            return raw, False
        new, changed = self.walk(doc)
        return (json.dumps(new) if changed else raw), changed


def columns(cur):
    cur.execute(
        """select c.table_name, c.column_name, c.data_type
             from information_schema.columns c
             join information_schema.tables t
               on t.table_schema = c.table_schema and t.table_name = c.table_name
            where c.table_schema = 'public' and t.table_type = 'BASE TABLE'
              and c.data_type = any(%s)
            order by 1, 2""", (list(TEXT_TYPES),))
    return cur.fetchall()


def run(conn, rotator, apply):
    report = {}
    with conn.cursor() as cur:
        for table, column, data_type in columns(cur):
            before = rotator.stats.copy()
            cur.execute(
                f'select ctid, "{column}" from "{table}" where "{column}"::text like %s'
                + (' for update' if apply else ''), (f'%{MARKER}%',))
            updates = []
            for ctid, raw in cur.fetchall():
                new, changed = rotator.cell(raw, data_type)
                if changed:
                    updates.append((new, ctid))
            if apply:
                for new, ctid in updates:
                    cur.execute(f'update "{table}" set "{column}" = %s where ctid = %s', (new, ctid))
            delta = rotator.stats - before
            if delta or updates:
                report[f'{table}.{column}'] = dict(delta, rows_rotated=len(updates))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--apply', action='store_true', help='write changes (one transaction)')
    args = ap.parse_args(argv)
    rotator = Rotator(load_keys())
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    try:
        report = run(conn, rotator, args.apply)
        if rotator.stats['undecryptable'] and args.apply:
            print('note: undecryptable values were left untouched', file=sys.stderr)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()
    for col, counts in report.items():
        print(col, json.dumps(counts, sort_keys=True))
    print('TOTAL', json.dumps(dict(rotator.stats), sort_keys=True), 'mode=' + ('apply' if args.apply else 'dry-run'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
