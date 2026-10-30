# SPDX-License-Identifier: GPL-3.0-only
"""Headless JSON-in/JSON-out LDDC adapter. No QApplication or GUI entry point.

Uses the GPL-3.0-only LDDC package; see THIRD_PARTY.md. Each process has a hard
parent deadline; completed source results survive another source timing out.
"""
import json
import os
import sys
import time
import copy
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, wait

from lyrics_matching import approximate_score, fingerprint, score_record, search_terms

SOURCE_LABELS = {'QM': 'QQ音乐', 'NE': '网易云音乐', 'KG': '酷狗音乐', 'LRCLIB': 'LRCLIB'}
PROGRESS_LOCK = Lock()


def serialize_lyrics(lyrics):
    """Preserve every original word/line, including equal/absent timestamps."""
    raw = {language: [dict(start=line.start, end=line.end,
                words=[dict(start=w.start, end=w.end, text=w.text) for w in line.words])
                for line in lines] for language, lines in lyrics.items()}
    original = raw.get('orig', [])
    rows = []
    for index, line in enumerate(original):
        stamp = line['start']
        if stamp is None:
            stamp = next((w['start'] for w in line['words'] if w['start'] is not None), None)
        rows.append(dict(time=stamp / 1000 if stamp is not None else None,
                         text=''.join(w['text'] for w in line['words']),
                         words=line['words'], original_index=index))
    plain = '\n'.join(r['text'] for r in rows)
    # Mixed timed/untimed content is displayed whole as plain text. Inventing a
    # timestamp or filtering untimed rows would corrupt the translation mapping.
    synced = bool(rows) and all(r['time'] is not None for r in rows)
    if synced:
        rows.sort(key=lambda r: (r['time'], r['original_index']))
        grouped = []
        for row in rows:
            if grouped and grouped[-1]['time'] == row['time']:
                grouped[-1]['text'] += '\n' + row['text']
                grouped[-1]['words'].extend(row['words'])
                grouped[-1]['original_indices'].append(row['original_index'])
            else:
                grouped.append(dict(row, words=list(row['words']), original_indices=[row['original_index']]))
        rows = grouped
    return dict(state='instrumental' if lyrics.is_inst() else 'synced' if synced else 'plain' if plain.strip() else 'not_found',
                lines=rows if synced else [], plain=plain, raw_lyrics=raw)


def get_api(source):
    if source == 'QM':
        from LDDC.core.api.lyrics.qm import QMAPI
        return QMAPI()
    if source == 'NE':
        from LDDC.core.api.lyrics.ne import NEAPI
        return NEAPI()
    if source == 'KG':
        from LDDC.core.api.lyrics.kg import KGAPI
        return KGAPI()
    from LDDC.core.api.lyrics.lrclib import LrclibAPI
    return LrclibAPI()


def descriptor(song):
    return dict(source=song.source.name, title=song.title, subtitle=song.subtitle,
                artist=list(song.artist or []), album=song.album, duration=song.duration,
                id=song.id, mid=song.mid, hash=song.hash,
                language=song.language.name if song.language else None)


def retry(call, deadline):
    import httpx
    for attempt in range(2):
        if time.monotonic() >= deadline:
            raise TimeoutError()
        try:
            return call()
        except httpx.TimeoutException:
            if attempt or deadline - time.monotonic() < 3:
                raise


def fetch(api, song, deadline):
    from LDDC.common.models import Language
    if song.language == Language.INSTRUMENTAL:
        return dict(state='instrumental', lines=[], plain='', raw_lyrics={})
    # KG SongInfo.get_lyrics picks the first lyric hit upstream. Rank the lyric
    # list by duration/confidence before passing an explicit LyricInfo instead.
    if song.source.name == 'KG':
        infos = retry(lambda: api.get_lyricslist(song), deadline)
        reliable = [i for i in infos if i.duration and abs(i.duration - song.duration) <= 4000]
        reliable.sort(key=lambda i: (i.score or 0, -abs(i.duration - song.duration)), reverse=True)
        if not reliable:
            raise ValueError('no_matching_lyric')
        song = reliable[0]
    return serialize_lyrics(retry(lambda: api.get_lyrics(song), deadline))


