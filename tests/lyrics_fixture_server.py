"""Browser-only local fixture. Does not control the user's Apple Music app."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_lyrics import remote
import base64
import json
import time
import tempfile
import os
from types import SimpleNamespace
import requests
from lyrics_translation import TranslationService
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

state = dict(ok=True, title='Test Song', artist='Artist', lyric_artist='Artist', album='Album',
             dur=180, pos=10, playing=False, vol=50, muted=False)
calls = []
lyric_mode = 'synced'
match_quality = ''
delay = 0
translation_case = ''
translation_delay = 0
translation_error = ''
remote.LYRIC_SETTINGS = remote.SettingsStore(Path(os.environ.get('REMOTE_FIXTURE_SETTINGS') or
    str(Path(tempfile.mkdtemp(prefix='remote-settings-fixture-')) / 'settings.json')))
settings_failure = False
settings_delay = 0
command_error = ''
queue_error = ''
catalog_search_error = ''
catalog_play_error = ''
catalog_delay = 0

# Catalog fixtures: no network. Motion URLs are local and missing on purpose,
# so real <video> loads fail and the page must fall back to the fluid backdrop.
CATALOG_TRACKS = [
    dict(id='2001', title='Catalog Song', artist='Catalog Artist', artists=[], duration=180, number=1, disc=1,
         explicit=False, disabled=False, popular=True),
    dict(id='2002', title='Second Song', artist='Catalog Artist & Guest Artist',
         artists=[dict(id='3001', name='Catalog Artist'), dict(id='3002', name='Guest Artist')],
         duration=200, number=2, disc=1, explicit=True, disabled=False, popular=False),
    dict(id='2003', title='Locked Song', artist='Catalog Artist', artists=[], duration=150, number=3, disc=1,
         explicit=False, disabled=True, popular=False),
]
CATALOG_ALBUM = dict(
    ok=True, source='web', id='1001', title='Catalog Album', artists=[dict(id='3001', name='Catalog Artist')],
    artist='Catalog Artist', genre_year='流行 · 2026年', description='专辑简介：第一段。' * 12,
    release='2026年1月1日', summary='3 首歌曲、9 分钟', copyright='℗ 2026 Fixture Records', label='Fixture Records',
    explicit=False, badges={}, artwork=dict(url='', bg='3a5f8a'),
    motion=dict(square=dict(hls='/__motion/square.m3u8', poster=None, file=dict(url='/__motion/square.mp4', width=768, height=768)),
                tall=dict(hls='/__motion/tall.m3u8', poster=None, file=dict(url='/__motion/tall.mp4', width=664, height=886))),
    tracks=CATALOG_TRACKS,
    more=[dict(title='更多 Catalog Artist 的作品', items=[dict(kind='album', id='1001', title='Catalog Album', subtitle='2026年', artwork=None)])])
CATALOG_ARTISTS = {
    '3001': dict(ok=True, source='web', id='3001', name='Catalog Artist', artwork=None, wide=None, color='8a3a5f',
                 bio='艺人简介。' * 40,
                 top=[dict(id='2001', album_id='1001', title='Catalog Song', subtitle='Catalog Album · 2026年', duration=180,
                           explicit=False, artwork=None)],
                 featured=dict(kind='album', id='1001', heading='最新发行', title='Catalog Album', subtitle='2026年1月1日', artwork=None),
                 shelves=[dict(title='专辑', items=[dict(kind='album', id='1001', title='Catalog Album', subtitle='2026年', artwork=None)])],
                 similar=[dict(id='3002', name='Guest Artist', artwork=None)]),
    '3002': dict(ok=True, source='itunes', id='3002', name='Guest Artist', artwork=None, wide=None, color=None, bio='',
                 top=[], featured=None, shelves=[], similar=[], genre='Pop'),
}
CATALOG_ERRORS = {'rate_limited': (429, dict(ok=False, error='rate_limited', err='请求过于频繁，请稍后再试', retry_after=42)),
                  'unavailable': (503, dict(ok=False, error='unavailable', err='暂时无法连接 Apple Music 曲库，请重试'))}


def catalog_get(path, query):
    """(status, payload) for /catalog/* GET routes."""
    if catalog_delay:
        time.sleep(catalog_delay)
    one = lambda name: query.get(name, [''])[0]
    if path == '/catalog/search':
        term, kind = one('term'), one('kind') or 'song'
        if catalog_search_error:
            return CATALOG_ERRORS[catalog_search_error]
        if term == 'none':
            return 200, dict(ok=True, kind=kind, term=term, items=[])
        items = {
            'song': [dict(kind='song', id=t['id'], album_id='1001', artist_id='3001', title=t['title'], artist=t['artist'],
                          album='Catalog Album', duration=t['duration'], explicit=t['explicit'], artwork=None)
                     for t in CATALOG_TRACKS[:2]],
            'album': [dict(kind='album', id='1001', artist_id='3001', title='Catalog Album', artist='Catalog Artist',
                           year='2026', genre='Pop', track_count=3, explicit=False, artwork=None)],
            'artist': [dict(kind='artist', id='3001', name='Catalog Artist', genre='Pop')],
        }.get(kind)
        if items is None:
            return 400, dict(ok=False, error='invalid', err='请求参数无效')
        return 200, dict(ok=True, kind=kind, term=term, items=items)
    if path == '/catalog/resolve':
        track = next((t for t in CATALOG_TRACKS if t['title'] == one('title')), None)
        if not track:
            return 200, dict(ok=True, match=None, reason='not_found')
        return 200, dict(ok=True, reason=None, match=dict(kind='song', id=track['id'], album_id='1001', artist_id='3001',
                                                         title=track['title'], artist=track['artist'], album='Catalog Album',
                                                         duration=track['duration'], artwork=None))
    if path == '/catalog/album/1001':
        return 200, CATALOG_ALBUM
    if path.startswith('/catalog/artist/') and path.rsplit('/', 1)[1] in CATALOG_ARTISTS:
        return 200, CATALOG_ARTISTS[path.rsplit('/', 1)[1]]
    return 404, dict(ok=False, error='not_found', err='Apple Music 曲库中没有找到')


class FixtureClient:
    def __init__(self):
        self.calls = []

    def translate_text(self, texts, **kw):
        self.calls.append(list(texts))
        wait, error = translation_delay, translation_error
        if wait:
            time.sleep(wait / 30)  # model requests now contain one fragment
        if error:
            response = requests.Response()
            response.status_code = int(error)
            raise requests.exceptions.HTTPError('fixture error', response=response)
        return [SimpleNamespace(text='简体中文译文：'+text+' · '+'这是长译文换行测试。'*8,
            detected_source_lang=kw.get('source_lang') or ('JA' if 'の' in text else 'EN')) for text in texts]


fixture_client = FixtureClient()
translator = TranslationService(Path(tempfile.mkdtemp(prefix='remote-translation-fixture-'))/'cache.sqlite3', client=fixture_client, sleeper=lambda _:None)


def fixture_texts(title):
    cases = {
        'english': [f'{title}: English lyric line {i+1}' for i in range(60)],
        'traditional': ['今天的風輕輕吹過我的心','我愛你直到永遠','乾杯之後頭髮隨風飛揚']*20,
        'simplified': ['今天的风轻轻吹过我的心','我爱你直到永远','干杯之后头发随风飞扬']*20,
        'mixed': ['今天的風 I will remember you 永遠吹過我的心','English refrain','我愛你直到永遠']*20,
        'japanese': ['君の声が聞こえる','夜空','愛']*20,
        'paired': ['I will remember you\n我會永遠記住你']*60,
    }
    return cases.get(translation_case, [f'{title}: 第 {i+1} 行 · English ' + ('长句中文换行测试'*16 if i % 5 == 0 else '歌词') for i in range(60)])


class Fixture(BaseHTTPRequestHandler):
    def send(self, payload, ctype='application/json', code=200):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        path = urlparse(self.path).path
        calls.append(self.path)
        if path == '/':
            return self.send(remote.PAGE.encode(), 'text/html; charset=utf-8')
        if path == '/artwork':
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600" fill="#5c2452"/><circle cx="300" cy="300" r="130" fill="#f786ad"/></svg>'
            return self.send(svg, 'image/svg+xml')
        if path == '/status':
            return self.send(dict(state, lyric_settings=remote.LYRIC_SETTINGS.snapshot()))
        if path == '/settings/lyrics':
            return self.send(remote.LYRIC_SETTINGS.snapshot())
        if path == '/lyrics':
            mode, wait, quality = lyric_mode, delay, match_quality
            query = parse_qs(urlparse(self.path).query)
            title = query.get('title', [''])[0]
            candidate = query.get('candidate', ['auto'])[0]
            if wait:
                time.sleep(wait)
            if mode == 'error':
                return self.send(dict(ok=False, state='error', error='timeout'))
            result = dict(ok=True, state=mode, source='网易云音乐' if candidate=='NE-fixture' else 'QQ音乐',record=dict(trackName=title),
                          candidates=[dict(id='QM-fixture',source='QQ音乐',record=dict(trackName=title,albumName='Studio',duration=180)),
                                      dict(id='NE-fixture',source='网易云音乐',record=dict(trackName=title,albumName='Studio',duration=180))])
            if quality:
                # Approximate tier: same core title and duration, another artist name.
                result['match_quality'] = quality
                result['candidates'][0]['approximate'] = quality == 'approximate'
            texts = fixture_texts(title)
            if candidate=='NE-fixture':
                texts = ['Alternate version '+text for text in texts]
            if mode == 'synced':
                result['lines'] = [dict(time=i*3, text=text) for i,text in enumerate(texts)]
            if mode == 'plain':
                result['plain'] = '\n'.join(texts) if translation_case else ('普通歌词 中文 English\n'+'长句测试'*80+'\n')*20
            if mode in ('synced','plain'):
                result = translator.register(result, '\n'.join(f'[{i*3//60:02}:{i*3%60:02}]'+text for i,text in enumerate(texts)) if mode=='synced' else '', result.get('plain',''))
            return self.send(result)
        if path == '/lyrics/translation':
            return self.send(translator.snapshot(parse_qs(urlparse(self.path).query).get('lyrics_id',[''])[0]))
        if path.startswith('/catalog/'):
            code, payload = catalog_get(path, parse_qs(urlparse(self.path).query))
            return self.send(payload, code=code)
        if path == '/__test':
            return self.send(dict(state=state, mode=lyric_mode, calls=calls, model_calls=fixture_client.calls))
        if path == '/modes':
            return self.send(dict(ok=True, shuffle=False, repeat='none'))
        if path == '/playlists':
            return self.send(dict(ok=True, items=[dict(name='Test Playlist')]))
        if path == '/queue' and queue_error:
            return self.send(dict(ok=False, err=queue_error))
        if path.startswith('/tracks/') or path in ('/search', '/queue'):
            return self.send(dict(ok=True, revision='a'*64, items=[dict(t='Test Song', a='Artist', d='3:00', id='1')]))
        self.send({}, code=404)

    def do_POST(self):
        global lyric_mode, delay, translation_case, translation_delay, translation_error, settings_failure, settings_delay
        global command_error, queue_error, catalog_search_error, catalog_play_error, catalog_delay, match_quality
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        calls.append(self.path)
        if self.path == '/__test':
            state.update(body.get('state', {}))
            lyric_mode = body.get('mode', lyric_mode)
            match_quality = body.get('match_quality', match_quality)
            delay = body.get('delay', delay)
            translation_case = body.get('translation_case', translation_case)
            translation_delay = body.get('translation_delay', translation_delay)
            translation_error = body.get('translation_error', translation_error)
            settings_failure = body.get('settings_failure', settings_failure)
            settings_delay = body.get('settings_delay', settings_delay)
            command_error = body.get('command_error', command_error)
            queue_error = body.get('queue_error', queue_error)
            catalog_search_error = body.get('catalog_search_error', catalog_search_error)
            catalog_play_error = body.get('catalog_play_error', catalog_play_error)
            catalog_delay = body.get('catalog_delay', catalog_delay)
            if 'translation_model_available' in body:
                translator.client = fixture_client if body['translation_model_available'] else None
            if body.get('reset_translation_errors'):
                translator.retry_at = 0
            if body.get('clear_calls'):
                calls.clear()
            return self.send(dict(ok=True))
        if self.path == '/lyrics/translation':
            return self.send(translator.start(body.get('lyrics_id',''), body.get('retry',False)))
        if self.path in ('/catalog/play', '/catalog/open'):
            try:
                request = remote.catalog_request(self.path, body)  # production validation
            except ValueError:
                return self.send(dict(ok=False, error='invalid', err='请求参数无效'), code=400)
            if self.path == '/catalog/open':
                return self.send(dict(ok=True))
            time.sleep(.3)
            if catalog_play_error:
                return self.send(dict(ok=False, err=catalog_play_error), code=503)
            track = next((t for t in CATALOG_TRACKS if t['id'] == request.get('song_id')), CATALOG_TRACKS[0])
            state.update(title=track['title'], artist='Catalog Artist — Catalog Album', lyric_artist='Catalog Artist',
                         album='Catalog Album', dur=track['duration'], pos=0, playing=True, loaded=True)
            return self.send(dict(ok=True, method='menu', title=track['title'], artist=state['artist']))
        if self.path == '/queue/play':
            time.sleep(.25)
            if body != dict(index=0, revision='a'*64):
                return self.send(dict(ok=False, err='invalid selection'), code=400)
            if command_error:
                return self.send(dict(ok=False, err=command_error), code=409 if command_error == 'stale queue' else 503)
            state.update(title='Test Song', playing=True, pos=0)
            return self.send(dict(ok=True))
        if self.path == '/settings/lyrics':
            if settings_delay:
                time.sleep(settings_delay)
            if settings_failure:
                return self.send(dict(ok=False, error='settings_save_failed'), code=503)
            code, data = remote.LYRIC_SETTINGS.update(body.get('lyric_offset'), body.get('revision'))
            return self.send(data, code=code)
        if self.path.startswith('/cmd/') and command_error:
            return self.send(dict(ok=False, err=command_error), code=503)
        if self.path == '/cmd/toggle':
            state['playing'] = not state['playing']
        if self.path.startswith('/cmd/vol/'):
            state['vol'] = int(self.path.rsplit('/', 1)[1])
        if self.path.startswith('/cmd/seek/'):
            state['pos'] = int(self.path.rsplit('/', 1)[1])
        if self.path == '/cmd/mute':
            state['muted'] = not state['muted']
        if self.path in ('/cmd/next', '/cmd/prev', '/play', '/splay'):
            state['title'] += ' next'
        self.send(b'', 'text/plain', code=204)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('REMOTE_FIXTURE_PORT', '18765'))
    print(f'fixture ready: http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Fixture).serve_forever()
