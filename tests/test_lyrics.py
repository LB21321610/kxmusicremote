"""Deterministic tests; local lyric source, no Apple Music playback mutations."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('remote', ROOT / 'apple-music-remote.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
LYRIC_ALIASES = remote._lyric_aliases  # LyricsTests patch it: the real one uses the network


def record(title='Song', artist='Artist', album='Album', duration=180, **kw):
    return dict(id=1, trackName=title, artistName=artist, albumName=album,
                duration=duration, instrumental=False, plainLyrics='Hello\n你好',
                syncedLyrics='[00:00.00] Hello\n[00:10.00] 你好', **kw)


class Source(BaseHTTPRequestHandler):
    calls = []
    mode = 'synced'

    def do_GET(self):
        type(self).calls.append((self.path, self.headers.get('User-Agent'), time.monotonic()))
        mode = type(self).mode
        if mode == 'timeout':
            time.sleep(.4)
        if mode in ('429', '503'):
            self.send_response(int(mode))
            self.send_header('Retry-After', '2')
            self.end_headers()
            self.wfile.write(b'<html>rate limited</html>')
            return
        if mode == 'invalid':
            payload = b'not json'
        elif mode == 'none' or (mode in ('search', 'approximate') and '/get?' in self.path):
            self.send_response(404)
            self.end_headers()
            return
        else:
            r = record()
            if mode == 'plain':
                r['syncedLyrics'] = '[ar:Artist] not timed'
            if mode == 'instrumental':
                r.update(instrumental=True, syncedLyrics=None, plainLyrics=None)
            if mode == 'search':
                r = [dict(r, id=2, duration=220), dict(r, id=3, trackName='Song (Live)'), r]
            if mode == 'approximate':
                r = [dict(r, artistName='Other Artist')]
            payload = json.dumps(r).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class LyricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = ThreadingHTTPServer(('127.0.0.1', 0), Source)
        remote.LRCLIB_URL = f'http://127.0.0.1:{cls.source.server_port}/api/'
        threading.Thread(target=cls.source.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        remote.LYRICS_WORKER.shutdown(wait=True)
        cls.source.shutdown()
        cls.source.server_close()

    def setUp(self):
        # This suite covers the preserved LRCLIB fallback, entirely offline.
        # LDDC discovery/selection/word timestamps have their own test suite.
        self.provider = patch.object(remote.multisource_lyrics, 'load',
                                     side_effect=lambda track, deadline, fallback, candidate='auto': fallback(track, deadline))
        self.provider.start()
        self.addCleanup(self.provider.stop)
        # Store-localized names come from the iTunes API; stay offline.
        self.aliases = patch.object(remote, '_lyric_aliases', return_value=[])
        self.aliases.start()
        self.addCleanup(self.aliases.stop)
        with remote.LYRICS_LOCK:
            remote.LYRICS_CACHE.clear()
            self.assertFalse(remote.LYRICS_PENDING)
        remote.LYRICS_NEXT_REQUEST = remote.LYRICS_RETRY_AT = 0
        Source.calls = []
        Source.mode = 'synced'
        self.track = dict(title='Song', artist='Artist', album='Album', duration=180)
        self.query = {k: [str(v)] for k, v in self.track.items()}

    def test_lrc_multiple_tags_translation_offset_and_blank(self):
        lines = remote.parse_lrc('[ar:A]\n[offset:-500]\n[00:01.20][00:05.200] 中文\n'
                                 '[00:01.200] English\n[00:10]\ninvalid\n[00:70] bad')
        self.assertEqual(lines, [{'time': .7, 'text': '中文\nEnglish'},
                                  {'time': 4.7, 'text': '中文'}, {'time': 9.5, 'text': ''}])

    def test_matching_uses_all_candidates_and_album(self):
        records = [record(duration=184), record(album='Other'), dict(record(), id=42)]
        self.assertEqual(remote.select_lyric_record(records, self.track)['id'], 42)
        self.assertIsNone(remote.select_lyric_record([record(artist='Wrong')], self.track))
        self.assertIsNone(remote.select_lyric_record([record(duration=float('nan'))], self.track))

    def test_versions_do_not_cross_match(self):
        for version in ['Live', 'Remix', 'Acoustic', 'Instrumental', 'Demo', '现场版', '混音版']:
            self.assertIsNone(remote.select_lyric_record([record(title='Song ('+version+')')], self.track))
            self.assertIsNone(remote.select_lyric_record([record(album='Album '+version)], self.track))
        # A remaster is the same recording.
        self.assertIsNotNone(remote.select_lyric_record([record(title='Song (Remastered)')], self.track))
        self.assertIsNotNone(remote.select_lyric_record([record(album='Album (Remastered 2011)')], self.track))
        live = dict(self.track, title='Song (Live)', album='Live Album')
        self.assertIsNotNone(remote.select_lyric_record([record(title=live['title'], album=live['album'])], live))

    def test_chinese_nfkc_and_punctuation(self):
        track = dict(title='七里香', artist='周杰伦', album='七里香', duration=299)
        self.assertIsNotNone(remote.select_lyric_record([record(**track)], track))
        self.assertEqual(remote._lyric_normalize('Ｓｏｎｇ — （Live）'), 'song live')

    def test_ambiguous_results_rejected(self):
        track = dict(self.track, album='')
        # Different albums of one recording only rank; lyrics vs instrumental is ambiguous.
        self.assertIsNotNone(remote.select_lyric_record([record(album='A'), record(album='B')], track))
        self.assertIsNone(remote.select_lyric_record([record(), dict(record(), instrumental=True)], self.track))

    def test_approximate_fallback_is_marked(self):
        Source.mode = 'approximate'
        result = remote.api_lyrics(self.query)
        self.assertEqual((result['state'], result['match_quality']), ('synced', 'approximate'))
        self.assertEqual(len(Source.calls), 2)
        self.assertIsNone(remote.select_lyric_record([record(artist='Other Artist', duration=183)], self.track, approximate=True))

    def test_aliases_are_part_of_the_cache_key(self):
        self.assertFalse(remote.api_lyrics(self.query)['cached'])
        self.assertTrue(remote.api_lyrics(self.query)['cached'])
        alias = [dict(title='歌', artist='歌手', album='专辑')]
        with patch.object(remote, '_lyric_aliases', return_value=alias):
            self.assertFalse(remote.api_lyrics(self.query)['cached'])
            self.assertTrue(remote.api_lyrics(self.query)['cached'])
        # The source loader matches against the aliases too.
        self.assertEqual(remote.multisource_lyrics.load.call_args.args[0]['aliases'], alias)
        self.assertEqual(len(Source.calls), 2)

    def test_lyric_aliases_from_the_zh_storefronts(self):
        track = dict(title='So Be It', artist='JJ Lin', album='Drifter - EP', duration=272)
        local = dict(title='最好是', artist='林俊傑', album='倖存者 - EP', country='tw')
        catalog = remote.apple_catalog
        with patch.object(catalog, 'resolve', return_value=dict(ok=True, match=dict(id='1535791952'))) as resolve, \
                patch.object(catalog, 'localized', return_value=local) as localized:
            self.assertEqual(LYRIC_ALIASES(track, time.monotonic() + 10), [dict(title='最好是', artist='林俊傑', album='倖存者 - EP')])
            self.assertEqual(localized.call_args.args, ('1535791952',))
            self.assertLessEqual(resolve.call_args.kwargs['deadline'], time.monotonic() + remote.LYRIC_ALIAS_BUDGET)
            # Both names already Chinese: no catalog request at all.
            self.assertEqual(LYRIC_ALIASES(dict(track, title='七里香', artist='周杰倫'), time.monotonic() + 10), [])
            self.assertEqual(resolve.call_count, 1)
            localized.return_value = dict(local, title='So Be It', artist='JJ Lin')
            self.assertEqual(LYRIC_ALIASES(track, time.monotonic() + 10), [])
            resolve.return_value = dict(ok=True, match=None, reason='not_found')
            self.assertEqual(LYRIC_ALIASES(track, time.monotonic() + 10), [])
            resolve.side_effect = catalog.CatalogError('rate_limited')
            self.assertEqual(LYRIC_ALIASES(track, time.monotonic() + 10), [])

    def test_service_matching_never_imports_lddc(self):
        # LDDC.core.algorithm pulls in PySide6; only the lddc_worker child may load it.
        code = ('import importlib.util, sys\n'
                f'spec = importlib.util.spec_from_file_location("remote", {str(ROOT / "apple-music-remote.py")!r})\n'
                'remote = importlib.util.module_from_spec(spec); spec.loader.exec_module(remote)\n'
                'track = dict(title="乾杯", artist="Mayday", album="", duration=285)\n'
                'records = [dict(trackName="干杯", artistName="五月天", albumName="", duration=285),\n'
                '           dict(trackName="Song", artistName="Other", albumName="", duration=285)]\n'
                'print(remote.select_lyric_record(records, track) is None,\n'
                '      remote.select_lyric_record(records, track, approximate=True)["trackName"],\n'
                '      "LDDC.core.algorithm" in sys.modules)\n')
        output = subprocess.run([sys.executable, '-c', code], capture_output=True, cwd=ROOT, timeout=120,
                                env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(output.stdout.decode('utf-8').split(), ['True', '干杯', 'False'], output.stderr.decode('utf-8', 'replace'))

    def test_windows_composite_metadata(self):
        self.assertEqual(remote.lyric_metadata('The Kid LAROI — Album (Deluxe) [Explicit]', ''),
                         ('The Kid LAROI', 'Album (Deluxe)'))
        self.assertEqual(remote.lyric_metadata('Artist-With-Hyphens', ''), ('Artist-With-Hyphens', ''))

    def test_cache_and_same_track_concurrent_dedup(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: remote.api_lyrics(self.query), range(8)))
        self.assertTrue(all(r['state'] == 'synced' for r in results))
        self.assertEqual(len(Source.calls), 1)
        self.assertTrue(remote.api_lyrics(self.query)['cached'])
        self.assertIn('AppleMusicRemote/', Source.calls[0][1])
        self.assertIn('duration=180', Source.calls[0][0])
        # Album/duration differences are different cache signatures.
        other = dict(self.query, duration=['181'])
        self.assertFalse(remote.api_lyrics(other)['cached'])
        self.assertEqual(len(Source.calls), 2)

    def test_search_fallback_and_throttling(self):
        Source.mode = 'search'
        result = remote.api_lyrics(self.query)
        self.assertEqual(result['record']['id'], 1)
        self.assertEqual(len(Source.calls), 2)
        self.assertGreaterEqual(Source.calls[1][2] - Source.calls[0][2], .24)

    def test_plain_instrumental_and_missing(self):
        for mode, expected in [('plain', 'plain'), ('instrumental', 'instrumental'), ('none', 'not_found')]:
            remote.LYRICS_CACHE.clear()
            Source.mode = mode
            result = remote.api_lyrics(self.query)
            self.assertEqual(result['state'], expected)
            self.assertTrue(remote.api_lyrics(self.query)['cached'])

    def test_rate_limit_non_json_body_and_global_backoff(self):
        for mode in ('429', '503'):
            self.setUp()
            Source.mode = mode
            self.assertEqual(remote.api_lyrics(self.query)['state'], 'error')
            Source.mode = 'synced'
            self.assertEqual(remote.api_lyrics(dict(self.query, title=['Other']))['state'], 'error')
            self.assertEqual(len(Source.calls), 1)
            self.assertGreater(remote.LYRICS_RETRY_AT, time.monotonic())

    def test_timeout_and_error_cache_expiry(self):
        Source.mode = 'timeout'
        with patch.object(remote, 'LYRICS_BUDGET', .1):
            started = time.monotonic()
            self.assertEqual(remote.api_lyrics(self.query)['state'], 'error')
            self.assertLess(time.monotonic()-started, .25)
        time.sleep(.15)
        self.assertTrue(remote.api_lyrics(self.query)['cached'])
        with remote.LYRICS_LOCK:
            key, (_, value) = next(iter(remote.LYRICS_CACHE.items()))
            remote.LYRICS_CACHE[key] = (0, value)
        Source.mode = 'synced'
        self.assertEqual(remote.api_lyrics(self.query)['state'], 'synced')

    def test_source_failure_and_invalid_response(self):
        Source.mode = 'invalid'
        self.assertEqual(remote.api_lyrics(self.query)['state'], 'error')
        remote.LYRICS_CACHE.clear()
        with patch.object(remote, 'LRCLIB_URL', 'http://127.0.0.1:1/'):
            self.assertEqual(remote.api_lyrics(self.query)['state'], 'error')

    def test_missing_metadata_does_not_call_source(self):
        for query in [dict(self.query, artist=['']), dict(self.query, duration=['0']),
                      dict(self.query, duration=['nan']), dict(self.query, title=['x'*501])]:
            self.assertEqual(remote.api_lyrics(query)['state'], 'not_found')
        self.assertEqual(Source.calls, [])

    def test_lyrics_http_does_not_block_controls_or_status(self):
        Source.mode = 'timeout'
        commands = []
        async def status():
            return dict(ok=True, title='Song', pos=10, dur=180)
        async def transport(command):
            commands.append(command)
        server = ThreadingHTTPServer(('127.0.0.1', 0), remote.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with patch.object(remote, '_status', status), patch.object(remote, '_transport', transport), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: json.load(urlopen(base+'/lyrics?'+urlencode(self.track), timeout=3)))
                time.sleep(.05)
                started = time.monotonic()
                with urlopen(Request(base+'/cmd/toggle', method='POST'), timeout=1) as response:
                    self.assertEqual(response.status, 204)
                with urlopen(base+'/status', timeout=1) as response:
                    self.assertTrue(json.load(response)['ok'])
                    self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertEqual(commands, ['toggle'])
                self.assertLess(time.monotonic()-started, .2)
                self.assertEqual(future.result()['state'], 'synced')
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