def source_job(source, track, deadline, progress=None):
    from LDDC.common.models import SearchType
    api = get_api(source)  # Guest login may itself fail; isolated per source.
    if hasattr(api, 'client'):
        import httpx
        api.client.timeout = httpx.Timeout(3)
    try:
        terms, titles = search_terms(track)
        if source != 'LRCLIB':
            # Mainland catalogs list Chinese releases under the store-localized names.
            terms, titles = terms[1:] + terms[:1], titles[1:] + titles[:1]
        candidates, seen = [], set()
        def collect(results):
            for song in list(results)[:30]:
                record = dict(id=song.id, trackName=song.full_title, artistName=song.str_artist,
                              albumName=song.album or '', duration=(song.duration or 0) / 1000,
                              language={'CHINESE': 'zh', 'JAPANESE': 'ja', 'KOREAN': 'ko', 'ENGLISH': 'en'}.get(
                                  song.language.name if song.language else '', ''))
                score = score_record(record, track)
                approximate = score is None
                if approximate:
                    score = approximate_score(record, track)
                if score is not None:
                    info = descriptor(song)
                    candidate_id = source + '-' + fingerprint(info)[:24]
                    if candidate_id not in seen:
                        seen.add(candidate_id)
                        candidates.append(dict(id=candidate_id, source=SOURCE_LABELS[source], record=record,
                                               score=score, approximate=approximate, descriptor=info))
        for index, words in enumerate(terms):
            if index and deadline - time.monotonic() <= 4:
                break
            collect(retry(lambda: api.search(words, SearchType.SONG), deadline))
        if not any(not c['approximate'] for c in candidates):
            for words in titles:
                if deadline - time.monotonic() <= 4:
                    break
                collect(retry(lambda: api.search(words, SearchType.SONG), deadline))
        candidates.sort(key=lambda c: (not c['approximate'], c['score'], c['id']), reverse=True)
        candidates = candidates[:6]
        if progress is not None:
            with PROGRESS_LOCK:
                progress.update(candidates=candidates)
        # Fetch two plausible alternatives per source, retaining other descriptors
        # for on-demand manual selection. A bad first lyric need not kill a source.
        for c in candidates[:2]:
            try:
                from LDDC.common.models import SongInfo
                payload = fetch(api, SongInfo.from_dict(c['descriptor']), deadline)
                with PROGRESS_LOCK:
                    c['payload'] = payload
            except Exception as exc:
                with PROGRESS_LOCK:
                    c['error'] = type(exc).__name__
        return dict(candidates=candidates, status='ok' if candidates else 'not_found')
    finally:
        if hasattr(api, 'client'):
            api.client.close()


def run(command):
    deadline = time.monotonic() + command.get('budget', 11)
    if command['operation'] == 'fetch':
        from LDDC.common.models import SongInfo
        info = command['descriptor']
        api = get_api(info['source'])
        try:
            return dict(payload=fetch(api, SongInfo.from_dict(info), deadline))
        finally:
            if hasattr(api, 'client'):
                api.client.close()
    pool = ThreadPoolExecutor(max_workers=4)
    progress = {source: {} for source in SOURCE_LABELS}
    jobs = {pool.submit(source_job, source, command['track'], deadline, progress[source]): source for source in SOURCE_LABELS}
    done, _ = wait(jobs, timeout=max(0, deadline - time.monotonic()))
    result = dict(candidates=[], source_status={})
    for job, source in jobs.items():
        if job not in done:
            with PROGRESS_LOCK:
                partial = copy.deepcopy(progress[source].get('candidates', []))
            result['candidates'].extend(partial)
            result['source_status'][SOURCE_LABELS[source]] = 'partial_timeout' if any(c.get('payload') for c in partial) else 'timeout'
            continue
        try:
            value = job.result()
            result['candidates'].extend(value['candidates'])
            result['source_status'][SOURCE_LABELS[source]] = value['status']
        except Exception as exc:
            result['source_status'][SOURCE_LABELS[source]] = type(exc).__name__
    pool.shutdown(wait=False, cancel_futures=True)
    return result


if __name__ == '__main__':
    try:
        command = json.loads(sys.stdin.buffer.read(16000))
        result = run(command)
    except Exception as exc:
        result = dict(error=type(exc).__name__)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode())
    sys.stdout.buffer.flush()
    # Abandon outstanding source threads. No service/GUI is launched and no
    # source thread can keep this one-shot child alive after its budget.
    os._exit(0)
