from __future__ import annotations

import sys

from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from lyrics_widget import LyricsWidget
from settings import Settings

COMPACT_SIZE = 46

class FloatingWindow(QWidget):
    visibilityChanged = Signal(bool)
    compactChanged = Signal(bool)
    screenChanged = Signal(str)

    def __init__(self, settings: Settings):
        super().__init__(None)
        self.settings = settings
        self._compact = False
        self._follow_primary = not bool(settings.screen_name)
        self._restore_geometry = QRect()
        self._target_screen = self._resolve_screen(settings.screen_name)
        self._bound_screen = None
        self._click_through = settings.click_through

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool        
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(1, 1)
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, self._click_through)

        self.lyrics = LyricsWidget(settings.font_family, settings.font_size, self)
        self.lyrics.set_max_active_cards(settings.max_active_cards)
        self.lyrics.set_background_opacity(settings.bg_opacity)

        # Pastikan tetap paling atas (Windows kadang menurunkan z-order jendela Tool)
        self._top_timer = QTimer(self)
        self._top_timer.setInterval(2500)
        self._top_timer.timeout.connect(self._reassert_topmost)

        self._apply_initial_geometry()
        self._layout_children()
        application = QGuiApplication.instance()
        if application is not None:
            application.screenAdded.connect(self._on_screen_added)
            application.screenRemoved.connect(self._on_screen_removed)
            application.primaryScreenChanged.connect(self._on_primary_screen_changed)
        self._bind_screen_geometry()

    # geometry
    @staticmethod
    def _resolve_screen(name: str):
        screens = QGuiApplication.screens()
        return next((screen for screen in screens if name and screen.name() == name),
                    QGuiApplication.primaryScreen() or (screens[0] if screens else None))

    def _apply_initial_geometry(self) -> None:
        if self._target_screen is None:
            self.setGeometry(0, 0, 800, 600)
            return
        self.setGeometry(self._target_screen.availableGeometry())

    def _bind_screen_geometry(self) -> None:
        if self._bound_screen is self._target_screen:
            return
        if self._bound_screen is not None:
            try:
                self._bound_screen.geometryChanged.disconnect(self._on_screen_geometry_changed)
            except (RuntimeError, TypeError):
                pass
            try:
                self._bound_screen.availableGeometryChanged.disconnect(self._on_screen_geometry_changed)
            except (RuntimeError, TypeError):
                pass
            try:
                self._bound_screen.logicalDotsPerInchChanged.disconnect(self._on_screen_dpi_changed)
            except (RuntimeError, TypeError):
                pass
        self._bound_screen = self._target_screen
        if self._bound_screen is not None:
            self._bound_screen.geometryChanged.connect(self._on_screen_geometry_changed)
            self._bound_screen.availableGeometryChanged.connect(self._on_screen_geometry_changed)
            self._bound_screen.logicalDotsPerInchChanged.connect(self._on_screen_dpi_changed)

    def set_target_screen(self, name: str) -> None:
        screen = self._resolve_screen(name)
        if screen is None:
            return
        self._target_screen = screen
        self._follow_primary = False
        self.settings.screen_name = screen.name()
        self._bind_screen_geometry()
        self._sync_screen_geometry()

    def _sync_screen_geometry(self) -> None:
        screen = self._target_screen
        if screen is None:
            return
        geometry = QRect(screen.availableGeometry())
        if not self._compact:
            self.setGeometry(geometry)
        self.screenChanged.emit(screen.name())

    def _on_screen_geometry_changed(self, _geometry) -> None:
        self._sync_screen_geometry()

    def _on_screen_dpi_changed(self, _dpi) -> None:
        self.lyrics.set_font(self.settings.font_family, self.settings.font_size)

    def _on_screen_added(self, _screen) -> None:
        self.screenChanged.emit(self.settings.screen_name)

    def _on_screen_removed(self, screen) -> None:
        if screen is self._target_screen:
            self._target_screen = QGuiApplication.primaryScreen()
            if not self._follow_primary:
                self.settings.screen_name = self._target_screen.name() if self._target_screen else ""
            self._bind_screen_geometry()
            self._sync_screen_geometry()
        else:
            self.screenChanged.emit(self.settings.screen_name)

    def _on_primary_screen_changed(self, screen) -> None:
        if self._follow_primary and screen is not None:
            self._target_screen = screen
            self._bind_screen_geometry()
            self._sync_screen_geometry()

    def _layout_children(self) -> None:
        w, h = self.width(), self.height()
        if self._compact:
            self.lyrics.hide()
            return
        self.lyrics.show()
        self.lyrics.setGeometry(0, 0, w, h)

    def resizeEvent(self, e) -> None:  # noqa: N802
        self._layout_children()
        super().resizeEvent(e)

    # -------------------------------------------------------- public actions
    @property
    def is_compact(self) -> bool:
        return self._compact

    def set_bg_opacity(self, percent: int) -> None:
        self.settings.bg_opacity = percent
        self.lyrics.set_background_opacity(percent)
        self.update()

    def show_overlay(self) -> None:
        self.show()
        self.raise_()
        self._top_timer.start()
        self._reassert_topmost()
        self.visibilityChanged.emit(True)

    def hide_overlay(self) -> None:
        self._top_timer.stop()
        self.hide()
        self.visibilityChanged.emit(False)

    def toggle_overlay(self) -> None:
        if self.isVisible():
            self.hide_overlay()
        else:
            self.show_overlay()

    def minimize_compact(self) -> None:
        if self._compact:
            return
        normal_geometry = (QRect(self._target_screen.availableGeometry())
                           if self._target_screen else QRect(self.geometry()))
        self._restore_geometry = QRect(normal_geometry)
        self._compact = True
        self.setMinimumSize(COMPACT_SIZE, COMPACT_SIZE)
        top_right = normal_geometry.topRight()
        self.setGeometry(top_right.x() - COMPACT_SIZE + 1, top_right.y(), COMPACT_SIZE, COMPACT_SIZE)
        self._layout_children()
        self.update()
        self.compactChanged.emit(True)

    def restore_normal(self) -> None:
        if not self._compact:
            return
        target = (QRect(self._target_screen.availableGeometry())
                  if self._target_screen else QRect(self._restore_geometry))
        self._compact = False
        self.setMinimumSize(1, 1)
        self.setGeometry(target)
        self._layout_children()
        self.update()
        self.compactChanged.emit(False)

    def set_click_through(self, enabled: bool) -> None:
        if self._click_through == enabled:
            return
        was_visible = self.isVisible()
        self._click_through = enabled
        self.settings.click_through = enabled
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, enabled)
        if was_visible:
            self.show_overlay()

    def _reassert_topmost(self) -> None:
        if sys.platform != "win32" or not self.isVisible():
            return
        try:
            import ctypes
            HWND_TOPMOST, flags = -1, 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
            ctypes.windll.user32.SetWindowPos(int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0, flags)
        except Exception:
            pass

    # ----------------------------------------------------------------- paint
    def paintEvent(self, _e) -> None:  # noqa: N802
        if not self._compact:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.width() / 2
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        alpha = max(14, int(255 * self.settings.bg_opacity / 100))
        palette = self.lyrics.lyric_palette
        background = palette.background
        p.fillPath(path, QColor(background.red(), background.green(), background.blue(), alpha))
        accent = palette.border
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 150), 1))
        p.drawPath(path)
        if self._compact:
            p.setPen(QColor(accent.red(), accent.green(), accent.blue(), 240))
            f = QFont("Segoe UI Symbol")
            f.setPixelSize(22)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "♪")

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if self._compact and e.button() == Qt.MouseButton.LeftButton:
            self.restore_normal()
