# SPDX-License-Identifier: GPL-3.0-only
"""Recording gates around LDDC's metadata similarity algorithms.

The service process uses only the non-fuzzy paths; LDDC (which pulls in
PySide6) is imported lazily by the fuzzy branch in the lddc_worker child.
"""
import difflib
import hashlib
import json
import math
import re
import unicodedata
import opencc

_converter = opencc.OpenCC('t2s.json')

_MARKERS = {
    'live': r'\blive\b|现场|演唱会', 'remix': r'\bremix\b|\bmix\b|混音',
    'acoustic': r'\bacoustic\b|不插电', 'instrumental': r'\binstrumental\b|伴奏|纯音乐',
    'demo': r'\bdemo\b', 'karaoke': r'\bkaraoke\b|卡拉',
    'sped': r'\bsped\b|\bnightcore\b|加速', 'slowed': r'\bslowed\b|慢速',
}
_VERSION = re.compile('|'.join(_MARKERS.values()))
# Title suffixes that do not change the recording: credits, soundtrack/theme
# notes, remasters, content ratings and edits. Live, Remix, Paused etc. stay.
_CREDIT = re.compile(r'(?:feat|ft|featuring|with)\b')
_NOISE = re.compile(r'\bfrom\b|soundtrack|\bost\b|主题曲|插曲|片尾曲|片头曲|推广曲|宣传曲|印象曲|电视剧|电影|动画|游戏|《'
                    r'|remaster|重制|\bexplicit\b|\bclean\b|\bbonus track\b'
                    r'|\b(?:single|album|radio|original) (?:version|edit)\b|\bmono\b|\bstereo\b')
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”|《[^》]*》|「[^」]*」|『[^』]*』')
_BRACKETS = re.compile(r'\(([^()]*)\)|\[([^\[\]]*)\]|【([^【】]*)】')
_ARTIST_SPLIT = re.compile(r'[,，&＆/／、;；]|\b(?:feat|ft|featuring)\b\.?|\s[x×]\s', re.I)
_CJK = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]')


def normalize(text):
    text = unicodedata.normalize('NFKC', text or '').casefold()
    # Cross-source Chinese metadata often uses different scripts.
    text = _converter.convert(text)
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in text).split())


def _strip_noise(title):
    """Title without noise suffixes, keeping its script and case (for searches)."""
    text = unicodedata.normalize('NFKC', title or '')

    def noise(segment):
        segment = _converter.convert(segment.casefold()).strip()
        if _CREDIT.match(segment):
            return True
        # (From "Live Forever") names a film; (Live from Wembley) is a live take.
        return bool(_NOISE.search(segment)) and not _VERSION.search(_QUOTED.sub(' ', segment))

    text = _BRACKETS.sub(lambda m: ' ' if noise(next(g for g in m.groups() if g is not None)) else m.group(0), text)
    while ' - ' in text:
        head, tail = text.rsplit(' - ', 1)
        if not noise(tail):
            break
        text = head
    return ' '.join(text.split())


def core_title(title):
    """Comparable title body; falls back to the whole title if nothing is left."""
    return normalize(_strip_noise(title)) or normalize(title)


def versions(title, album=''):
    # Callers pass core_title(title). A remaster is the same recording.
    text = normalize(title + ' ' + album)
    return {k for k, pattern in _MARKERS.items() if re.search(pattern, text)}


def artist_names(text):
    text = _converter.convert(unicodedata.normalize('NFKC', text or '').casefold())
    return [name for name in (normalize(part) for part in _ARTIST_SPLIT.split(text)) if name]


def artists_overlap(a, b):
    """One credited artist on both sides, also as the leading or trailing
    words of a bilingual name (G.E.M. / G.E.M. 邓紫棋). The extra words must be
    in the other script, so Artist / Wrong Artist or Future / Future Islands
    stay different artists."""
    for x in artist_names(a):
        for y in artist_names(b):
            if x == y:
                return True
            short, long = sorted((x.split(), y.split()), key=len)
            cjk = any(_CJK.search(w) for w in short)
            for extra in (long[len(short):] if long[:len(short)] == short else None,
                          long[:-len(short)] if long[-len(short):] == short else None):
                if extra and all(bool(_CJK.search(w)) != cjk for w in extra):
                    return True
    return False


def _ratio(a, b):
    # Same as LDDC text_difference, without importing LDDC.
    return difflib.SequenceMatcher(lambda c: c == ' ', a, b).ratio()


def _names(track):
    """The track's own names, then store-localized aliases of the same song."""
    return [track] + [a for a in track.get('aliases') or [] if isinstance(a, dict) and a.get('title') and a.get('artist')]


