from __future__ import annotations

import sys
import time

from PySide6.QtCore import QLockFile, QObject, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from floating_window import FloatingWindow
from lyrics_widget import LyricPalette, default_lyric_palette
from lyrics_provider import LyricsProvider, LyricsResult
from lyrics_sync import LYRIC_LEAD_MS, LyricsSync, PlaybackClock
from settings import APP_NAME, AppConfig, Settings, app_dir
from spotify_client import (PlaybackState, SpotifyAuthError, SpotifyClient,
                            SpotifyNetworkError, SpotifyRateLimit, Track)
from system_tray import SystemTray
from workers import AuthTask, LyricsTask, PaletteTask, PollTask, WorkerSignals

TICK_MS = 33                 # smooth card movement and playback-progress updates
OFFLINE_GRACE_S = 20.0       # lirik tetap berjalan (estimasi) selama koneksi putus sesaat
IDLE_POLL_MS = 4000
MAX_LYRIC_RETRY_DELAY_MS = 60_000


class Controller(QObject):
    def __init__(self, app: QApplication, config: AppConfig):
        super().__init__()
        self.app, self.config = app, config
        self.settings = Settings.load()
        self.pool = QThreadPool.globalInstance()
        self.signals = WorkerSignals()

        data_dir = app_dir()
        self._init_error = ""
        try:
            if config.source == "smtc":
                from smtc_client import SmtcClient
                self.client = SmtcClient(config.smtc_app_filter)
            else:
                self.client = SpotifyClient(config.client_id, config.redirect_uri, data_dir / "token.json")
        except Exception as exc:  # noqa: BLE001 - mis. winrt belum terpasang / bukan Windows
            self.client = None
            self._init_error = str(exc)
        self.provider = LyricsProvider(data_dir, config.user_agent)
        self.clock = PlaybackClock()
        self.sync = LyricsSync()

        self.window = FloatingWindow(self.settings)
        self.tray = SystemTray(self.settings.bg_opacity, self.settings.screen_name, self.settings.click_through,
                               self.settings.max_active_cards)
        self.tray.set_reconnect_visible(config.source == "spotify")
        self._idle_ms = IDLE_POLL_MS if config.source == "spotify" else 2000

        # state
        self._track_key: str | None = None
        self._track: Track | None = None
        self._artwork_source: tuple[str, str | None] | None = None
        self._active_artwork_id: str | None = None
        self._palette_cache: dict[str, LyricPalette] = {}
        self._palette_pending: set[str] = set()
        self._generation = 0
        self._poll_inflight = False
        self._fail_count = 0
        self._last_ok = time.monotonic()
        self._offline_shown = False
        self._auth_blocked = False
        self._shown_idx = -2
        self._lyric_attempts = 0
        self._early_poll_done = False
        self._last_message = ""
        self._notified: set[str] = set()

        # timers
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.timeout.connect(self._poll)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(TICK_MS)
        self._tick_timer.timeout.connect(self._tick)

        # wiring
        self.signals.playback.connect(self._on_playback)
        self.signals.playback_error.connect(self._on_playback_error)
        self.signals.lyrics.connect(self._on_lyrics)
        self.signals.palette.connect(self._on_palette)
        self.signals.auth_done.connect(self._on_auth_done)

        self.window.visibilityChanged.connect(self.tray.set_overlay_visible)
        self.window.visibilityChanged.connect(self._on_visibility_changed)
        self.window.compactChanged.connect(self._on_compact_changed)
        self.window.screenChanged.connect(self._update_screens)
        self.tray.toggleRequested.connect(self.window.toggle_overlay)
        self.tray.showRequested.connect(self.window.show_overlay)
        self.tray.hideRequested.connect(self.window.hide_overlay)
        self.tray.restoreRequested.connect(self._restore_from_tray)
        self.tray.screenSelected.connect(self._on_screen_selected)
        self.tray.clickThroughChanged.connect(self.window.set_click_through)
        self.tray.maxActiveCardsChanged.connect(self._on_max_active_cards)
        self.tray.opacityChanged.connect(self._on_opacity)
        self.tray.fontSizeDelta.connect(self._on_font_delta)
        self.tray.offsetDelta.connect(self._on_offset_delta)
        self.tray.reconnectRequested.connect(self._reconnect)
        self.tray.quitRequested.connect(self.quit)

        self.window.lyrics.set_font(self.settings.font_family, self.settings.font_size)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.window.show_overlay()
        if self._init_error:
            self._set_message("Sumber playback tidak tersedia. Lihat README")
            self._notify_once("init", self._init_error[:200])
            return
        if self.config.source == "smtc":
            self._set_message("♪")
            self._poll()
            return
        if not self.config.client_id:
            self._set_message("Isi SPOTIFY_CLIENT_ID di file .env")
            self._notify_once("no_client_id", "SPOTIFY_CLIENT_ID belum diisi. Lihat README.md.")
            return
        if self.client.has_token():
            self._set_message("♪")
            self._poll()
        else:
            self._start_auth()

    def quit(self) -> None:
        self.tray.icon.hide()
        self.app.quit()

    def _notify_once(self, key: str, message: str) -> None:
        if key not in self._notified:
            self._notified.add(key)
            self.tray.notify(message)

    def _set_message(self, text: str) -> None:
        self._last_message = text
        self.sync.clear()
        self._shown_idx = -2
        self.window.lyrics.set_message(text)
        self._ensure_tick_timer()

    def _on_visibility_changed(self, visible: bool) -> None:
        if visible:
            self._tick()
        self._ensure_tick_timer()

    def _ensure_tick_timer(self) -> None:
        should_run = self.window.isVisible() and self.clock.playing \
            and not self.window.is_compact and not self._offline_shown
        self.window.lyrics.set_playing(should_run)
        if should_run and not self._tick_timer.isActive():
            self._tick_timer.start()
        elif not should_run and self._tick_timer.isActive():
            self._tick_timer.stop()

    def _on_compact_changed(self, compact: bool) -> None:
        self.tray.set_compact(compact)
        if not compact:
            self._tick()
        self._ensure_tick_timer()

    def _update_screens(self, selected_name: str) -> None:
        from PySide6.QtGui import QGuiApplication
        self.tray.update_screens(QGuiApplication.screens(), selected_name)

    def _on_screen_selected(self, name: str) -> None:
        self.window.set_target_screen(name)
        self.settings.save()

    # ---------------------------------------------------------------- auth
    def _start_auth(self) -> None:
        self._auth_blocked = True
        self._set_message("Menunggu izin Spotify di browser…")
        self.pool.start(AuthTask(self.client, self.signals))

    def _on_auth_done(self, ok: bool, message: str) -> None:
        if ok:
            self._auth_blocked = False
            self._fail_count = 0
            self._set_message("♪")
            self._poll()
        else:
            self._set_message("Spotify belum terhubung")
            self._notify_once("auth_fail", f"Gagal menghubungkan Spotify: {message[:120]}. "
                                           "Gunakan menu tray > Hubungkan ulang Spotify.")

    def _reconnect(self) -> None:
        self.client.logout()
        self._track_key = None
        self._notified.discard("auth_fail")
        self._start_auth()

    # ------------------------------------------------------------- polling
    def _schedule(self, ms: float) -> None:
        self._poll_timer.start(int(ms))

    def _poll(self) -> None:
        if self._poll_inflight or self._auth_blocked or not self.client.has_token():
            return
        self._poll_inflight = True
        self.pool.start(PollTask(self.client, self.signals))

    def _poll_ms(self, playing: bool) -> float:
        base = self.config.poll_interval * 1000
        return base if playing else base * 1.7

    def _on_playback(self, state: PlaybackState) -> None:
        self._poll_inflight = False
        self._fail_count = 0
        self._last_ok = time.monotonic()
        if self._offline_shown:
            self._offline_shown = False
            self._restore_view()

        if state.status in ("idle", "unsupported") or state.track is None:
            self._enter_no_track()
            self._schedule(self._idle_ms)
            return

        track = state.track
        playing = state.status == "playing"
        self._update_artwork_theme(track)
        new_track = track.key != self._track_key
        if new_track:
            self._begin_track(track)
        self.clock.set_duration(track.duration_ms)
        seeked = self.clock.update(state.progress_ms, state.sampled_at, playing, force=new_track)
        self._schedule(self._poll_ms(playing))
        self._tick(reconcile=seeked)
        self._ensure_tick_timer()

    def _on_playback_error(self, exc: Exception) -> None:
        self._poll_inflight = False
        self._fail_count += 1
        if isinstance(exc, SpotifyAuthError):
            self._auth_blocked = True
            self._set_message("Akses Spotify ditolak. Tray > Hubungkan ulang Spotify")
            self._notify_once("auth_lost", "Sesi Spotify tidak valid. Gunakan menu tray > Hubungkan ulang Spotify.")
            return
        if isinstance(exc, SpotifyRateLimit):
            delay = exc.retry_after + 1
        elif isinstance(exc, SpotifyNetworkError):
            delay = min(30.0, 2.0 * (2 ** min(self._fail_count, 4)))
            if time.monotonic() - self._last_ok > OFFLINE_GRACE_S and not self._offline_shown:
                self._offline_shown = True
                self.clock.pause()
                self.window.lyrics.set_message("Koneksi terputus")
                self._ensure_tick_timer()
        else:
            delay = 10.0
        self._schedule(delay * 1000)

    # ------------------------------------------------------------ track flow
    def _enter_no_track(self) -> None:
        if self._track_key is not None:
            self._track_key = None
            self._track = None
            self._artwork_source = None
            self._active_artwork_id = None
            self.window.lyrics.set_theme_palette(default_lyric_palette())
            self._generation += 1
            self.clock.reset()
            self.window.lyrics.set_theme_color(None)
        self._set_message("♪")

    def _begin_track(self, track: Track) -> None:
        self._track_key = track.key
        self._track = track
        self._generation += 1
        self._lyric_attempts = 0
        self._early_poll_done = False
        self.sync.clear()
        self._shown_idx = -2
        self._last_message = ""
        self.window.lyrics.clear()
        self._ensure_tick_timer()
        self._request_lyrics()

    def _update_artwork_theme(self, track: Track) -> None:
        source = (track.key, track.artwork_id)
        if source == self._artwork_source:
            return
        self._artwork_source = source

        if not track.artwork_id or not track.artwork_data:
            self._active_artwork_id = None
            self.window.lyrics.set_theme_palette(default_lyric_palette())
            return

        self._active_artwork_id = track.artwork_id
        cached = self._palette_cache.get(track.artwork_id)
        if cached is not None:
            self.window.lyrics.set_theme_palette(cached)
        elif track.artwork_id not in self._palette_pending:
            self._palette_pending.add(track.artwork_id)
            self.pool.start(PaletteTask(track.artwork_id, track.artwork_data, self.signals))

    def _on_palette(self, artwork_id: str, palette: LyricPalette | None) -> None:
        self._palette_pending.discard(artwork_id)
        if palette is None:
            palette = default_lyric_palette()
        self._palette_cache[artwork_id] = palette
        if len(self._palette_cache) > 64:
            self._palette_cache.pop(next(iter(self._palette_cache)))
        if artwork_id == self._active_artwork_id:
            self.window.lyrics.set_theme_palette(palette)

    def _request_lyrics(self) -> None:
        if self._track is not None:
            self.pool.start(LyricsTask(self.provider, self._track, self._generation, self.signals))

    def _on_lyrics(self, generation: int, result: LyricsResult) -> None:
        if generation != self._generation:
            return                                   # hasil basi (lagu sudah berganti)
        if result.status == "synced":
            self.sync.set_lines(result.lines)
            self._ensure_tick_timer()
            self._show_lines()
            self._tick()
            self._ensure_tick_timer()
        elif result.status == "instrumental":
            self._set_message("♪ Instrumental")
        elif result.status == "error":
            self._lyric_attempts += 1
            retry_delay = min(
                MAX_LYRIC_RETRY_DELAY_MS,
                3000 * (2 ** min(self._lyric_attempts - 1, 5)),
            )
            QTimer.singleShot(retry_delay, lambda g=generation: self._retry_lyrics(g))
            detail = result.error.strip() or "Kesalahan tidak diketahui"
            track_key = self._track_key or "unknown"
            self._notify_once(
                f"lyrics_error:{track_key}",
                f"Gagal memuat lirik; akan mencoba lagi: {detail[:120]}",
            )
        else:
            self._set_message("Lirik tidak ditemukan")

    def _retry_lyrics(self, generation: int) -> None:
        if generation == self._generation:
            self._request_lyrics()

    def _current_index(self) -> int:
        return self.sync.index_at(self.clock.position_ms(), self.settings.lyrics_offset_ms)

    def _show_lines(self) -> None:
        idx = self._current_index()
        self._shown_idx = idx
        position = self.clock.position_ms() + self.settings.lyrics_offset_ms + LYRIC_LEAD_MS
        progress_position = self.clock.position_ms() + self.settings.lyrics_offset_ms
        lines = [(line.time_ms, line.text) for line in self.sync.lines]
        duration = self._track.duration_ms if self._track else 0
        track_id = self._track.key if self._track else ""
        self.window.lyrics.set_lines(
            lines, idx, position, duration, progress_position, track_id,
            playback_position_ms=self.clock.position_ms())

    def _restore_view(self) -> None:
        if self.sync.has_lines():
            self._show_lines()
        elif self._last_message:
            self.window.lyrics.set_message(self._last_message)
        else:
            self.window.lyrics.clear()

    # ------------------------------------------------------------------ tick
    def _tick(self, reconcile: bool = False) -> None:
        track = self._track
        self.window.lyrics.set_playback_progress(
            self.clock.position_ms(), track.duration_ms if track is not None else 0)
        if track and track.duration_ms and self.clock.playing and not self._early_poll_done \
                and self.clock.position_ms() >= track.duration_ms - 500:
            self._early_poll_done = True             # deteksi pergantian lagu lebih cepat
            self._schedule(600)

        if not self.sync.has_lines() or self._offline_shown:
            self._ensure_tick_timer()
            return
        idx = self._current_index()
        self._shown_idx = idx
        self._update_progress(seeked=reconcile)
        self._ensure_tick_timer()

    def _update_progress(self, seeked: bool = False) -> None:
        pos = self.clock.position_ms() + self.settings.lyrics_offset_ms + LYRIC_LEAD_MS
        dur = self._track.duration_ms if self._track else 0
        self.window.lyrics.update_playback_position(
            pos, self._shown_idx, dur, self.clock.playing and self.window.isVisible()
            and not self.window.is_compact, seeked,
            playback_position_ms=self.clock.position_ms())

    def _on_max_active_cards(self, count: int) -> None:
        self.settings.max_active_cards = count
        self.settings.sanitize()
        self.window.lyrics.set_max_active_cards(self.settings.max_active_cards)
        self.settings.save()

    # -------------------------------------------------------------- settings
    def _on_opacity(self, value: int) -> None:
        self.settings.bg_opacity = value
        self.window.set_bg_opacity(value)
        self.settings.save()

    def _on_font_delta(self, delta: int) -> None:
        self.settings.font_size += delta
        self.settings.sanitize()
        self.window.lyrics.set_font(self.settings.font_family, self.settings.font_size)
        self.settings.save()

    def _on_offset_delta(self, delta: int) -> None:
        self.settings.lyrics_offset_ms = 0 if delta == 0 else self.settings.lyrics_offset_ms + delta
        self.settings.sanitize()
        self._tick()
        self.settings.save()

    def _restore_from_tray(self) -> None:
        if not self.window.isVisible():
            self.window.show_overlay()
        self.window.restore_normal()

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)             # tetap berjalan saat overlay di-Hide

    lock = QLockFile(str(app_dir() / "app.lock"))    # cegah dua instance sekaligus
    if not lock.tryLock(200):
        return 0

    controller = Controller(app, AppConfig.from_env())
    controller.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
