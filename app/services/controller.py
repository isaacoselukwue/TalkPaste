"""The dictation controller — orchestrates the end-to-end runtime pipeline.

This is the single brain shared by the headless CLI (``run-headless``) and the
tray app. It wires together the platform adapter (hotkeys + injection), audio
capture, the ASR backend, deterministic formatting, optional rewrite and the
history store, and exposes a small state machine.

Threading model
---------------
Hotkey callbacks arrive on the adapter's own thread. Recording start/stop is
cheap and handled inline. The heavy work (transcribe → format → rewrite →
insert) runs on a dedicated worker thread so the caller (and, in the GUI, the
Qt event loop) is never blocked. State changes are reported via the
``on_state`` callback; GUI consumers marshal that onto the UI thread.

The ASR model is lazy-loaded on first use (never at construction), satisfying
the "load models after the tray is up" requirement.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import Paths, get_paths
from app.logging_setup import get_logger
from app.models import AppState, HotkeyMode, PasteMode, Settings
from app.platform.base import (
    HotkeyAction,
    HotkeyEvent,
    PasteResult,
    PlatformAdapter,
    create_platform_adapter,
)
from app.services.asr_base import ASRBackend, ASRError, TranscriptionResult, create_asr_backend
from app.services.dictionary_store import DictionaryStore
from app.services.formatter import Formatter
from app.services.history_store import HistoryEntry, HistoryStore
from app.services.snippets_store import SnippetStore

log = get_logger("controller")

#: ``on_state(state, message)`` — message is a short human-readable detail.
StateCallback = Callable[[AppState, str], None]
#: ``on_result(result)`` — fired after a completed dictation.
ResultCallback = Callable[["DictationResult"], None]


@dataclass
class DictationResult:
    """The outcome of a single dictation cycle."""

    raw_text: str
    final_text: str
    transcription: TranscriptionResult | None = None
    paste: PasteResult | None = None
    rewritten: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class DictationController:
    """Coordinates hotkeys, capture, transcription, formatting and injection."""

    def __init__(
        self,
        settings: Settings,
        *,
        paths: Paths | None = None,
        adapter: PlatformAdapter | None = None,
        on_state: StateCallback | None = None,
        on_result: ResultCallback | None = None,
    ) -> None:
        self.settings = settings
        self.paths = (paths or get_paths()).ensure()
        self.on_state = on_state
        self.on_result = on_result

        self._adapter = adapter
        self._asr: ASRBackend | None = None
        self._audio = None  # lazy AudioEngine
        self._rewriter = None  # lazy RewriteEngine

        self.dictionary = DictionaryStore(self.paths.dictionary_file)
        self.snippets = SnippetStore(self.paths.snippets_file)
        self.history = HistoryStore(self.paths.history_file, settings.history.max_entries)

        self._state = AppState.IDLE
        self._lock = threading.RLock()
        self._recording = False
        self._worker: threading.Thread | None = None
        self._running = False


    @property
    def state(self) -> AppState:
        return self._state

    def current_level(self) -> float:
        """Current mic RMS level (0..1) while recording, else 0.0."""

        if self._audio is not None and self._recording:
            try:
                return float(self._audio.level)
            except Exception:
                return 0.0
        return 0.0

    @property
    def adapter(self) -> PlatformAdapter:
        if self._adapter is None:
            self._adapter = create_platform_adapter(self.settings)
        return self._adapter


    def start(self) -> None:
        """Register hotkeys and enter the idle state (does not load the model)."""

        if self._running:
            return
        self._running = True
        log.info("Starting dictation controller on %s", self.adapter.kind.value)
        try:
            self.adapter.start_hotkeys(self._on_hotkey)
        except Exception as exc:  # pragma: no cover - platform dependent
            log.error("Failed to register hotkeys: %s", exc)
            self._set_state(AppState.ERROR, f"Hotkey registration failed: {exc}")
            return
        self._set_state(AppState.IDLE, "Ready")

    def stop(self) -> None:
        """Stop hotkeys and release resources."""

        if not self._running:
            return
        self._running = False
        log.info("Stopping dictation controller")
        try:
            self.adapter.stop_hotkeys()
        except Exception:  # pragma: no cover
            pass
        if self._recording:
            self._safe_stop_audio()
        self.unload()

    def close(self) -> None:
        self.stop()
        try:
            self.adapter.close()
        except Exception:  # pragma: no cover
            pass

    def preload_model(self) -> None:
        """Warm the ASR model in a background thread (optional optimisation)."""

        def _load() -> None:
            try:
                self._ensure_asr().ensure_loaded()
                log.info("ASR model preloaded")
            except ASRError as exc:
                log.warning("Model preload failed: %s", exc)

        threading.Thread(target=_load, name="asr-preload", daemon=True).start()

    def unload(self) -> None:
        if self._asr is not None:
            self._asr.unload()
        if self._rewriter is not None:
            self._rewriter.unload()


    def _on_hotkey(self, action: HotkeyAction, event: HotkeyEvent) -> None:
        log.debug("Hotkey: %s %s", action.value, event.value)
        if action is HotkeyAction.CANCEL:
            self.cancel()
            return
        if action is HotkeyAction.TOGGLE:
            self.toggle_dictation()
            return
        if action is HotkeyAction.DICTATE:
            if self.settings.hotkeys.mode is HotkeyMode.HANDS_FREE_TOGGLE:
                # In toggle mode a DICTATE press acts as a toggle.
                if event is HotkeyEvent.PRESS:
                    self.toggle_dictation()
            else:
                if event is HotkeyEvent.PRESS:
                    self.begin_dictation()
                elif event is HotkeyEvent.RELEASE:
                    self.end_dictation()

    def toggle_dictation(self) -> None:
        with self._lock:
            if self._recording:
                self.end_dictation()
            else:
                self.begin_dictation()


    def begin_dictation(self) -> None:
        """Start capturing audio (idempotent)."""

        with self._lock:
            if self._recording:
                return
            try:
                engine = self._ensure_audio()
                engine.start()
                self._recording = True
                self._set_state(AppState.LISTENING, "Listening…")
            except Exception as exc:
                log.error("Could not start audio capture: %s", exc)
                self._set_state(AppState.ERROR, f"Microphone error: {exc}")

    def end_dictation(self) -> None:
        """Stop capturing and process the utterance on a worker thread."""

        with self._lock:
            if not self._recording:
                return
            self._recording = False
            try:
                audio = self._audio.stop() if self._audio else None
            except Exception as exc:
                log.error("Could not stop audio capture: %s", exc)
                self._set_state(AppState.ERROR, f"Microphone error: {exc}")
                return

        if audio is None or len(audio) == 0:
            log.info("No audio captured; nothing to transcribe")
            self._set_state(AppState.IDLE, "No audio captured")
            return

        self._set_state(AppState.PROCESSING, "Transcribing…")
        self._worker = threading.Thread(
            target=self._process_and_insert,
            args=(audio, self.settings.audio.sample_rate),
            name="dictation-worker",
            daemon=True,
        )
        self._worker.start()

    def cancel(self) -> None:
        """Abort an in-progress capture, discarding audio."""

        with self._lock:
            if self._recording:
                self._recording = False
                self._safe_stop_audio()
                log.info("Dictation cancelled by user")
                self._set_state(AppState.IDLE, "Cancelled")


    def _process_and_insert(self, audio, sample_rate: int) -> None:
        try:
            result = self.process_audio(audio, sample_rate)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Dictation processing failed")
            self._set_state(AppState.ERROR, f"Error: {exc}")
            return

        if result.error:
            self._set_state(AppState.ERROR, result.error)
            return

        if not result.final_text.strip():
            self._set_state(AppState.IDLE, "Nothing recognised")
            return

        paste = self._insert_text(result.final_text)
        result.paste = paste
        self._record_history(result)

        if self.on_result:
            try:
                self.on_result(result)
            except Exception:  # pragma: no cover
                log.exception("on_result callback raised")

        if paste.needs_manual_paste:
            self._set_state(AppState.READY, "Copied — press paste to insert")
        elif paste.injected:
            self._set_state(AppState.READY, "Inserted")
        else:
            self._set_state(AppState.ERROR, paste.error or "Insertion failed")

        # Return to idle shortly after signalling readiness.
        self._set_state(AppState.IDLE, "Ready")

    def process_audio(self, audio, sample_rate: int | None = None) -> DictationResult:
        """Transcribe → format → optional rewrite. Does NOT insert text.

        Public so the CLI can reuse it for file transcription.
        """

        sample_rate = sample_rate or self.settings.audio.sample_rate
        try:
            asr = self._ensure_asr()
            asr.ensure_loaded()
            transcription = asr.transcribe(audio, sample_rate=sample_rate)
        except ASRError as exc:
            log.error("Transcription failed: %s", exc)
            return DictationResult(raw_text="", final_text="", error=str(exc))

        raw = transcription.text
        formatter = self._build_formatter()
        final = formatter.format(raw)

        rewritten = False
        if self.settings.rewrite.enabled and final.strip():
            rewriter = self._ensure_rewriter()
            if rewriter.is_available():
                cleaned = rewriter.rewrite(final)
                rewritten = cleaned != final
                final = cleaned
            else:
                log.info("Rewrite enabled but unavailable; skipping")

        log.info(
            "Transcribed %.1fs of audio in %.2fs (RTF %.2f) via %s/%s",
            transcription.duration,
            transcription.inference_seconds,
            transcription.real_time_factor,
            transcription.backend,
            transcription.model,
        )
        return DictationResult(
            raw_text=raw,
            final_text=final,
            transcription=transcription,
            rewritten=rewritten,
        )

    def transcribe_file(self, path: str | Path) -> DictationResult:
        """Transcribe a WAV file and run the text pipeline (CLI helper)."""

        from app.services.asr_base import read_wav_mono_float32

        audio, sr = read_wav_mono_float32(path)
        return self.process_audio(audio, sample_rate=sr)


    def _insert_text(self, text: str) -> PasteResult:
        if self.settings.paste.mode is PasteMode.COPY_ONLY:
            copied = self.adapter.set_clipboard(text)
            return PasteResult(
                copied=copied,
                injected=False,
                method="copy-only (configured)",
                needs_manual_paste=True,
                error=None if copied else "Clipboard unavailable",
            )
        try:
            return self.adapter.insert_text(text)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("insert_text raised: %s", exc)
            return PasteResult(copied=False, injected=False, error=str(exc))

    def _record_history(self, result: DictationResult) -> None:
        if not self.settings.history.enabled:
            return
        t = result.transcription
        entry = HistoryEntry(
            text=result.final_text,
            raw_text=result.raw_text,
            duration=t.duration if t else 0.0,
            inference_seconds=t.inference_seconds if t else 0.0,
            backend=t.backend if t else "",
            model=t.model if t else "",
            rewritten=result.rewritten,
            paste_method=result.paste.method if result.paste else "",
        )
        try:
            self.history.add(entry)
        except Exception:  # pragma: no cover
            log.exception("Failed to record history")


    def _ensure_asr(self) -> ASRBackend:
        if self._asr is None:
            self._asr = create_asr_backend(self.settings.asr)
            log.info("ASR backend: %s", self._asr.describe())
        return self._asr

    def _ensure_audio(self):
        if self._audio is None:
            from app.services.audio_engine import AudioEngine

            self._audio = AudioEngine(self.settings.audio)
        return self._audio

    def _ensure_rewriter(self):
        if self._rewriter is None:
            from app.services.rewrite import RewriteEngine

            self._rewriter = RewriteEngine(self.settings.rewrite)
        return self._rewriter

    def _build_formatter(self) -> Formatter:
        # Reload stores each cycle so edits made in settings take effect live.
        self.dictionary.load()
        self.snippets.load()
        return Formatter(
            self.settings.formatting,
            dictionary=self.dictionary.as_mapping(),
            snippets=self.snippets.as_mapping(),
        )


    def _safe_stop_audio(self) -> None:
        try:
            if self._audio:
                self._audio.stop()
        except Exception:  # pragma: no cover
            pass

    def _set_state(self, state: AppState, message: str = "") -> None:
        self._state = state
        log.debug("State -> %s (%s)", state.value, message)
        if self.on_state:
            try:
                self.on_state(state, message)
            except Exception:  # pragma: no cover
                log.exception("on_state callback raised")
