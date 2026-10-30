"""Shared server settings; independent of the project and working directory."""
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import threading


def valid_offset(value):
    return (type(value) in (int, float) and -10 <= value <= 10
            and math.isfinite(value))


class SettingsStore:
    def __init__(self, path=None):
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'AppleMusicRemote'
        self.path = Path(path) if path else base / 'settings.json'
        if not self.path.is_absolute():
            raise ValueError('Settings path must be absolute')
        self.lock = threading.Lock()

    @contextmanager
    def _locked(self):
        # Serialize threads AND processes, including independent server instances.
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix('.lock').open('a+b') as handle:
                if os.name == 'nt':
                    import msvcrt
                    # Windows locks byte ranges beyond EOF. Writing a sentinel
                    # before acquiring the lock races with another process
                    # holding it and can fail with PermissionError on first use.
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == 'nt':
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self):
        try:
            doc = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            doc = {}
        if (not isinstance(doc, dict) or not valid_offset(doc.get('lyric_offset', 0))
                or type(doc.get('revision', 0)) is not int or doc.get('revision', 0) < 0):
            raise ValueError('Invalid settings file')
        return doc

    @staticmethod
    def _snapshot(doc):
        return dict(ok=True, lyric_offset=doc.get('lyric_offset', 0), revision=doc.get('revision', 0))

    def snapshot(self):
        try:
            with self._locked():
                return self._snapshot(self._read())
        except (OSError, ValueError):
            logging.exception('Cannot read lyric settings')
            return dict(ok=False, error='settings_unavailable')

    def update(self, offset, revision):
        if not valid_offset(offset) or type(revision) is not int or revision < 0:
            return 400, dict(ok=False, error='invalid_settings')
        try:
            with self._locked():
                doc = self._read()
                if revision != doc.get('revision', 0):
                    return 409, dict(self._snapshot(doc), ok=False, error='settings_conflict')
                if offset == doc.get('lyric_offset', 0):
                    return 200, self._snapshot(doc)
                doc.update(lyric_offset=offset, revision=revision + 1)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                            dir=self.path.parent, prefix='settings-', suffix='.tmp', delete=False) as handle:
                        temporary = handle.name
                        json.dump(doc, handle, ensure_ascii=False, allow_nan=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    # Packaged Windows hosts can redirect a nominal C:\\AppData
                    # write onto another volume. Resolve the CREATED file and
                    # replace its sibling, so both paths address the same real
                    # directory even when the target does not exist yet.
                    actual = Path(temporary).resolve(strict=True)
                    os.replace(actual, actual.with_name(self.path.name))
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
                return 200, self._snapshot(doc)
        except (OSError, ValueError):
            logging.exception('Cannot save lyric settings')
            return 503, dict(ok=False, error='settings_save_failed')
