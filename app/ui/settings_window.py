"""Settings dialog covering every configurable surface of TalkPaste."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import Paths, get_paths, save_settings
from app.logging_setup import get_logger
from app.models import (
    ASRBackendKind,
    HotkeyMode,
    LanguageMode,
    ModelProfile,
    PasteMode,
    Settings,
)
from app.services.dictionary_store import DictionaryStore, MappingStore
from app.services.snippets_store import SnippetStore

log = get_logger("ui.settings")


class MappingEditorDialog(QDialog):
    """Two-column (trigger → replacement) editor for a :class:`MappingStore`."""

    def __init__(self, store: MappingStore, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle(title)
        self.resize(460, 360)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Trigger", "Replacement"])
        self.table.horizontalHeader().setStretchLastSection(True)

        for key, value in sorted(store.entries.items()):
            self._append_row(key, value)

        add_btn = QPushButton("Add row", self)
        add_btn.clicked.connect(lambda: self._append_row("", ""))
        del_btn = QPushButton("Remove selected", self)
        del_btn.clicked.connect(self._remove_selected)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(add_btn)
        top.addWidget(del_btn)
        top.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addWidget(buttons)

    def _append_row(self, key: str, value: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(key))
        self.table.setItem(row, 1, QTableWidgetItem(value))

    def _remove_selected(self) -> None:
        for index in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(index)

    def _save(self) -> None:
        entries: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            key_item = self.table.item(row, 0)
            val_item = self.table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            value = val_item.text() if val_item else ""
            if key:
                entries[key] = value
        self.store.clear(save=False)
        for key, value in entries.items():
            self.store.add(key, value, save=False)
        self.store.save()
        log.info("Saved %d %s entries", len(entries), self.store.label)
        self.accept()


class SettingsWindow(QDialog):
    """The main settings dialog. Emits :attr:`settings_saved` on Apply/OK."""

    settings_saved = Signal(object)

    def __init__(self, settings: Settings, paths: Paths | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths or get_paths()
        self.setWindowTitle("TalkPaste settings")
        self.resize(560, 640)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_model_tab(), "Model")
        tabs.addTab(self._build_shortcuts_tab(), "Shortcuts")
        tabs.addTab(self._build_paste_tab(), "Paste")
        tabs.addTab(self._build_text_tab(), "Text")
        tabs.addTab(self._build_audio_tab(), "Audio")
        tabs.addTab(self._build_diagnostics_tab(), "Diagnostics")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).clicked.connect(self._on_ok)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

        self._load_into_widgets()


    def _build_model_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.profile_combo = QComboBox()
        for profile in ModelProfile:
            self.profile_combo.addItem(profile.value.title(), profile)
        form.addRow("Profile", self.profile_combo)

        self.backend_combo = QComboBox()
        self.backend_combo.addItem("faster-whisper", ASRBackendKind.FASTER_WHISPER)
        self.backend_combo.addItem("whisper.cpp (low RAM)", ASRBackendKind.WHISPER_CPP)
        form.addRow("Backend", self.backend_combo)

        self.language_combo = QComboBox()
        self.language_combo.addItem("English", LanguageMode.ENGLISH)
        self.language_combo.addItem("Multilingual", LanguageMode.MULTILINGUAL)
        form.addRow("Language mode", self.language_combo)

        self.language_code = QLineEdit()
        self.language_code.setPlaceholderText("auto-detect (e.g. fr, de) — multilingual only")
        form.addRow("Language code", self.language_code)

        self.model_name = QLineEdit()
        self.model_name.setPlaceholderText("used only for the Custom profile, e.g. distil-large-v3")
        form.addRow("Custom model", self.model_name)

        self.compute_combo = QComboBox()
        self.compute_combo.addItems(["int8", "int8_float16", "float16", "float32"])
        form.addRow("Compute type", self.compute_combo)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 64)
        form.addRow("CPU threads", self.threads_spin)

        self.beam_spin = QSpinBox()
        self.beam_spin.setRange(1, 10)
        form.addRow("Beam size", self.beam_spin)

        rewrite_group = QGroupBox("Local rewrite (optional, off by default)")
        rform = QFormLayout(rewrite_group)
        self.rewrite_enabled = QCheckBox("Enable grammar/punctuation rewrite")
        rform.addRow(self.rewrite_enabled)
        path_row = QHBoxLayout()
        self.rewrite_path = QLineEdit()
        self.rewrite_path.setPlaceholderText("path to a GGUF model (0.6B–1.7B)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_rewrite_model)
        path_row.addWidget(self.rewrite_path)
        path_row.addWidget(browse)
        rform.addRow("Model file", path_row)
        self.rewrite_timeout = QDoubleSpinBox()
        self.rewrite_timeout.setRange(0.0, 60.0)
        self.rewrite_timeout.setSuffix(" s")
        rform.addRow("Timeout", self.rewrite_timeout)
        form.addRow(rewrite_group)

        return w

    def _build_shortcuts_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Push to talk (hold)", HotkeyMode.PUSH_TO_TALK)
        self.mode_combo.addItem("Hands-free (toggle)", HotkeyMode.HANDS_FREE_TOGGLE)
        form.addRow("Trigger mode", self.mode_combo)

        self.ptt_edit = QKeySequenceEdit()
        form.addRow("Push-to-talk", self.ptt_edit)
        self.toggle_edit = QKeySequenceEdit()
        form.addRow("Hands-free toggle", self.toggle_edit)
        self.cancel_edit = QKeySequenceEdit()
        form.addRow("Cancel", self.cancel_edit)

        hint = QLabel("Avoid F12. Defaults: Ctrl+Alt+Space (hold), "
                      "Ctrl+Alt+Shift+Space (toggle), Esc (cancel).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        form.addRow(hint)
        return w

    def _build_paste_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        self.paste_mode = QComboBox()
        self.paste_mode.addItem("Paste into focused app", PasteMode.PASTE)
        self.paste_mode.addItem("Copy only (paste manually)", PasteMode.COPY_ONLY)
        form.addRow("Behaviour", self.paste_mode)

        self.restore_clip = QCheckBox("Restore previous clipboard after paste")
        form.addRow(self.restore_clip)

        self.paste_delay = QSpinBox()
        self.paste_delay.setRange(0, 2000)
        self.paste_delay.setSuffix(" ms")
        form.addRow("Delay before paste", self.paste_delay)

        self.restore_delay = QSpinBox()
        self.restore_delay.setRange(0, 5000)
        self.restore_delay.setSuffix(" ms")
        form.addRow("Delay before restore", self.restore_delay)

        self.allow_ydotool = QCheckBox("Allow ydotool on Wayland (advanced, opt-in)")
        form.addRow(self.allow_ydotool)
        return w

    def _build_text_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        form = QFormLayout()

        self.remove_fillers = QCheckBox("Remove fillers (um, uh, …)")
        self.british = QCheckBox("British English spelling")
        self.commands = QCheckBox("Spoken punctuation/formatting commands")
        self.snippets_on = QCheckBox("Expand snippets")
        self.dictionary_on = QCheckBox("Apply custom dictionary")
        self.developer = QCheckBox("Developer mode (kebab/constant/pascal case, CLI flags)")
        self.auto_cap = QCheckBox("Auto-capitalise sentences")
        for cb in (self.remove_fillers, self.british, self.commands, self.snippets_on,
                   self.dictionary_on, self.developer, self.auto_cap):
            form.addRow(cb)
        layout.addLayout(form)

        row = QHBoxLayout()
        edit_dict = QPushButton("Edit dictionary…")
        edit_dict.clicked.connect(self._edit_dictionary)
        edit_snip = QPushButton("Edit snippets…")
        edit_snip.clicked.connect(self._edit_snippets)
        row.addWidget(edit_dict)
        row.addWidget(edit_snip)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return w

    def _build_audio_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.device_combo = QComboBox()
        self.device_combo.addItem("System default", None)
        self._populate_devices()
        form.addRow("Input device", self.device_combo)

        refresh = QPushButton("Refresh devices")
        refresh.clicked.connect(self._populate_devices)
        form.addRow(refresh)
        return w

    def _build_diagnostics_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.diag_text = QPlainTextEdit()
        self.diag_text.setReadOnly(True)
        recheck = QPushButton("Re-check platform capabilities")
        recheck.clicked.connect(self._refresh_diagnostics)
        layout.addWidget(recheck)
        layout.addWidget(self.diag_text)
        self._refresh_diagnostics()
        return w


    def _load_into_widgets(self) -> None:
        s = self.settings
        self._select_data(self.profile_combo, s.asr.profile)
        self._select_data(self.backend_combo, s.asr.backend)
        self._select_data(self.language_combo, s.asr.language_mode)
        self.language_code.setText(s.asr.language or "")
        self.model_name.setText(s.asr.model_name)
        self.compute_combo.setCurrentText(s.asr.compute_type)
        self.threads_spin.setValue(s.asr.cpu_threads)
        self.beam_spin.setValue(s.asr.beam_size)

        self.rewrite_enabled.setChecked(s.rewrite.enabled)
        self.rewrite_path.setText(s.rewrite.model_path or "")
        self.rewrite_timeout.setValue(s.rewrite.timeout_seconds)

        self._select_data(self.mode_combo, s.hotkeys.mode)
        self.ptt_edit.setKeySequence(QKeySequence.fromString(s.hotkeys.push_to_talk))
        self.toggle_edit.setKeySequence(QKeySequence.fromString(s.hotkeys.hands_free_toggle))
        self.cancel_edit.setKeySequence(QKeySequence.fromString(s.hotkeys.cancel))

        self._select_data(self.paste_mode, s.paste.mode)
        self.restore_clip.setChecked(s.paste.restore_clipboard)
        self.paste_delay.setValue(s.paste.paste_delay_ms)
        self.restore_delay.setValue(s.paste.restore_delay_ms)
        self.allow_ydotool.setChecked(s.paste.allow_ydotool)

        self.remove_fillers.setChecked(s.formatting.remove_fillers)
        self.british.setChecked(s.formatting.british_english)
        self.commands.setChecked(s.formatting.enable_commands)
        self.snippets_on.setChecked(s.formatting.enable_snippets)
        self.dictionary_on.setChecked(s.formatting.enable_dictionary)
        self.developer.setChecked(s.formatting.developer_mode)
        self.auto_cap.setChecked(s.formatting.auto_capitalize)

        if s.audio.device_index is not None:
            idx = self.device_combo.findData(s.audio.device_index)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)

    def _collect_from_widgets(self) -> None:
        s = self.settings
        # Enums subclass ``str``, so QComboBox.currentData() hands back the bare
        # string value — coerce each back to its enum member.
        s.asr.profile = ModelProfile(self.profile_combo.currentData())
        s.asr.backend = ASRBackendKind(self.backend_combo.currentData())
        s.asr.language_mode = LanguageMode(self.language_combo.currentData())
        s.asr.language = self.language_code.text().strip() or None
        s.asr.model_name = self.model_name.text().strip()
        s.asr.compute_type = self.compute_combo.currentText()
        s.asr.cpu_threads = self.threads_spin.value()
        s.asr.beam_size = self.beam_spin.value()

        s.rewrite.enabled = self.rewrite_enabled.isChecked()
        s.rewrite.model_path = self.rewrite_path.text().strip() or None
        s.rewrite.timeout_seconds = self.rewrite_timeout.value()

        s.hotkeys.mode = HotkeyMode(self.mode_combo.currentData())
        s.hotkeys.push_to_talk = self.ptt_edit.keySequence().toString() or s.hotkeys.push_to_talk
        s.hotkeys.hands_free_toggle = (
            self.toggle_edit.keySequence().toString() or s.hotkeys.hands_free_toggle
        )
        s.hotkeys.cancel = self.cancel_edit.keySequence().toString() or s.hotkeys.cancel

        s.paste.mode = PasteMode(self.paste_mode.currentData())
        s.paste.restore_clipboard = self.restore_clip.isChecked()
        s.paste.paste_delay_ms = self.paste_delay.value()
        s.paste.restore_delay_ms = self.restore_delay.value()
        s.paste.allow_ydotool = self.allow_ydotool.isChecked()

        s.formatting.remove_fillers = self.remove_fillers.isChecked()
        s.formatting.british_english = self.british.isChecked()
        s.formatting.enable_commands = self.commands.isChecked()
        s.formatting.enable_snippets = self.snippets_on.isChecked()
        s.formatting.enable_dictionary = self.dictionary_on.isChecked()
        s.formatting.developer_mode = self.developer.isChecked()
        s.formatting.auto_capitalize = self.auto_cap.isChecked()

        s.audio.device_index = self.device_combo.currentData()

    def _apply(self) -> None:
        self._collect_from_widgets()
        try:
            save_settings(self.settings, self.paths)
        except OSError as exc:
            QMessageBox.warning(self, "TalkPaste", f"Could not save settings:\n{exc}")
            return
        log.info("Settings saved")
        self.settings_saved.emit(self.settings)

    def _on_ok(self) -> None:
        self._apply()
        self.accept()


    def _populate_devices(self) -> None:
        current = self.device_combo.currentData()
        self.device_combo.clear()
        self.device_combo.addItem("System default", None)
        try:
            from app.services.audio_engine import list_audio_devices

            for dev in list_audio_devices():
                label = f"[{dev.index}] {dev.name}" + ("  (default)" if dev.is_default else "")
                self.device_combo.addItem(label, dev.index)
        except Exception as exc:  # noqa: BLE001 - audio stack may be absent
            self.device_combo.addItem(f"(devices unavailable: {exc})", None)
        idx = self.device_combo.findData(current)
        if idx >= 0:
            self.device_combo.setCurrentIndex(idx)

    def _refresh_diagnostics(self) -> None:
        try:
            from app.platform.base import create_platform_adapter, detect_platform_kind

            kind = detect_platform_kind()
            caps = create_platform_adapter(self.settings, kind).detect_capabilities()
            lines = [
                f"Platform      : {kind.value}",
                f"Session       : {caps.session_type or 'n/a'}",
                f"Hotkeys       : {'yes' if caps.hotkey_available else 'no'} ({caps.hotkey_method})",
                f"Paste         : {'yes' if caps.paste_available else 'no'} ({caps.paste_method})",
                f"Clipboard     : {'yes' if caps.clipboard_available else 'no'} ({caps.clipboard_method})",
            ]
            if caps.notes:
                lines.append("\nNotes:")
                lines += [f"  • {n}" for n in caps.notes]
            if caps.details:
                lines.append("\nDetails:")
                lines += [f"  {k} = {v}" for k, v in caps.details.items()]
            self.diag_text.setPlainText("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            self.diag_text.setPlainText(f"Diagnostics failed: {exc}")

    def _browse_rewrite_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select GGUF rewrite model", "", "GGUF models (*.gguf);;All files (*)"
        )
        if path:
            self.rewrite_path.setText(path)

    def _edit_dictionary(self) -> None:
        store = DictionaryStore(self.paths.dictionary_file)
        MappingEditorDialog(store, "Custom dictionary", self).exec()

    def _edit_snippets(self) -> None:
        store = SnippetStore(self.paths.snippets_file)
        MappingEditorDialog(store, "Snippets", self).exec()

    @staticmethod
    def _select_data(combo: QComboBox, value: object) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
