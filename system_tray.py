from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

OPACITY_PRESETS = (20, 35, 55, 70, 85)


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(28, 28, 36))
    p.setPen(QColor(255, 255, 255, 90))
    p.drawEllipse(3, 3, 58, 58)
    p.setPen(QColor(255, 255, 255))
    f = QFont("Segoe UI Symbol")
    f.setPixelSize(38)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "♪")
    p.end()
    return QIcon(pm)


class SystemTray(QObject):
    toggleRequested = Signal()
    showRequested = Signal()
    hideRequested = Signal()
    restoreRequested = Signal()
    opacityChanged = Signal(int)
    fontSizeDelta = Signal(int)
    offsetDelta = Signal(int)          # ms; 0 = reset
    reconnectRequested = Signal()
    screenSelected = Signal(str)
    clickThroughChanged = Signal(bool)
    maxActiveCardsChanged = Signal(int)
    quitRequested = Signal()

    def __init__(self, current_opacity: int, screen_name: str = "", click_through: bool = True,
                 max_active_cards: int = 3, parent=None):
        super().__init__(parent)
        self.icon = QSystemTrayIcon(make_icon(), self)
        self.icon.setToolTip("Floating Lyrics")
        menu = QMenu()

        self.act_show = QAction("Tampilkan overlay", menu)
        self.act_show.triggered.connect(self.showRequested)
        self.act_hide = QAction("Sembunyikan overlay", menu)
        self.act_hide.triggered.connect(self.hideRequested)
        self.act_restore = QAction("Kembalikan ukuran", menu)
        self.act_restore.triggered.connect(self.restoreRequested)
        self.act_restore.setVisible(False)
        menu.addAction(self.act_show)
        menu.addAction(self.act_hide)
        menu.addAction(self.act_restore)
        menu.addSeparator()

        self.screen_menu = menu.addMenu("Display")
        self._screen_group = QActionGroup(self.screen_menu)
        self._screen_group.setExclusive(True)
        self.update_screens(QGuiApplication.screens(), screen_name)

        self.act_click_through = QAction("Click-through (use tray to disable)", menu, checkable=True)
        self.act_click_through.setChecked(click_through)
        self.act_click_through.toggled.connect(self.clickThroughChanged)
        menu.addAction(self.act_click_through)

        card_limit_menu = menu.addMenu("Maximum active lyric cards")
        card_limit_group = QActionGroup(card_limit_menu)
        card_limit_group.setExclusive(True)
        for count in range(1, 7):
            action = QAction(str(count), card_limit_menu, checkable=True)
            action.setChecked(count == max_active_cards)
            action.triggered.connect(lambda _=False, value=count: self.maxActiveCardsChanged.emit(value))
            card_limit_group.addAction(action)
            card_limit_menu.addAction(action)
        menu.addSeparator()

        op_menu = menu.addMenu("Transparansi latar")
        group = QActionGroup(op_menu)
        group.setExclusive(True)
        nearest = min(OPACITY_PRESETS, key=lambda v: abs(v - current_opacity))
        for v in OPACITY_PRESETS:
            a = QAction(f"{v}%", op_menu, checkable=True)
            a.setChecked(v == nearest)
            a.triggered.connect(lambda _=False, val=v: self.opacityChanged.emit(val))
            group.addAction(a)
            op_menu.addAction(a)

        font_menu = menu.addMenu("Ukuran font")
        font_menu.addAction("Perbesar").triggered.connect(lambda: self.fontSizeDelta.emit(2))
        font_menu.addAction("Perkecil").triggered.connect(lambda: self.fontSizeDelta.emit(-2))

        sync_menu = menu.addMenu("Penyesuaian sinkronisasi")
        sync_menu.addAction("Lirik lebih cepat (+250 ms)").triggered.connect(lambda: self.offsetDelta.emit(250))
        sync_menu.addAction("Lirik lebih lambat (-250 ms)").triggered.connect(lambda: self.offsetDelta.emit(-250))
        sync_menu.addAction("Reset").triggered.connect(lambda: self.offsetDelta.emit(0))

        menu.addSeparator()
        self.act_reconnect = menu.addAction("Hubungkan ulang Spotify")
        self.act_reconnect.triggered.connect(self.reconnectRequested)
        menu.addAction("Keluar").triggered.connect(self.quitRequested)

        self._menu = menu
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self.icon.show()

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggleRequested.emit()

    def set_overlay_visible(self, visible: bool) -> None:
        self.act_show.setVisible(not visible)
        self.act_hide.setVisible(visible)

    def update_screens(self, screens, selected_name: str) -> None:
        self.screen_menu.clear()
        self._screen_group = QActionGroup(self.screen_menu)
        self._screen_group.setExclusive(True)
        if not selected_name:
            primary = QGuiApplication.primaryScreen()
            selected_name = primary.name() if primary is not None else ""
        for index, screen in enumerate(screens):
            name = screen.name()
            label = name or f"Display {index + 1}"
            action = QAction(label, self.screen_menu, checkable=True)
            action.setData(name)
            action.setChecked(name == selected_name)
            action.triggered.connect(lambda _=False, screen_name=name: self.screenSelected.emit(screen_name))
            self._screen_group.addAction(action)
            self.screen_menu.addAction(action)

    def set_reconnect_visible(self, visible: bool) -> None:
        self.act_reconnect.setVisible(visible)

    def set_compact(self, compact: bool) -> None:
        self.act_restore.setVisible(compact)

    def notify(self, message: str) -> None:
        """Dipakai jarang (sekali per jenis masalah) agar tidak mengganggu."""
        self.icon.showMessage("Floating Lyrics", message, QSystemTrayIcon.MessageIcon.Information, 5000)
