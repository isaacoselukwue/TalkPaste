"""Small always-on-top status indicator shown while dictating."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.logging_setup import get_logger
from app.models import AppState

log = get_logger("ui.status")

_STATE_COLOURS: dict[AppState, str] = {
    AppState.IDLE: "#9aa0a6",
    AppState.LISTENING: "#ea4335",
    AppState.PROCESSING: "#fbbc05",
    AppState.READY: "#34a853",
    AppState.ERROR: "#d93025",
}

_STATE_LABELS: dict[AppState, str] = {
    AppState.IDLE: "Idle",
    AppState.LISTENING: "Listening…",
    AppState.PROCESSING: "Transcribing…",
    AppState.READY: "Ready",
    AppState.ERROR: "Error",
}


class _LevelDot(QWidget):
    """A coloured dot whose size tracks the microphone level."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self._colour = QColor(_STATE_COLOURS[AppState.IDLE])
        self._level = 0.0

    def set_colour(self, colour: str) -> None:
        self._colour = QColor(colour)
        self.update()

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = 5 + int(self._level * 4)
        painter.setBrush(self._colour)
        painter.setPen(Qt.PenStyle.NoPen)
        centre = self.rect().center()
        painter.drawEllipse(centre, radius, radius)


class StatusPopup(QWidget):
    """Frameless, translucent popup reflecting the current :class:`AppState`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._dot = _LevelDot(self)
        self._label = QLabel("Idle", self)
        self._label.setStyleSheet("color: white; font-size: 13px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 16, 8)
        layout.setSpacing(10)
        layout.addWidget(self._dot)
        layout.addWidget(self._label)

        self.setStyleSheet(
            "QWidget { background-color: rgba(32, 33, 36, 220); border-radius: 14px; }"
        )

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def set_state(self, state: AppState, message: str = "") -> None:
        self._dot.set_colour(_STATE_COLOURS.get(state, "#9aa0a6"))
        self._label.setText(message or _STATE_LABELS.get(state, state.value.title()))
        self.adjustSize()

    def set_level(self, level: float) -> None:
        self._dot.set_level(level)

    def show_for_state(self, state: AppState, message: str = "") -> None:
        """Show for active states; auto-hide shortly after idle/ready."""

        self.set_state(state, message)
        if state in (AppState.LISTENING, AppState.PROCESSING):
            self._hide_timer.stop()
            self._reposition()
            self.show()
        elif state in (AppState.READY, AppState.ERROR):
            self._reposition()
            self.show()
            self._hide_timer.start(1600)
        else:
            self._hide_timer.start(400)

    def _reposition(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        x = geo.center().x() - self.width() // 2
        y = geo.bottom() - self.height() - 80
        self.move(x, y)
