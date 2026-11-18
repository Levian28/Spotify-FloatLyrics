from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from lyrics_provider import LyricsProvider, LyricsResult
from lyrics_widget import extract_lyric_palette
from spotify_client import PlaybackState, SpotifyClient, Track


class WorkerSignals(QObject):
    playback = Signal(object)
    playback_error = Signal(object)
    lyrics = Signal(int, object)
    palette = Signal(str, object)
    auth_done = Signal(bool, str)


class AuthTask(QRunnable):
    def __init__(self, client: SpotifyClient, signals: WorkerSignals):
        super().__init__()
        self.client = client
        self.signals = signals

    @Slot()
    def run(self) -> None:
        try:
            self.client.authorize()
        except Exception as exc:
            self.signals.auth_done.emit(False, str(exc))
        else:
            self.signals.auth_done.emit(True, "")


class PollTask(QRunnable):
    def __init__(self, client: SpotifyClient, signals: WorkerSignals):
        super().__init__()
        self.client = client
        self.signals = signals

    @Slot()
    def run(self) -> None:
        try:
            state: PlaybackState = self.client.get_playback()
        except Exception as exc:
            self.signals.playback_error.emit(exc)
        else:
            self.signals.playback.emit(state)


class LyricsTask(QRunnable):
    def __init__(
        self,
        provider: LyricsProvider,
        track: Track,
        generation: int,
        signals: WorkerSignals,
    ):
        super().__init__()
        self.provider = provider
        self.track = track
        self.generation = generation
        self.signals = signals

    @Slot()
    def run(self) -> None:
        try:
            result = self.provider.get_lyrics(self.track)

        except Exception as exc:
            result = LyricsResult("error", error=f"{type(exc).__name__}: {exc}")
        self.signals.lyrics.emit(self.generation, result)


class PaletteTask(QRunnable):
    def __init__(self, artwork_id: str, artwork_data: bytes, signals: WorkerSignals):
        super().__init__()
        self.artwork_id = artwork_id
        self.artwork_data = artwork_data
        self.signals = signals

    @Slot()
    def run(self) -> None:
        try:
            palette = extract_lyric_palette(self.artwork_data)
        except Exception:
            palette = None
        self.signals.palette.emit(self.artwork_id, palette)
