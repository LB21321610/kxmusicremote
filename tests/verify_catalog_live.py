"""Live catalog verification against the real Apple Music app. CHANGES PLAYBACK.

Starts a temporary loopback-only server (127.0.0.1:8799) with the current
code, records the playback state, then checks through HTTP like a client:
current-song match, album/artist pages, search "深呼吸" and play it, play its
album, and seek/shuffle/modes while the mini player is open. Real-data
screenshots come from tests/catalog_live_screens.cjs. The original song,
position, play/pause, shuffle and repeat are restored afterwards; the queue
becomes that song's album. Volume and mute are never written.

Run only with the user's permission:
    .venv\\Scripts\\python.exe tests\\verify_catalog_live.py
"""
import asyncio
import ctypes
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8799'
OUT = ROOT / 'tests' / 'artifacts' / 'catalog-live.json'
LAUNCH = ("import importlib.util,sys;from http.server import ThreadingHTTPServer;"
          "spec=importlib.util.spec_from_file_location('remote',r'%s');m=importlib.util.module_from_spec(spec);"
          "spec.loader.exec_module(m);m.uiautomation.SetGlobalSearchTimeout(2);"
          "ThreadingHTTPServer(('127.0.0.1',8799),m.Handler).serve_forever()") % (ROOT / 'apple-music-remote.py')
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
report = {'started': datetime.now().isoformat(timespec='seconds'), 'steps': [], 'method':
          'Temporary 127.0.0.1:8799 server with the current code; real Apple Music 1.1792, SMTC and catalog.'}


