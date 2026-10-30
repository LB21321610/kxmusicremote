"""Audit regressions using isolated HTTP, SQLite, SMTC and launcher fixtures."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from test_lyrics import remote


class OriginTests(unittest.TestCase):
    def test_all_writes_reject_foreign_origins_and_allow_native_clients(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), remote.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(route, headers):
            try:
                with urlopen(Request(base+route, data=b'', headers=headers), timeout=3) as res:
                    return res.status
            except HTTPError as exc:
                return exc.code
        try:
            with patch.object(remote, 'set_volume') as volume, patch.object(remote, '_transport', AsyncMock()) as transport:
                for origin in ('http://untrusted.example', 'null', base.replace('http:', 'https:'), base+'/path', 'http://[broken'):
                    for route in ('/cmd/vol/37', '/cmd/toggle', '/queue/play', '/settings/lyrics', '/lyrics/translation'):
                        self.assertEqual(post(route, {'Origin': origin, 'Content-Type': 'text/plain'}), 403)
                self.assertEqual(post('/cmd/vol/37', {'Sec-Fetch-Site': 'cross-site'}), 403)
                volume.assert_not_called(); transport.assert_not_called()
                for headers in ({}, {'Origin': base}, {'Sec-Fetch-Site': 'same-origin'}):
                    self.assertEqual(post('/cmd/vol/37', headers), 204)
                self.assertEqual(volume.call_count, 3)
        finally:
            server.shutdown(); server.server_close()


class QueueConfirmationTests(unittest.TestCase):
    item = dict(t='Same title', a='Requested artist', d='3:00')
    target = dict(title='Same title', artist='Requested artist', album='Album', duration=180, position=40, playing=True)

    def confirm(self, snapshots, before=None, timeout=.01):
        kwargs = {'side_effect': snapshots} if isinstance(snapshots, list) else {'return_value': snapshots}
        with patch.object(remote, '_queue_playback_snapshot', AsyncMock(**kwargs)):
            asyncio.run(remote._confirm_queue_play(self.item, before, timeout=timeout))

    def test_wrong_artist_and_duration_cannot_confirm(self):
        for current in (dict(self.target, artist='Wrong artist'), dict(self.target, duration=200)):
            with self.assertRaisesRegex(RuntimeError, '尚未确认'):
                self.confirm(current)

    def test_same_recording_noop_does_not_confirm(self):
        with self.assertRaisesRegex(RuntimeError, '尚未确认'):
            self.confirm(self.target, before=self.target)

    def test_switch_restart_resume_and_transition_confirm(self):
        self.confirm(self.target, before=dict(self.target, artist='Other'))
        self.confirm(dict(self.target, position=0), before=self.target)
        self.confirm(self.target, before=dict(self.target, playing=False))
        self.confirm([dict(self.target, playing=False), self.target], before=self.target, timeout=.5)

    def test_snapshot_splits_composite_artist(self):
        from types import SimpleNamespace as NS
        from datetime import timedelta
        session = NS(try_get_media_properties_async=AsyncMock(return_value=NS(
            title='Ｓame Title', artist='Requested Artist — Album [Explicit]', album_title='')),
            get_timeline_properties=lambda: NS(end_time=timedelta(seconds=180), position=timedelta(seconds=5)),
            get_playback_info=lambda: NS(playback_status=remote.PlaybackStatus.PLAYING))
        with patch.object(remote, '_session', AsyncMock(return_value=session)):
            snapshot = asyncio.run(remote._queue_playback_snapshot())
        self.assertEqual(snapshot['artist'], 'Requested Artist')
        self.assertTrue(remote._queue_track_matches(snapshot, self.item))
        composite = dict(self.item, a='Requested Artist — Album [Explicit]')
        self.assertTrue(remote._queue_track_matches(snapshot, composite))
        self.assertFalse(remote._queue_track_matches(dict(snapshot, album='Other album'), composite))


class LauncherRecoveryTests(unittest.TestCase):
    def run_launcher(self, action, url, owner='none'):
        root = Path(remote.__file__).parent
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name in ('Remote-With-Translation.ps1', 'Remote-Process.ps1'):
                shutil.copy2(root/name, directory/name)
            for operation in ('Start', 'Stop'):
                (directory/f'{operation}-Remote.ps1').write_text(
                    f"'{operation}' | Add-Content -LiteralPath (Join-Path $PSScriptRoot 'actions.txt')", encoding='utf-8-sig')
            script = directory/'harness.ps1'
            shutil.copy2(root/'tests/launcher_fixture.ps1', script)
            run = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                                  action, url, owner], capture_output=True, timeout=15)
            actions = (directory/'actions.txt').read_text().strip() if (directory/'actions.txt').exists() else ''
            killed = (directory/'killed.txt').read_text().strip() if (directory/'killed.txt').exists() else ''
            return run.returncode, actions, killed, (directory/'.remote-translation-process.json').exists()

    def test_bad_url_does_not_block_start_or_stop(self):
        for url in ('https://127.0.0.1:11434', 'not-a-url', 'http://[broken'):
            for action in ('Start', 'Stop'):
                code, actions, killed, _ = self.run_launcher(action, url)
                self.assertEqual(code, 0); self.assertEqual(actions, action); self.assertEqual(killed, '')

    def test_stop_cleans_owned_process_with_changed_config(self):
        code, actions, killed, state = self.run_launcher('Stop', 'invalid-url', 'valid')
        self.assertEqual(code, 0); self.assertEqual(actions, 'Stop')
        self.assertEqual(killed, '12345'); self.assertFalse(state)

    def test_stale_process_identity_remains_protected(self):
        code, actions, killed, state = self.run_launcher('Stop', 'invalid-url', 'stale')
        self.assertNotEqual(code, 0); self.assertEqual(actions, 'Stop')
        self.assertEqual(killed, ''); self.assertTrue(state)

    def test_valid_url_and_model_load_unload_keep_working(self):
        calls = []
        class Ollama(BaseHTTPRequestHandler):
            def do_GET(self):
                data = b'{"models":[{"name":"test-model:latest"}]}'
                self.send_response(200); self.end_headers(); self.wfile.write(data)
            def do_POST(self):
                calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Ollama)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            code, actions, _, _ = self.run_launcher('Start', url)
            self.assertEqual((code, actions), (0, 'Start'))
            code, actions, killed, state = self.run_launcher('Stop', url, 'valid')
            self.assertEqual((code, actions, killed, state), (0, 'Stop', '12345', False))
            self.assertEqual([c['keep_alive'] for c in calls], ['10m', 0])
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
