"""Catalog backend, catalog routes and the Apple Music 1.1792 UIA adaptations.

No network and no Apple Music: HTTP fetches, UI Automation and SMTC are all
replaced by fakes built from the structures observed in the live spike
(tests/artifacts/catalog-spike.json).
"""
import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import itertools
import json
import socket
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from test_lyrics import remote
import apple_catalog as catalog
import lyrics_matching

ITUNES_PER_MINUTE = catalog.ITUNES.per_minute  # before tests swap in roomy limiters
MVOD = 'https://mvod.itunes.apple.com/itunes-assets/HLSVideo221/v4/aa/bb/cc/x/'
ART = 'https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/a6/asset.jpg/{w}x{h}bb.{f}'


def page(sections):
    data = {'data': [{'intent': {'$kind': 'AlbumDetailPageIntent'}, 'data': {'sections': sections}}], 'userTokenHash': ''}
    return ('<!doctype html><script type="application/json" id="serialized-server-data">'
            + json.dumps(data, ensure_ascii=False) + '</script>').encode()


def song_descriptor(song_id, album_id='670209426'):
    return {'kind': 'song', 'identifiers': {'storeAdamID': song_id},
            'url': f'https://music.apple.com/us/album/x/{album_id}?i={song_id}'}


def link(kind, item_id, title):
    return {'title': title, 'segue': {'$kind': 'flowAction', 'destination': {
        'kind': 'catalogItemDetailPage', 'contentDescriptor': {'kind': kind, 'identifiers': {'storeAdamID': item_id}}}}}


ALBUM_SECTIONS = [
    {'id': 'album-detail-header-section', 'itemKind': 'containerDetailHeaderLockup', 'items': [{
        'title': '热爱', 'subtitleLinks': [link('artist', '208628282', '羽·泉')],
        'quaternaryTitle': '国语流行\u202f·\u202f2001年',
        'modalPresentationDescriptor': {'paragraphText': '这张名为《热爱》的大碟……'},
        'artwork': {'dictionary': {'url': ART.replace('Music124', 'Video221'), 'bgColor': 'c4c6c7'}},
        'audioBadges': {'lossless': True, 'dolbyAtmos': False},
        'videoArtwork': {'dictionary': {'motionDetailSquare': {
            'video': MVOD + 'P971711148_default.m3u8', 'previewFrame': {'url': ART, 'bgColor': 'c4c6c7'}}}},
        'tallVideoArtwork': {'dictionary': {'motionDetailTall': {
            'video': MVOD + 'P971721795_default.m3u8', 'previewFrame': {'url': ART}}}},
    }]},
    {'id': 'track-list', 'itemKind': 'trackLockup', 'containerArtwork': {'dictionary': {
        'url': ART, 'bgColor': 'a9a3a3', 'textColor1': '141315', 'width': 1400, 'height': 1400}},
     'items': [
        {'title': '選擇犧牲', 'trackNumber': 1, 'discNumber': 1, 'duration': 257710, 'artistName': '羽·泉',
         'contentDescriptor': song_descriptor('670209454')},
        {'title': '深呼吸', 'trackNumber': 2, 'discNumber': 1, 'duration': 299546, 'artistName': '羽·泉',
         'showPopularityIndicator': True, 'contentDescriptor': song_descriptor('670209455'),
         'subtitleLinks': [link('artist', '208628282', '羽·泉'), link('artist', '1', 'Guest')]},
        {'title': 'broken row without id'},
     ]},
    {'id': 'footer', 'itemKind': 'containerDetailTracklistFooterLockup', 'items': [{
        'description': '2001年9月25日\n10 首歌曲、47 分钟\n℗ 2001 Rock Records Co., Ltd.',
        'linkSection': {'link': {'title': '滾石唱片'}}}]},
    {'id': 'more-by-artist', 'itemKind': 'squareLockup', 'header': {'item': {'titleLink': {'title': '更多羽·泉的作品'}}},
     'items': [{'titleLinks': [{'title': '冷酷到底'}], 'subtitleLinks': [{'title': '2000年'}],
                'contentDescriptor': {'kind': 'album', 'identifiers': {'storeAdamID': '669000534'}},
                'artwork': {'dictionary': {'url': ART}}},
               {'titleLinks': [{'title': 'a playlist'}], 'contentDescriptor': {'kind': 'playlist', 'identifiers': {'storeAdamID': '5'}}}]},
]

MASTER = '\n'.join([
    '#EXTM3U', '#EXT-X-VERSION:7',
    '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=2147332,BANDWIDTH=2385171,CODECS="avc1.64001f",RESOLUTION=768x768',
    MVOD + 'v768.m3u8',
    '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=1851823,BANDWIDTH=2041719,CODECS="hvc1.2.20000000.L123.B0",RESOLUTION=960x960',
    MVOD + 'hevc960.m3u8',
    '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=259046,BANDWIDTH=278228,CODECS="avc1.64001f",RESOLUTION=360x360',
    'v360.m3u8',
    '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=3889225,BANDWIDTH=4000000,CODECS="avc1.64001f",RESOLUTION=960x960',
    MVOD + 'v960.m3u8',
    '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=3845166,BANDWIDTH=4000000,CODECS="avc1.640020",RESOLUTION=1080x1080',
    MVOD + 'v1080.m3u8'])
VARIANT = '\n'.join(['#EXTM3U', '#EXT-X-PLAYLIST-TYPE:VOD',
                     '#EXT-X-MAP:URI="P971711148_768-.mp4",BYTERANGE="897@0"',
                     '#EXTINF:3.7,', '#EXT-X-BYTERANGE:965626@897', 'P971711148_768-.mp4',
                     '#EXTINF:3.7,', '#EXT-X-BYTERANGE:928655@966523', 'P971711148_768-.mp4', '#EXT-X-ENDLIST'])


def fake_fetch(mapping):
    """Replace apple_catalog.fetch: first substring match wins; records URLs."""
    seen = []

    def fetch(url, deadline, limit, limiter, accept='*/*'):
        catalog.check_url(url)
        seen.append(url)
        for key, value in mapping.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value.encode() if isinstance(value, str) else value
        raise catalog.CatalogError('not_found')
    fetch.seen = seen
    return fetch


def itunes(*records):
    return json.dumps({'resultCount': len(records), 'results': list(records)})


def track(song_id, title, album_id='670209426', album='熱愛', artist='Yu Quan', millis=299546, **kw):
    return dict(dict(wrapperType='track', kind='song', trackId=int(song_id), collectionId=int(album_id), artistId=208628282,
                     trackName=title, collectionName=album, artistName=artist, trackTimeMillis=millis, trackNumber=2,
                     discNumber=1, trackExplicitness='notExplicit', releaseDate='2001-08-16T12:00:00Z',
                     artworkUrl100='https://is1-ssl.mzstatic.com/image/thumb/Music124/v4/a6/asset.jpg/100x100bb.jpg'), **kw)


