"""Offline production store/API verification, including concurrent Windows writes."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from test_lyrics import remote
from remote_settings import SettingsStore


def process_write(path, value):
    return SettingsStore(Path(path)).update(value, 0)[0]


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.json'
        self.store = SettingsStore(self.path)

    def test_default_decimal_bounds_restart_and_preserve_unknown_data(self):
        self.assertEqual(self.store.snapshot()['lyric_offset'], 0)
        self.path.write_text(json.dumps(dict(legacy_tracks={'song': 2}, unrelated='keep')))
        for value in (-10, -.5, .125, 10, 0):
            code, data = self.store.update(value, self.store.snapshot()['revision'])
            self.assertEqual(code, 200)
            self.assertEqual(SettingsStore(self.path).snapshot(), data)
        self.assertEqual(json.loads(self.path.read_text())['legacy_tracks'], {'song': 2})
        self.assertEqual(json.loads(self.path.read_text())['unrelated'], 'keep')

    def test_invalid_values_and_stale_revision_do_not_write(self):
        self.assertEqual(self.store.update(.5, 0)[0], 200)
        before = self.path.read_bytes()
        for value in (True, False, None, '.5', [], {}, float('nan'), float('inf'), -float('inf'),
                      10.01, -10.01, 10**1000):
            self.assertEqual(self.store.update(value, 1)[0], 400)
        for rev in (True, -1, '1', 1.0):
            self.assertEqual(self.store.update(0, rev)[0], 400)
        self.assertEqual(self.store.update(-.5, 0)[0], 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_interrupted_atomic_replace_preserves_saved_value(self):
        self.store.update(.5, 0)
        before = self.path.read_bytes()
        with patch('remote_settings.os.replace', side_effect=PermissionError('fixture')):
            self.assertEqual(self.store.update(-.5, 1)[0], 503)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot()['lyric_offset'], .5)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_redirected_temp_file_replaces_sibling_on_its_actual_volume(self):
        # Model Windows package redirection: the requested directory and the
        # newly created file's real directory differ. A nonexistent destination
        # cannot be resolved on its own.
        actual_directory = self.path.parent / 'redirected'
        actual_directory.mkdir()
        real_temporary_file = tempfile.NamedTemporaryFile

        def redirected_temporary_file(**kwargs):
            self.assertEqual(kwargs['dir'], self.path.parent)
            kwargs['dir'] = actual_directory
            return real_temporary_file(**kwargs)

        with patch('remote_settings.tempfile.NamedTemporaryFile', side_effect=redirected_temporary_file), \
                patch('remote_settings.os.replace', wraps=os.replace) as replace:
            code, saved = self.store.update(-.5, 0)
        self.assertEqual(code, 200)
        source, destination = replace.call_args.args
        self.assertEqual(source.parent, actual_directory.resolve())
        self.assertEqual(destination, actual_directory.resolve() / self.path.name)
        self.assertEqual(json.loads(destination.read_text())['lyric_offset'], -.5)
        self.assertEqual(saved['revision'], 1)
        self.assertFalse(source.exists())

    def test_actual_appdata_directory_save_and_reload(self):
        # Test the real AppData location as well as Temp: packaged Windows hosts
        # virtualize the former but often leave Temp writes unchanged.
        path = SettingsStore().path.with_name('settings-save-test-' + uuid.uuid4().hex + '.json')
        self.addCleanup(lambda: path.with_suffix('.lock').unlink(missing_ok=True))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        store = SettingsStore(path)
        for value in (-.5, .125, 0):
            code, saved = store.update(value, store.snapshot()['revision'])
            self.assertEqual(code, 200)
            self.assertEqual(SettingsStore(path).snapshot(), saved)

    def test_corrupt_existing_file_is_not_overwritten(self):
        self.path.write_text('{interrupted')
        self.assertFalse(self.store.snapshot()['ok'])
        self.assertEqual(self.store.update(.5, 0)[0], 503)
        self.assertEqual(self.path.read_text(), '{interrupted')

    def test_concurrent_threads_and_processes_only_one_revision_wins(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda i: self.store.update(i / 10, 0)[0], range(1, 9)))
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(409), 7)
        # Separate stores/processes use the OS file lock, not the Python lock.
        second = self.path.parent / 'process.json'
        with multiprocessing.get_context('spawn').Pool(4) as pool:
            codes = pool.starmap(process_write, [(str(second), i / 10) for i in range(1, 5)])
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(409), 3)
        self.assertEqual(SettingsStore(second).snapshot()['revision'], 1)

    def test_default_path_does_not_depend_on_working_directory(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name}):
            store = SettingsStore()
            self.assertEqual(store.path, Path(self.temp.name) / 'AppleMusicRemote' / 'settings.json')
        with self.assertRaises(ValueError):
            SettingsStore('relative.json')

    def test_actual_server_process_restart_from_different_working_directory(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        environment = dict(os.environ, LOCALAPPDATA=self.temp.name, PYTHONIOENCODING='utf-8')
        script = Path(remote.__file__).resolve()
        for index in range(2):
            cwd = self.path.parent / f'launch-{index}'
            cwd.mkdir()
            process = subprocess.Popen([sys.executable, str(script), '--port', str(port)],
                cwd=cwd, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                deadline = time.monotonic() + 15
                while True:
                    self.assertIsNone(process.poll(), 'Production server exited early')
                    try:
                        with urlopen(base + '/settings/lyrics', timeout=1) as response:
                            saved = json.load(response)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail('Production server did not become ready')
                        time.sleep(.1)
                if index == 0:
                    payload = json.dumps(dict(lyric_offset=-.5, revision=saved['revision'])).encode()
                    with urlopen(Request(base + '/settings/lyrics', data=payload,
                            headers={'Content-Type': 'application/json'})) as response:
                        self.assertEqual(json.load(response)['lyric_offset'], -.5)
                else:
                    self.assertEqual(saved['lyric_offset'], -.5)
                    self.assertEqual(saved['revision'], 1)
            finally:
                process.terminate()
                process.wait(timeout=10)
        expected = self.path.parent / 'AppleMusicRemote' / 'settings.json'
        self.assertEqual(json.loads(expected.read_text())['lyric_offset'], -.5)

    def test_production_http_validation_status_and_restart(self):
        async def offline_status():
            return dict(ok=False, err='Apple Music closed')
        with patch.object(remote, 'LYRIC_SETTINGS', self.store), patch.object(remote, '_status', offline_status):
            server = ThreadingHTTPServer(('127.0.0.1', 0), remote.Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'

                def post(body, ctype='application/json'):
                    request = Request(base + '/settings/lyrics', data=body,
                                      headers={'Content-Type': ctype})
                    try:
                        with urlopen(request) as response:
                            return response.status, json.load(response)
                    except HTTPError as exc:
                        return exc.code, exc.read()

                self.assertEqual(post(b'{"lyric_offset":-0.5,"revision":0}')[0], 200)
                for body in (b'{', b'[]', b'{"lyric_offset":true,"revision":1}',
                             b'{"lyric_offset":NaN,"revision":1}',
                             b'{"lyric_offset":Infinity,"revision":1}',
                             b'{"lyric_offset":11,"revision":1}',
                             b'{"lyric_offset":0,"revision":1,"extra":1}'):
                    self.assertEqual(post(body)[0], 400)
                self.assertEqual(post(b'{}', 'text/plain')[0], 400)
                self.assertEqual(post(b'{"lyric_offset":0.5,"revision":0}')[0], 409)
                remote.LYRIC_SETTINGS = SettingsStore(self.path)  # Restart/reload the server-owned store.
                with urlopen(base + '/status') as response:
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    self.assertEqual(json.load(response)['lyric_settings']['lyric_offset'], -.5)
                with patch('remote_settings.os.replace', side_effect=PermissionError('fixture')):
                    self.assertEqual(post(b'{"lyric_offset":0.5,"revision":1}')[0], 503)
                with urlopen(base + '/settings/lyrics') as response:
                    self.assertEqual(json.load(response)['lyric_offset'], -.5)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
