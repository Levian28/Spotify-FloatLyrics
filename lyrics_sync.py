from __future__ import annotations

import time
from bisect import bisect_right

from lyrics_provider import LyricLine

SEEK_THRESHOLD_MS = 1500
SOFT_CORRECTION = 0.35 
LYRIC_LEAD_MS = 0 


class PlaybackClock:
    def __init__(self) -> None:
        self._base_pos = 0.0
        self._base_time = time.monotonic()
        self._playing = False
        self._duration = 0

    def reset(self) -> None:
        self._base_pos = 0.0
        self._base_time = time.monotonic()
        self._playing = False

    def set_duration(self, duration_ms: int) -> None:
        self._duration = duration_ms

    @property
    def playing(self) -> bool:
        return self._playing

    def position_ms(self) -> float:
        pos = self._base_pos
        if self._playing:
            pos += (time.monotonic() - self._base_time) * 1000.0
        if self._duration:
            pos = min(pos, float(self._duration))
        return max(0.0, pos)

    def pause(self) -> None:
        self._base_pos = self.position_ms()
        self._base_time = time.monotonic()
        self._playing = False

    def update(self, progress_ms: int, sampled_at: float, playing: bool, force: bool = False) -> bool:
        """Terapkan sampel baru dari Spotify. Return True jika terdeteksi seek / lompatan."""
        now = time.monotonic()
        measured = progress_ms + ((now - sampled_at) * 1000.0 if playing else 0.0)
        current = self.position_ms()
        position_jump = abs(measured - current) > SEEK_THRESHOLD_MS
        jumped = force or position_jump or playing != self._playing

        if jumped:
            self._base_pos = measured
        else:
            self._base_pos = current + (measured - current) * SOFT_CORRECTION
        self._base_time = now
        self._playing = playing
        return position_jump and not force


class LyricsSync:
    def __init__(self) -> None:
        self.lines: list[LyricLine] = []
        self._times: list[int] = []

    def set_lines(self, lines: list[LyricLine]) -> None:
        self.lines = lines
        self._times = [l.time_ms for l in lines]

    def clear(self) -> None:
        self.set_lines([])

    def has_lines(self) -> bool:
        return bool(self.lines)

    def texts(self) -> list[str]:
        return [l.text for l in self.lines]

    def index_at(self, position_ms: float, offset_ms: int = 0) -> int:
        """Indeks baris aktif; -1 jika lagu belum mencapai baris pertama."""
        return bisect_right(self._times, position_ms + offset_ms + LYRIC_LEAD_MS) - 1