class CatalogBase(unittest.TestCase):
    def setUp(self):
        catalog.clear_cache()
        for name in ('ITUNES', 'PAGES', 'MOTION'):
            p = patch.object(catalog, name, catalog.Limiter(1000))
            p.start(); self.addCleanup(p.stop)


class ParsingTests(CatalogBase):
    def test_album_page_with_motion_artwork_and_footer(self):
        fetch = fake_fetch({'P971711148_default.m3u8': MASTER, 'P971721795_default.m3u8': MASTER,
                            'v768.m3u8': VARIANT})
        with patch.object(catalog, 'fetch', fetch):
            album = catalog.parse_album(catalog._server_data(page(ALBUM_SECTIONS)), '670209426', time.monotonic() + 5)
        self.assertEqual((album['title'], album['artist'], album['genre_year']), ('热爱', '羽·泉', '国语流行 · 2001年'))
        self.assertEqual([t['id'] for t in album['tracks']], ['670209454', '670209455'])
        self.assertEqual(album['tracks'][1]['duration'], 299.546)
        self.assertEqual(album['tracks'][1]['artists'], [{'id': '208628282', 'name': '羽·泉'}, {'id': '1', 'name': 'Guest'}])
        self.assertEqual(album['artwork']['url'], ART.replace('{f}', 'jpg'))
        self.assertEqual(album['artwork']['bg'], 'a9a3a3')
        self.assertEqual((album['release'], album['summary'], album['label']), ('2001年9月25日', '10 首歌曲、47 分钟', '滾石唱片'))
        self.assertTrue(album['copyright'].startswith('℗'))
        square = album['motion']['square']
        self.assertEqual(square['hls'], MVOD + 'P971711148_default.m3u8')
        self.assertEqual(square['file']['url'], MVOD + 'P971711148_768-.mp4')
        self.assertEqual((square['file']['width'], square['file']['codecs']), (768, 'avc1.64001f'))
        self.assertEqual([i['id'] for i in album['more'][0]['items']], ['669000534'])

    def test_variant_choice_prefers_largest_h264_within_bandwidth(self):
        fetch = fake_fetch({'master.m3u8': MASTER, 'v768.m3u8': VARIANT})
        with patch.object(catalog, 'fetch', fetch):
            chosen = catalog.motion(MVOD + 'master.m3u8', time.monotonic() + 5)
        self.assertEqual((chosen['width'], chosen['bandwidth']), (768, 2147332))
        self.assertNotIn('hevc960.m3u8', ' '.join(fetch.seen))
        fetch = fake_fetch({'master.m3u8': MASTER, 'v360.m3u8': VARIANT})
        with patch.object(catalog, 'fetch', fetch):
            low = catalog.motion(MVOD + 'master.m3u8', time.monotonic() + 5, max_bandwidth=300_000)
        self.assertEqual(low['width'], 360)  # relative URI resolved against the master
        self.assertTrue(fetch.seen[-1].startswith(MVOD))

    def test_segmented_or_foreign_variants_are_not_used(self):
        segmented = VARIANT.replace('P971711148_768-.mp4\n#EXTINF:3.7,\n#EXT-X-BYTERANGE:928655@966523\nP971711148_768-.mp4',
                                    'seg1.mp4\n#EXTINF:3.7,\nseg2.mp4')
        with patch.object(catalog, 'fetch', fake_fetch({'master.m3u8': MASTER, 'v768.m3u8': segmented})):
            self.assertIsNone(catalog.motion(MVOD + 'master.m3u8', time.monotonic() + 5))
        foreign = VARIANT.replace('URI="P971711148_768-.mp4"', 'URI="https://evil.example/a.mp4"').replace(
            '\nP971711148_768-.mp4', '\nhttps://evil.example/a.mp4')
        with patch.object(catalog, 'fetch', fake_fetch({'master.m3u8': MASTER, 'v768.m3u8': foreign})):
            with self.assertRaises(catalog.CatalogError):
                catalog.motion(MVOD + 'master.m3u8', time.monotonic() + 5)
        sections = json.loads(json.dumps(ALBUM_SECTIONS))
        sections[0]['items'][0]['videoArtwork']['dictionary']['motionDetailSquare']['video'] = 'http://mvod.itunes.apple.com/x.m3u8'
        sections[0]['items'][0]['artwork']['dictionary']['url'] = 'https://evil.example/{w}x{h}.jpg'
        sections[1]['containerArtwork']['dictionary']['url'] = 'https://evil.example/{w}x{h}.jpg'
        with patch.object(catalog, 'fetch', fake_fetch({'P971721795_default.m3u8': MASTER, 'v768.m3u8': VARIANT})):
            album = catalog.parse_album({'sections': sections}, '670209426', time.monotonic() + 5)
        self.assertIsNone(album['motion']['square'])
        self.assertIsNone(album['artwork'])

    def test_artist_page_featured_tracks_shelves_and_similar(self):
        sections = [
            {'itemKind': 'artistDetailHeader', 'items': [{'title': 'Taylor Swift', 'bio': '推出三张乡村乐专辑后……', 'keyColor': 'afb8be',
                                                          'artwork': {'dictionary': {'url': ART.replace('bb.{f}', '{c}.{f}'), 'bgColor': '070706'}, 'cropStyle': 'sr'}}]},
            {'itemKind': 'artistFeaturedContentAndTracks', 'items': [{
                'featuredContent': {'contentDescriptor': {'kind': 'album', 'identifiers': {'storeAdamID': '6814997249'}},
                                    'heading': '推荐专辑', 'detailText': {'title': 'The Life of a Showgirl', 'headline': '2026年9月24日'}},
                'tracks': [{'title': 'Patient Zero', 'duration': 225868, 'subtitleLinks': [{'title': 'Showgirl · 2026年'}],
                            'contentDescriptor': song_descriptor('6814997425', '6814997249'),
                            'segue': {'destination': {'contentDescriptor': {'kind': 'album', 'identifiers': {'storeAdamID': '6814997249'}}}}}]}]},
            {'itemKind': 'adaptiveHorizontalSquareLockup', 'header': {'item': {'titleLink': {'title': '代表专辑'}}},
             'items': [{'squareLockup': {'titleLinks': [{'title': 'folklore'}], 'contentDescriptor': {'kind': 'album', 'identifiers': {'storeAdamID': '1528112358'}}}}]},
            {'itemKind': 'squareLockup', 'header': {'item': {'titleLink': {'title': '艺人歌单'}}},
             'items': [{'titleLinks': [{'title': 'x'}], 'contentDescriptor': {'kind': 'playlist', 'identifiers': {'storeAdamID': '9'}}}]},
            {'itemKind': 'bubbleLockup', 'items': [{'title': 'Lorde', 'contentDescriptor': {'kind': 'artist', 'identifiers': {'storeAdamID': '602767352'}}}]},
        ]
        artist = catalog.parse_artist({'sections': sections}, '159260351')
        self.assertEqual((artist['name'], artist['color'], artist['bio'][:4]), ('Taylor Swift', 'afb8be', '推出三张'))
        self.assertTrue(artist['artwork']['url'].endswith('sr.jpg'))
        self.assertEqual(artist['top'][0], {'id': '6814997425', 'album_id': '6814997249', 'title': 'Patient Zero',
                                            'subtitle': 'Showgirl · 2026年', 'duration': 225.868, 'explicit': False, 'artwork': None})
        self.assertEqual(artist['featured']['id'], '6814997249')
        self.assertEqual([s['title'] for s in artist['shelves']], ['代表专辑'])  # playlists are not album shelves
        self.assertEqual(artist['similar'][0]['id'], '602767352')

    def test_broken_page_falls_back_to_itunes_lookup(self):
        lookup = itunes(dict(wrapperType='collection', collectionId=670209426, artistId=208628282, collectionName='熱愛',
                             artistName='Yu Quan', releaseDate='2001-09-25T07:00:00Z', primaryGenreName='Mandopop', trackCount=10),
                        track('670209455', '深呼吸'))
        fetch = fake_fetch({'music.apple.com/us/album/670209426': '<html>changed layout</html>', 'lookup': lookup})
        with patch.object(catalog, 'fetch', fetch):
            album = catalog.album('670209426')
        self.assertEqual((album['source'], album['title'], album['motion']), ('itunes', '熱愛', {'square': None, 'tall': None}))
        self.assertEqual(album['tracks'][0]['id'], '670209455')
        with patch.object(catalog, 'fetch', fake_fetch({'music.apple.com': catalog.CatalogError('timeout'), 'lookup': itunes()})):
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.artist('208628282')
        self.assertEqual(caught.exception.code, 'not_found')

    def test_ids_links_and_language_settings(self):
        for bad in ('0', '12a', '1' * 16, '', None, '１２３'):
            with self.assertRaises(catalog.CatalogError):
                catalog.album(bad)
        self.assertEqual(catalog.app_link('album', '670209426', '670209455'),
                         'music://music.apple.com/us/album/670209426?i=670209455')
        self.assertEqual(catalog.app_link('artist', '208628282'), 'music://music.apple.com/us/artist/208628282')
        for args in (('artist', '1', '2'), ('playlist', '1'), ('album', '1', 'x'), ('album', '1;calc')):
            with self.assertRaises(catalog.CatalogError):
                catalog.app_link(*args)
        with patch.dict('os.environ', {'X_STORE': 'jp', 'X_BAD': 'us/../x'}):
            self.assertEqual(catalog._setting('X_STORE', 'us', r'[a-z]{2}'), 'jp')
            self.assertEqual(catalog._setting('X_BAD', 'us', r'[a-z]{2}'), 'us')


