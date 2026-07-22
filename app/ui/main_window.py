"""The main TalkPaste window — a dashboard with a big Start/Stop control.

The tray app owns the :class:`~app.services.controller.DictationController` and
this window; it forwards state/result updates here (already on the GUI thread).
Closing the window hides it to the tray rather than quitting.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME, __version__
from app.logging_setup import get_logger
from app.models import AppState

log = get_logger("ui.window")

_STATE_COLOURS: dict[AppState, str] = {
    AppState.IDLE: "#5f6368",
    AppState.LISTENING: "#ea4335",
    AppState.PROCESSING: "#f9ab00",
    AppState.READY: "#188038",
    AppState.ERROR: "#d93025",
}

_STATE_LABELS: dict[AppState, str] = {
    AppState.IDLE: "Idle",
    AppState.LISTENING: "Listening",
    AppState.PROCESSING: "Transcribing",
    AppState.READY: "Ready",
    AppState.ERROR: "Error",
}

_STYLE = """
QWidget#Root { background: #f5f6f8; }
QLabel#Title { font-size: 20px; font-weight: 600; color: #202124; }
QLabel#Version { color: #80868b; font-size: 11px; }
QLabel#StatePill {
    color: white; font-weight: 600; padding: 4px 14px; border-radius: 11px;
}
QFrame#Card {
    background: white; border: 1px solid #e0e2e6; border-radius: 12px;
}
QLabel#SectionHeading { color: #5f6368; font-size: 11px; font-weight: 600; }
QPushButton#Record {
    border-radius: 46px; background: #ea4335; color: white;
    font-size: 15px; font-weight: 600;
}
QPushButton#Record:hover { background: #d93b2f; }
QPushButton#Record[recording="true"] { background: #188038; }
QPushButton#Record[recording="true"]:hover { background: #14702f; }
QPushButton.Secondary {
    background: white; border: 1px solid #dadce0; border-radius: 8px;
    padding: 7px 14px; color: #3c4043;
}
QPushButton.Secondary:hover { background: #f1f3f4; }
QTextEdit, QListWidget {
    background: white; border: 1px solid #e0e2e6; border-radius: 8px;
}
QProgressBar { border: none; background: #e8eaed; border-radius: 4px; height: 8px; }
QProgressBar::chunk { background: #ea4335; border-radius: 4px; }
QLabel#Hint { color: #80868b; font-size: 11px; }
"""


def _play_icon(recording: bool) -> QIcon:
    pm = QPixmap(40, 40)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("white"))
    p.setPen(Qt.PenStyle.NoPen)
    if recording:
        p.drawRoundedRect(12, 12, 16, 16, 3, 3)
    else:
        p.drawPolygon(QPolygon([QPoint(15, 11), QPoint(15, 29), QPoint(30, 20)]))
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    """Dashboard window controlling and displaying dictation."""

    def __init__(
        self,
        controller,
        *,
        on_open_settings: Callable[[], None] | None = None,
        on_open_diagnostics: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.controller = controller
        self._on_open_settings = on_open_settings
        self._on_open_diagnostics = on_open_diagnostics
        self._on_quit = on_quit
        self._recording = False

        self.setWindowTitle(APP_NAME)
        self.resize(460, 620)

        root = QWidget()
        root.setObjectName("Root")
        root.setStyleSheet(_STYLE)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(16)

        outer.addLayout(self._build_header())
        outer.addWidget(self._build_record_card())
        outer.addWidget(self._build_last_card(), 1)
        outer.addWidget(self._build_recent_card(), 1)
        outer.addLayout(self._build_footer())

        self._level_timer = QTimer(self)
        self._level_timer.setInterval(60)
        self._level_timer.timeout.connect(self._poll_level)

        self.update_state(AppState.IDLE, "Ready")
        self.refresh_recent()

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        version = QLabel(f"v{__version__} · local dictation")
        version.setObjectName("Version")
        title_box.addWidget(title)
        title_box.addWidget(version)
        row.addLayout(title_box)
        row.addStretch(1)
        self.state_pill = QLabel("Idle")
        self.state_pill.setObjectName("StatePill")
        self.state_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.state_pill, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _build_record_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 20, 18, 18)
        lay.setSpacing(14)

        self.record_btn = QPushButton("  Start dictation")
        self.record_btn.setObjectName("Record")
        self.record_btn.setIcon(_play_icon(False))
        self.record_btn.setFixedHeight(92)
        self.record_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.record_btn.clicked.connect(self._toggle)
        lay.addWidget(self.record_btn)

        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setValue(0)
        self.level_bar.setTextVisible(False)
        lay.addWidget(self.level_bar)

        self.status_label = QLabel("Press the button, or hold your global shortcut, and speak.")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)
        return card

    def _build_last_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 14)
        heading = QLabel("LAST TRANSCRIPT")
        heading.setObjectName("SectionHeading")
        lay.addWidget(heading)
        self.last_text = QTextEdit()
        self.last_text.setReadOnly(True)
        self.last_text.setPlaceholderText("Your most recent transcript will appear here.")
        lay.addWidget(self.last_text)
        row = QHBoxLayout()
        row.addStretch(1)
        copy_btn = QPushButton("Copy")
        copy_btn.setProperty("class", "Secondary")
        copy_btn.clicked.connect(self._copy_last)
        row.addWidget(copy_btn)
        lay.addLayout(row)
        return card

    def _build_recent_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 16, 14)
        heading = QLabel("RECENT")
        heading.setObjectName("SectionHeading")
        lay.addWidget(heading)
        self.recent_list = QListWidget()
        self.recent_list.itemActivated.connect(self._copy_recent_item)
        self.recent_list.itemDoubleClicked.connect(self._copy_recent_item)
        lay.addWidget(self.recent_list)
        return card

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        settings_btn = QPushButton("Settings…")
        settings_btn.setProperty("class", "Secondary")
        settings_btn.clicked.connect(lambda: self._on_open_settings and self._on_open_settings())
        diag_btn = QPushButton("Diagnostics…")
        diag_btn.setProperty("class", "Secondary")
        diag_btn.clicked.connect(lambda: self._on_open_diagnostics and self._on_open_diagnostics())
        row.addWidget(settings_btn)
        row.addWidget(diag_btn)
        row.addStretch(1)
        hk = self.controller.settings.hotkeys.push_to_talk
        hint = QLabel(f"Shortcut: {hk}")
        hint.setObjectName("Hint")
        row.addWidget(hint)
        return row

    def update_state(self, state: AppState, message: str = "") -> None:
        colour = _STATE_COLOURS.get(state, "#5f6368")
        self.state_pill.setText(_STATE_LABELS.get(state, state.value.title()))
        self.state_pill.setStyleSheet(f"QLabel#StatePill {{ background: {colour}; }}")
        if message:
            self.status_label.setText(message)

        recording = state is AppState.LISTENING
        if recording != self._recording:
            self._recording = recording
            self.record_btn.setText("  Stop dictation" if recording else "  Start dictation")
            self.record_btn.setIcon(_play_icon(recording))
            self.record_btn.setProperty("recording", "true" if recording else "false")
            self.record_btn.style().unpolish(self.record_btn)
            self.record_btn.style().polish(self.record_btn)

        if recording:
            self._level_timer.start()
        else:
            self._level_timer.stop()
            self.level_bar.setValue(0)

    def add_result(self, result) -> None:
        if result and result.final_text:
            self.last_text.setPlainText(result.final_text)
        self.refresh_recent()

    def refresh_recent(self) -> None:
        self.recent_list.clear()
        try:
            entries = self.controller.history.recent(20)
        except Exception:  # noqa: BLE001
            entries = []
        if not entries:
            item = QListWidgetItem("No transcripts yet.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recent_list.addItem(item)
            return
        for entry in entries:
            preview = " ".join(entry.text.split())
            if len(preview) > 80:
                preview = preview[:77] + "…"
            item = QListWidgetItem(preview)
            item.setData(Qt.ItemDataRole.UserRole, entry.text)
            item.setToolTip("Double-click to copy")
            self.recent_list.addItem(item)

    def _toggle(self) -> None:
        self.controller.toggle_dictation()

    def _poll_level(self) -> None:
        self.level_bar.setValue(int(self.controller.current_level() * 100))

    def _copy_last(self) -> None:
        text = self.last_text.toPlainText()
        if text.strip():
            self.controller.adapter.set_clipboard(text)
            self.status_label.setText("Copied to clipboard.")

    def _copy_recent_item(self, item: QListWidgetItem) -> None:
        text = item.data(Qt.ItemDataRole.UserRole)
        if text:
            self.controller.adapter.set_clipboard(text)
            self.status_label.setText("Copied to clipboard.")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        event.ignore()
        self.hide()
        log.debug("Main window hidden to tray")
