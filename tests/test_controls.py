"""Regression tests for wrong-window routing and silent playback failures."""
import asyncio
from contextlib import nullcontext
import json
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
from test_lyrics import remote


class ControlsTests(unittest.TestCase):
    def test_browser_title_never_wins_over_native_process(self):
        browser = NS(Name='Apple Music Remote - Google Chrome', ProcessId=1,
                     ClassName='Chrome_WidgetWin_1', ControlTypeName='WindowControl')
        app = NS(Name='Apple Music', ProcessId=2,
                 ClassName='WinUIDesktopWin32WindowClass', ControlTypeName='WindowControl')
        with patch.object(remote.uiautomation, 'GetProcesses', return_value=[NS(pid=2, exeName='AppleMusic.exe')]), \
             patch.object(remote.uiautomation, 'GetRootControl') as root:
            root.return_value.GetChildren.return_value = [browser, app]
            self.assertIs(remote._window(), app)
            root.return_value.GetChildren.return_value = [browser]
            with self.assertRaisesRegex(RuntimeError, 'Apple Music'):
                remote._window()

    def test_no_fallback_to_another_apps_media_session(self):
        manager = Mock()
        manager.get_sessions.return_value = [NS(source_app_user_model_id='Chrome')]
        with patch.object(remote, 'SessionManager', NS(request_async=AsyncMock(return_value=manager))):
            self.assertIsNone(asyncio.run(remote._session()))
            manager.get_current_session.assert_not_called()

    def test_transport_rejection_is_reported(self):
        session = Mock(try_toggle_play_pause_async=AsyncMock(return_value=False))
        with patch.object(remote, '_session', AsyncMock(return_value=session)):
            with self.assertRaises(RuntimeError):
                asyncio.run(remote._transport('toggle'))

    def test_seek_limits_and_missing_or_readonly_slider(self):
        pattern = Mock(Minimum=0, Maximum=180, IsReadOnly=False)
        slider = Mock()
        slider.GetPattern.return_value = pattern
        with patch.object(remote, '_window'), patch.object(remote, '_app_windows', return_value=[]), \
             patch.object(remote.uiautomation, 'UIAutomationInitializerInThread', return_value=nullcontext()), \
             patch.object(remote.uiautomation, 'SliderControl', return_value=slider):
            remote.seek(999)
            pattern.SetValue.assert_called_with(180)
            remote.seek(-10)
            pattern.SetValue.assert_called_with(0)
            pattern.IsReadOnly = True
            with self.assertRaisesRegex(RuntimeError, '不支持'):
                remote.seek(10)
            slider.Exists.return_value = False
            with self.assertRaisesRegex(RuntimeError, '进度条不可用'):
                remote.seek(10)

    def test_chinese_repeat_states(self):
        self.assertEqual(remote.REPEAT_STATES['全部重复播放'], 'all')
        self.assertEqual(remote.REPEAT_STATES['单曲重复播放'], 'one')
        self.assertEqual(remote.REPEAT_STATES['不重复播放'], 'none')

    def test_queue_restores_panel_after_read_failure(self):
        panel = {'state': 0}
        with patch.object(remote, '_window'), \
             patch.object(remote.uiautomation, 'UIAutomationInitializerInThread', return_value=nullcontext()), \
             patch.object(remote, '_queue_panel_state', side_effect=lambda _: panel['state']), \
             patch.object(remote, '_set_queue_panel', side_effect=lambda _, state: panel.update(state=state)), \
             patch.object(remote, '_queue_list', side_effect=RuntimeError('fixture read failure')):
            self.assertEqual(remote.api_queue(), {'ok': False, 'err': 'fixture read failure'})
            self.assertEqual(panel['state'], 0)

    def test_queue_keeps_artist_without_duration(self):
        texts = [NS(ControlTypeName='TextControl', Name=name) for name in ('Song', 'Artist')]
        row = Mock(Name='Song Artist')
        row.GetChildren.return_value = [Mock(GetChildren=Mock(return_value=texts))]
        with patch.object(remote, '_window'), \
             patch.object(remote.uiautomation, 'UIAutomationInitializerInThread', return_value=nullcontext()), \
             patch.object(remote, '_queue_panel_state', return_value=1), \
             patch.object(remote, '_set_queue_panel'), \
             patch.object(remote, '_queue_list', return_value=Mock(GetChildren=Mock(return_value=[row]))):
            self.assertEqual(remote.api_queue()['items'], [{'t': 'Song', 'a': 'Artist', 'd': ''}])

    def test_http_failures_are_not_success_and_invalid_routes_do_nothing(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), remote.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(route):
            try:
                with urlopen(Request(base + route, data=b'')) as response:
                    return response.status, response.read()
            except HTTPError as exc:
                return exc.code, exc.read()
        try:
            with patch.object(remote, 'seek', side_effect=RuntimeError('missing slider')):
                code, body = post('/cmd/seek/30')
                self.assertEqual(code, 503)
                self.assertEqual(json.loads(body)['err'], 'missing slider')
            with patch.object(remote, 'seek') as seek:
                self.assertEqual(post('/cmd/seek/30')[0], 204)
                seek.assert_called_once_with(30)
            with patch.object(remote, '_transport') as transport, patch.object(remote, 'set_volume') as volume:
                for route in ('/cmd/toggle/x/y', '/cmd/vol/101', '/cmd/seek/-2'):
                    self.assertEqual(post(route)[0], 404)
                transport.assert_not_called()
                volume.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