class MatchingTests(CatalogBase):
    def test_exact_recording_with_album_ranking(self):
        records = [track('670209455', '深呼吸'), track('2', '深呼吸', album_id='9', album='滾石香港黃金十年 - 羽泉精選'),
                   track('3', '深呼吸 (Live)', album_id='8', album='演唱会'), track('4', '深呼吸', artist='Other')]
        song, reason = catalog.pick_recording(records, '深呼吸', 'Yu Quan', '熱愛', 300)
        self.assertEqual((song['id'], song['album_id'], reason), ('670209455', '670209426', None))
        self.assertEqual(catalog.pick_recording(records, '深呼吸', 'Yu Quan', '', 300), (None, 'ambiguous'))
        self.assertEqual(catalog.pick_recording(records, '深呼吸', 'Yu Quan', '熱愛', 305), (None, 'not_found'))
        live, _ = catalog.pick_recording(records, '深呼吸 (Live)', 'Yu Quan', '演唱会', 300)
        self.assertEqual(live['id'], '3')

    def test_explicit_and_clean_editions(self):
        explicit = track('10', 'A COLD PLAY', album_id='1853697543', album='BEFORE I FORGET', artist='The Kid LAROI',
                         millis=178627, trackExplicitness='explicit')
        clean = dict(explicit, trackId=11, collectionId=1853697544, trackExplicitness='cleaned')
        single = dict(explicit, trackId=12, collectionId=1837520829, collectionName='A COLD PLAY - Single', trackTimeMillis=179817)
        records = [clean, explicit, single]
        self.assertEqual(catalog.pick_recording(records, 'A COLD PLAY', 'The Kid LAROI', 'BEFORE I FORGET', 178)[0]['id'], '10')
        self.assertEqual(catalog.pick_recording(records, 'A COLD PLAY', 'The Kid LAROI', 'BEFORE I FORGET', 178, 'clean')[0]['id'], '11')

    def test_resolve_searches_again_with_album_and_caches_for_a_day(self):
        fetch = fake_fetch({'term=%E6%B7%B1%E5%91%BC%E5%90%B8+Yu+Quan+%E7%86%B1%E6%84%9B': itunes(track('670209455', '深呼吸')),
                            'search': itunes(track('5', '深呼吸', artist='Somebody'))})
        with patch.object(catalog, 'fetch', fetch):
            result = catalog.resolve('深呼吸', 'Yu Quan', '熱愛', 300)
            again = catalog.resolve(' 深呼吸 ', 'Yu  Quan', '熱愛', 300.4)
        self.assertEqual(result['match']['id'], '670209455')
        self.assertIs(again, result)
        self.assertEqual(len(fetch.seen), 2)
        self.assertTrue(all('country=us' in url for url in fetch.seen))
        key = next(k for k in catalog._CACHE if k[0] == 'resolve')
        self.assertAlmostEqual(catalog._CACHE[key][0] - time.monotonic(), catalog.ITEM_TTL, delta=5)
        for bad in (('', 'a', '', 100), ('t', 'a', '', 0), ('t', 'a', '', float('nan')), ('t', 'a', '', 100, 'weird')):
            with self.assertRaises(catalog.CatalogError):
                catalog.resolve(*bad)

    def test_localized_names_country_order_id_filter_and_cache(self):
        other = track('999', '別的歌', artist='某人')
        song = track('1535791952', '最好是', album_id='1535791950', album='倖存者 - EP', artist='林俊傑', millis=272771)
        fetch = fake_fetch({'country=tw': itunes(other), 'country=hk': itunes(other, song)})
        with patch.object(catalog, 'fetch', fetch):
            result = catalog.localized('1535791952')
            again = catalog.localized('1535791952')
        self.assertEqual(result, dict(title='最好是', artist='林俊傑', album='倖存者 - EP', country='hk'))
        self.assertIs(again, result)
        self.assertEqual([url.split('country=')[1][:2] for url in fetch.seen], ['tw', 'hk'])
        self.assertTrue(all('/lookup?' in url and 'id=1535791952' in url for url in fetch.seen))
        self.assertAlmostEqual(catalog._CACHE[('localized', '1535791952')][0] - time.monotonic(), catalog.ITEM_TTL, delta=5)
        with patch.object(catalog, 'fetch', fake_fetch({'lookup': itunes()})):
            self.assertIsNone(catalog.localized('42'))
        with self.assertRaises(catalog.CatalogError):
            catalog.localized('not-an-id')

    def test_resolve_passes_its_deadline_to_the_cache(self):
        with patch.object(catalog, 'cached', return_value={'ok': True}) as cached:
            catalog.resolve('So Be It', 'JJ Lin', 'Drifter - EP', 272, deadline=123.0)
        self.assertEqual(cached.call_args.kwargs['deadline'], 123.0)

    def test_helpers_match_the_lyric_matcher(self):
        samples = ['深呼吸', 'RATHER BE (feat. Lithe)', 'Ｆｕｌｌｗｉｄｔｈ', 'Ünïcödé — Café', '忘‧記', 'Song (Live) [Remastered 2011]',
                   '演唱會版', 'Nightcore Mix', '', '  spaced  out  ']
        for value in samples:
            self.assertEqual(catalog.normalize(value), remote._lyric_normalize(value))
            # The catalog identifies one Apple song, so a remaster still counts
            # there; lyrics treat it as the same recording.
            self.assertEqual(catalog.recording_versions(value, value) - {'remaster'}, lyrics_matching.versions(value, value))
        for artist, album in (('Yu Quan — 熱愛', ''), ('The Kid LAROI — BEFORE I FORGET [Explicit]', ''),
                              ('A-ha', 'Hunting High [Clean]'), ('X — Y', 'Z'), ('', '')):
            self.assertEqual(catalog.split_artist_album(artist, album), remote.lyric_metadata(artist, album))


