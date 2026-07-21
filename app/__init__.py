"""TalkPaste — a fully-local, cross-platform dictation desktop app.

Press-and-hold a global shortcut to dictate, release to stop, transcribe
locally with a Whisper-family model, clean up the text deterministically,
optionally rewrite it with a local LLM, then insert it into the currently
focused application.

The package is organised into three layers:

* :mod:`app.services` — audio capture, ASR backends, deterministic text
  processing, optional rewrite and the persistence stores. Platform agnostic.
* :mod:`app.platform` — the three platform adapters (Windows, Linux/X11,
  Linux/Wayland) that own global-hotkey capture and text injection.
* :mod:`app.ui` — the PySide6 tray app, settings window and status popup.

The CLI (:mod:`app.cli`) and headless controller work without importing any
UI code, which keeps the CLI milestone independent of Qt.
"""

from __future__ import annotations

__all__ = ["__version__", "APP_NAME", "APP_SLUG", "APP_AUTHOR"]

__version__ = "0.1.0"

#: Human-facing product name.
APP_NAME = "TalkPaste"

#: Filesystem-safe slug used for config/data directories and executables.
APP_SLUG = "talkpaste"

#: Author/organisation used by :mod:`platformdirs` to locate per-user dirs.
APP_AUTHOR = "TalkPaste"