def _delta(record, track, limit):
    try:
        delta = abs(float(record['duration']) - track['duration'])
    except (ValueError, TypeError, KeyError):
        return None
    return delta if math.isfinite(delta) and delta <= limit else None


def _album_score(album, expected):
    album_match = bool(expected and normalize(album) == normalize(expected))
    return (_ratio(normalize(album), normalize(expected)) * 100 if album and expected else 0), album_match


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def track_key(track):
    key = [normalize(track[f]) for f in ('title', 'artist', 'album')] + [round(track['duration'], 3)]
    aliases = [[normalize(a['title']), normalize(a['artist'])] for a in _names(track)[1:]]
    return fingerprint(key + [aliases] if aliases else key)


def score_record(record, track, fuzzy=True):
    """Exact-tier score of a source record, or None. Without fuzzy, the core
    titles must be equal and the artists must overlap (no LDDC needed)."""
    title, artist, album = (record.get(k) or '' for k in ('trackName', 'artistName', 'albumName'))
    delta = _delta(record, track, 4)
    if delta is None or not title or not artist:
        return None
    core = core_title(title)
    flags = versions(core, album)
    best = None
    for names in _names(track):
        target = core_title(names['title'])
        # A high fuzzy title score alone must never cross recording types.
        if versions(target, names.get('album') or '') != flags:
            continue
        if core == target:
            title_score = 100
        elif not fuzzy:
            continue
        else:
            from LDDC.core.algorithm import calculate_title_score
            title_score = calculate_title_score(core, target)
            # Additional literal similarity protects different remix names.
            if title_score < 82 or _ratio(core, target) * 100 < (96 if flags else 82):
                continue
        if artists_overlap(artist, names['artist']):
            artist_score = 100
        elif not fuzzy:
            continue
        else:
            from LDDC.core.algorithm import calculate_artist_score
            artist_score = calculate_artist_score(normalize(names['artist']), normalize(artist))
            if artist_score < 85:
                continue
        album_score, album_match = _album_score(album, names.get('album') or '')
        # Album/recording confidence is ranked before synchronization granularity.
        score = round(title_score * .45 + artist_score * .35 + album_score * .2
                      + (15 if album_match else 0) - delta * 2, 3)
        best = score if best is None else max(best, score)
    return best


def approximate_score(record, track):
    """Approximate tier: same core title (two or more characters), same
    version markers and duration within two seconds; the artist is ignored."""
    title, album = record.get('trackName') or '', record.get('albumName') or ''
    delta = _delta(record, track, 2)
    if delta is None or not title:
        return None
    core = core_title(title)
    if len(core.replace(' ', '')) < 2:
        return None
    best = None
    for names in _names(track):
        if core_title(names['title']) != core or versions(core, album) != versions(core, names.get('album') or ''):
            continue
        album_score, album_match = _album_score(album, names.get('album') or '')
        score = round(45 + album_score * .2 + (15 if album_match else 0) - delta * 2, 3)
        best = score if best is None else max(best, score)
    return best


def search_terms(track):
    """(terms, titles): "first artist + core title" per name set, then the core
    titles alone for a search without artist. Aliases are converted to
    simplified Chinese for the mainland sources."""
    terms, titles = [], []
    for index, names in enumerate(_names(track)):
        title = _strip_noise(names['title']) or names['title'].strip()
        artist = next((a.strip() for a in _ARTIST_SPLIT.split(unicodedata.normalize('NFKC', names['artist'])) if a.strip()), '')
        if index:
            title, artist = _converter.convert(title), _converter.convert(artist)
        for value, bucket in ((f'{artist} {title}'.strip(), terms), (title, titles)):
            if normalize(value) and normalize(value) not in [normalize(v) for v in bucket]:
                bucket.append(value)
    return terms, titles


def choose_candidate(candidates):
    available = [c for c in candidates if c.get('payload', {}).get('state') in ('synced', 'plain', 'instrumental')]
    # Any exact candidate, even with a lower score, beats the approximate tier.
    available = [c for c in available if not c.get('approximate')] or available
    available.sort(key=lambda c: (c['score'], c['payload']['state'] == 'synced', c['source'] != 'LRCLIB', c['id']), reverse=True)
    if not available:
        return None
    best = available[0]
    # Platforms and editions (Deluxe, Explicit, Single...) carrying the same
    # recording are fine; the album only ranks. Lyrics against an instrumental
    # result with similar confidence needs an explicit user choice.
    if any(best['score'] - other['score'] < 4 and
           (best['payload']['state'] == 'instrumental') != (other['payload']['state'] == 'instrumental')
           for other in available[1:]):
        return None
    return best
