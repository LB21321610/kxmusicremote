"""Queue selection: duplicate identity, stale views, native actions and HTTP validation."""
from contextlib import nullcontext
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from test_lyrics import remote


class QueueSelectionTests(unittest.TestCase):
    def setUp(self):
        self.panel = 0
        self.rows = [Mock(Name='Duplicate'), Mock(Name='Duplicate')]
        self.entries = [(row, dict(t='Duplicate', a='Artist', d='3:00')) for row in self.rows]
        for name, kwargs in (
            ('_window', {}),
            ('_queue_list', {}),
            ('_queue_entries', {'side_effect': lambda _: self.entries}),
            ('_queue_panel_state', {'side_effect': lambda _: self.panel}),
            ('_set_queue_panel', {'side_effect': lambda _, value: setattr(self, 'panel', value)}),
        ):
            p = patch.object(remote, name, **kwargs)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(remote.uiautomation, 'UIAutomationInitializerInThread', return_value=nullcontext())
        p.start(); self.addCleanup(p.stop)
        p = patch.object(remote, '_confirm_queue_play', new_callable=AsyncMock)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(remote, '_queue_playback_snapshot', new_callable=AsyncMock, return_value=None)
        p.start(); self.addCleanup(p.stop)

    def test_duplicate_selects_exact_occurrence_and_restores_panel(self):
        revision = remote.api_queue()['revision']
        self.assertEqual(self.panel, 0)
        self.assertTrue(remote.play_queue_item(1, revision)['ok'])
        self.rows[0].GetPattern.assert_not_called()
        self.rows[1].GetPattern.return_value.Invoke.assert_called_once()
        self.assertEqual(self.panel, 0)

    def test_native_double_click_checks_hit_target(self):
        row = self.rows[0]
        row.GetPattern.return_value = None
        row.IsOffscreen = False
        row.BoundingRectangle = Mock(left=10, top=20, right=200, bottom=60)
        row.BoundingRectangle.width.return_value = 190
        row.BoundingRectangle.height.return_value = 40
        row.GetRuntimeId.return_value = [1, 2, 3]
        with patch.object(remote.uiautomation, 'ControlFromPoint', return_value=row):
            remote.play_queue_item(0, remote.api_queue()['revision'])
        row.DoubleClick.assert_called_once()
        self.rows[0].GetSelectionItemPattern.assert_not_called()
        row.DoubleClick.reset_mock()
        with patch.object(remote.uiautomation, 'ControlFromPoint', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '遮挡'):
                remote.play_queue_item(0, remote.api_queue()['revision'])
        row.DoubleClick.assert_not_called()

    def test_stale_queue_and_invalid_index_do_not_play(self):
        revision = remote.api_queue()['revision']
        self.entries.pop(0)
        for index, rev in ((0, revision), (4, remote.api_queue()['revision'])):
            with self.assertRaises(remote.QueueChangedError):
                remote.play_queue_item(index, rev)
        for row in self.rows:
            row.GetPattern.assert_not_called()
        self.assertEqual(self.panel, 0)

    def test_native_failure_restores_panel(self):
        self.rows[0].GetPattern.return_value.Invoke.side_effect = RuntimeError('native failure')
        with self.assertRaisesRegex(RuntimeError, 'native failure'):
            remote.play_queue_item(0, remote.api_queue()['revision'])
        self.assertEqual(self.panel, 0)

    def test_restore_failure_after_play_does_not_encourage_duplicate_retry(self):
        revision = remote.api_queue()['revision']
        with patch.object(remote, '_set_queue_panel', side_effect=[None, RuntimeError('restore')]):
            result = remote.play_queue_item(0, revision)
        self.assertTrue(result['ok'])
        self.assertEqual(result['restore_err'], 'restore')

    def test_http_validation_conflict_and_failure(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), remote.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def post(body, origin=None):
            headers = {'Content-Type': 'application/json'}
            if origin: headers['Origin'] = origin
            request = Request(f'http://127.0.0.1:{server.server_port}/queue/play',
                              data=json.dumps(body).encode(), headers=headers)
            try:
                with urlopen(request) as response:
                    return response.status
            except HTTPError as exc:
                return exc.code
        try:
            body = dict(index=0, revision='a'*64)
            with patch.object(remote, 'play_queue_item', return_value={'ok': True}) as play:
                for invalid in ({}, [], dict(body, index=True), dict(body, index=-1),
                                dict(body, revision='bad'), dict(body, extra=1)):
                    self.assertEqual(post(invalid), 400)
                self.assertEqual(post(body, 'http://other.example'), 403)
                play.assert_not_called()
                self.assertEqual(post(body), 200)
                play.assert_called_once_with(0, 'a'*64)
            for error, code in ((remote.QueueChangedError('stale'), 409), (RuntimeError('native'), 503)):
                with patch.object(remote, 'play_queue_item', side_effect=error):
                    self.assertEqual(post(body), code)
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main()