class ArtworkRecoveryTests(CatalogBase):
    def records(self):
        # The two album IDs and song metadata come from the saved diagnostic.
        return [track('6763126438', 'NIGHTS LIKE THIS', '1895173348', 'THE FIRST TIME', 'The Kid LAROI', 86984),
                track('6763128559', 'NIGHTS LIKE THIS', '1895174191', 'THE FIRST TIME', 'The Kid LAROI', 86984)]

    def resolve(self):
        return catalog.resolve('NIGHTS LIKE THIS', 'The Kid LAROI', 'THE FIRST TIME', 86.984)

    def wait_stored(self):
        # Future.result() may return just before its cache callback completes.
        until = time.monotonic() + 2
        while catalog._PENDING and time.monotonic() < until:
            time.sleep(.005)
        self.assertFalse(catalog._PENDING)

    def test_ambiguous_identity_selects_motion_album_and_reuses_album_cache(self):
        def load_page(kind, album_id, deadline):
            self.assertGreater(deadline, time.monotonic())
            return {'id': album_id}

        def parse(info, album_id, deadline):
            return {'ok': True, 'motion': {'square': {'hls': MVOD + 'x.m3u8'} if album_id == '1895174191' else None}}

        with patch.object(catalog, '_itunes', return_value=self.records()), \
                patch.object(catalog, '_page', side_effect=load_page) as pages, \
                patch.object(catalog, 'parse_album', side_effect=parse):
            result = self.resolve()
            self.assertEqual((result['match'], result['reason'], result['artwork_album_id']),
                             (None, 'ambiguous', '1895174191'))
            catalog.album('1895174191')
        self.assertEqual([call.args[1] for call in pages.call_args_list], ['1895173348', '1895174191'])
        deadlines = [call.args[2] for call in pages.call_args_list]
        self.assertEqual(deadlines[0], deadlines[1])
        self.assertLessEqual(deadlines[0] - time.monotonic(), 12)

    def test_versions_and_incomplete_metadata_cannot_supply_artwork(self):
        base = self.records()[0]
        variants = [dict(collectionName=name) for name in
                    ('OTHER ALBUM', 'THE FIRST TIME (Deluxe)', 'THE FIRST TIME (Live)', 'THE FIRST TIME (Remix)')]
        variants += [dict(trackName='NIGHTS LIKE THIS (Remix)'), dict(artistName='Somebody'),
                     dict(trackTimeMillis=90985)]
        for variant in variants:
            with self.subTest(variant=variant), patch.object(catalog, 'album') as albums:
                records = [dict(base, **variant), dict(base, trackId=2, collectionId=3, **variant)]
                artwork = catalog.pick_artwork_album(records, 'NIGHTS LIKE THIS', 'The Kid LAROI',
                                                     'THE FIRST TIME', 86.984, time.monotonic() + 12)
                self.assertIsNone(artwork)
                albums.assert_not_called()

    def test_three_distinct_albums_maximum_and_numeric_tiebreak(self):
        base = self.records()[0]
        records = [dict(base, collectionId=i, trackId=i) for i in (100, 20, 3, 2, 2)]
        with patch.object(catalog, 'album', return_value={'motion': {'square': {'hls': MVOD + 'x.m3u8'}}}) as albums:
            result = catalog.pick_artwork_album(records, base['trackName'], base['artistName'],
                                                base['collectionName'], 86.984, time.monotonic() + 12)
        self.assertEqual(result, '2')
        self.assertEqual([call.args[0] for call in albums.call_args_list], ['2', '3', '20'])

    def test_clean_explicit_artwork_sharing_and_album_failure(self):
        records = [dict(self.records()[0], collectionName='THE FIRST TIME [Clean]', trackExplicitness='cleaned'),
                   dict(self.records()[1], collectionName='THE FIRST TIME [Explicit]', trackExplicitness='explicit')]
        with patch.object(catalog, 'album', return_value={'motion': {'square': {'hls': MVOD + 'x.m3u8'}}}) as albums:
            self.assertEqual(catalog.pick_artwork_album(records, 'NIGHTS LIKE THIS', 'The Kid LAROI',
                'THE FIRST TIME', 86.984, time.monotonic() + 12), '1895173348')
            self.assertEqual(albums.call_count, 2)
        with patch.object(catalog, '_itunes', return_value=self.records()), \
                patch.object(catalog, 'album', side_effect=catalog.CatalogError('timeout')):
            result = self.resolve()
        self.assertEqual((result['match'], result['reason'], result['artwork_album_id']), (None, 'ambiguous', None))

    def test_negative_resolve_cache_expires_after_sixty_seconds(self):
        for records, reason in ((self.records(), 'ambiguous'), ([], 'not_found')):
            with self.subTest(reason=reason):
                catalog.clear_cache()
                with patch.object(catalog, '_itunes', return_value=records) as search, \
                        patch.object(catalog, 'album', return_value={'motion': {}}):
                    first = self.resolve()
                    self.wait_stored()
                    self.assertEqual(first['reason'], reason)
                    self.assertIs(self.resolve(), first)
                    count = search.call_count
                    key = next(k for k in catalog._CACHE if k[0] == 'resolve')
                    expires = catalog._CACHE[key][0]
                    self.assertAlmostEqual(expires - time.monotonic(), 60, delta=2)
                    with patch.object(catalog.time, 'monotonic', return_value=expires + 1):
                        self.assertIsNot(self.resolve(), first)
                        self.wait_stored()
                    self.assertGreater(search.call_count, count)


