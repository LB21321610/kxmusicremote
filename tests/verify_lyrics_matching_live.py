"""Read-only before/after lyric matching check against the running Windows service.

Only requests /lyrics with fixed metadata of songs really played on this PC;
never touches playback, volume or the queue.

    .venv\\Scripts\\python.exe tests\\verify_lyrics_matching_live.py before|after
    .venv\\Scripts\\python.exe tests\\verify_lyrics_matching_live.py --compare before after
"""
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8765'
ARTIFACTS = ROOT/'tests/artifacts'
SHOWN = ('synced', 'plain', 'instrumental')
# Each new song may cost the alias lookup 2-4 iTunes requests; the service
# allows 18 a minute. Spacing the requests keeps a burst from hitting that cap.
SPACING = 15
RETRIES = 3
# title, artist, album, seconds, issue. Apple-side metadata comes from cached
# records; album/duration may differ slightly, which does not affect a comparison.
CASES = [
    ('乾杯', 'Mayday', '步步自選作品輯 1999-2013', 285, '英文歌手'),
    ('步步', 'Mayday', '步步自選作品輯 1999-2013', 273, '英文歌手'),
    ('任性 (電視劇《難哄》主題曲)', 'Mayday', '任性 (電視劇《難哄》主題曲) - Single', 265, '英文歌手＋后缀'),
    ('Best Of You', 'G.E.M.', 'T.I.M.E. - EP', 257, '英文歌手'),
    ('Therefore', 'G.E.M.', 'Heartbeat', 230, '英文歌手'),
    ('So Be It', 'JJ Lin', 'Drifter - EP', 272, '英文歌手'),
    ('Love Certificate (Remastered)', 'Yanzi Sun', 'Sun Yan-Zi (Remastered)', 255, '英文歌名＋remaster'),
    ('我的愛', 'Yanzi Sun', '孫燕姿Stefanie同名專輯', 261, '英文歌手'),
    ('第一天', 'Yanzi Sun', '完美的一天', 254, '英文歌手'),
    ('A LITTLE MUCH', 'iann dior', 'CYCLES', 155, '专辑歧义'),
    ('Wandered To LA', 'Juice WRLD & Justin Bieber', 'Fighting Demons (Digital Deluxe)', 189, '专辑歧义'),
    ('Wanderlust', 'The Weeknd', 'Kiss Land', 307, '专辑歧义'),
    ('SugarCrash!', 'ElyOtto', 'SugarCrash!', 80, '专辑歧义'),
    ('RATHER BE (feat. Lithe)', 'The Kid LAROI', 'BEFORE I FORGET', 264, 'feat 后缀'),
    ("WHAT'S THE MOVE?", 'The Kid LAROI, Future & BabyDrill', "WHAT'S THE MOVE? - Single", 199, '多歌手'),
    ('七里香', '周杰伦', '七里香', 299, '对照'),
    ('After Hours', 'The Weeknd', 'After Hours', 361, '对照'),
    ('No Such Recording 937fab123', 'No Such Artist', 'No Such Album', 123, '应仍 not_found'),
]


def get(path):
    with urlopen(BASE+path, timeout=25) as response:
        return json.load(response)


def preview(result):
    """First lines only, to spot-check that an approximate match is the same song."""
    texts = [l.get('text') or '' for l in result.get('lines') or []] or (result.get('plain') or '').split('\n')
    return [t for t in (t.strip() for t in texts) if t][:3]


def item(case, result, elapsed):
    title, artist, album, duration, issue = case
    record = result.get('record') or {}
    return dict(title=title, artist=artist, album=album, duration=duration, issue=issue,
                state=result.get('state'), source=result.get('source'), error=result.get('error'),
                match_quality=result.get('match_quality'),
                match_requires_selection=result.get('match_requires_selection'),
                record={k: record.get(k) for k in ('trackName', 'artistName', 'albumName', 'duration')},
                candidates=len(result.get('candidates') or []),
                candidate_list=[dict(source=c.get('source'), score=c.get('score'), state=c.get('state'),
                                     approximate=c.get('approximate'),
                                     **{k: (c.get('record') or {}).get(k) for k in ('trackName', 'artistName', 'albumName', 'duration')})
                                for c in result.get('candidates') or []],
                source_status=result.get('source_status'), source_cache_hit=result.get('source_cache_hit'),
                lines=len(result.get('lines') or []), preview=preview(result), elapsed_seconds=round(elapsed, 2))


def summary(items):
    count = lambda test: sum(1 for i in items if test(i))
    return dict(total=len(items), lyrics_shown=count(lambda i: i['state'] in SHOWN),
                exact=count(lambda i: i['state'] in SHOWN and i['match_quality'] != 'approximate'),
                approximate=count(lambda i: i['state'] in SHOWN and i['match_quality'] == 'approximate'),
                needs_selection=count(lambda i: i['match_requires_selection']),
                not_found=count(lambda i: i['state'] == 'not_found'), error=count(lambda i: i['state'] == 'error'))


def run(label):
    items, requested = [], {}
    for case in CASES:
        query = dict(title=case[0], artist=case[1], album=case[2], duration=case[3])
        if requested:
            time.sleep(max(0, max(requested.values()) + SPACING - time.monotonic()))
        started = time.monotonic()
        result = get('/lyrics?'+urlencode(query))
        requested[case[0]] = started
        items.append(item(case, result, time.monotonic()-started))
        print(case[0], '->', items[-1]['state'], items[-1]['source'], items[-1]['match_quality'], flush=True)
    # Errors (LRCLIB answers 503 at times) are cached for 30 s by the service and
    # the source cache; retry up to RETRIES times after that.
    for index, case in enumerate(CASES):
        attempts = []
        while items[index]['state'] == 'error' and len(attempts) < RETRIES:
            attempts.append(dict(state=items[index]['state'], error=items[index]['error']))
            time.sleep(max(0, requested[case[0]] + 31 - time.monotonic()))
            query = dict(title=case[0], artist=case[1], album=case[2], duration=case[3])
            requested[case[0]] = started = time.monotonic()
            items[index] = dict(item(case, get('/lyrics?'+urlencode(query)), time.monotonic()-started), first_attempt=attempts[0],
                                attempts=len(attempts)+1)
            print(case[0], 'retry ->', items[index]['state'], items[index]['source'], items[index]['match_quality'], flush=True)
    report = dict(label=label, read_only=True, base=BASE, time=time.strftime('%Y-%m-%d %H:%M:%S'),
                  summary=summary(items), tracks=items)
    target = ARTIFACTS/f'lyrics-matching-{label}.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report['summary'], ensure_ascii=False))
    print('written', target)


def compare(before, after):
    a, b = (json.loads((ARTIFACTS/f'lyrics-matching-{label}.json').read_text(encoding='utf8')) for label in (before, after))
    describe = lambda i: (f"{i['state']}/{i['source']}" + (' 近似' if i['match_quality'] == 'approximate' else '')
                          + (' 需选择' if i['match_requires_selection'] else ''))
    for old, new in zip(a['tracks'], b['tracks']):
        print(f"{old['title'][:28]:<30}{describe(old):<34}-> {describe(new)}")
    print(before, json.dumps(a['summary'], ensure_ascii=False))
    print(after, json.dumps(b['summary'], ensure_ascii=False))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--compare']:
        compare(*sys.argv[2:4])
    else:
        run(sys.argv[1] if len(sys.argv) > 1 else 'run')
