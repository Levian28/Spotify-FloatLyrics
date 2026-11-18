from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QParallelAnimationGroup, QPropertyAnimation,
                            QPauseAnimation, QRect, QRectF, Qt, Property)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QTextLayout, QTextOption)
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget

from lyric_queue import LyricEvent, LyricState

ACCENT = QColor(57, 229, 232)
ACCENT_HIGHLIGHT = QColor(153, 250, 244)
THEME_TRANSITION_MS = 1000
CARD_PAD_X = 22
CARD_PAD_Y = 14
CARD_RADIUS = 14
CARD_SHADOW = 14
CARD_PROGRESS_HEIGHT = 2.5
MAX_ACTIVE_CARDS = 3
ENTER_MS = 280
EXIT_MS = 460
LYRIC_CROSSFADE_MS = 460
GLOW_BREATH_MS = 4800
MIN_DISPLAY_MS = 2800
MAX_DISPLAY_MS = 9000
UPWARD_SPEED_PX_PER_SECOND = 64


def _reduced_motion_enabled() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    style_hints = app.styleHints()
    accessibility = style_hints.accessibility()
    return accessibility.motionPreference().name == "ReducedMotion"


@dataclass
class LyricPalette:
    primary: QColor
    secondary: QColor
    background: QColor
    border: QColor
    glow: QColor
    progress: QColor
    text: QColor


def build_lyric_palette(primary: QColor, secondary: QColor | None = None) -> LyricPalette:
    primary = QColor(primary)
    hue, saturation, value, _ = primary.getHsv()
    if hue < 0:
        primary = QColor(ACCENT)
    else:
        primary.setHsv(hue, max(90, min(210, saturation)), max(190, min(245, value)))

    secondary = QColor(secondary) if secondary is not None else primary.lighter(145)
    second_hue, second_saturation, second_value, _ = secondary.getHsv()
    if second_hue < 0:
        secondary = primary.lighter(145)
    else:
        secondary.setHsv(second_hue, max(75, min(205, second_saturation)),
                         max(180, min(250, second_value)))

    background = QColor(
        8 + round(primary.red() * 0.10),
        15 + round(primary.green() * 0.10),
        20 + round(primary.blue() * 0.10),
    )
    return LyricPalette(
        primary=primary,
        secondary=secondary,
        background=background,
        border=QColor(primary),
        glow=QColor(primary),
        progress=QColor(primary),
        text=QColor(244, 250, 252),
    )


def default_lyric_palette() -> LyricPalette:
    return build_lyric_palette(ACCENT, ACCENT_HIGHLIGHT)


def extract_lyric_palette(image_data: bytes) -> LyricPalette | None:
    image = QImage.fromData(image_data)
    if image.isNull():
        return None
    sample = image.scaled(
        40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    ).convertToFormat(QImage.Format.Format_RGB32)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for y in range(sample.height()):
        for x in range(sample.width()):
            color = sample.pixelColor(x, y)
            _, saturation, value, _ = color.getHsv()
            if value < 32 or saturation < 28:
                continue
            key = (color.red() >> 4, color.green() >> 4, color.blue() >> 4)
            bucket = buckets.setdefault(key, [0, 0, 0, 0, 0])
            bucket[0] += 1
            bucket[1] += color.red()
            bucket[2] += color.green()
            bucket[3] += color.blue()
            bucket[4] += saturation
    if not buckets:
        return None

    colors: list[tuple[float, QColor]] = []
    for count, red, green, blue, saturation in buckets.values():
        avg_saturation = saturation / count
        score = count * (0.55 + avg_saturation / 255)
        colors.append((score, QColor(round(red / count), round(green / count),
                                     round(blue / count))))
    colors.sort(key=lambda item: item[0], reverse=True)
    primary = colors[0][1]
    primary_hue = primary.hue()
    secondary = None
    for _, candidate in colors[1:]:
        hue_distance = abs(candidate.hue() - primary_hue)
        hue_distance = min(hue_distance, 360 - hue_distance)
        rgb_distance = sum(abs(a - b) for a, b in zip(
            (candidate.red(), candidate.green(), candidate.blue()),
            (primary.red(), primary.green(), primary.blue())))
        if hue_distance >= 28 or rgb_distance >= 105:
            secondary = candidate
            break
    return build_lyric_palette(primary, secondary)


def _mix_color(start: QColor, end: QColor, amount: float) -> QColor:
    return QColor(
        round(start.red() + (end.red() - start.red()) * amount),
        round(start.green() + (end.green() - start.green()) * amount),
        round(start.blue() + (end.blue() - start.blue()) * amount),
    )


def _mix_palette(start: LyricPalette, end: LyricPalette, amount: float) -> LyricPalette:
    return LyricPalette(
        primary=_mix_color(start.primary, end.primary, amount),
        secondary=_mix_color(start.secondary, end.secondary, amount),
        background=_mix_color(start.background, end.background, amount),
        border=_mix_color(start.border, end.border, amount),
        glow=_mix_color(start.glow, end.glow, amount),
        progress=_mix_color(start.progress, end.progress, amount),
        text=_mix_color(start.text, end.text, amount),
    )


