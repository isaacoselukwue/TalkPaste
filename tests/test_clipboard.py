"""Tests for clipboard backend selection and behaviour."""

from __future__ import annotations

import app.services.clipboard as clip
from app.services.clipboard import (
    NullClipboardBackend,
    get_clipboard_backend,
    reset_clipboard_backend,
)


def setup_function():
    reset_clipboard_backend()


def teardown_function():
    reset_clipboard_backend()


def test_null_backend_reports_unavailable():
    backend = NullClipboardBackend()
    assert backend.available is False
    assert backend.get_text() is None
    assert backend.set_text("x") is False


def test_selection_falls_back_to_null(monkeypatch):
    # Force every candidate to be unavailable.
    class Dummy:
        name = "dummy"
        available = False

        def get_text(self):
            return None

        def set_text(self, t):
            return False

    monkeypatch.setattr(clip, "_candidate_backends", lambda: [Dummy()])
    backend = get_clipboard_backend(force_refresh=True)
    assert isinstance(backend, NullClipboardBackend)


def test_selection_picks_first_available(monkeypatch):
    class Available:
        name = "mem"

        def __init__(self):
            self._value = None

        @property
        def available(self):
            return True

        def get_text(self):
            return self._value

        def set_text(self, t):
            self._value = t
            return True

    inst = Available()
    monkeypatch.setattr(clip, "_candidate_backends", lambda: [inst])
    backend = get_clipboard_backend(force_refresh=True)
    assert backend is inst
    assert backend.set_text("hi") is True
    assert backend.get_text() == "hi"


def test_caching(monkeypatch):
    calls = {"n": 0}

    class Available:
        name = "mem"
        available = True

        def get_text(self):
            return None

        def set_text(self, t):
            return True

    def candidates():
        calls["n"] += 1
        return [Available()]

    monkeypatch.setattr(clip, "_candidate_backends", candidates)
    get_clipboard_backend(force_refresh=True)
    get_clipboard_backend()  # cached; should not re-enumerate
    assert calls["n"] == 1
