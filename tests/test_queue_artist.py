"""Queue metadata regressions, including frozen Apple Music 1.1792 UIA rows."""
import asyncio
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch
from test_lyrics import remote


class Control:
    def __init__(self, name='', kind='TextControl', children=(), automation_id=''):
        self.Name = name
        self.ControlTypeName = kind
        self.AutomationId = automation_id
        self.children = list(children)

    def GetChildren(self):
        return self.children


def captured_control(data):
    return Control(data['name'], data['type'],
                   [captured_control(child) for child in data.get('children', [])],
                   data.get('automation_id', ''))


def row(*texts, name='row'):
    return Control(name, 'ListItemControl', [Control(kind='GroupControl', children=[
        value if isinstance(value, Control) else Control(value) for value in texts])])


def queue(*rows):
    return Control(kind='ListControl', children=rows)


class QueueArtistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture = json.loads((Path(__file__).parent / 'artifacts' /
                                 'queue-artist-diagnosis-20261029.json').read_text(encoding='utf-8'))

    def parse(self, *texts):
        return remote._queue_entries(queue(row(*texts)))[0][1]

    def test_frozen_current_rows_recover_all_fourteen_artists(self):
        entries = remote._queue_entries(queue(*map(captured_control, self.capture['rows'])))
        self.assertEqual(len(entries), 20)
        affected = 0
        for (_, item), analysis in zip(entries, self.capture['row_analysis']):
            probe = analysis.get('matching_probe')
            with self.subTest(title=item['t']):
                if probe:
                    affected += 1
                    self.assertEqual(item, probe['true_metadata_item'])
                    self.assertTrue(remote._queue_track_matches(probe['snapshot'], item))
                else:
                    self.assertEqual(item, analysis['parsed_item'])
        self.assertEqual(affected, 14)

    def test_normal_row(self):
        self.assertEqual(self.parse('Song', 'Artist — Album', '3:00'),
                         dict(t='Song', a='Artist — Album', d='3:00'))

    def test_bmp_and_supplementary_icons_and_whitespace(self):
        for icons in [('\ue000',), ('\uf8ff', '\U000f0001', '\U0010009d'),
                      (' \U0010009d\t',), ('\ue000\U0010009d',)]:
            with self.subTest(icons=icons):
                self.assertEqual(self.parse('Song', *icons, 'Artist', '3:00'),
                                 dict(t='Song', a='Artist', d='3:00'))

    def test_old_nested_badge_and_hyperlink_do_not_duplicate_text(self):
        badge = Control('儿童不宜', 'GroupControl', [Control('\U0010009d')],
                        'ExplicitBadgeFontIcon')
        link = Control('Artist', 'HyperlinkControl', [Control('Artist')])
        self.assertEqual(self.parse('Song', badge, link, 'Artist', '3:00'),
                         dict(t='Song', a='Artist', d='3:00'))

    def test_real_unicode_and_mixed_icon_text_are_preserved_verbatim(self):
        for text in ('  孫燕姿 — 選擇  ', 'Beyoncé feat. JAY-Z & 친구 — 日本語 🎵',
                     '🎤', 'Artist\ue000Name', '\U0010009d Artist', 'Artist\U0010009d'):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text, '\U0010009d', text, '3:00'),
                                 dict(t=text, a=text, d='3:00'))

    def test_missing_artist_or_duration(self):
        for texts, expected in [
            (('Song', '\U0010009d', 'Artist'), dict(t='Song', a='Artist', d='')),
            (('Song', '\U0010009d', '3:00'), dict(t='Song', a='', d='3:00')),
            (('Song', '\U0010009d'), dict(t='Song', a='', d='')),
        ]:
            with self.subTest(texts=texts):
                self.assertEqual(self.parse(*texts), expected)

    def test_icons_and_empty_nodes_alone_are_not_songs(self):
        for texts in [(), ('',), ('  \t',), ('\U0010009d',), ('', '\ue000', '\U0010009d')]:
            with self.subTest(texts=texts):
                self.assertEqual(remote._queue_entries(queue(row(*texts))), [])

    def test_parsed_icon_row_keeps_strict_smtc_confirmation(self):
        item = self.parse('Song', '\U0010009d', 'Artist — Album', '3:00')
        target = dict(title='Song', artist='Artist', album='Album', duration=180,
                      position=0, playing=True)
        self.assertTrue(remote._queue_track_matches(target, item))
        with patch.object(remote, '_queue_playback_snapshot', AsyncMock(return_value=target)):
            asyncio.run(remote._confirm_queue_play(item, before=dict(target, title='Previous')))
        for wrong in (dict(target, title='Other'), dict(target, artist='Other'),
                      dict(target, album='Other'), dict(target, duration=200)):
            with self.subTest(snapshot=wrong):
                self.assertFalse(remote._queue_track_matches(wrong, item))
                with patch.object(remote, '_queue_playback_snapshot', AsyncMock(return_value=wrong)):
                    with self.assertRaisesRegex(RuntimeError, '尚未确认'):
                        asyncio.run(remote._confirm_queue_play(item, timeout=.01))

    def test_read_and_play_use_same_parser_revision_and_duplicate_position(self):
        rows = [row('Duplicate', '\U0010009d', 'Artist — Album', '3:00', name='Duplicate')
                for _ in range(2)]
        source = queue(*rows)
        target = dict(title='Duplicate', artist='Artist', album='Album', duration=180,
                      position=0, playing=True)
        activate = Mock()
        for initial in (0, 1):
            panel = {'state': initial}
            activate.reset_mock()
            with self.subTest(initial_panel=initial), ExitStack() as stack:
                for obj, name, options in [
                    (remote.uiautomation, 'UIAutomationInitializerInThread', dict(return_value=nullcontext())),
                    (remote, '_window', {}),
                    (remote, '_queue_list', dict(return_value=source)),
                    (remote, '_queue_panel_state', dict(side_effect=lambda _: panel['state'])),
                    (remote, '_set_queue_panel', dict(side_effect=lambda _, state: panel.update(state=state))),
                    (remote, '_activate_queue_row', dict(new=activate)),
                    (remote, '_queue_playback_snapshot', dict(new=AsyncMock(side_effect=[
                        dict(target, title='Previous'), target]))),
                ]:
                    stack.enter_context(patch.object(obj, name, **options))
                result = remote.api_queue()
                self.assertEqual(result['items'], [dict(t='Duplicate', a='Artist — Album', d='3:00')] * 2)
                self.assertEqual(result['revision'], remote.api_queue()['revision'])
                self.assertEqual(panel['state'], initial)
                self.assertTrue(remote.play_queue_item(1, result['revision'])['ok'])
                self.assertIs(activate.call_args.args[1], rows[1])
                self.assertEqual(panel['state'], initial)
                source.children[0].children[0].children[2].Name = 'Other Artist — Album'
                activate.reset_mock()
                with self.assertRaises(remote.QueueChangedError):
                    remote.play_queue_item(1, result['revision'])
                activate.assert_not_called()
                self.assertEqual(panel['state'], initial)
                source.children[0].children[0].children[2].Name = 'Artist — Album'

    def test_revision_tracks_order_and_duplicate_count(self):
        first = row('Song', '\U0010009d', 'Artist — Album A', '3:00', name='Song')
        second = row('Song', 'Artist — Album B', '3:00', name='Song')
        original = remote._queue_revision(remote._queue_entries(queue(first, second)))
        self.assertNotEqual(original, remote._queue_revision(remote._queue_entries(queue(second, first))))
        self.assertNotEqual(original, remote._queue_revision(remote._queue_entries(queue(first, second, first))))


if __name__ == '__main__':
    unittest.main()