class HttpSafetyTests(CatalogBase):
    def test_allow_list_and_redirect_checks(self):
        for url in ('https://itunes.apple.com/search', 'https://music.apple.com/us/album/1', 'https://mvod.itunes.apple.com/a.m3u8'):
            self.assertEqual(catalog.check_url(url), url)
        for url in ('http://itunes.apple.com/search', 'https://evil.example/', 'https://music.apple.com.evil.example/',
                    'https://user@music.apple.com/', 'https://music.apple.com:8443/', 'file:///c:/x', 'https://[broken/'):
            with self.assertRaises(catalog.CatalogError):
                catalog.check_url(url)
        handler = catalog._CheckedRedirects()
        req = Request('https://music.apple.com/us/album/670209426')
        follow = handler.redirect_request(req, None, 301, 'Moved', {}, '/us/album/%E7%86%B1/670209426')
        self.assertEqual(follow.full_url, 'https://music.apple.com/us/album/%E7%86%B1/670209426')
        with self.assertRaises(catalog.CatalogError):
            handler.redirect_request(req, None, 302, 'Found', {}, 'https://evil.example/')

    def test_size_time_and_status_limits(self):
        class Response:
            def __init__(self, chunks, url='https://itunes.apple.com/search'):
                self.chunks, self.url = list(chunks), url
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def geturl(self): return self.url
            def read1(self, n): return self.chunks.pop(0) if self.chunks else b''
        limiter = catalog.Limiter(100)
        url = 'https://itunes.apple.com/search?term=x'
        with patch.object(catalog._OPENER, 'open', return_value=Response([b'{"a"', b':1}'])) as opener:
            self.assertEqual(catalog.fetch(url, time.monotonic() + 5, 100, limiter), b'{"a":1}')
            self.assertLessEqual(opener.call_args.kwargs['timeout'], catalog.REQUEST_TIMEOUT)
        with patch.object(catalog._OPENER, 'open', return_value=Response([b'x' * 60, b'x' * 60])):
            with self.assertRaises(catalog.CatalogError):
                catalog.fetch(url, time.monotonic() + 5, 100, limiter)
        with patch.object(catalog._OPENER, 'open', return_value=Response([b'x'], url='https://evil.example/')):
            with self.assertRaises(catalog.CatalogError):
                catalog.fetch(url, time.monotonic() + 5, 100, limiter)
        with patch.object(catalog._OPENER, 'open') as opener:
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.fetch(url, time.monotonic() - 1, 100, limiter)
            opener.assert_not_called()
            self.assertEqual(caught.exception.code, 'timeout')
            with self.assertRaises(catalog.CatalogError):
                catalog.fetch('https://evil.example/x', time.monotonic() + 5, 100, limiter)
            opener.assert_not_called()
        for error, code in ((HTTPError(url, 404, 'nf', {}, None), 'not_found'), (HTTPError(url, 500, 'x', {}, None), 'unavailable'),
                            (socket.timeout(), 'timeout'), (URLError('down'), 'unavailable')):
            with patch.object(catalog._OPENER, 'open', side_effect=error):
                with self.assertRaises(catalog.CatalogError) as caught:
                    catalog.fetch(url, time.monotonic() + 5, 100, limiter)
                self.assertEqual(caught.exception.code, code)
        for status in (403, 429):
            limiter = catalog.Limiter(100)
            with patch.object(catalog._OPENER, 'open', side_effect=HTTPError(url, status, 'x', {'Retry-After': '30'}, None)):
                with self.assertRaises(catalog.CatalogError) as caught:
                    catalog.fetch(url, time.monotonic() + 5, 100, limiter)
            self.assertEqual((caught.exception.code, caught.exception.status), ('rate_limited', 429))
            with patch.object(catalog._OPENER, 'open') as opener:
                with self.assertRaises(catalog.CatalogError):
                    catalog.fetch(url, time.monotonic() + 5, 100, limiter)
                opener.assert_not_called()  # backing off

    def test_throttle_is_18_per_minute_with_backoff(self):
        self.assertEqual(ITUNES_PER_MINUTE, 18)
        limiter = catalog.Limiter(18)
        for _ in range(18):
            limiter.acquire()
        with self.assertRaises(catalog.CatalogError) as caught:
            limiter.acquire()
        self.assertEqual(caught.exception.code, 'rate_limited')
        self.assertTrue(0 < caught.exception.retry_after <= 60)
        limiter.sent.clear()
        limiter.backoff(30)
        with self.assertRaises(catalog.CatalogError) as caught:
            limiter.acquire()
        self.assertAlmostEqual(caught.exception.retry_after, 30, delta=1)

    def test_coalescing_and_cache_lifetimes(self):
        gate, calls = threading.Event(), []

        def loader(deadline):
            calls.append(deadline)
            gate.wait(2)
            return {'ok': True}
        results = []
        threads = [threading.Thread(target=lambda: results.append(catalog.cached(('k',), 900, loader, 5))) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(.2)
        gate.set()
        for thread in threads:
            thread.join(3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r is results[0] for r in results))
        self.assertAlmostEqual(catalog._CACHE[('k',)][0] - time.monotonic(), 900, delta=5)
        failures = []

        def failing(deadline):
            failures.append(1)
            raise catalog.CatalogError('unavailable')
        for _ in range(2):
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.cached(('bad',), 900, failing, 5)
        self.assertEqual(len(failures), 1)
        self.assertAlmostEqual(caught.exception.retry_after, catalog.FAILURE_TTL, delta=2)
        with patch.object(catalog, 'fetch', fake_fetch({'search': itunes(track('1', 'a'))})):
            catalog.search('a', 'song')
        key = next(k for k in catalog._CACHE if k[0] == 'search')
        self.assertAlmostEqual(catalog._CACHE[key][0] - time.monotonic(), catalog.SEARCH_TTL, delta=5)
        self.assertEqual((catalog.SEARCH_TTL, catalog.ITEM_TTL, catalog.FAILURE_TTL), (900, 86400, 60))
        for bad in (('', 'song'), ('x' * 101, 'song'), ('ok', 'video')):
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.search(*bad)
            self.assertEqual(caught.exception.status, 400)

    def test_worker_is_separate_from_ui_automation(self):
        self.assertIsNot(catalog.WORKER, getattr(remote, 'LYRICS_WORKER', None))
        self.assertNotIn('uiautomation', catalog.__dict__)
        self.assertNotIn('UIA_LOCK', catalog.__dict__)


