from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from spotify_client import PlaybackState, SpotifyError, Track

AD_TITLES = {"advertisement", "spotify"}


@dataclass
class RawSession:
    app_id: str
    status: str 
    title: str
    artist: str
    album: str
    start_ms: int
    end_ms: int
    position_ms: int
    updated: datetime | None
    artwork_id: str | None = None
    artwork_data: bytes | None = None

def _import_winrt():
    """Coba paket `winrt-*` (aktif dikembangkan), lalu `winsdk` (lama)."""
    try:
        from winrt.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
        return Manager, Status
    except ImportError:
        pass
    try:
        from winsdk.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as Manager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
        )
        return Manager, Status
    except ImportError as exc:
        raise RuntimeError(
            "Paket WinRT belum terpasang. Jalankan: pip install -r requirements.txt (hanya Windows)."
        ) from exc


def _ms(delta) -> int:
    try:
        return int(delta.total_seconds() * 1000)
    except AttributeError:
        return 0


class SmtcClient:
    def __init__(self, app_filter: str = "spotify"):
        if sys.platform != "win32":
            raise RuntimeError("Sumber SMTC hanya tersedia di Windows.")
        self._Manager, self._Status = _import_winrt()
        self.app_filter = app_filter.strip().lower()
        self._fb: dict | None = None
        self._lock = threading.Lock()
        self._artwork_identity: tuple[str, str, str, str] | None = None
        self._artwork_id: str | None = None
        self._artwork_data: bytes | None = None

    def has_token(self) -> bool:
        return True

    def logout(self) -> None:
        pass

    def get_playback(self) -> PlaybackState:
        with self._lock:
            try:
                raw = asyncio.run(self._read())
            except Exception as exc:  # noqa: BLE001
                raise SpotifyError(f"SMTC: {exc}") from exc
            return self.to_state(raw, datetime.now(timezone.utc), time.monotonic())

    # --- pembacaan WinRT
    async def _read(self) -> RawSession | None:
        mgr = await self._Manager.request_async()
        sessions = list(mgr.get_sessions())
        chosen = None
        if self.app_filter:
            matches = [s for s in sessions if self.app_filter in (s.source_app_user_model_id or "").lower()]
            for s in matches:                                   # utamakan yang sedang playing
                if s.get_playback_info().playback_status == self._Status.PLAYING:
                    chosen = s
                    break
            chosen = chosen or (matches[0] if matches else None)
        else:
            chosen = mgr.get_current_session()
        if chosen is None:
            self._artwork_identity = None
            self._artwork_id = None
            self._artwork_data = None
            return None

        props = await chosen.try_get_media_properties_async()
        app_id = chosen.source_app_user_model_id or ""
        title = (props.title or "").strip()
        artist = (props.artist or "").strip()
        album = (props.album_title or "").strip()
        artwork_identity = (app_id, title, artist, album)
        if artwork_identity != self._artwork_identity:
            self._artwork_identity = artwork_identity
            self._artwork_id = None
            self._artwork_data = await self._read_thumbnail(getattr(props, "thumbnail", None))

            if self._artwork_data:
                self._artwork_id = hashlib.sha256(self._artwork_data).hexdigest()

        ps = chosen.get_playback_info().playback_status
        status = "playing" if ps == self._Status.PLAYING else \
                 "paused" if ps == self._Status.PAUSED else "stopped"
        tl = chosen.get_timeline_properties()
        updated = tl.last_updated_time
        valid = isinstance(updated, datetime) and updated.year > 2000
        if valid and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return RawSession(
            app_id=app_id,
            status=status,
            title=title,
            artist=artist,
            album=album,
            start_ms=_ms(tl.start_time),
            end_ms=_ms(tl.end_time),
            position_ms=_ms(tl.position),
            updated=updated if valid else None,
            artwork_id=self._artwork_id,
            artwork_data=self._artwork_data,
        )

    @staticmethod
    async def _read_thumbnail(thumbnail) -> bytes | None:
        if thumbnail is None:
            return None
        stream = None
        reader = None
        try:
            stream = await thumbnail.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 10 * 1024 * 1024:
                return None
            try:
                from winrt.windows.storage.streams import DataReader
            except ImportError:
                from winsdk.windows.storage.streams import DataReader  # type: ignore
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(size)
            data = bytearray(size)
            reader.read_bytes(data)
            return bytes(data)
        except Exception:
            return None
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    # --- konversi (murni, dapat diuji tanpa Windows)
    def to_state(self, raw: RawSession | None, now_utc: datetime, mono: float) -> PlaybackState:
        if raw is None or raw.status == "stopped" or not raw.title:
            self._fb = None
            return PlaybackState(status="idle", sampled_at=mono)
        if raw.title.lower() in AD_TITLES:
            self._fb = None
            return PlaybackState(status="unsupported", sampled_at=mono)

        playing = raw.status == "playing"
        duration = max(0, raw.end_ms - raw.start_ms)
        if duration > 0 and raw.updated is not None:
            pos = raw.position_ms
            if playing:
                pos += max(0.0, (now_utc - raw.updated).total_seconds() * 1000.0)
            pos = int(max(0, min(pos, duration)))
            self._fb = None
        else:
            pos = self._stopwatch(f"{raw.artist}|{raw.title}", playing, mono)

        track = Track(
            id=None, title=raw.title, artist=raw.artist, artists_all=raw.artist,
            album=raw.album, duration_ms=duration, is_local=False,
            artwork_id=raw.artwork_id, artwork_data=raw.artwork_data,
        )
        return PlaybackState(status="playing" if playing else "paused",
                             track=track, progress_ms=pos, sampled_at=mono)

    def _stopwatch(self, key: str, playing: bool, mono: float) -> int:
        fb = self._fb
        if fb is None or fb["key"] != key:
            fb = self._fb = {"key": key, "base": 0.0, "t": mono, "playing": playing}
        else:
            if fb["playing"]:
                fb["base"] += (mono - fb["t"]) * 1000.0
            fb["t"], fb["playing"] = mono, playing
        return int(fb["base"])