def http(method, route, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else (b'' if method == 'POST' else None)
    request = Request(BASE + route, data=data, method=method,
                      headers={'Content-Type': 'application/json'} if body is not None else {})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def step(label, ok, **details):
    report['steps'].append(dict(step=label, ok=bool(ok), **details))
    print(('PASS ' if ok else 'FAIL ') + label, json.dumps(details, ensure_ascii=False)[:300], flush=True)
    return ok


def status():
    return http('GET', '/status')[1]


def wait_status(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        current = status()
        if current and predicate(current):
            return current
        time.sleep(.4)
    return status()


def resolve(state):
    query = urlencode({'title': state['title'], 'artist': state['artist'], 'album': state.get('album', ''),
                       'duration': state['dur']})
    return http('GET', '/catalog/resolve?' + query)


def windows():
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        pids = {p.pid for p in auto.GetProcesses(detailedInfo=False) if p.exeName.casefold() == 'applemusic.exe'}
        out = []
        for window in auto.GetRootControl().GetChildren():
            if window.ProcessId in pids and window.ClassName == 'WinUIDesktopWin32WindowClass':
                main = auto.Control(searchFromControl=window, AutomationId='NavView', searchDepth=6).Exists(0, 0)
                out.append(dict(name=window.Name, hwnd=window.NativeWindowHandle, main=main))
        return out


def open_mini_player():
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        main = next(w for w in auto.GetRootControl().GetChildren()
                    if w.ClassName == 'WinUIDesktopWin32WindowClass'
                    and auto.Control(searchFromControl=w, AutomationId='NavView', searchDepth=6).Exists(0, 0))
        main.SetActive(waitTime=0.3)
        auto.SendKeys('{Ctrl}{Shift}m', waitTime=1.5)


def close_mini_player():
    user32 = ctypes.windll.user32
    for window in windows():
        if window['main']:
            user32.ShowWindow(window['hwnd'], 9)  # SW_RESTORE
    time.sleep(1)
    for window in windows():
        if not window['main']:
            user32.PostMessageW(window['hwnd'], 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(1.2)


def main():
    server = subprocess.Popen([sys.executable, '-c', LAUNCH], cwd=ROOT, stdout=subprocess.DEVNULL,
                              stderr=open(ROOT / 'tests' / 'artifacts' / 'catalog-live-server.log', 'w', encoding='utf-8'))
    original = original_match = original_modes = None
    try:
        for _ in range(60):
            try:
                if http('GET', '/settings/lyrics', timeout=2)[0] == 200:
                    break
            except OSError:
                time.sleep(.25)
        original, original_modes = status(), http('GET', '/modes')[1]
        report['original'] = dict(status=original, modes=original_modes, windows=windows())
        step('record original state', original and original.get('ok'), title=original.get('title'),
             pos=round(original.get('pos', 0), 1), playing=original.get('playing'), vol=original.get('vol'),
             muted=original.get('muted'), modes=original_modes)
        if original.get('loaded') is not False and original.get('dur'):
            code, data = resolve(original)
            original_match = (data or {}).get('match')
            step('match current song in the catalog', code == 200 and data.get('ok'), match=original_match,
                 reason=(data or {}).get('reason'))
        code, album = http('GET', '/catalog/album/670209426')
        step('album page data (熱愛)', code == 200 and album['source'] == 'web' and album['title'] == '热爱'
             and album['motion']['square'] and album['motion']['tall'], title=album.get('title'),
             tracks=len(album.get('tracks', [])), square=album['motion']['square']['file'], tall=album['motion']['tall']['file'])
        code, artist = http('GET', '/catalog/artist/208628282')
        step('artist page data (羽·泉)', code == 200 and artist['source'] == 'web' and artist['artwork'],
             name=artist.get('name'), top=len(artist.get('top', [])), shelves=[s['title'] for s in artist.get('shelves', [])])
        code, found = http('GET', '/catalog/search?' + urlencode({'term': '深呼吸 羽泉', 'kind': 'song'}))
        hit = next((i for i in (found or {}).get('items', []) if i['id'] == '670209455'), None)
        step('search 深呼吸', code == 200 and hit, first=[(i['title'], i['album']) for i in found['items'][:3]])
        started = time.monotonic()
        code, played = http('POST', '/catalog/play', {'kind': 'song', 'album_id': hit['album_id'], 'song_id': hit['id']})
        now = wait_status(lambda s: s.get('title') == '深呼吸' and s.get('playing'))
        step('play 深呼吸 from search in Apple Music', code == 200 and played.get('ok') and now.get('title') == '深呼吸',
             response=played, seconds=round(time.monotonic() - started, 1), now=now.get('title'))
        subprocess.run(['node', str(ROOT / 'tests' / 'catalog_live_screens.cjs')], cwd=ROOT, timeout=240)
        report['screens'] = json.loads((ROOT / 'tests' / 'artifacts' / 'catalog-live-screens.json').read_text(encoding='utf-8'))
        step('real-data screenshots (390, 1440)', report['screens'].get('ok'), **{k: v for k, v in report['screens'].items() if k != 'ok'})
        started = time.monotonic()
        code, played = http('POST', '/catalog/play', {'kind': 'album', 'album_id': '670209426', 'shuffle': False})
        now = wait_status(lambda s: s.get('title') == '選擇犧牲' and s.get('playing'))
        step('play the whole album', code == 200 and now.get('title') == '選擇犧牲', response=played,
             seconds=round(time.monotonic() - started, 1))
        open_mini_player()
        mini = windows()
        step('mini player opened', any(not w['main'] for w in mini), windows=mini)
        code, modes = http('GET', '/modes')
        step('mini player: read shuffle/repeat', code == 200 and modes.get('ok'), modes=modes)
        code, _ = http('POST', '/cmd/seek/60')
        now = wait_status(lambda s: abs(s.get('pos', 0) - 60) < 4, 5)
        step('mini player: seek to 1:00', code == 204 and abs(now['pos'] - 60) < 4, pos=round(now['pos'], 1))
        first = http('POST', '/cmd/shuffle')[0]
        time.sleep(.8)
        toggled = http('GET', '/modes')[1]
        second = http('POST', '/cmd/shuffle')[0]
        time.sleep(.8)
        back = http('GET', '/modes')[1]
        step('mini player: shuffle on and off', first == 204 and second == 204 and toggled['shuffle'] != modes['shuffle']
             and back['shuffle'] == modes['shuffle'], toggled=toggled, back=back)
    finally:
        try:
            if any(not w['main'] for w in windows()):
                close_mini_player()
            restore = {}
            if original_match:
                code, data = http('POST', '/catalog/play', {'kind': 'song', 'album_id': original_match['album_id'],
                                                            'song_id': original_match['id']})
                restore['play'] = (code, data)
                if code == 200:
                    time.sleep(1)
                    restore['seek'] = http('POST', '/cmd/seek/%d' % int(original['pos']))[0]
                    if not original.get('playing'):
                        time.sleep(.5)
                        restore['pause'] = http('POST', '/cmd/toggle')[0]
            if original_modes and original_modes.get('ok'):
                modes = http('GET', '/modes')[1] or {}
                if modes.get('shuffle') != original_modes['shuffle']:
                    restore['shuffle'] = http('POST', '/cmd/shuffle')[0]
                for _ in range(3):
                    modes = http('GET', '/modes')[1] or {}
                    if modes.get('repeat') == original_modes['repeat']:
                        break
                    restore.setdefault('repeat', []).append(http('POST', '/cmd/repeat')[0])
                    time.sleep(.6)
            time.sleep(1)
            final = status()
            report['restore'] = restore
            report['final'] = dict(status=final, modes=http('GET', '/modes')[1], windows=windows())
            step('restore original song, position and modes',
                 bool(original_match) and final.get('title') == original.get('title')
                 and abs(final.get('pos', 0) - original.get('pos', 0)) < 6 and final.get('playing') == original.get('playing'),
                 title=final.get('title'), pos=round(final.get('pos', 0), 1), playing=final.get('playing'),
                 vol=final.get('vol'), muted=final.get('muted'), restore=restore)
        finally:
            server.terminate()
            server.wait(10)
            report['finished'] = datetime.now().isoformat(timespec='seconds')
            OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
            print('report:', OUT)


if __name__ == '__main__':
    main()
