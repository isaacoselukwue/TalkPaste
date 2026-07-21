#!/usr/bin/env python3
"""Render TalkPaste's UI to real PNG screenshots (no display server needed).

Runs Qt under the 'offscreen' platform, populates each surface with sample
data, and saves PNGs into assets/. Re-run any time the UI changes:

    python scripts/capture_screenshots.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["TALKPASTE_DATA_DIR"] = tempfile.mkdtemp(prefix="talkpaste-shots-")

from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from app.config import get_paths, load_settings  # noqa: E402
from app.models import AppState  # noqa: E402
from app.services.controller import DictationController  # noqa: E402
from app.services.history_store import HistoryEntry, HistoryStore  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"

SAMPLES = [
    "Hey team, quick update — the new parser is merged and the tests are all "
    "green. I'll cut a release this afternoon.",
    "Remember to buy milk, eggs and coffee on the way home.",
    "The stand-up is moved to 3 PM on Thursday; please update your calendars.",
    "Add a retry with exponential back-off around the upload call, and log the "
    "attempt count.",
]


def _seed_history() -> None:
    store = HistoryStore(get_paths().ensure().history_file, max_entries=500)
    for text in SAMPLES:
        store.add(HistoryEntry(text=text, backend="faster_whisper", model="base.en"))


def _save(widget, name: str, w: int, h: int) -> None:
    widget.resize(w, h)
    widget.show()
    QApplication.processEvents()
    QApplication.processEvents()
    path = ASSETS / name
    widget.grab().save(str(path))
    widget.hide()
    print(f"  wrote {path.relative_to(ASSETS.parent)}")


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    app = QApplication([])  # noqa: F841 - kept alive for the widgets
    _seed_history()

    settings = load_settings(get_paths())
    controller = DictationController(settings)

    from app.ui.main_window import MainWindow
    from app.ui.settings_window import SettingsWindow
    from app.ui.status_popup import StatusPopup

    print("Rendering screenshots:")

    # Main window — idle, populated.
    window = MainWindow(controller)
    window.last_text.setPlainText(SAMPLES[0])
    window.refresh_recent()
    window.update_state(AppState.IDLE, "Ready")
    _save(window, "screenshot-main.png", 460, 620)

    # Main window — listening, with an active level meter.
    window.update_state(AppState.LISTENING, "Listening… speak now")
    window.level_bar.setValue(64)
    _save(window, "screenshot-listening.png", 460, 620)

    # Settings — Model tab.
    settings_win = SettingsWindow(settings, get_paths())
    tabs = settings_win.findChild(QTabWidget)
    tabs.setCurrentIndex(0)
    _save(settings_win, "screenshot-settings.png", 560, 640)

    # Settings — Shortcuts tab.
    tabs.setCurrentIndex(1)
    _save(settings_win, "screenshot-shortcuts.png", 560, 640)

    # Settings — Diagnostics tab.
    tabs.setCurrentIndex(5)
    settings_win._refresh_diagnostics()
    _save(settings_win, "screenshot-diagnostics.png", 560, 640)

    # Status popup — listening.
    popup = StatusPopup()
    popup.set_state(AppState.LISTENING, "Listening…")
    popup.set_level(0.6)
    _save(popup, "screenshot-popup.png", 220, 44)

    print("Done.")


if __name__ == "__main__":
    main()
