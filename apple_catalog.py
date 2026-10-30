"""Apple Music catalog lookups for the remote: search, current-song match,
album and artist pages, motion artwork.

Independent of Windows APIs, SMTC and UI Automation, so it can be tested on
its own and never takes UIA_LOCK. Only three HTTPS hosts are contacted:

- itunes.apple.com      iTunes Search/Lookup API (no key, about 20 requests
                        per minute; throttled to 18 and backed off on 403/429)
- music.apple.com       public album/artist pages; their embedded
                        "serialized-server-data" JSON has the localized
                        description, artist image, colors and motion artwork
- mvod.itunes.apple.com motion artwork HLS playlists

The page data is undocumented. Parsing is defensive and falls back to the
iTunes Lookup API (no artist image, description or motion artwork).
"""

import html
import json
import logging
import math
import os
import re
import socket
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ALLOWED_HOSTS = frozenset({"itunes.apple.com", "music.apple.com", "mvod.itunes.apple.com"})
ID_RE = re.compile(r"[1-9][0-9]{0,14}")
COLOR_RE = re.compile(r"[0-9a-fA-F]{6}")
ARTWORK_HOST_RE = re.compile(r"(?:[a-z0-9-]+\.)*mzstatic\.com")


def _setting(name, default, pattern):
    value = (os.environ.get(name) or "").strip()
    if value and re.fullmatch(pattern, value):
        return value
    if value:
        logging.warning("Ignoring invalid %s=%r; using %s", name, value, default)
    return default