class WindowTests(unittest.TestCase):
    def windows(self, *names):
        return [NS(Name=n, ProcessId=2, ClassName='WinUIDesktopWin32WindowClass', ControlTypeName='WindowControl',
                   NativeWindowHandle=i + 1) for i, n in enumerate(names)]

    def test_main_window_wins_over_mini_player(self):
        mini, main = self.windows('迷你播放程序', 'Apple Music')
        with patch.object(remote, '_app_windows', return_value=[mini, main]), \
             patch.object(remote, '_is_main_window', side_effect=lambda w: w is main):
            self.assertIs(remote._window(), main)
        with patch.object(remote, '_app_windows', return_value=[mini]), \
             patch.object(remote, '_is_main_window', return_value=False):
            self.assertIs(remote._window(), mini)
        with patch.object(remote, '_app_windows', return_value=[main]), patch.object(remote, '_is_main_window') as probe:
            self.assertIs(remote._window(), main)
            probe.assert_not_called()  # single window: no extra UIA search
        with patch.object(remote, '_app_windows', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'Apple Music'):
                remote._window()
        with patch.object(remote.uiautomation, 'Control', side_effect=RuntimeError('gone')):
            self.assertFalse(remote._is_main_window(main))

    def test_seek_uses_mini_player_scrubber_when_lcd_is_missing(self):
        mini, main = self.windows('迷你播放程序', 'Apple Music')
        lcd, scrubber = Mock(), Mock()
        lcd.Exists.return_value = False
        pattern = Mock(Minimum=0, Maximum=257, IsReadOnly=False)
        scrubber.Exists.return_value = True
        scrubber.GetPattern.return_value = pattern
        sliders = {'LCDScrubber': lcd, 'Scrubber': scrubber}
        with patch.object(remote, '_window', return_value=main), patch.object(remote, '_app_windows', return_value=[mini, main]), \
             patch.object(remote.uiautomation, 'UIAutomationInitializerInThread', return_value=nullcontext()), \
             patch.object(remote.uiautomation, 'SliderControl', side_effect=lambda **kw: sliders[kw['AutomationId']]) as slider:
            remote.seek(60)
        pattern.SetValue.assert_called_once_with(60)
        self.assertIs(slider.call_args.kwargs['searchFromControl'], mini)

    def test_repeat_names_of_1_1792(self):
        self.assertEqual([remote.REPEAT_STATES[n] for n in ('全部重复播放', '重复播放一首', '不重复')], ['all', 'one', 'none'])


class StatusTests(unittest.TestCase):
    def status(self, title, artist, seconds, playing):
        session = Mock()
        session.try_get_media_properties_async = AsyncMock(return_value=NS(title=title, artist=artist, album_title=''))
        session.get_playback_info.return_value = NS(
            playback_status=remote.PlaybackStatus.PLAYING if playing else remote.PlaybackStatus.PAUSED)
        session.get_timeline_properties.return_value = NS(
            end_time=timedelta(seconds=seconds), position=timedelta(seconds=0), last_updated_time=datetime.now(timezone.utc))
        with patch.object(remote, '_session', AsyncMock(return_value=session)), \
             patch.object(remote, 'get_volume', return_value=(40, False)):
            return asyncio.run(remote._status())

    def test_no_loaded_song_is_reported_as_idle(self):
        idle = self.status('未在播放', '', 0, False)
        self.assertEqual((idle['ok'], idle['loaded'], idle['title'], idle['dur'], idle['vol']), (True, False, '', 0, 40))
        connecting = self.status('正在连接…', '', 0, True)
        self.assertTrue(connecting['loaded'])
        song = self.status('深呼吸', 'Yu Quan — 熱愛', 299, True)
        self.assertEqual((song['loaded'], song['lyric_artist'], song['album']), (True, 'Yu Quan', '熱愛'))


class Element(Mock):
    """Minimal UIA element fake."""


def text(name):
    return Element(ControlTypeName='TextControl', Name=name)


def album_row(name, texts, more=True):
    group = Element()
    children = [Element(ControlTypeName='ButtonControl', AutomationId='FavoriteButton', Name='喜爱')]
    children += [text(t) for t in texts]
    if more:
        children.append(Element(ControlTypeName='ButtonControl', AutomationId='', Name='更多'))
    group.GetChildren.return_value = children
    row = Element(ControlTypeName='ListItemControl', Name=name, IsOffscreen=False)
    row.GetChildren.return_value = [group]
    return row


