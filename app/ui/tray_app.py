"""System-tray application: the GUI shell around the dictation controller."""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from app import APP_NAME
from app.config import Paths, get_paths, load_settings
from app.logging_setup import get_logger
from app.models import AppState, Settings
from app.services.controller import DictationController, DictationResult
from app.ui.settings_window import SettingsWindow
from app.ui.status_popup import StatusPopup

log = get_logger("ui.tray")

_STATE_COLOURS: dict[AppState, str] = {
    AppState.IDLE: "#9aa0a6",
    AppState.LISTENING: "#ea4335",
    AppState.PROCESSING: "#fbbc05",
    AppState.READY: "#34a853",
    AppState.ERROR: "#d93025",
}


def _make_icon(colour: str) -> QIcon:
    """Render a simple coloured-dot tray icon (no image assets needed)."""

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(8, 8, 48, 48)
    painter.end()
    return QIcon(pixmap)


class TrayApp(QObject):
    """Owns the tray icon, menu, status popup and dictation controller."""

    _state_changed = Signal(object, str)
    _result_ready = Signal(object)

    def __init__(self, settings: Settings, paths: Paths, app: QApplication) -> None:
        super().__init__()
        self.settings = settings
        self.paths = paths
        self.app = app
        self._icons = {state: _make_icon(colour) for state, colour in _STATE_COLOURS.items()}

        self.controller = DictationController(
            settings,
            paths=paths,
            on_state=self._state_changed.emit,
            on_result=self._result_ready.emit,
        )

        self.popup = StatusPopup()
        self.tray = QSystemTrayIcon(self._icons[AppState.IDLE])
        self.tray.setToolTip(f"{APP_NAME} — idle")
        self._status_action: QAction | None = None
        self._settings_window: SettingsWindow | None = None

        self._build_menu()
        self.tray.activated.connect(self._on_activated)

        # Qt queues cross-thread signal emissions onto the GUI thread for us.
        self._state_changed.connect(self._on_state, Qt.ConnectionType.QueuedConnection)
        self._result_ready.connect(self._on_result, Qt.ConnectionType.QueuedConnection)


    def start(self) -> None:
        self.tray.show()
        self.controller.start()
        QTimer.singleShot(0, self.controller.preload_model)

    def quit(self) -> None:
        log.info("Quit requested")
        self.controller.close()
        self.popup.close()
        self.tray.hide()
        self.app.quit()


    def _build_menu(self) -> None:
        menu = QMenu()

        self._status_action = menu.addAction("Idle")
        self._status_action.setEnabled(False)
        menu.addSeparator()

        toggle = menu.addAction("Start / stop dictation")
        toggle.triggered.connect(self.controller.toggle_dictation)
        cancel = menu.addAction("Cancel")
        cancel.triggered.connect(self.controller.cancel)
        menu.addSeparator()

        self._recent_menu = menu.addMenu("Recent transcripts")
        menu.addSeparator()

        # Populate the submenu when the tray menu opens: an empty submenu can't
        # be opened, so its own aboutToShow would never fire.
        menu.aboutToShow.connect(self._populate_recent)
        self._populate_recent()

        settings_action = menu.addAction("Settings…")
        settings_action.triggered.connect(self._open_settings)
        diag_action = menu.addAction("Diagnostics…")
        diag_action.triggered.connect(self._show_diagnostics)
        menu.addSeparator()

        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.quit)

        self.tray.setContextMenu(menu)

    def _populate_recent(self) -> None:
        self._recent_menu.clear()
        entries = self.controller.history.recent(10)
        if not entries:
            empty = self._recent_menu.addAction("(no transcripts yet)")
            empty.setEnabled(False)
            return
        for entry in entries:
            preview = entry.text.replace("\n", " ")
            if len(preview) > 60:
                preview = preview[:57] + "…"
            action = self._recent_menu.addAction(preview)
            action.triggered.connect(lambda _=False, t=entry.text: self._copy_text(t))

    def _copy_text(self, text: str) -> None:
        self.controller.adapter.set_clipboard(text)
        self._notify("Copied", "Transcript copied to the clipboard.")


    def _on_state(self, state: AppState, message: str) -> None:
        self.tray.setIcon(self._icons.get(state, self._icons[AppState.IDLE]))
        self.tray.setToolTip(f"{APP_NAME} — {message or state.value}")
        if self._status_action is not None:
            self._status_action.setText(message or state.value.title())
        self.popup.show_for_state(state, message)
        if state is AppState.ERROR:
            self._notify(f"{APP_NAME} error", message, icon=QSystemTrayIcon.MessageIcon.Warning)

    def _on_result(self, result: DictationResult) -> None:
        if result.ok and result.paste and result.paste.needs_manual_paste:
            self._notify("Copied — paste manually", result.final_text[:80])


    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.controller.toggle_dictation()

    def _open_settings(self) -> None:
        if self._settings_window is None:
            self._settings_window = SettingsWindow(self.settings, self.paths)
            self._settings_window.settings_saved.connect(self._on_settings_saved)
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _on_settings_saved(self, settings: Settings) -> None:
        log.info("Applying updated settings; restarting hotkeys")
        self.settings = settings
        self.controller.settings = settings
        self.controller.stop()
        self.controller.start()

    def _show_diagnostics(self) -> None:
        caps = self.controller.adapter.detect_capabilities()
        lines = [
            f"Platform: {caps.kind.value}  (session: {caps.session_type or 'n/a'})",
            f"Hotkeys: {'yes' if caps.hotkey_available else 'no'} — {caps.hotkey_method}",
            f"Paste: {'yes' if caps.paste_available else 'no'} — {caps.paste_method}",
            f"Clipboard: {'yes' if caps.clipboard_available else 'no'} — {caps.clipboard_method}",
        ]
        if caps.notes:
            lines.append("")
            lines += [f"• {n}" for n in caps.notes]
        QMessageBox.information(None, f"{APP_NAME} diagnostics", "\n".join(lines))

    def _notify(self, title: str, message: str,
                icon: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information) -> None:
        if self.settings.general.notifications and self.tray.supportsMessages():
            self.tray.showMessage(title, message, icon, 3000)


def run_tray(settings: Settings | None = None, paths: Paths | None = None,
             argv: list[str] | None = None) -> int:
    """Create the QApplication and run the tray app event loop."""

    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    paths = (paths or get_paths()).ensure()
    settings = settings or load_settings(paths)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("No system tray is available in this session.")
        QMessageBox.critical(
            None, APP_NAME,
            "No system tray is available in this desktop session.\n"
            "Use the headless CLI instead:  python -m app.cli run-headless",
        )
        return 1

    tray = TrayApp(settings, paths, app)
    tray.start()
    log.info("%s tray app started", APP_NAME)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_tray())
