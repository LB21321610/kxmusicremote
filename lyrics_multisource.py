# SPDX-License-Identifier: GPL-3.0-only
"""Bounded Windows-side LDDC jobs and persistent content cache."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing

from lyrics_matching import choose_candidate, fingerprint, track_key

ROOT = Path(__file__).resolve().parent
SCHEMA = 'lddc-84631e8-recording-v3'
USABLE = ('synced', 'plain', 'instrumental')


def run_worker(command, deadline):
    budget = min(11, deadline - time.monotonic() - .5)
    if budget <= 0:
        return dict(error='timeout')
    # The helper never needs DeepL secrets and cannot expose them via upstream logs.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('DEEPL')}
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'lddc_worker.py')],
            input=json.dumps(dict(command, budget=budget)).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=ROOT, env=env, timeout=budget + .5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode or len(result.stdout) > 4_000_000:
            return dict(error='worker_failed')
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return dict(error='timeout')
    except Exception:
        return dict(error='worker_unavailable')


class MultiSourceLyrics:
    def __init__(self, path=None, worker=run_worker):
        self.path = Path(path or ROOT / 'lyrics_sources.sqlite3')
        self.worker = worker
        self.lock = threading.RLock()
        self.pending = {}

    def _read(self, key):
        with self.lock, closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS lyrics_cache (key TEXT PRIMARY KEY, value TEXT, expires REAL)')
            row = db.execute('SELECT value,expires FROM lyrics_cache WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row and row[1] > time.time() else None

    def _write(self, key, value, ttl=86400):
        with self.lock, closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute('INSERT OR REPLACE INTO lyrics_cache VALUES (?,?,?)',
                       (key, json.dumps(value, ensure_ascii=False), time.time() + ttl))
            db.execute('DELETE FROM lyrics_cache WHERE key IN (SELECT key FROM lyrics_cache ORDER BY expires DESC LIMIT -1 OFFSET 512)')

    def _merged(self, key, call, deadline):
        """Merge discovery across requests for different versions of one track."""
        with self.lock:
            # A different caller may have filled the disk cache between our
            # initial read and acquiring the job lock.
            cached = self._read(key)
            if cached is not None:
                return cached
            event = self.pending.get(key)
            owner = event is None
            if owner:
                event = self.pending[key] = threading.Event()
        if not owner:
            event.wait(max(0, deadline - time.monotonic()))
            return self._read(key) or dict(error='timeout')
        try:
            value = call()
            self._write(key, value, 30 if value.get('error') else 86400 if value.get('payload') or value.get('candidates') else 300)
            return value
        finally:
            with self.lock:
                self.pending.pop(key, None)
                event.set()

    def load(self, track, deadline, fallback, candidate='auto'):
        key = SCHEMA + ':' + track_key(track)
        bundle = self._read(key)
        disk_hit = bundle is not None
        if bundle is None:
            bundle = self._merged(key, lambda: self.worker(dict(operation='discover', track=track), deadline), deadline)
        candidates = copy.deepcopy(bundle.get('candidates', []))
        chosen = next((c for c in candidates if c['id'] == candidate), None) if candidate != 'auto' else choose_candidate(candidates)
        if candidate != 'auto' and not chosen:
            return dict(ok=False, state='error', source='LDDC', error='candidate_expired')
        result = None
        # Automatic order: LDDC exact, LRCLIB fallback exact, LDDC approximate,
        # fallback approximate. A manually chosen candidate is used as is.
        if candidate == 'auto' and (not chosen or chosen.get('approximate')):
            # Keep the existing LRCLIB exact-get/backoff path when the helper
            # cannot find a reliable usable recording (including missing deps).
            backup = fallback(track, deadline)
            for field in ('lyrics_id', 'content_hash', 'display_rows'):
                backup.pop(field, None)
            if backup.get('state') in USABLE:
                approximate = backup.get('match_quality') == 'approximate'
                fallback_id = 'LRCLIB-' + fingerprint(backup.get('record', {}))[:24]
                backup['candidate_id'] = fallback_id
                if not any(c['id'] == fallback_id for c in candidates):
                    candidates.append(dict(id=fallback_id, source='LRCLIB', record=backup.get('record', {}), score=0,
                                           approximate=approximate, payload=copy.deepcopy(backup)))
                # Also cache the fallback payload so choosing it later requires
                # neither another source request nor a new translation mapping.
                self._write(key, dict(bundle, candidates=candidates), 86400)
                if not approximate or not chosen:
                    result, chosen = backup, None
            elif not chosen:
                result = backup
        if result is None:
            if not chosen.get('payload'):
                payload_key = SCHEMA + ':candidate:' + fingerprint(chosen['descriptor'])
                fetched = self._read(payload_key)
                if fetched is None:
                    fetched = self._merged(payload_key, lambda: self.worker(dict(operation='fetch', descriptor=chosen['descriptor']), deadline), deadline)
                if fetched.get('payload'):
                    chosen['payload'] = fetched['payload']
                else:
                    return dict(ok=False, state='error', source=chosen['source'], error='source_unavailable')
            result = dict(chosen['payload'], ok=True, source=chosen['source'], record=chosen['record'], candidate_id=chosen['id'])
        if result.get('state') in USABLE:
            approximate = chosen.get('approximate') if chosen else result.get('match_quality') == 'approximate'
            result['match_quality'] = 'approximate' if approximate else 'exact'
        result.update(candidates=[dict(id=c['id'], source=c['source'], record=c['record'], score=c['score'],
                                      approximate=bool(c.get('approximate')),
                                      state=c.get('payload', {}).get('state', 'unloaded')) for c in candidates],
                      source_status=bundle.get('source_status', {'LDDC': bundle.get('error', 'unavailable')}),
                      source_cache_hit=disk_hit, track_id=track_key(track),
                      match_requires_selection=not chosen and bool(candidates))
        if result.get('state') == 'not_found':
            result['source'] = 'LDDC 多源'
        return result


service = MultiSourceLyrics()