class AlbumPageTests(unittest.TestCase):
    def test_row_parsing_matches_the_observed_layout(self):
        self.assertEqual(remote._album_row_info(album_row('音轨 2 深呼吸 5 分钟', ['2', '深呼吸', '5:00'])),
                         {'title': '深呼吸', 'duration': '5:00', 'number': 2})
        # The playing row shows no number; featured artists sit between title and duration.
        self.assertEqual(remote._album_row_info(album_row('音轨 7 5:21AM 1 分钟，40 秒钟', ['5:21AM', 'The Kid LAROI、Andrew Aged', '1:40'])),
                         {'title': '5:21AM', 'duration': '1:40', 'number': 7})
        self.assertEqual(remote._album_row_info(album_row('音轨 5 RATHER BE 儿童不宜 4 分钟', ['5', 'RATHER BE', '\U0010009d', '4:24']))['title'],
                         'RATHER BE')

    def test_row_lookup_by_title_duration_then_track_number(self):
        rows = [album_row('音轨 1 Intro 1 分钟', ['1', 'Intro', '1:00']), album_row('音轨 2 深呼吸 5 分钟', ['2', '深呼吸', '5:00']),
                album_row('音轨 9 Intro 1 分钟', ['9', 'Intro', '1:01'])]
        track_list = Element(ControlTypeName='ListControl')
        header = Element()
        header.GetParentControl.return_value.GetParentControl.return_value = track_list
        with patch.object(remote, '_walk_rows', return_value=iter([rows])):
            self.assertIs(remote._find_album_row(header, {'title': '深呼吸', 'duration': 299.546, 'number': 2}), rows[1])
        with patch.object(remote, '_walk_rows', return_value=iter([rows])):
            self.assertIs(remote._find_album_row(header, {'title': 'Intro', 'duration': 60, 'number': 9}), rows[2])
        with patch.object(remote, '_walk_rows', return_value=iter([rows])):
            with self.assertRaisesRegex(RuntimeError, '同名'):
                remote._find_album_row(header, {'title': 'Intro', 'duration': 60, 'number': None})
        with patch.object(remote, '_walk_rows', return_value=iter([rows])):
            with self.assertRaisesRegex(RuntimeError, '找不到这首歌'):
                remote._find_album_row(header, {'title': '深呼吸', 'duration': 250, 'number': 2})

    def test_menu_item_is_the_one_naming_the_song(self):
        items = [Element(ControlTypeName='MenuItemControl', ClassName='MenuFlyoutItem', Name=n)
                 for n in ('置顶歌曲', '下载', '插播', '播放“深呼吸”', '添加到队列')]
        menu = Element()
        menu.Exists.return_value = True
        menu.GetChildren.return_value = items
        with patch.object(remote.uiautomation, 'MenuControl', return_value=menu):
            self.assertIs(remote._album_row_menu_item(Element(), '深呼吸'), items[3])
            self.assertIsNone(remote._album_row_menu_item(Element(), '問'))
        menu.Exists.return_value = False
        with patch.object(remote.uiautomation, 'MenuControl', return_value=menu):
            self.assertIsNone(remote._album_row_menu_item(Element(), '深呼吸'))

    def test_play_row_uses_menu_and_never_row_invoke(self):
        row = album_row('音轨 2 深呼吸 5 分钟', ['2', '深呼吸', '5:00'])
        more = row.GetChildren.return_value[0].GetChildren.return_value[-1]
        item = Element()
        with patch.object(remote, '_album_row_menu_item', side_effect=[None, item]), patch.object(remote.time, 'sleep'), \
             patch.object(remote, '_double_click_album_row') as double:
            self.assertEqual(remote._play_album_row(Element(), row, '深呼吸'), 'menu')
        more.GetInvokePattern.return_value.Invoke.assert_called_once()
        item.GetInvokePattern.return_value.Invoke.assert_called_once()
        double.assert_not_called()
        row.GetInvokePattern.assert_not_called()
        self.assertNotIn(10000, [c.args[0] for c in row.GetPattern.call_args_list if c.args])

    def test_missing_menu_closes_it_and_double_clicks_instead(self):
        row = album_row('音轨 2 深呼吸 5 分钟', ['2', '深呼吸', '5:00'])
        clock = itertools.count(0, 1.0)
        with patch.object(remote, '_album_row_menu_item', return_value=None), patch.object(remote.time, 'sleep'), \
             patch.object(remote.time, 'monotonic', side_effect=lambda: next(clock)), \
             patch.object(remote, '_close_menu') as close, patch.object(remote, '_double_click_album_row') as double:
            self.assertEqual(remote._play_album_row(Element(), row, '深呼吸'), 'double_click')
        close.assert_called_once()
        double.assert_called_once()

    def test_double_click_only_on_the_unobstructed_title(self):
        row = album_row('音轨 2 深呼吸 5 分钟', ['2', '深呼吸', '5:00'])
        title = row.GetChildren.return_value[0].GetChildren.return_value[2]
        title.BoundingRectangle = Mock(left=100, top=200, right=300, bottom=240)
        title.BoundingRectangle.width.return_value, title.BoundingRectangle.height.return_value = 200, 40
        row.GetRuntimeId.return_value = [7, 7]
        hit = Element(ControlTypeName='TextControl')
        hit.GetRuntimeId.return_value = [1]
        hit.GetParentControl.return_value = Element(ControlTypeName='GroupControl', GetRuntimeId=Mock(return_value=[2]),
                                                    GetParentControl=Mock(return_value=Element(ControlTypeName='ListItemControl',
                                                                                               GetRuntimeId=Mock(return_value=[7, 7]))))
        with patch.object(remote.uiautomation, 'ControlFromPoint', return_value=hit) as point:
            remote._double_click_album_row(Element(), row, '深呼吸')
        point.assert_called_once_with(200, 220)
        title.DoubleClick.assert_called_once()
        title.DoubleClick.reset_mock()
        for blocker in (Element(ControlTypeName='HyperlinkControl'), None):
            with patch.object(remote.uiautomation, 'ControlFromPoint', return_value=blocker):
                with self.assertRaisesRegex(RuntimeError, '遮挡'):
                    remote._double_click_album_row(Element(), row, '深呼吸')
        title.DoubleClick.assert_not_called()

    def test_album_header_waits_for_the_requested_album(self):
        header = Element()
        header.Exists.return_value = True
        header.GetChildren.side_effect = [[text('BEFORE I FORGET')], [text('熱愛'), text('Yu Quan')]]
        with patch.object(remote.uiautomation, 'GroupControl', return_value=header), patch.object(remote.time, 'sleep'):
            self.assertIs(remote._album_header(Element(), '熱愛', timeout=5), header)
        header.GetChildren.side_effect = None
        header.GetChildren.return_value = [text('BEFORE I FORGET')]
        with patch.object(remote.uiautomation, 'GroupControl', return_value=header), patch.object(remote.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '没有打开这张专辑'):
                remote._album_header(Element(), '熱愛', timeout=0)


META = {'album_id': '670209426', 'title': '熱愛', 'artist': 'Yu Quan',
        'tracks': {'670209455': {'title': '深呼吸', 'artist': 'Yu Quan', 'duration': 299.546, 'number': 2, 'disc': 1}}}


def snapshot(title, artist='Yu Quan', album='熱愛', duration=299, position=0.5, playing=True):
    return dict(title=title, artist=artist, album=album, duration=duration, position=position, playing=playing)