# The Windows app shows store metadata of the account's storefront, which the
# iTunes API returns for the same country. Page text uses LANGUAGE.
STOREFRONT = _setting("REMOTE_CATALOG_STOREFRONT", "us", r"[a-z]{2}").lower()
LANGUAGE = _setting("REMOTE_CATALOG_LANGUAGE", "zh-Hans-CN", r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleMusicRemote/1.3"
REQUEST_TIMEOUT = 8.0
PAGE_LIMIT = 6_000_000
JSON_LIMIT = 2_000_000
PLAYLIST_LIMIT = 256_000
SEARCH_TTL = 15 * 60
ITEM_TTL = 24 * 3600
FAILURE_TTL = 60
CACHE_LIMIT = 512
# Motion artwork plays as a background loop: H.264 only, about 1-3 Mbps.
MOTION_MAX_BANDWIDTH = 2_600_000
MOTION_MAX_EDGE = 1200

MESSAGES = {
    "invalid": "请求参数无效",
    "not_found": "Apple Music 曲库中没有找到",
    "rate_limited": "请求过于频繁，请稍后再试",
    "timeout": "Apple Music 曲库响应超时，请重试",
    "unavailable": "暂时无法连接 Apple Music 曲库，请重试",
    "busy": "曲库请求较多，请稍后重试",
}
STATUS = {"invalid": 400, "not_found": 404, "rate_limited": 429}


class CatalogError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code if code in MESSAGES else "unavailable"
        self.retry_after = retry_after

    @property
    def status(self):
        return STATUS.get(self.code, 503)

    def payload(self):
        out = {"ok": False, "error": self.code, "err": MESSAGES[self.code]}
        if self.retry_after:
            out["retry_after"] = max(1, math.ceil(self.retry_after))
        return out


# --------------------------------------------------------------------------
# Matching helpers. Same algorithms as apple-music-remote.py's
# _lyric_normalize / lyric_metadata and lyrics_matching.versions, except that
# a remaster still counts here (kept in sync by tests/test_catalog.py), so
# this module needs no Windows packages.
# --------------------------------------------------------------------------

def normalize(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


def split_artist_album(artist, album):
    artist, album = (artist or "").strip(), (album or "").strip()
    if " — " in artist:
        name, suffix = artist.rsplit(" — ", 1)
        if name and suffix and (not album or normalize(suffix) == normalize(album)):
            artist, album = name, album or suffix
    album = re.sub(r"\s*\[(?:explicit|clean)\]\s*$", "", album, flags=re.I).strip()
    return artist, album


def recording_versions(title, album):
    text = normalize(title + " " + album)
    markers = {
        "live": r"\blive\b|现场|現場|演唱会|演唱會",
        "remix": r"\bremix\b|\bmix\b|混音",
        "acoustic": r"\bacoustic\b|不插电|不插電",
        "instrumental": r"\binstrumental\b|伴奏|纯音乐|純音樂",
        "demo": r"\bdemo\b", "karaoke": r"\bkaraoke\b|卡拉",
        "sped": r"\bsped\b|\bnightcore\b|加速",
        "slowed": r"\bslowed\b|慢速", "remaster": r"\bremaster(?:ed)?\b|重制|重製",
    }
    return frozenset(key for key, pattern in markers.items() if re.search(pattern, text))


# --------------------------------------------------------------------------
# HTTP: host allow-list, redirect checks, size and time limits, throttling.
# --------------------------------------------------------------------------

def check_url(url):
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise CatalogError("unavailable") from None
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username
            or parts.password or port not in (None, 443)):
        logging.warning("Refusing catalog URL outside the allow-list: %s://%s", parts.scheme, parts.hostname)
        raise CatalogError("unavailable")
    return url


class _CheckedRedirects(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = check_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = build_opener(_CheckedRedirects)


class Limiter:
    """Rolling one-minute request budget with server-requested backoff."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.sent = deque()
        self.retry_at = 0.0
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            if now < self.retry_at:
                raise CatalogError("rate_limited", self.retry_at - now)
            while self.sent and now - self.sent[0] >= 60:
                self.sent.popleft()
            if len(self.sent) >= self.per_minute:
                raise CatalogError("rate_limited", 60 - (now - self.sent[0]))
            self.sent.append(now)

    def backoff(self, seconds):
        with self.lock:
            self.retry_at = max(self.retry_at, time.monotonic() + max(5.0, min(600.0, seconds)))


ITUNES = Limiter(18)
PAGES = Limiter(30)
MOTION = Limiter(60)


def _retry_after(headers):
    try:
        return float(headers.get("Retry-After", "60"))
    except (TypeError, ValueError):
        return 60.0


def fetch(url, deadline, limit, limiter, accept="*/*"):
    """GET an allow-listed HTTPS URL; returns the body bytes."""
    check_url(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CatalogError("timeout")
    limiter.acquire()
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                    "Accept-Encoding": "identity"})
    try:
        with _OPENER.open(request, timeout=min(REQUEST_TIMEOUT, remaining)) as response:
            check_url(response.geturl())
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise CatalogError("timeout")
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise CatalogError("unavailable")
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPError as exc:
        if exc.code == 404:
            raise CatalogError("not_found") from None
        if exc.code in (403, 429):
            limiter.backoff(_retry_after(exc.headers))
            raise CatalogError("rate_limited", limiter.retry_at - time.monotonic()) from None
        raise CatalogError("unavailable") from None
    except CatalogError:
        raise
    except (socket.timeout, TimeoutError):
        raise CatalogError("timeout") from None
    except (URLError, HTTPException, OSError, ValueError) as exc:
        logging.info("Catalog request failed: %s", type(exc).__name__)
        raise CatalogError("unavailable") from None


# --------------------------------------------------------------------------
# Cache with request coalescing on a dedicated pool (never UIA_LOCK).
# --------------------------------------------------------------------------

_LOCK = threading.Lock()
_CACHE = OrderedDict()   # key -> (expires, ok, value or error code, retry_at)
_PENDING = {}
WORKER = ThreadPoolExecutor(max_workers=4, thread_name_prefix="catalog")


def _store(key, ttl, future):
    now = time.monotonic()
    retry_at = 0.0
    try:
        value = future.result()
        entry = (now + (ttl(value) if callable(ttl) else ttl), True, value, 0.0)
    except CatalogError as exc:
        if exc.retry_after:
            retry_at = now + exc.retry_after
        entry = (now + max(FAILURE_TTL, exc.retry_after or 0), False, exc.code, retry_at)
    except Exception:
        logging.exception("Catalog lookup failed")
        entry = (now + FAILURE_TTL, False, "unavailable", 0.0)
    with _LOCK:
        _PENDING.pop(key, None)
        _CACHE[key] = entry
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_LIMIT:
            _CACHE.popitem(last=False)


def cached(key, ttl, loader, budget, *, deadline=None, inline=False):
    deadline = min(deadline, time.monotonic() + budget) if deadline is not None else time.monotonic() + budget
    with _LOCK:
        entry = _CACHE.get(key)
        now = time.monotonic()
        if entry and entry[0] > now:
            _CACHE.move_to_end(key)
            if entry[1]:
                return entry[2]
            # A cached failure answers until it expires; say when a retry helps.
            raise CatalogError(entry[2], max(entry[0], entry[3]) - now)
        future = _PENDING.get(key)
        created = future is None
        if created:
            if len(_PENDING) >= 16:
                raise CatalogError("busy")
            future = Future() if inline else WORKER.submit(loader, deadline)
            _PENDING[key] = future
    if created:
        future.add_done_callback(lambda done: _store(key, ttl, done))
        if inline:
            # Album selection can run inside a resolve worker. Do not submit
            # another task to the same bounded pool and wait for it there.
            try:
                future.set_result(loader(deadline))
            except Exception as exc:
                future.set_exception(exc)
    try:
        return future.result(timeout=max(0.01, deadline - time.monotonic()))
    except FutureTimeout:
        raise CatalogError("timeout") from None


def clear_cache():
    with _LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------

def _get(node, *path, default=None):
    for part in path:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and -len(node) <= part < len(node):
            node = node[part]
        else:
            return default
        if node is None:
            return default
    return node


def _text(value, limit=4000):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _id(value):
    value = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _color(value):
    return value.lower() if isinstance(value, str) and COLOR_RE.fullmatch(value) else None


def _artwork(node, crop="bb"):
    """Artwork template with only {w}/{h} placeholders, plus its colors."""
    if not isinstance(node, dict):
        return None
    data = node.get("dictionary") if isinstance(node.get("dictionary"), dict) else node
    url = data.get("url")
    if not isinstance(url, str) or "{w}" not in url or "{h}" not in url:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not ARTWORK_HOST_RE.fullmatch(parts.hostname or ""):
        return None
    style = node.get("cropStyle")
    if isinstance(style, str) and re.fullmatch(r"[a-z]{2}", style):
        crop = style
    out = {"url": url.replace("{c}", crop).replace("{f}", "jpg")}
    for key, name in (("bgColor", "bg"), ("textColor1", "text1"), ("textColor2", "text2")):
        if _color(data.get(key)):
            out[name] = _color(data.get(key))
    if isinstance(data.get("width"), int) and isinstance(data.get("height"), int):
        out["width"], out["height"] = data["width"], data["height"]
    return out


def _itunes_artwork(record):
    url = record.get("artworkUrl100") or record.get("artworkUrl60")
    if not isinstance(url, str):
        return None
    template = re.sub(r"/\d+x\d+bb\.(?:jpg|png)$", "/{w}x{h}bb.jpg", url)
    return _artwork({"url": template})


def _year(value):
    return value[:4] if isinstance(value, str) and re.match(r"\d{4}", value) else ""


def _descriptor_id(node, kind):
    descriptor = _get(node, "contentDescriptor") or {}
    if descriptor.get("kind") != kind:
        return None
    return _id(_get(descriptor, "identifiers", "storeAdamID"))


def _segue_id(node, kind):
    return _descriptor_id(_get(node, "segue", "destination") or {}, kind)


def _links(links, kind):
    out = []
    for link in links if isinstance(links, list) else []:
        name = _text(_get(link, "title"), 200)
        if name:
            out.append({"id": _segue_id(link, kind), "name": name})
    return out


def _server_data(body):
    text = body.decode("utf-8", "replace")
    match = re.search(r'<script[^>]*\bid="serialized-server-data"[^>]*>(.*?)</script>', text, re.S)
    if not match:
        raise CatalogError("unavailable")
    raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        try:
            data = json.loads(html.unescape(raw))
        except ValueError:
            raise CatalogError("unavailable") from None
    # {"data":[{"intent":...,"data":{"sections":[...]}}]}; search a few levels.
    queue = [data]
    for _ in range(200):
        if not queue:
            break
        node = queue.pop(0)
        if isinstance(node, dict):
            if isinstance(node.get("sections"), list):
                return node
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            queue.extend(v for v in node if isinstance(v, (dict, list)))
    raise CatalogError("unavailable")


def _page(kind, item_id, deadline):
    url = (f"https://music.apple.com/{STOREFRONT}/{kind}/{item_id}?" + urlencode({"l": LANGUAGE}))
    return _server_data(fetch(url, deadline, PAGE_LIMIT, PAGES, "text/html"))


def _itunes(endpoint, params, deadline, country=None):
    url = f"https://itunes.apple.com/{endpoint}?" + urlencode(dict(params, country=country or STOREFRONT))
    try:
        data = json.loads(fetch(url, deadline, JSON_LIMIT, ITUNES, "application/json"))
    except ValueError:
        raise CatalogError("unavailable") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise CatalogError("unavailable")
    return [r for r in data["results"] if isinstance(r, dict)]


# --------------------------------------------------------------------------
# iTunes records
# --------------------------------------------------------------------------

def _seconds(millis):
    return round(millis / 1000, 3) if isinstance(millis, (int, float)) and millis > 0 else 0


def song_record(record):
    song_id, album_id = _id(record.get("trackId")), _id(record.get("collectionId"))
    if not song_id or not album_id or record.get("kind") not in (None, "song"):
        return None
    return {"kind": "song", "id": song_id, "album_id": album_id, "artist_id": _id(record.get("artistId")),
            "title": _text(record.get("trackName"), 300), "artist": _text(record.get("artistName"), 300),
            "album": _text(record.get("collectionName"), 300), "duration": _seconds(record.get("trackTimeMillis")),
            "number": record.get("trackNumber") if isinstance(record.get("trackNumber"), int) else None,
            "disc": record.get("discNumber") if isinstance(record.get("discNumber"), int) else None,
            "explicit": record.get("trackExplicitness") == "explicit", "year": _year(record.get("releaseDate")),
            "artwork": _itunes_artwork(record)}


def album_record(record):
    album_id = _id(record.get("collectionId"))
    if not album_id:
        return None
    return {"kind": "album", "id": album_id, "artist_id": _id(record.get("artistId")),
            "title": _text(record.get("collectionName"), 300), "artist": _text(record.get("artistName"), 300),
            "year": _year(record.get("releaseDate")), "genre": _text(record.get("primaryGenreName"), 100),
            "track_count": record.get("trackCount") if isinstance(record.get("trackCount"), int) else None,
            "explicit": record.get("collectionExplicitness") == "explicit", "artwork": _itunes_artwork(record)}


def artist_record(record):
    artist_id = _id(record.get("artistId"))
    if not artist_id:
        return None
    return {"kind": "artist", "id": artist_id, "name": _text(record.get("artistName"), 300),
            "genre": _text(record.get("primaryGenreName"), 100)}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

SEARCH_ENTITIES = {"song": ("song", 25, song_record), "album": ("album", 25, album_record),
                   "artist": ("musicArtist", 20, artist_record)}


def search(term, kind):
    term = " ".join((term or "").split())
    if not term or len(term) > 100 or kind not in SEARCH_ENTITIES:
        raise CatalogError("invalid")
    entity, limit, convert = SEARCH_ENTITIES[kind]

    def load(deadline):
        records = _itunes("search", {"term": term, "media": "music", "entity": entity, "limit": limit}, deadline)
        items = [item for item in map(convert, records) if item]
        return {"ok": True, "kind": kind, "term": term, "items": items}
    return cached(("search", kind, term.casefold(), STOREFRONT), SEARCH_TTL, load, 10)


# --------------------------------------------------------------------------
# Current-song match
# --------------------------------------------------------------------------

def pick_recording(records, title, artist, album, duration, edition=""):
    """Conservative recording match, the same rules as the lyric matcher.

    Title and artist must agree after normalization, duration within 3 s and
    version markers must agree. Album agreement ranks first; an explicit or
    clean marker picks the matching edition. Near-ties between different
    recordings are rejected instead of guessed.
    """
    ranked = []
    for record in records:
        song = song_record(record)
        if not song or normalize(song["title"]) != normalize(title) or normalize(song["artist"]) != normalize(artist):
            continue
        delta = abs(song["duration"] - duration)
        if not song["duration"] or not math.isfinite(delta) or delta > 3:
            continue
        if recording_versions(song["title"], song["album"]) != recording_versions(title, album):
            continue
        score = (30 if album and normalize(song["album"]) == normalize(album) else 0) + 10 - delta * 3
        explicitness = record.get("trackExplicitness")
        if edition == "clean":
            score += 5 if explicitness == "cleaned" else 0
        elif edition == "explicit":
            score += 5 if explicitness == "explicit" else 0
        elif explicitness == "cleaned":
            score -= 5  # Apple Music shows the explicit edition unless restricted
        ranked.append((score, song))
    if not ranked:
        return None, "not_found"
    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, best = ranked[0]
    for score, other in ranked[1:]:
        if best_score - score < 4 and other["id"] != best["id"]:
            return None, "ambiguous"
    return best, None


def pick_artwork_album(records, title, artist, album_name, duration, deadline):
    """Presentation only: require full metadata agreement, ignoring clean/explicit.

    Never supplies a song identity. Keep complete album names (including deluxe
    labels) and recording versions; only trailing content-rating tags are ignored.
    """
    def album_key(name):
        return normalize(re.sub(r"\s*\[(?:explicit|clean)\]\s*$", "", name, flags=re.I))

    expected_album = album_key(album_name)
    if not expected_album:
        return None
    ids = set()
    for record in records:
        song = song_record(record)
        if (song and normalize(song["title"]) == normalize(title)
                and normalize(song["artist"]) == normalize(artist)
                and album_key(song["album"]) == expected_album
                and song["duration"] and abs(song["duration"] - duration) <= 3
                and recording_versions(song["title"], song["album"]) == recording_versions(title, album_name)):
            ids.add(song["album_id"])
    checked = []
    for album_id in sorted(ids, key=int)[:3]:
        if time.monotonic() >= deadline:
            return None
        try:
            info = album(album_id, deadline=deadline)
        except CatalogError:
            # An incomplete comparison must not guess a presentation album.
            return None
        has_motion = any(m and (m.get("hls") or _get(m, "file", "url"))
                         for m in (info.get("motion") or {}).values())
        checked.append((not has_motion, int(album_id), album_id))
    return min(checked)[2] if checked else None


def resolve(title, artist, album, duration, edition="", *, deadline=None):
    title, artist, album = (" ".join((v or "").split()) for v in (title, artist, album))
    if (not title or not artist or len(title) > 300 or len(artist) > 300 or len(album) > 300
            or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 1 <= duration <= 86400 or edition not in ("", "clean", "explicit")):
        raise CatalogError("invalid")

    def load(deadline):
        records = _itunes("search", {"term": f"{title} {artist}", "media": "music", "entity": "song",
                                     "limit": 25}, deadline)
        match, reason = pick_recording(records, title, artist, album, duration, edition)
        if match is None and reason == "not_found" and album:
            records = _itunes("search", {"term": f"{title} {artist} {album}", "media": "music",
                                         "entity": "song", "limit": 25}, deadline)
            match, reason = pick_recording(records, title, artist, album, duration, edition)
        artwork_album_id = match["album_id"] if match else (
            pick_artwork_album(records, title, artist, album, duration, deadline)
            if reason == "ambiguous" else None)
        return {"ok": True, "match": match, "reason": reason, "artwork_album_id": artwork_album_id}
    key = ("resolve", normalize(title), normalize(artist), normalize(album), round(duration), edition, STOREFRONT)
    return cached(key, lambda result: ITEM_TTL if result.get("match") else FAILURE_TTL, load, 12, deadline=deadline)


# --------------------------------------------------------------------------
# Store-localized names, for lyric searches only. A us account shows Chinese
# releases under English names (Mayday, So Be It); the same song id in these
# storefronts has the original ones (五月天, 最好是). Checked 2026-10-29.
# --------------------------------------------------------------------------

LOCALIZED_STOREFRONTS = ("tw", "hk")


def localized(song_id, *, deadline=None):
    """{title, artist, album, country} of the same song id in the first
    LOCALIZED_STOREFRONTS store that has it, or None."""
    song_id = _id(song_id)
    if not song_id:
        raise CatalogError("invalid")

    def load(deadline):
        for country in LOCALIZED_STOREFRONTS:
            records = _itunes("lookup", {"id": song_id}, deadline, country=country)
            song = next((s for s in map(song_record, records) if s and s["id"] == song_id), None)
            if song:
                return {"title": song["title"], "artist": song["artist"], "album": song["album"], "country": country}
        return None
    return cached(("localized", song_id), ITEM_TTL, load, 8, deadline=deadline)


# --------------------------------------------------------------------------
# Motion artwork
# --------------------------------------------------------------------------

def _m3u8_attributes(text):
    return {key: value.strip('"') for key, value in
            re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text)}


def motion(master, deadline, max_bandwidth=MOTION_MAX_BANDWIDTH):
    """Pick an H.264 variant of a motion-artwork master playlist and return its
    single fragmented MP4 file, which plays in any browser's <video>."""
    check_url(master)
    lines = fetch(master, deadline, PLAYLIST_LIMIT, MOTION).decode("utf-8", "replace").splitlines()
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise CatalogError("unavailable")
    variants = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _m3u8_attributes(line.split(":", 1)[1])
        uri = next((l.strip() for l in lines[index + 1:] if l.strip() and not l.startswith("#")), "")
        codecs = [c.strip() for c in attrs.get("CODECS", "").split(",") if c.strip()]
        size = re.fullmatch(r"(\d+)x(\d+)", attrs.get("RESOLUTION", ""))
        try:
            bandwidth = int(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH"))
        except (TypeError, ValueError):
            continue
        if not uri or not codecs or not all(c.startswith("avc1") for c in codecs) or not size:
            continue
        width, height = int(size.group(1)), int(size.group(2))
        if max(width, height) <= MOTION_MAX_EDGE:
            variants.append((width, height, bandwidth, urljoin(master, uri), ",".join(codecs)))
    if not variants:
        return None
    fitting = [v for v in variants if v[2] <= max_bandwidth]
    width, height, bandwidth, variant, codecs = (max(fitting, key=lambda v: (v[0] * v[1], -v[2])) if fitting
                                                 else min(variants, key=lambda v: v[2]))
    lines = fetch(check_url(variant), deadline, PLAYLIST_LIMIT, MOTION).decode("utf-8", "replace").splitlines()
    files = set()
    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            uri = _m3u8_attributes(line.split(":", 1)[1]).get("URI")
            if uri:
                files.add(urljoin(variant, uri))
        elif line and not line.startswith("#"):
            files.add(urljoin(variant, line))
    if len(files) != 1:
        return None  # segmented variants need an HLS player
    url = check_url(files.pop())
    if not urlsplit(url).path.endswith(".mp4"):
        return None
    return {"url": url, "width": width, "height": height, "bandwidth": bandwidth, "codecs": codecs}


def _motion_entry(dictionary, shape, deadline):
    if not isinstance(dictionary, dict):
        return None
    for key, value in dictionary.items():
        if shape not in key.lower() or not isinstance(value, dict):
            continue
        video = value.get("video")
        try:
            if not isinstance(video, str) or not urlsplit(video).path.endswith(".m3u8"):
                continue
            check_url(video)
        except (CatalogError, ValueError):
            continue
        entry = {"hls": video, "poster": _artwork(value.get("previewFrame"))}
        try:
            entry["file"] = motion(video, deadline)
        except CatalogError as exc:
            logging.info("Motion artwork variant unavailable: %s", exc.code)
            entry["file"] = None
        return entry
    return None


# --------------------------------------------------------------------------
# Albums
# --------------------------------------------------------------------------

def _section(sections, kind):
    return next((s for s in sections if isinstance(s, dict) and s.get("itemKind") == kind), None)


def _shelf_items(section, kind, limit=20):
    items = []
    for item in section.get("items") or []:
        if not isinstance(item, dict):
            continue
        for node in (item, item.get("squareLockup"), item.get("horizontalLockup")):
            if not isinstance(node, dict):
                continue
            item_id = _descriptor_id(node, kind)
            if not item_id:
                continue
            title = _text(node.get("title") or _get(node, "titleLinks", 0, "title"), 300)
            subtitle = _text(_get(node, "subtitleLinks", 0, "title") or node.get("subtitle"), 300)
            items.append({"kind": kind, "id": item_id, "title": title, "subtitle": subtitle,
                          "artwork": _artwork(node.get("artwork"))})
            break
        if len(items) >= limit:
            break
    return items


def _shelf_title(section):
    return _text(_get(section, "header", "item", "titleLink", "title"), 100)


def parse_album(page, album_id, deadline):
    sections = [s for s in page.get("sections") or [] if isinstance(s, dict)]
    header_section = _section(sections, "containerDetailHeaderLockup")
    header = _get(header_section, "items", 0)
    if not isinstance(header, dict) or not _text(header.get("title")):
        raise CatalogError("unavailable")
    tracks = []
    track_art = None
    for section in sections:
        if section.get("itemKind") != "trackLockup":
            continue
        track_art = track_art or _artwork(section.get("containerArtwork"))
        for item in section.get("items") or []:
            song_id = _descriptor_id(item, "song") if isinstance(item, dict) else None
            if not song_id:
                continue
            tracks.append({"id": song_id, "title": _text(item.get("title"), 300),
                           "artist": _text(item.get("artistName"), 300),
                           "artists": [a for a in _links(item.get("subtitleLinks"), "artist") if a["id"]],
                           "duration": _seconds(item.get("duration")),
                           "number": item.get("trackNumber") if isinstance(item.get("trackNumber"), int) else None,
                           "disc": item.get("discNumber") if isinstance(item.get("discNumber"), int) else None,
                           "explicit": bool(item.get("showExplicitBadge")),
                           "disabled": bool(item.get("isDisabled")),
                           "popular": bool(item.get("showPopularityIndicator"))})
    footer = _get(_section(sections, "containerDetailTracklistFooterLockup"), "items", 0) or {}
    lines = [line.strip() for line in _text(footer.get("description")).splitlines() if line.strip()]
    genre_year = _text(header.get("quaternaryTitle"), 200).replace(" ", " ")
    artists = _links(header.get("subtitleLinks"), "artist")
    more = []
    for section in sections:
        if section.get("itemKind") == "squareLockup":
            items = _shelf_items(section, "album")
            if items:
                more.append({"title": _shelf_title(section), "items": items})
    square = _motion_entry(_get(header, "videoArtwork", "dictionary"), "square", deadline)
    tall = _motion_entry(_get(header, "tallVideoArtwork", "dictionary"), "tall", deadline)
    artwork = track_art or _artwork(header.get("artwork"))
    return {
        "ok": True, "source": "web", "id": album_id,
        "title": _text(header.get("title"), 300),
        "artists": artists, "artist": " & ".join(a["name"] for a in artists),
        "genre_year": genre_year,
        "description": _text(_get(header, "modalPresentationDescriptor", "paragraphText"), 6000),
        "release": lines[0] if lines else "", "summary": lines[1] if len(lines) > 1 else "",
        "copyright": next((line for line in lines[2:] if line.startswith(("℗", "©"))), ""),
        "label": _text(_get(footer, "linkSection", "link", "title"), 200),
        "explicit": bool(header.get("showExplicitBadge")),
        "badges": {k: bool(v) for k, v in (header.get("audioBadges") or {}).items() if isinstance(v, bool)},
        "artwork": artwork, "motion": {"square": square, "tall": tall},
        "tracks": tracks, "more": more[:3],
    }


def _album_from_itunes(album_id, deadline):
    records = _itunes("lookup", {"id": album_id, "entity": "song", "limit": 200}, deadline)
    collection = next((r for r in records if r.get("wrapperType") == "collection"), None)
    if not collection:
        raise CatalogError("not_found")
    info = album_record(collection)
    tracks = []
    for record in records:
        song = song_record(record) if record.get("wrapperType") == "track" else None
        if song:
            tracks.append({"id": song["id"], "title": song["title"], "artist": song["artist"], "artists": [],
                           "duration": song["duration"], "number": song["number"], "disc": song["disc"],
                           "explicit": song["explicit"], "disabled": record.get("isStreamable") is False,
                           "popular": False})
    tracks.sort(key=lambda t: (t["disc"] or 0, t["number"] or 0))
    return {
        "ok": True, "source": "itunes", "id": album_id, "title": info["title"],
        "artists": [{"id": info["artist_id"], "name": info["artist"]}] if info["artist"] else [],
        "artist": info["artist"], "genre_year": " · ".join(v for v in (info["genre"], info["year"]) if v),
        "description": "", "release": _text(collection.get("releaseDate"), 10),
        "summary": f"{len(tracks)} 首歌曲" if tracks else "",
        "copyright": _text(collection.get("copyright"), 300), "label": "", "explicit": info["explicit"],
        "badges": {}, "artwork": info["artwork"], "motion": {"square": None, "tall": None},
        "tracks": tracks, "more": [],
    }


def album(album_id, *, deadline=None):
    album_id = _id(album_id)
    if not album_id:
        raise CatalogError("invalid")

    def load(deadline):
        try:
            return parse_album(_page("album", album_id, deadline), album_id, deadline)
        except CatalogError as exc:
            logging.info("Album page unavailable (%s); using iTunes lookup", exc.code)
            return _album_from_itunes(album_id, deadline)
    return cached(("album", album_id, STOREFRONT, LANGUAGE), ITEM_TTL, load, 15,
                  deadline=deadline, inline=True)


# --------------------------------------------------------------------------
# Artists
# --------------------------------------------------------------------------

def _top_song(item):
    song_id = _descriptor_id(item, "song")
    album_id = _segue_id(item, "album") or _descriptor_id(_get(item, "playAction", "containerContentDescriptor") or {}, "album")
    if not song_id:
        return None
    return {"id": song_id, "album_id": album_id, "title": _text(item.get("title"), 300),
            "subtitle": _text(_get(item, "subtitleLinks", 0, "title"), 300),
            "duration": _seconds(item.get("duration")), "explicit": bool(item.get("showExplicitBadge")),
            "artwork": _artwork(item.get("artwork"))}


def parse_artist(page, artist_id):
    sections = [s for s in page.get("sections") or [] if isinstance(s, dict)]
    header = _get(_section(sections, "artistDetailHeader"), "items", 0)
    if not isinstance(header, dict) or not _text(header.get("title")):
        raise CatalogError("unavailable")
    top, shelves, similar, featured = [], [], [], None
    for section in sections:
        kind = section.get("itemKind")
        if kind == "trackLockup" and not top:
            top = [t for t in map(_top_song, section.get("items") or []) if t and t["album_id"]]
        elif kind == "artistFeaturedContentAndTracks":
            block = _get(section, "items", 0) or {}
            if not top:
                top = [t for t in map(_top_song, block.get("tracks") or []) if t and t["album_id"]]
            content = block.get("featuredContent") or {}
            content_id = _descriptor_id(content, "album")
            if content_id:
                featured = {"kind": "album", "id": content_id, "heading": _text(content.get("heading"), 100),
                            "title": _text(_get(content, "detailText", "title"), 300),
                            "subtitle": _text(_get(content, "detailText", "headline"), 200),
                            "artwork": _artwork(content.get("artwork"))}
        elif kind in ("squareLockup", "adaptiveHorizontalSquareLockup"):
            items = _shelf_items(section, "album")
            if items:
                shelves.append({"title": _shelf_title(section), "items": items})
        elif kind == "bubbleLockup" and not similar:
            for item in section.get("items") or []:
                other = _descriptor_id(item, "artist") if isinstance(item, dict) else None
                if other:
                    similar.append({"id": other, "name": _text(item.get("title"), 300),
                                    "artwork": _artwork(item.get("artwork"), "cc")})
    artwork = _artwork(header.get("artwork"), "cc")
    wide = _artwork(header.get("wideArtwork"))
    return {
        "ok": True, "source": "web", "id": artist_id, "name": _text(header.get("title"), 300),
        "artwork": artwork, "wide": wide, "color": _color(header.get("keyColor")) or (artwork or {}).get("bg"),
        "bio": _text(header.get("bio"), 8000), "top": top[:20], "featured": featured,
        "shelves": shelves[:6], "similar": similar[:15],
    }


def _artist_from_itunes(artist_id, deadline):
    records = _itunes("lookup", {"id": artist_id, "entity": "album", "limit": 25}, deadline)
    info = next((artist_record(r) for r in records if r.get("wrapperType") == "artist"), None)
    if not info:
        raise CatalogError("not_found")
    albums = [a for a in (album_record(r) for r in records if r.get("wrapperType") == "collection") if a]
    items = [{"kind": "album", "id": a["id"], "title": a["title"], "subtitle": a["year"], "artwork": a["artwork"]}
             for a in albums]
    return {"ok": True, "source": "itunes", "id": artist_id, "name": info["name"], "artwork": None, "wide": None,
            "color": None, "bio": "", "top": [], "featured": None,
            "shelves": [{"title": "专辑", "items": items}] if items else [], "similar": [],
            "genre": info["genre"]}


def artist(artist_id):
    artist_id = _id(artist_id)
    if not artist_id:
        raise CatalogError("invalid")

    def load(deadline):
        try:
            return parse_artist(_page("artist", artist_id, deadline), artist_id)
        except CatalogError as exc:
            logging.info("Artist page unavailable (%s); using iTunes lookup", exc.code)
            return _artist_from_itunes(artist_id, deadline)
    return cached(("artist", artist_id, STOREFRONT, LANGUAGE), ITEM_TTL, load, 12)


# --------------------------------------------------------------------------
# What the Windows app shows (store metadata language), for UI Automation
# --------------------------------------------------------------------------

def app_metadata(album_id):
    """Album title and track titles/durations as the Windows app displays them."""
    album_id = _id(album_id)
    if not album_id:
        raise CatalogError("invalid")

    def load(deadline):
        records = _itunes("lookup", {"id": album_id, "entity": "song", "limit": 200}, deadline)
        collection = next((r for r in records if r.get("wrapperType") == "collection"), None)
        if not collection:
            raise CatalogError("not_found")
        tracks = {}
        for record in records:
            song = song_record(record) if record.get("wrapperType") == "track" else None
            if song and song["album_id"] == album_id:
                tracks[song["id"]] = {"title": song["title"], "artist": song["artist"], "duration": song["duration"],
                                      "number": song["number"], "disc": song["disc"]}
        return {"album_id": album_id, "title": _text(collection.get("collectionName"), 300),
                "artist": _text(collection.get("artistName"), 300), "tracks": tracks}
    return cached(("app", album_id, STOREFRONT), ITEM_TTL, load, 12)


def app_link(kind, item_id, song_id=None):
    """music:// link handled by the Windows app (opens the page, no autoplay)."""
    if (kind not in ("album", "artist") or not _id(item_id)
            or (song_id is not None and (kind != "album" or not _id(song_id)))):
        raise CatalogError("invalid")
    item_id, song_id = _id(item_id), _id(song_id) if song_id is not None else None
    link = f"music://music.apple.com/{quote(STOREFRONT)}/{kind}/{item_id}"
    return link + (f"?i={song_id}" if song_id else "")
