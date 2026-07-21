"""GUI smoke tests — skipped entirely when PySide6 is not installed.

These run headless via the Qt 'offscreen' platform plugin and verify the UI
classes construct, load settings and wire up without a display server.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.requires_qt

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.models import AppState, HotkeyMode, ModelProfile, Settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_status_popup(qapp):
    from app.ui.status_popup import StatusPopup

    popup = StatusPopup()
    popup.set_state(AppState.LISTENING, "Listening")
    popup.set_level(0.7)
    popup.show_for_state(AppState.PROCESSING)
    popup.show_for_state(AppState.IDLE)  # schedules hide, must not raise


def test_settings_window_roundtrip(qapp, data_dir):
    from app.config import get_paths
    from app.ui.settings_window import SettingsWindow

    settings = Settings()
    settings.asr.profile = ModelProfile.ACCURATE
    settings.hotkeys.mode = HotkeyMode.HANDS_FREE_TOGGLE
    win = SettingsWindow(settings, get_paths().ensure())

    assert win.ptt_edit.keySequence().toString() == "Ctrl+Alt+Space"

    # Change a value in the widget and collect it back; enums must round-trip
    # to real members (not bare strings) despite Qt's str-enum coercion.
    win.british.setChecked(False)
    win._collect_from_widgets()
    assert settings.formatting.british_english is False
    assert settings.asr.profile is ModelProfile.ACCURATE
    assert settings.asr.resolved_model() == "small.en"


def test_settings_apply_persists(qapp, data_dir):
    from app.config import get_paths, load_settings
    from app.ui.settings_window import SettingsWindow

    settings = Settings()
    paths = get_paths().ensure()
    win = SettingsWindow(settings, paths)
    saved = {}
    win.settings_saved.connect(lambda s: saved.setdefault("hit", True))
    win.threads_spin.setValue(7)
    win._apply()

    assert saved.get("hit") is True
    assert load_settings(paths).asr.cpu_threads == 7


def test_mapping_editor(qapp, data_dir):
    from app.config import get_paths
    from app.services.dictionary_store import DictionaryStore
    from app.ui.settings_window import MappingEditorDialog

    store = DictionaryStore(get_paths().ensure().dictionary_file)
    dialog = MappingEditorDialog(store, "Dictionary")
    dialog._append_row("github", "GitHub")
    dialog._save()
    assert DictionaryStore(get_paths().dictionary_file).get("github") == "GitHub"


def test_tray_app_builds(qapp, data_dir):
    from app.config import get_paths
    from app.ui.tray_app import TrayApp, _make_icon

    assert not _make_icon("#34a853").isNull()
    tray = TrayApp(Settings(), get_paths().ensure(), qapp)
    assert tray.tray.contextMenu() is not None
    assert len(tray.tray.contextMenu().actions()) >= 6
    # Simulate a controller state change reaching the GUI slot directly.
    tray._on_state(AppState.PROCESSING, "Transcribing…")
    assert "Transcribing" in tray.tray.toolTip()