class PlayFlowTests(unittest.TestCase):
    def setUp(self):
        self.window, self.header = Element(), Element()
        self.links = []
        for name, kwargs in (
            ('_window', {'return_value': self.window}),
            ('_open_app_link', {'side_effect': self.links.append}),
            ('_album_header', {'return_value': self.header}),
            ('CATALOG_CONFIRM_TIMEOUT', {'new': .6}),
        ):
            p = patch.object(remote, name, **kwargs)
            p.start(); self.addCleanup(p.stop)
        for target, name, kwargs in ((remote.uiautomation, 'UIAutomationInitializerInThread', {'return_value': nullcontext()}),
                                     (remote.apple_catalog, 'app_metadata', {'return_value': META})):
            p = patch.object(target, name, **kwargs)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(remote.time, 'sleep')
        p.start(); self.addCleanup(p.stop)

    def snapshots(self, *values):
        queue = list(values)
        return patch.object(remote, '_queue_playback_snapshot',
                            AsyncMock(side_effect=lambda: queue.pop(0) if len(queue) > 1 else queue[0]))

    def test_song_opens_album_link_plays_row_and_confirms(self):
        before = snapshot('A COLD PLAY', 'The Kid LAROI', 'BEFORE I FORGET', 178, 96)
        with self.snapshots(before, snapshot('正在连接…', '', '', 0), snapshot('深呼吸', duration=0), snapshot('深呼吸')), \
             patch.object(remote, '_find_album_row', return_value='row') as find, \
             patch.object(remote, '_play_album_row', return_value='menu') as play:
            result = remote.play_catalog_item('song', '670209426', '670209455')
        self.assertEqual(self.links, ['music://music.apple.com/us/album/670209426?i=670209455'])
        find.assert_called_once_with(self.header, META['tracks']['670209455'])
        play.assert_called_once_with(self.window, 'row', '深呼吸')
        self.assertEqual((result['ok'], result['method'], result['title']), (True, 'menu', '深呼吸'))

    def test_unconfirmed_or_same_recording_is_an_error(self):
        playing_same = snapshot('深呼吸', position=120)
        with self.snapshots(playing_same, playing_same), patch.object(remote, '_find_album_row'), \
             patch.object(remote, '_play_album_row', return_value='menu'):
            with self.assertRaisesRegex(RuntimeError, '尚未确认'):
                remote.play_catalog_item('song', '670209426', '670209455')
        with self.snapshots(snapshot('A'), snapshot('深呼吸', artist='Someone else')), patch.object(remote, '_find_album_row'), \
             patch.object(remote, '_play_album_row', return_value='double_click'):
            with self.assertRaisesRegex(RuntimeError, '尚未确认'):
                remote.play_catalog_item('song', '670209426', '670209455')

    def test_unknown_song_or_missing_app_touches_nothing(self):
        with self.assertRaisesRegex(RuntimeError, '不在该专辑'):
            remote.play_catalog_item('song', '670209426', '999')
        with patch.object(remote, '_window', side_effect=RuntimeError('找不到 Apple Music 应用窗口')):
            with self.assertRaisesRegex(RuntimeError, 'Apple Music'):
                remote.play_catalog_item('song', '670209426', '670209455')
        self.assertEqual(self.links, [])

    def test_album_play_and_shuffle_buttons(self):
        button = Element()
        button.Exists.return_value = True
        with self.snapshots(snapshot('Other', album='Else'), snapshot('選擇犧牲', duration=257)), \
             patch.object(remote.uiautomation, 'ButtonControl', return_value=button) as control, \
             patch.object(remote, '_shuffle_on', return_value=False):
            result = remote.play_catalog_item('album', '670209426', shuffle=False)
        self.assertEqual((result['method'], control.call_args.kwargs['AutomationId']), ('play_button', 'PlayButtonElement'))
        self.assertEqual(self.links[-1], 'music://music.apple.com/us/album/670209426')
        button.GetInvokePattern.return_value.Invoke.assert_called_once()
        # Shuffling the album that already plays only switches shuffle on.
        same = snapshot('選擇犧牲', duration=257, position=4)
        states = iter([False, False, True, True, True, True, True, True])
        with self.snapshots(same, same), patch.object(remote.uiautomation, 'ButtonControl', return_value=button) as control, \
             patch.object(remote, '_shuffle_on', side_effect=lambda w: next(states)):
            result = remote.play_catalog_item('album', '670209426', shuffle=True)
        self.assertEqual((result['method'], control.call_args.kwargs['AutomationId']), ('shuffle_button', 'ShuffleButtonElement'))
        button.Exists.return_value = False
        with self.snapshots(same), patch.object(remote.uiautomation, 'ButtonControl', return_value=button):
            with self.assertRaisesRegex(RuntimeError, '播放按钮'):
                remote.play_catalog_item('album', '670209426', shuffle=False)

    def test_open_page_only_opens_a_link(self):
        with patch.object(remote, '_open_app_link') as opener:
            self.assertEqual(remote.open_catalog_page('artist', '208628282'), {'ok': True})
        opener.assert_called_once_with('music://music.apple.com/us/artist/208628282')


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), remote.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, route):
        try:
            with urlopen(self.base + route, timeout=5) as response:
                return response.status, json.loads(response.read()), response.headers.get('Cache-Control')
        except HTTPError as exc:
            return exc.code, json.loads(exc.read() or b'{}'), exc.headers.get('Cache-Control')

    def post(self, route, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = Request(self.base + route, data=data, headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b'{}')
        except HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}

    def test_get_routes_status_codes_and_no_store(self):
        with patch.object(remote.apple_catalog, 'search', return_value={'ok': True, 'items': []}) as search:
            self.assertEqual(self.get('/catalog/search?term=%E6%B7%B1&kind=album'), (200, {'ok': True, 'items': []}, 'no-store'))
        search.assert_called_once_with('深', 'album')
        for code, status in (('invalid', 400), ('not_found', 404), ('rate_limited', 429), ('unavailable', 503), ('timeout', 503)):
            with patch.object(remote.apple_catalog, 'album', side_effect=catalog.CatalogError(code, 30)):
                got, body, _ = self.get('/catalog/album/670209426')
            self.assertEqual((got, body['error']), (status, code))
            self.assertIn('err', body)
        with patch.object(remote.apple_catalog, 'artist', side_effect=ValueError('bug')):
            self.assertEqual(self.get('/catalog/artist/1')[0], 503)
        self.assertEqual(self.get('/catalog/album/abc')[0], 404)
        self.assertEqual(self.get('/catalog/elsewhere')[0], 404)
        with patch.object(remote.apple_catalog, 'resolve', return_value={'ok': True, 'match': None}) as resolve:
            got = self.get('/catalog/resolve?title=A&artist=The+Kid+LAROI+%E2%80%94+BEFORE+I+FORGET+%5BExplicit%5D&album=&duration=178')
        self.assertEqual(got[0], 200)
        resolve.assert_called_once_with('A', 'The Kid LAROI', 'BEFORE I FORGET', 178.0, 'explicit')
        self.assertEqual(self.get('/catalog/resolve?title=A&artist=B&duration=x')[0], 400)

    def test_play_and_open_validation_origin_and_failures(self):
        song = {'kind': 'song', 'album_id': '670209426', 'song_id': '670209455'}
        album = {'kind': 'album', 'album_id': '670209426', 'shuffle': True}
        with patch.object(remote, 'play_catalog_item', return_value={'ok': True, 'method': 'menu'}) as play, \
             patch.object(remote, 'open_catalog_page', return_value={'ok': True}) as open_page:
            for bad in ({}, [], dict(song, kind='video'), dict(song, song_id=670209455), dict(song, album_id='0'),
                        dict(song, extra=1), dict(album, shuffle='yes'), {'kind': 'album', 'album_id': '1'}):
                self.assertEqual(self.post('/catalog/play', bad)[0], 400)
            # Oversized bodies are refused from the headers alone (raw socket: the body is never sent).
            with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=5) as raw:
                raw.sendall(b'POST /catalog/play HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 300\r\n\r\n')
                self.assertTrue(raw.recv(64).startswith(b'HTTP/1.0 400'))
            self.assertEqual(self.post('/catalog/play', song, {'Origin': 'http://evil.example'})[0], 403)
            play.assert_not_called()
            self.assertEqual(self.post('/catalog/play', song), (200, {'ok': True, 'method': 'menu'}))
            play.assert_called_once_with(kind='song', album_id='670209426', song_id='670209455')
            self.assertEqual(self.post('/catalog/play', album, {'Origin': self.base})[0], 200)
            self.assertEqual(play.call_args.kwargs, {'kind': 'album', 'album_id': '670209426', 'shuffle': True})
            for bad in ({'kind': 'song', 'id': '1'}, {'kind': 'artist', 'id': 'x'}):
                self.assertEqual(self.post('/catalog/open', bad)[0], 400)
            self.assertEqual(self.post('/catalog/open', {'kind': 'artist', 'id': '208628282'})[0], 200)
            open_page.assert_called_once_with(kind='artist', item_id='208628282')
        with patch.object(remote, 'play_catalog_item', side_effect=RuntimeError('专辑页没有可用的播放按钮')):
            self.assertEqual(self.post('/catalog/play', song), (503, {'ok': False, 'err': '专辑页没有可用的播放按钮'}))
        with patch.object(remote, 'play_catalog_item', side_effect=catalog.CatalogError('rate_limited', 12)):
            code, body = self.post('/catalog/play', song)
        self.assertEqual((code, body['error'], body['retry_after']), (429, 'rate_limited', 12))

    def test_volume_is_never_part_of_catalog_playback(self):
        source = remote.play_catalog_item.__code__.co_names + remote.open_catalog_page.__code__.co_names
        self.assertFalse({'set_volume', 'toggle_mute'} & set(source))


if __name__ == '__main__':
    unittest.main()
