from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from email.utils import parsedate_to_datetime

import requests

from spotify_client import Track

LRCLIB_BASE = "https://lrclib.net/api"
HTTP_TIMEOUT = 8
HTTP_RETRIES = 2
RETRY_BACKOFF_S = 0.75
MAX_RETRY_AFTER_S = 5.0
DURATION_TOLERANCE_S = 5
NEGATIVE_CACHE_TTL = 24 * 3600

_TS_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE | re.MULTILINE)
_WORD_TAG_RE = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")
_TITLE_NOISE_RE = re.compile(
    r"\s*[\(\[][^)\]]*(feat\.?|ft\.?|with|remaster|live|version|edit|mono|stereo)[^)\]]*[\)\]]"
    r"|\s*-\s*(\d{4}\s*)?(remaster(ed)?|live|mono|stereo|single version|radio edit).*$",
    re.IGNORECASE,
)


@dataclass
class LyricLine:
    time_ms: int
    text: str


@dataclass
class LyricsResult:
    status: str                       # "synced" | "instrumental" | "not_found" | "error"
    lines: list[LyricLine] = field(default_factory=list)
    error: str = ""


def parse_lrc(lrc: str) -> list[LyricLine]:
    offset = 0
    m = _OFFSET_RE.search(lrc)
    if m:
        offset = int(m.group(1))
    lines: list[LyricLine] = []
    for raw in lrc.splitlines():
        stamps = list(_TS_RE.finditer(raw))
        if not stamps:
            continue
        text = _WORD_TAG_RE.sub("", _TS_RE.sub("", raw)).strip()
        for s in stamps:
            minutes, seconds, frac = int(s.group(1)), int(s.group(2)), s.group(3) or "0"
            ms = int((frac + "00")[:3])  # "5" -> 500, "50" -> 500, "500" -> 500
            t = (minutes * 60 + seconds) * 1000 + ms - offset
            lines.append(LyricLine(max(0, t), text or "♪"))
    lines.sort(key=lambda l: l.time_ms)
    return lines


def clean_title(title: str) -> str:
    cleaned = _TITLE_NOISE_RE.sub("", title).strip()
    return cleaned or title


class LyricsProvider:
    def __init__(self, cache_dir: Path, user_agent: str):
        self.cache_dir = cache_dir / "lyrics_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, LyricsResult] = {}
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})

    # cache
    def _cache_file(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
        return self.cache_dir / f"{safe}.json"

    def _cache_get(self, key: str) -> LyricsResult | None:
        with self._lock:
            if key in self._mem:
                return self._mem[key]
        try:
            data = json.loads(self._cache_file(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        status = data.get("status")
        if status == "not_found" and time.time() - data.get("ts", 0) > NEGATIVE_CACHE_TTL:
            return None
        if status not in ("synced", "instrumental", "not_found"):
            return None
        result = LyricsResult(status, parse_lrc(data.get("lrc", "")) if status == "synced" else [])
        with self._lock:
            self._mem[key] = result
        return result

    def _cache_put(self, key: str, result: LyricsResult, lrc: str = "") -> None:
        with self._lock:
            self._mem[key] = result
        try:
            self._cache_file(key).write_text(
                json.dumps({"status": result.status, "lrc": lrc, "ts": time.time()}),
                encoding="utf-8",
            )
        except OSError:
            pass

    # fetch
    def get_lyrics(self, track: Track) -> LyricsResult:
        """Blocking. Panggil dari thread worker."""
        key = track.key
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            record = self._find_record(track)
        except requests.RequestException as exc:
            return LyricsResult("error", error=str(exc))

        if record is None:
            result = LyricsResult("not_found")
            self._cache_put(key, result)
            return result
        if record.get("instrumental"):
            result = LyricsResult("instrumental")
            self._cache_put(key, result)
            return result

        lrc = record.get("syncedLyrics") or ""
        lines = parse_lrc(lrc)
        if not lines:
            result = LyricsResult("not_found")
            self._cache_put(key, result)
            return result
        result = LyricsResult("synced", lines)
        self._cache_put(key, result, lrc)
        return result

    def _request(self, path: str, params: dict):
        url = LRCLIB_BASE + path
        for attempt in range(HTTP_RETRIES + 1):
            try:
                response = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
            except requests.RequestException:
                if attempt >= HTTP_RETRIES:
                    raise
                time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
                continue

            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= HTTP_RETRIES:
                    raise requests.RequestException(f"LRCLIB {response.status_code}")
                time.sleep(self._retry_delay(response, attempt))
                continue
            if not response.ok:
                return None
            try:
                return response.json()
            except requests.RequestException:
                if attempt >= HTTP_RETRIES:
                    raise
                time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
        raise requests.RequestException("LRCLIB request retries exhausted")

    @staticmethod
    def _retry_delay(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(MAX_RETRY_AFTER_S, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after).timestamp()
                    return min(MAX_RETRY_AFTER_S, max(0.0, retry_at - time.time()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return RETRY_BACKOFF_S * (2 ** attempt)

    def _find_record(self, track: Track) -> dict | None:
        duration_s = round(track.duration_ms / 1000) if track.duration_ms else None
        title_clean = clean_title(track.title)

        # 1) pencocokan tepat (judul + artis + album + durasi)
        params = {"track_name": track.title, "artist_name": track.artist}
        if track.album:
            params["album_name"] = track.album
        if duration_s:
            params["duration"] = duration_s
        rec = self._request("/get", params)
        if rec and (rec.get("syncedLyrics") or rec.get("instrumental")):
            return rec

        # 2) pencarian longgar
        first_artist = re.split(r",| & | feat\.? | x ", track.artist, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        for artist in dict.fromkeys([track.artist, first_artist]):
            for title in dict.fromkeys([track.title, title_clean]):
                results = self._request("/search", {"track_name": title, "artist_name": artist})
                best = self._pick_best(results or [], duration_s)
                if best:
                    return best
        return None

    @staticmethod
    def _pick_best(results: list[dict], duration_s: int | None) -> dict | None:
        candidates = [r for r in results if r.get("syncedLyrics") or r.get("instrumental")]
        if not candidates:
            return None
        if duration_s is None:
            return candidates[0]
        scored = []
        for r in candidates:
            d = r.get("duration")
            diff = abs(float(d) - duration_s) if d is not None else 999
            scored.append((diff, r))
        scored.sort(key=lambda x: x[0])
        return scored[0][1] if scored[0][0] <= DURATION_TOLERANCE_S else None