class LyricCardWidget(QWidget):
    def __init__(self, text: str, font: QFont, bg_opacity: float, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._text = text
        self._font = QFont(font)
        self._background = bg_opacity
        self._palette = default_lyric_palette()
        self._accent = QColor(self._palette.primary)
        self._accent_highlight = QColor(self._palette.secondary)
        self._active = True
        self._progress = 0.0
        self._motion_scale = 1.0
        self._motion_rotation = 0.0
        self._slash_progress = 0.0
        self._ambient_phase = 0.0
        particle_rng = random.Random(sum(map(ord, text)))
        self._particles = self._make_particles(particle_rng)
        self._rows: list[tuple[str, float]] = []
        self._line_spacing = 0.0
        self._ascent = 0.0
        self._playing = False
        self._reduced_motion = _reduced_motion_enabled()
        self._glow_animation = QPropertyAnimation(self, b"ambientPhase", self)
        self._glow_animation.setStartValue(0.0)
        self._glow_animation.setKeyValueAt(0.5, 1.0)
        self._glow_animation.setEndValue(0.0)
        self._glow_animation.setDuration(GLOW_BREATH_MS)
        self._glow_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._glow_animation.setLoopCount(-1)
        self.setGraphicsEffect(QGraphicsOpacityEffect(self))
        self.configure(text, font, bg_opacity, 400)

    @property
    def opacity_effect(self) -> QGraphicsOpacityEffect:
        effect = self.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            raise RuntimeError("Lyric card opacity effect is unavailable")
        return effect

    def configure(self, text: str, font: QFont, bg_opacity: float, max_width: int) -> None:
        self._text = text
        self._font = QFont(font)
        self._background = bg_opacity
        metrics = QFontMetricsF(self._font)
        self._line_spacing = metrics.lineSpacing()
        self._ascent = metrics.ascent()
        card_max_width = max(1, max_width - CARD_SHADOW * 2)
        wrap_width = max(48.0, card_max_width - CARD_PAD_X * 2)
        layout = QTextLayout(text, self._font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        rows: list[tuple[str, float]] = []
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(wrap_width)
            row = text[line.textStart():line.textStart() + line.textLength()].rstrip()
            rows.append((row, metrics.horizontalAdvance(row)))
        layout.endLayout()
        self._rows = rows or [("", 0.0)]
        self._particles = self._make_particles(random.Random(sum(map(ord, text))))
        content_width = max((width for _, width in self._rows), default=0)
        width = min(card_max_width, max(140, int(content_width + CARD_PAD_X * 2)))
        height = int(len(self._rows) * self._line_spacing + CARD_PAD_Y * 2
                     + CARD_PROGRESS_HEIGHT + 6 + CARD_SHADOW * 2)
        self.setFixedSize(max(1, width + CARD_SHADOW * 2), max(1, height))
        self.update()

    @staticmethod
    def _make_particles(particle_rng: random.Random) -> list[tuple[float, float, float, float]]:
        particles = []
        for index in range(8):
            side = index % 4
            x = particle_rng.uniform(0.01, 0.07) if side == 0 else \
                particle_rng.uniform(0.93, 0.99) if side == 1 else \
                particle_rng.uniform(0.10, 0.90)
            y = particle_rng.uniform(0.10, 0.90) if side < 2 else \
                particle_rng.uniform(0.01, 0.07) if side == 2 else \
                particle_rng.uniform(0.93, 0.99)
            particles.append((x, y, particle_rng.uniform(0.7, 1.5),
                              particle_rng.uniform(0.0, 6.28)))
        return particles

    def set_active(self, active: bool) -> None:
        self._active = active
        if active and self._playing and not self._reduced_motion:
            if self._glow_animation.state() != self._glow_animation.State.Running:
                self._glow_animation.start()
        else:
            self._glow_animation.stop()
            self._ambient_phase = 0.0
        self.update()

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        if not self._active or self._reduced_motion:
            return
        if playing:
            if self._glow_animation.state() == self._glow_animation.State.Paused:
                self._glow_animation.resume()
            elif self._glow_animation.state() != self._glow_animation.State.Running:
                self._glow_animation.start()
        else:
            self._glow_animation.pause()

    def stop_ambient_animation(self) -> None:
        self._glow_animation.stop()

    def set_progress(self, progress: float) -> None:
        progress = max(0.0, min(1.0, progress))
        if abs(progress - self._progress) < 0.002:
            return
        self._progress = progress
        self.update()

    def set_background(self, opacity: float) -> None:
        self._background = opacity
        self.update()

    def set_theme_color(self, color: QColor) -> None:
        self.set_theme_palette(build_lyric_palette(color))

    def set_theme_palette(self, palette: LyricPalette) -> None:
        self._palette = palette
        self._accent = QColor(palette.primary)
        self._accent_highlight = QColor(palette.secondary)
        self.update()

    def _get_motion_scale(self) -> float:
        return self._motion_scale

    def _set_motion_scale(self, scale: float) -> None:
        self._motion_scale = scale
        self.update()

    motionScale = Property(float, _get_motion_scale, _set_motion_scale)

    def _get_motion_rotation(self) -> float:
        return self._motion_rotation

    def _set_motion_rotation(self, degrees: float) -> None:
        self._motion_rotation = degrees
        self.update()

    motionRotation = Property(float, _get_motion_rotation, _set_motion_rotation)

    def _get_slash_progress(self) -> float:
        return self._slash_progress

    def _set_slash_progress(self, progress: float) -> None:
        self._slash_progress = max(0.0, min(1.0, progress))
        self.update()

    slashProgress = Property(float, _get_slash_progress, _set_slash_progress)

    def _get_ambient_phase(self) -> float:
        return self._ambient_phase

    def _set_ambient_phase(self, phase: float) -> None:
        self._ambient_phase = max(0.0, min(1.0, phase))
        self.update()

    ambientPhase = Property(float, _get_ambient_phase, _set_ambient_phase)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        center = self.rect().center()
        painter.translate(center)
        painter.rotate(self._motion_rotation)
        painter.scale(self._motion_scale, self._motion_scale)
        painter.translate(-center)
        rect = QRectF(self.rect()).adjusted(
            CARD_SHADOW + 0.6, CARD_SHADOW + 0.6,
            -CARD_SHADOW - 0.6, -CARD_SHADOW - 0.6)
        tint = self._palette.glow
        shadow_rect = rect.translated(0, 5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for width, alpha in ((17, 7), (12, 10), (7, 14), (3, 18)):
            painter.setPen(QPen(QColor(0, 0, 0, alpha), width,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawRoundedRect(shadow_rect, CARD_RADIUS, CARD_RADIUS)
        glow_alpha = (16 + round(self._ambient_phase * 6)) if self._active else 5
        painter.setPen(QPen(QColor(tint.red(), tint.green(), tint.blue(), glow_alpha),
                            8 if self._active else 4))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), CARD_RADIUS, CARD_RADIUS)

        shape = QPainterPath()
        shape.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        opacity = max(0.35, self._background + (0.08 if self._active else -0.08))
        alpha = int(255 * min(0.95, opacity))
        background = QLinearGradient(rect.topLeft(), rect.bottomRight())
        background.setColorAt(0, QColor(self._palette.background.red(),
                                        self._palette.background.green(),
                                        self._palette.background.blue(), alpha))
        background.setColorAt(0.52, QColor(8, 20, 27, alpha))
        background.setColorAt(1, QColor(
            min(255, self._palette.background.red() + 5),
            min(255, self._palette.background.green() + 6),
            min(255, self._palette.background.blue() + 7), alpha))
        painter.setBrush(QBrush(background))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(shape)

        painter.save()
        painter.setClipPath(shape)
        highlight = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        highlight.setColorAt(0, QColor(255, 255, 255, 20 if self._active else 11))
        highlight.setColorAt(0.45, QColor(tint.red(), tint.green(), tint.blue(), 5))
        highlight.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(rect, QBrush(highlight))
        if self._active and not self._reduced_motion:
            painter.setPen(Qt.PenStyle.NoPen)
            for norm_x, norm_y, radius, phase in self._particles:
                drift = self._ambient_phase * 2.4
                particle_x = rect.left() + norm_x * rect.width() \
                    + 1.5 * math.sin(phase + drift)
                particle_y = rect.top() + norm_y * rect.height() \
                    + 1.5 * math.cos(phase + drift)
                alpha = 10 + round(12 * (0.5 + 0.5 * math.sin(
                    phase + self._ambient_phase * 6.28)))
                painter.setBrush(QColor(tint.red(), tint.green(), tint.blue(), alpha))
                painter.drawEllipse(QPointF(particle_x, particle_y), radius, radius)
        top_edge = QPainterPath()
        top_edge.moveTo(rect.left() + CARD_RADIUS, rect.top() + 1)
        top_edge.lineTo(rect.right() - CARD_RADIUS, rect.top() + 1)
        painter.setPen(QPen(QColor(self._palette.border.red(),
                                   self._palette.border.green(),
                                   self._palette.border.blue(),
                                   58 if self._active else 26), 1))
        painter.drawPath(top_edge)
        painter.restore()

        border = QLinearGradient(rect.topLeft(), rect.bottomRight())
        if self._active:
            border.setColorAt(0, QColor(self._accent_highlight.red(),
                                        self._accent_highlight.green(),
                                        self._accent_highlight.blue(), 150))
            border.setColorAt(1, QColor(self._accent.red(), self._accent.green(),
                                        self._accent.blue(), 105))
            painter.setPen(QPen(border, 1.15))
        else:
            painter.setPen(QPen(QColor(202, 223, 229, 48), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)

        painter.setFont(self._font)
        text_height = len(self._rows) * self._line_spacing
        first_baseline = rect.center().y() - text_height / 2 + self._ascent - 2
        for i, (text, text_width) in enumerate(self._rows):
            x = rect.center().x() - text_width / 2
            baseline = first_baseline + i * self._line_spacing
            painter.setPen(QColor(0, 0, 0, 125))
            painter.drawText(QPoint(round(x), round(baseline + 1.5)), text)
            painter.setPen(QColor(self._palette.text.red(), self._palette.text.green(),
                                  self._palette.text.blue(), 255 if self._active else 160))
            painter.drawText(QPoint(round(x), round(baseline)), text)

        progress_rect = QRectF(
            rect.left() + CARD_PAD_X, rect.bottom() - CARD_PAD_Y,
            max(1.0, rect.width() - CARD_PAD_X * 2), CARD_PROGRESS_HEIGHT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(225, 245, 248, 32 if self._active else 20))
        painter.drawRoundedRect(progress_rect, CARD_PROGRESS_HEIGHT / 2,
                                CARD_PROGRESS_HEIGHT / 2)
        progress_fill = QRectF(progress_rect)
        progress_fill.setWidth(progress_rect.width() * self._progress)
        if progress_fill.width() > 0:
            if self._active:
                gradient = QLinearGradient(progress_fill.topLeft(), progress_fill.topRight())
                gradient.setColorAt(0, self._palette.progress)
                gradient.setColorAt(1, self._palette.secondary)
                painter.setPen(QPen(QColor(tint.red(), tint.green(), tint.blue(), 35), 7))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(progress_fill, CARD_PROGRESS_HEIGHT / 2,
                                        CARD_PROGRESS_HEIGHT / 2)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(gradient))
            else:
                painter.setBrush(QColor(180, 205, 230, 140))
            painter.drawRoundedRect(progress_fill, CARD_PROGRESS_HEIGHT / 2,
                                    CARD_PROGRESS_HEIGHT / 2)
            if self._active and self._progress < 1.0:
                head = QPointF(progress_fill.right(), progress_rect.center().y())
                painter.setBrush(QColor(self._accent_highlight.red(),
                                        self._accent_highlight.green(),
                                        self._accent_highlight.blue(), 55))
                painter.drawEllipse(head, 4.0, 4.0)
                painter.setBrush(self._accent_highlight)
                painter.drawEllipse(head, 1.8, 1.8)

        if self._slash_progress > 0.0:
            painter.save()
            clip = QPainterPath()
            clip.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
            painter.setClipPath(clip)
            center_x = (rect.left() - rect.height()
                        + (rect.width() + rect.height()) * self._slash_progress)
            start = QPointF(center_x - rect.height() * 0.45, rect.bottom())
            end = QPointF(center_x + rect.height() * 0.45, rect.top())
            painter.setPen(QPen(QColor(self._accent.red(), self._accent.green(),
                                       self._accent.blue(), 75), 8.0,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(start, end)
            painter.setPen(QPen(self._accent_highlight, 2.0,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(start, end)
            painter.restore()


@dataclass
class ActiveLyricCard:
    event: LyricEvent
    widget: LyricCardWidget
    normalized_position: tuple[float, float]
    animation: QParallelAnimationGroup | None = None
    display_animation: QPauseAnimation | None = None
    display_complete: bool = False
    start_x: int = 0
    start_y: int = 0
    target_y: int = 0
    created_position_ms: float = 0.0
    cycle_resetting: bool = False
    cycle_count: int = 0


class LyricsWidget(QWidget):
    def __init__(self, family: str = "Segoe UI", size: int = 20, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._message = ""
        self._font_family = family
        self._font_size = size
        self._font = QFont(family)
        self._font.setPointSizeF(float(size))
        self._font.setWeight(QFont.Weight.DemiBold)
        self._duration_ms = 0
        self._playback_duration_ms = 0
        self._position_ms = 0.0
        self._progress_position_ms = 0.0
        self._background_opacity = 0.55
        self._palette = default_lyric_palette()
        self._palette_from = self._palette
        self._palette_to = self._palette
        self._theme_progress = 1.0
        self._theme_animation: QPropertyAnimation | None = None
        self._accent = QColor(self._palette.primary)
        self._accent_highlight = QColor(self._palette.secondary)
        self._playing = False
        self._track_id = ""
        self._current_index = -1
        self._lines: list[tuple[int, str]] = []
        self.events: list[LyricEvent] = []
        self.active_cards: dict[str, ActiveLyricCard] = {}
        self.max_active_cards = MAX_ACTIVE_CARDS
        self._position_history: deque[tuple[float, float]] = deque(maxlen=8)
        self._random = random.SystemRandom()

    @property
    def visible_lyrics(self) -> list[int]:
        return sorted(card.event.lyric_index for card in self.active_cards.values()
                      if card.event.state != LyricState.EXITING)

    @property
    def last_triggered_index(self) -> int:
        return self._current_index

    @property
    def current_lyric_index(self) -> int:
        return self._current_index

    def set_max_active_cards(self, count: int) -> None:
        self.max_active_cards = max(1, min(6, int(count)))
        while len(self.active_cards) > self.max_active_cards:
            oldest = min(
                self.active_cards.values(),
                key=lambda item: (
                    item.event.state != LyricState.EXITING,
                    item.event.lyric_index == self._current_index,
                    item.event.start_ms,
                ),
            )
            self._remove_card(oldest.event.event_id)

    def set_font(self, family: str, size: int) -> None:
        self._font_family = family
        self._font_size = size
        self._font = QFont(family)
        self._font.setPointSizeF(float(size))
        self._font.setWeight(QFont.Weight.DemiBold)
        self._relayout_cards()

    def set_lines(self, lines: list[tuple[int, str]], index: int,
                  timeline_ms: float = 0.0, duration_ms: int = 0,
                  progress_ms: float | None = None, track_id: str = "",
                  playback_position_ms: float | None = None) -> None:
        self.clear_cards()
        self._message = ""
        self._track_id = track_id
        self._lines = list(lines)
        self._duration_ms = duration_ms
        self._playback_duration_ms = duration_ms
        self._position_ms = timeline_ms if progress_ms is None else progress_ms
        self._progress_position_ms = max(
            0.0, self._position_ms if playback_position_ms is None else playback_position_ms)
        self.events = [
            LyricEvent(
                event_id=f"{track_id}:{i}",
                track_id=track_id,
                lyric_index=i,
                text=text,
                start_ms=start,
                end_ms=(lines[i + 1][0] if i + 1 < len(lines)
                        else duration_ms if duration_ms > start else start + 5000),
            )
            for i, (start, text) in enumerate(lines)
        ]
        self._current_index = -1
        self.update_playback_position(
            self._position_ms, index, duration_ms, self._playing, seeked=True,
            playback_position_ms=self._progress_position_ms)

    def update_playback_position(self, position_ms: float, index: int, duration_ms: int,
                                 playing: bool, seeked: bool = False,
                                 playback_position_ms: float | None = None) -> None:
        self._position_ms = max(0.0, position_ms)
        self._progress_position_ms = max(
            0.0, self._position_ms if playback_position_ms is None else playback_position_ms)
        self._duration_ms = duration_ms
        self._playback_duration_ms = duration_ms
        self.set_playing(playing)
        if seeked or index < self._current_index or index > self._current_index + 1:
            self._reconcile(index)
        elif index > self._current_index:
            self._transition_out_previous(index)
            self._start_event(index)
            self._current_index = index
        elif self._current_index < 0 and index >= 0:
            self._start_event(index)
            self._current_index = index
        elif 0 <= index < len(self.events):
            event = self.events[index]
            if self._position_ms < event.end_ms and event.state in (LyricState.EXITING, LyricState.REMOVED):
                self._reconcile(index)

        for event_id, card in tuple(self.active_cards.items()):
            event = card.event
            if event.state == LyricState.EXITING:
                continue
            span = max(1, event.end_ms - event.start_ms)
            progress = max(0.0, min(1.0, (self._position_ms - event.start_ms) / span))
            card.widget.set_progress(progress)
            self._update_card_position(card)
            card.widget.set_active(event.lyric_index == self._current_index)
            if self._position_ms >= event.end_ms:
                self._maybe_start_exit(event_id)

        self.update()

    def advance_to(self, index: int, animate: bool = True) -> bool:
        if index == self._current_index:
            return False
        self.update_playback_position(self._position_ms, index, self._duration_ms,
                                      self._playing, seeked=not animate)
        return True

    def reconcile_to(self, index: int) -> None:
        self._reconcile(index)
        self.update()

    def set_message(self, text: str) -> None:
        self.clear_cards()
        self.events.clear()
        self._lines.clear()
        self._current_index = -1
        self._position_ms = 0.0
        self._progress_position_ms = 0.0
        self._duration_ms = 0
        self._playback_duration_ms = 0
        self._message = text
        self.update()

    def clear(self) -> None:
        self.set_message("")

    def set_playback_progress(self, position_ms: float, duration_ms: int) -> None:
        self._progress_position_ms = max(0.0, position_ms)
        self._playback_duration_ms = max(0, duration_ms)
        self.update()

    def clear_cards(self) -> None:
        for event_id in tuple(self.active_cards):
            self._remove_card(event_id)
        self.active_cards.clear()
        self._current_index = -1

    def set_background_opacity(self, percent: int) -> None:
        self._background_opacity = max(0.35, min(0.95, percent / 100.0))
        for card in self.active_cards.values():
            card.widget.set_background(self._background_opacity)

    @property
    def accent_color(self) -> QColor:
        return QColor(self._accent)

    @property
    def lyric_palette(self) -> LyricPalette:
        return self._palette

    def _get_theme_progress(self) -> float:
        return self._theme_progress

    def _set_theme_progress(self, progress: float) -> None:
        self._theme_progress = max(0.0, min(1.0, progress))
        self._apply_palette(_mix_palette(self._palette_from, self._palette_to, self._theme_progress))

    themeProgress = Property(float, _get_theme_progress, _set_theme_progress)

    def _apply_palette(self, palette: LyricPalette) -> None:
        self._palette = palette
        self._accent = QColor(palette.primary)
        self._accent_highlight = QColor(palette.secondary)
        for card in self.active_cards.values():
            card.widget.set_theme_palette(palette)
        self.update()
        parent = self.parentWidget()
        if parent is not None:
            parent.update()

    def set_theme_palette(self, palette: LyricPalette, animate: bool = True) -> None:
        if self._theme_animation is not None:
            self._theme_animation.stop()
            self._theme_animation.deleteLater()
            self._theme_animation = None
        if not animate or palette == self._palette:
            self._palette_from = palette
            self._palette_to = palette
            self._theme_progress = 1.0
            self._apply_palette(palette)
            return
        self._palette_from = self._palette
        self._palette_to = palette
        animation = QPropertyAnimation(self, b"themeProgress", self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(THEME_TRANSITION_MS)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._theme_animation = animation
        animation.start()

    def set_theme_color(self, color: QColor | None) -> None:
        palette = default_lyric_palette() if color is None else build_lyric_palette(color)
        self.set_theme_palette(palette)

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        for card in self.active_cards.values():
            card.widget.set_playing(playing)
            for animation in (card.animation, card.display_animation):
                if animation is None:
                    continue
                state = animation.state()
                if playing and state == animation.State.Paused:
                    animation.resume()
                elif not playing and state == animation.State.Running:
                    animation.pause()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._relayout_cards(reposition=True)
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._playback_duration_ms > 0:
            bar_width = max(1.0, self.width() * 0.72)
            bar_height = 3.0
            bar = QRectF((self.width() - bar_width) / 2, self.height() - 9,
                         bar_width, bar_height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 58))
            painter.drawRoundedRect(bar, bar_height / 2, bar_height / 2)
            progress = min(1.0, self._progress_position_ms / self._playback_duration_ms)
            fill = QRectF(bar)
            fill.setWidth(bar.width() * progress)
            if fill.width() > 0:
                gradient = QLinearGradient(fill.topLeft(), fill.topRight())
                gradient.setColorAt(0, self._palette.progress)
                gradient.setColorAt(1, self._palette.secondary)
                painter.setBrush(QBrush(gradient))
                painter.drawRoundedRect(fill, bar_height / 2, bar_height / 2)
        if not self._message:
            return
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self._font)
        painter.setPen(QColor(255, 255, 255, 205))
        painter.drawText(QRectF(24, 0, max(1, self.width() - 48), self.height()),
                         Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)

    def _reconcile(self, index: int) -> None:
        self.clear_cards()
        self._current_index = index
        for event in self.events:
            event.state = LyricState.REMOVED if event.lyric_index < index else LyricState.SCHEDULED
        if 0 <= index < len(self.events):
            event = self.events[index]
            if self._position_ms < event.end_ms:
                self._create_card(event, animate=self._playing)

    def _transition_out_previous(self, next_index: int) -> None:
                for event_id, card in tuple(self.active_cards.items()):
                    if card.event.lyric_index != next_index and card.event.state not in (
                            LyricState.EXITING, LyricState.REMOVED):
                        self._start_exit(event_id, LYRIC_CROSSFADE_MS)

    def _start_event(self, index: int) -> None:
        if not 0 <= index < len(self.events):
            return
        event = self.events[index]
        if event.state != LyricState.SCHEDULED:
            return
        if self._position_ms >= event.end_ms:
            event.state = LyricState.REMOVED
            return
        self._create_card(event, animate=self._playing)

    def _create_card(self, event: LyricEvent, animate: bool) -> None:
        if event.event_id in self.active_cards:
            return
        if self._position_ms >= event.end_ms:
            event.state = LyricState.REMOVED
            return
        while len(self.active_cards) >= self.max_active_cards:
            oldest = min(
                self.active_cards.values(),
                key=lambda item: (
                    item.event.state != LyricState.EXITING,
                    item.event.lyric_index == self._current_index,
                    item.event.start_ms,
                ),
            )
            self._remove_card(oldest.event.event_id)
        width = self._maximum_card_width()
        card = LyricCardWidget(event.text, self._font, self._background_opacity, self)
        self._configure_card(card, event.text, width)
        card.set_theme_color(self._accent)
        normalized = self._choose_position(card.size())
        self._record_position(normalized)
        target = self._rect_for_position(card.size(), normalized)
        start_x = target.x()
        start_y = max(target.y(), self.height() - CARD_SHADOW + 1)
        start = QRect(start_x, start_y, card.width(), card.height())
        card.setGeometry(start)
        card.opacity_effect.setOpacity(0.0 if animate else 1.0)
        card.motionScale = 0.97 if animate else 1.0
        card.motionRotation = 0.0
        card.show()
        event.state = LyricState.ENTERING if animate else LyricState.VISIBLE
        active = ActiveLyricCard(
            event, card, normalized, start_x=start_x,
            start_y=start_y, target_y=target.y(),
            created_position_ms=self._position_ms)
        self.active_cards[event.event_id] = active
        card.set_active(event.lyric_index == self._current_index)
        card.set_playing(self._playing)
        self._update_card_position(active)
        if animate:
            duration = self._random.randint(ENTER_MS - 40, ENTER_MS + 35)
            self._animate(card, 0.0, 1.0, duration,
                          lambda eid=event.event_id: self._finish_enter(eid),
                          start_scale=0.97, end_scale=1.0)
        else:
            self._start_display_lifetime(active)

    def _finish_enter(self, event_id: str) -> None:
        card = self.active_cards.get(event_id)
        if card and card.event.state == LyricState.ENTERING:
            card.event.state = LyricState.VISIBLE
            if card.animation is not None:
                card.animation.deleteLater()
            card.animation = None
            card.widget.set_active(card.event.lyric_index == self._current_index)
            self._start_display_lifetime(card)

    @staticmethod
    def _display_duration_ms(text: str) -> int:
        words = len(text.split())
        characters = len(text)
        punctuation = sum(character in ".,!?;:" for character in text)
        duration = 1800 + words * 110 + characters * 28 + punctuation * 90
        return max(MIN_DISPLAY_MS, min(MAX_DISPLAY_MS, duration))

    def _start_display_lifetime(self, card: ActiveLyricCard) -> None:
        if card.display_animation is not None or card.display_complete:
            return
        timer = QPauseAnimation(self._display_duration_ms(card.event.text), card.widget)
        event_id = card.event.event_id
        timer.finished.connect(lambda eid=event_id: self._on_display_complete(eid))
        card.display_animation = timer
        timer.start()
        if not self._playing:
            timer.pause()

    def _on_display_complete(self, event_id: str) -> None:
        card = self.active_cards.get(event_id)
        if card is None:
            return
        card.display_complete = True
        if card.display_animation is not None:
            card.display_animation.deleteLater()
            card.display_animation = None
        self._maybe_start_exit(event_id)

    def _maybe_start_exit(self, event_id: str) -> None:
        card = self.active_cards.get(event_id)
        if (card is not None and card.display_complete
                and self._position_ms >= card.event.end_ms):
            self._start_exit(event_id)

    def _start_exit(self, event_id: str, duration_ms: int = EXIT_MS) -> None:
        card = self.active_cards.get(event_id)
        if card is None or card.event.state in (LyricState.EXITING, LyricState.REMOVED):
            return
        card.event.state = LyricState.EXITING
        card.widget.set_active(False)
        start_scale = card.widget.motionScale
        self._animate(card.widget, card.widget.opacity_effect.opacity(), 0.0,
                      duration_ms, lambda eid=event_id: self._finish_exit(eid),
                      start_scale=start_scale, end_scale=0.98)

    def _finish_exit(self, event_id: str) -> None:
        card = self.active_cards.pop(event_id, None)
        if card is not None:
            card.event.state = LyricState.REMOVED
            self._dispose_card(card)

    def _animate(self, widget: LyricCardWidget, start_opacity: float, end_opacity: float,
                 duration_ms: int, finished,
                 start_scale: float = 1.0, end_scale: float = 1.0,
                 start_rotation: float = 0.0, end_rotation: float = 0.0,
                 slash: bool = False) -> None:
        for card in self.active_cards.values():
            if card.widget is widget and card.animation is not None:
                card.animation.stop()
                card.animation.deleteLater()
                card.animation = None
                break
        group = QParallelAnimationGroup(widget)
        if widget._reduced_motion:
            duration_ms = min(duration_ms, 120)
            start_scale = end_scale = 1.0
        opacity = QPropertyAnimation(widget.opacity_effect, b"opacity", group)
        opacity.setStartValue(start_opacity)
        opacity.setEndValue(end_opacity)
        opacity.setDuration(duration_ms)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
        scale = QPropertyAnimation(widget, b"motionScale", group)
        scale.setStartValue(start_scale)
        scale.setEndValue(end_scale)
        scale.setDuration(duration_ms)
        scale.setEasingCurve(QEasingCurve.Type.OutCubic)
        rotation = QPropertyAnimation(widget, b"motionRotation", group)
        rotation.setStartValue(start_rotation)
        rotation.setEndValue(end_rotation)
        rotation.setDuration(duration_ms)
        rotation.setEasingCurve(QEasingCurve.Type.OutCubic)
        group.addAnimation(opacity)
        group.addAnimation(scale)
        group.addAnimation(rotation)
        if slash:
            slash_animation = QPropertyAnimation(widget, b"slashProgress", group)
            slash_animation.setStartValue(0.0)
            slash_animation.setEndValue(1.0)
            slash_animation.setDuration(duration_ms)
            slash_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            group.addAnimation(slash_animation)
        for card in self.active_cards.values():
            if card.widget is widget:
                card.animation = group
                break
        group.finished.connect(finished)
        group.start()
        if not self._playing:
            group.pause()

    def _dispose_card(self, card: ActiveLyricCard) -> None:
        if card.animation is not None:
            card.animation.stop()
            card.animation.deleteLater()
        if card.display_animation is not None:
            card.display_animation.stop()
            card.display_animation.deleteLater()
        card.widget.stop_ambient_animation()
        card.widget.hide()
        card.widget.setGraphicsEffect(None)
        card.widget.deleteLater()
        card.animation = None

    def _remove_card(self, event_id: str) -> None:
        card = self.active_cards.pop(event_id, None)
        if card is None:
            return
        card.event.state = LyricState.REMOVED
        self._dispose_card(card)

    def _maximum_card_width(self) -> int:
        return max(1, min(440, int(self.width() * 0.38), self.width() - 32))

    def _configure_card(self, card: LyricCardWidget, text: str, max_width: int) -> None:
        available_height = max(1, self.height() - 48)
        width = max(max_width, self.width() - 48)
        font = QFont(self._font)
        card.configure(text, font, self._background_opacity, max_width)
        if card.height() > available_height:
            card.configure(text, font, self._background_opacity, width)
        while card.height() > available_height and font.pointSizeF() > 10:
            font.setPointSizeF(max(10.0, font.pointSizeF() - 1.0))
            card.configure(text, font, self._background_opacity, width)

    def _choose_position(self, size) -> tuple[float, float]:
        margin = 24
        max_x = max(margin, self.width() - size.width() - margin)
        top_y = min(margin, max(0, self.height() - size.height()))
        candidates: list[tuple[float, float, float, float, float]] = []
        for _ in range(64):
            x = float(self._random.uniform(margin, max_x)) if max_x > margin else float(max_x / 2)
            y = float(top_y)
            rect = QRect(round(x), round(y), size.width(), size.height())
            overlap = 0.0
            for active in self.active_cards.values():
                other = (active.widget.geometry() if active.event.state == LyricState.EXITING
                         else self._rect_for_position(active.widget.size(), active.normalized_position))
                intersection = rect.intersected(other)
                if not intersection.isEmpty():
                    overlap += intersection.width() * intersection.height() / max(1, rect.width() * rect.height())
            recent_distance = min(
                (((x / max(1, self.width()) - hx) ** 2
                  + (y / max(1, self.height()) - hy) ** 2) ** 0.5 for hx, hy in self._position_history),
                default=1.0,
            )
            score = overlap * 100 + max(0.0, 0.20 - recent_distance) * 3
            candidates.append((score, overlap, recent_distance, x, y))
        suitable = [candidate for candidate in candidates
                    if candidate[1] < 0.05 and candidate[2] >= 0.20]
        if not suitable:
            suitable = [candidate for candidate in candidates if candidate[1] < 0.05]
        if not suitable:
            suitable = candidates
        best_score = min(candidate[0] for candidate in suitable)
        shortlist = [candidate for candidate in suitable if candidate[0] <= best_score + 0.25]
        _, _, _, x, y = self._random.choice(shortlist)
        normalized = (x / max(1, self.width()), y / max(1, self.height()))
        return normalized

    def _record_position(self, normalized: tuple[float, float]) -> None:
        self._position_history.append(normalized)

    def _rect_for_position(self, size, normalized: tuple[float, float]) -> QRect:
        margin = 24
        max_x = max(margin, self.width() - size.width() - margin)
        max_y = min(margin, max(0, self.height() - size.height()))
        x = min(max_x, max(margin, round(normalized[0] * self.width())))
        y = min(max_y, max(0, round(normalized[1] * self.height())))
        return QRect(x, y, size.width(), size.height())

    def _update_card_position(self, active: ActiveLyricCard) -> None:
        if active.cycle_resetting or active.event.state in (LyricState.EXITING, LyricState.REMOVED):
            return
        card = active.widget
        margin_x = min(24, max(0, (self.width() - card.width()) // 2))
        max_x = max(margin_x, self.width() - card.width() - margin_x)
        x = min(max_x, max(margin_x, active.start_x))
        elapsed_ms = max(0.0, self._position_ms - active.created_position_ms)
        travel_distance = max(0, active.start_y - active.target_y)
        travel_ms = travel_distance * 1000 / UPWARD_SPEED_PX_PER_SECOND
        progress = min(1.0, elapsed_ms / max(1.0, travel_ms))
        eased_progress = 1.0 - (1.0 - progress) ** 2
        y = round(active.start_y - travel_distance * eased_progress)
        card.move(x, y)
        if y == active.target_y and active.start_y > active.target_y:
            self._start_cycle_reset(active.event.event_id)

    def _start_cycle_reset(self, event_id: str) -> None:
        active = self.active_cards.get(event_id)
        if (active is None or active.cycle_resetting
                or active.event.state in (LyricState.EXITING, LyricState.REMOVED)):
            return
        if active.display_complete and self._position_ms >= active.event.end_ms:
            self._start_exit(event_id)
            return
        active.cycle_resetting = True
        opacity = active.widget.opacity_effect.opacity()
        self._animate(
            active.widget, opacity, 0.0, EXIT_MS,
            lambda eid=event_id: self._finish_cycle_exit(eid),
            start_scale=active.widget.motionScale, end_scale=0.98,
        )

    def _finish_cycle_exit(self, event_id: str) -> None:
        active = self.active_cards.get(event_id)
        if active is None or active.event.state in (LyricState.EXITING, LyricState.REMOVED):
            return
        if active.display_complete and self._position_ms >= active.event.end_ms:
            self._start_exit(event_id)
            return

        margin_y = min(24, max(0, (self.height() - active.widget.height()) // 2))
        active.target_y = margin_y
        active.start_y = max(margin_y, self.height() - CARD_SHADOW + 1)
        active.cycle_count += 1
        active.widget.move(active.start_x, active.start_y)
        active.widget.motionScale = 0.98
        self._animate(
            active.widget, 0.0, 1.0, ENTER_MS,
            lambda eid=event_id: self._finish_cycle_enter(eid),
            start_scale=0.98, end_scale=1.0,
        )

    def _finish_cycle_enter(self, event_id: str) -> None:
        active = self.active_cards.get(event_id)
        if active is not None:
            if active.animation is not None:
                active.animation.deleteLater()
                active.animation = None
            active.created_position_ms = self._position_ms
            active.cycle_resetting = False
            active.widget.set_active(active.event.lyric_index == self._current_index)

    def _relayout_cards(self, reposition: bool = False) -> None:
        if not hasattr(self, "active_cards") or not self.active_cards:
            return
        max_width = self._maximum_card_width()
        for event_id, active in sorted(tuple(self.active_cards.items()),
                                       key=lambda item: item[1].event.start_ms):
            if active.event.state == LyricState.EXITING:
                self._remove_card(event_id)
                continue
            animation = active.animation
            if animation is not None:
                animation.stop()
                animation.deleteLater()
                active.animation = None
            if active.event.state == LyricState.ENTERING:
                active.event.state = LyricState.VISIBLE
                self._start_display_lifetime(active)
            self._configure_card(active.widget, active.event.text, max_width)
            if reposition:
                active.normalized_position = self._choose_position(active.widget.size())
                self._record_position(active.normalized_position)
                target = self._rect_for_position(active.widget.size(), active.normalized_position)
                active.start_x = target.x()
                margin_y = min(24, max(0, (self.height() - active.widget.height()) // 2))
                active.target_y = margin_y
                active.start_y = max(
                    active.target_y, self.height() - active.widget.height() - margin_y)
                active.target_y = target.y()
                active.created_position_ms = self._position_ms
                active.cycle_resetting = False
            self._update_card_position(active)
            active.widget.opacity_effect.setOpacity(1.0)
            active.widget.motionScale = 1.0
            active.widget.motionRotation = 0.0
