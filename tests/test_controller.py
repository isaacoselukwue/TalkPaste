"""Integration tests for the dictation controller pipeline.

Uses the in-memory FakeASRBackend/FakeAdapter from conftest so no model,
microphone or native input stack is required.
"""

from __future__ import annotations

import numpy as np
import pytest

import app.services.controller as controller_mod
from app.models import PasteMode, Settings
from app.services.controller import DictationController
from tests.conftest import FakeAdapter, FakeASRBackend


@pytest.fixture()
def patched_asr(monkeypatch):
    """Force the controller to use the fake backend."""

    holder = {}

    def factory(asr_settings, transcript="hello comma world period"):
        backend = FakeASRBackend(asr_settings, transcript=transcript)
        holder["backend"] = backend
        return backend

    monkeypatch.setattr(controller_mod, "create_asr_backend", lambda s: factory(s))
    return holder


def make_controller(settings, adapter, paths):
    return DictationController(settings, paths=paths, adapter=adapter)


def test_process_audio_formats_transcript(paths, patched_asr):
    settings = Settings()
    adapter = FakeAdapter(settings)
    ctrl = make_controller(settings, adapter, paths)
    audio = np.zeros(16000, dtype=np.float32)

    result = ctrl.process_audio(audio, sample_rate=16000)
    assert result.ok
    # Fake transcript "hello comma world period" -> formatted.
    assert result.final_text == "Hello, world."
    assert result.raw_text == "hello comma world period"
    assert result.transcription.backend == "fake"


def test_transcribe_file(paths, patched_asr, sample_wav):
    settings = Settings()
    ctrl = DictationController(settings, paths=paths, adapter=FakeAdapter(settings))
    result = ctrl.transcribe_file(sample_wav)
    assert result.ok
    assert result.final_text == "Hello, world."


def test_insert_text_paste_mode(paths, patched_asr):
    settings = Settings()
    adapter = FakeAdapter(settings, can_inject=True)
    ctrl = make_controller(settings, adapter, paths)
    paste = ctrl._insert_text("some text")
    assert paste.injected
    assert adapter.inserted == ["some text"]
    assert adapter.clipboard == "some text"


def test_insert_text_copy_only_mode(paths, patched_asr):
    settings = Settings()
    settings.paste.mode = PasteMode.COPY_ONLY
    adapter = FakeAdapter(settings, can_inject=True)
    ctrl = make_controller(settings, adapter, paths)
    paste = ctrl._insert_text("copied text")
    assert not paste.injected
    assert paste.needs_manual_paste
    assert adapter.clipboard == "copied text"


def test_fallback_when_injection_unavailable(paths, patched_asr):
    settings = Settings()
    adapter = FakeAdapter(settings, can_inject=False)
    ctrl = make_controller(settings, adapter, paths)
    paste = ctrl._insert_text("text")
    assert not paste.injected
    assert paste.needs_manual_paste
    assert paste.copied


def test_history_recorded(paths, patched_asr):
    settings = Settings()
    ctrl = DictationController(settings, paths=paths, adapter=FakeAdapter(settings))
    result = ctrl.process_audio(np.zeros(16000, dtype=np.float32))
    ctrl._record_history(result)
    entries = ctrl.history.all()
    assert len(entries) == 1
    assert entries[0].text == "Hello, world."
    assert entries[0].backend == "fake"


def test_dictionary_and_snippets_feed_formatter(paths, patched_asr):
    settings = Settings()
    ctrl = DictationController(settings, paths=paths, adapter=FakeAdapter(settings))
    ctrl.dictionary.add("world", "World")  # persisted
    result = ctrl.process_audio(np.zeros(8000, dtype=np.float32))
    # "world" replaced by dictionary before capitalisation keeps "World".
    assert "World" in result.final_text


def test_state_callbacks_fire(paths, patched_asr):
    settings = Settings()
    states = []
    ctrl = DictationController(
        settings, paths=paths, adapter=FakeAdapter(settings),
        on_state=lambda s, m: states.append(s),
    )
    ctrl.start()
    from app.models import AppState
    assert AppState.IDLE in states
    ctrl.stop()


def test_empty_audio_returns_idle(paths, patched_asr):
    settings = Settings()
    ctrl = DictationController(settings, paths=paths, adapter=FakeAdapter(settings))
    # end_dictation with no recording is a no-op (does not raise).
    ctrl.end_dictation()
