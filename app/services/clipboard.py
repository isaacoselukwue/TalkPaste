"""Clipboard backends with graceful fallback across GUI and headless contexts.

The tray app uses Qt's clipboard (``QGuiApplication.clipboard()``) as the
blueprint requires, but the headless CLI has no ``QApplication``, and some
paste flows must work before Qt is up. We therefore define a small
:class:`ClipboardBackend` interface with several implementations and a
selector that prefers Qt when a Qt app already exists, otherwise falls back to
native OS tools (``xclip``/``xsel``/``wl-copy`` on Linux, ``clip``/PowerShell
on Windows, ``pbcopy`` on macOS).

The selected backend is cached; call :func:`reset_clipboard_backend` in tests.
"""

from __future__ import annotations

import abc
import shutil
import subprocess
import sys

from app.logging_setup import get_logger

log = get_logger("clipboard")


class ClipboardBackend(abc.ABC):
    """Abstract get/set-text clipboard access."""

    name: str = "base"

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this backend can be used right now."""

    @abc.abstractmethod
    def get_text(self) -> str | None:
        """Return current clipboard text (``None`` if empty/unavailable)."""

    @abc.abstractmethod
    def set_text(self, text: str) -> bool:
        """Write ``text`` to the clipboard; return success."""


class QtClipboardBackend(ClipboardBackend):
    """Uses ``QGuiApplication.clipboard()``. Requires a running Qt app."""

    name = "qt"

    @property
    def available(self) -> bool:
        try:
            from PySide6.QtWidgets import QApplication

            return QApplication.instance() is not None
        except Exception:
            return False

    def _clipboard(self):
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        return app.clipboard() if app is not None else None

    def get_text(self) -> str | None:
        cb = self._clipboard()
        if cb is None:
            return None
        text = cb.text()
        return text if text else None

    def set_text(self, text: str) -> bool:
        cb = self._clipboard()
        if cb is None:
            return False
        cb.setText(text)
        return True


class _SubprocessClipboardBackend(ClipboardBackend):
    """Base for clipboard backends that shell out to an external tool."""

    #: command + args to read text (reads from the tool's stdout)
    _get_cmd: list[str] = []
    #: command + args to write text (text is piped to stdin)
    _set_cmd: list[str] = []
    _tool: str = ""

    @property
    def available(self) -> bool:
        return bool(self._tool) and shutil.which(self._tool) is not None

    def get_text(self) -> str | None:
        if not self.available or not self._get_cmd:
            return None
        try:
            out = subprocess.run(
                self._get_cmd,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if out.returncode != 0:
                return None
            text = out.stdout.decode("utf-8", errors="replace")
            return text if text else None
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            log.debug("Clipboard get via %s failed: %s", self._tool, exc)
            return None

    def set_text(self, text: str) -> bool:
        if not self.available or not self._set_cmd:
            return False
        try:
            proc = subprocess.run(
                self._set_cmd,
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=5,
                check=False,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            log.debug("Clipboard set via %s failed: %s", self._tool, exc)
            return False


class XclipClipboardBackend(_SubprocessClipboardBackend):
    name = "xclip"
    _tool = "xclip"
    _get_cmd = ["xclip", "-selection", "clipboard", "-o"]
    _set_cmd = ["xclip", "-selection", "clipboard", "-i"]


class XselClipboardBackend(_SubprocessClipboardBackend):
    name = "xsel"
    _tool = "xsel"
    _get_cmd = ["xsel", "--clipboard", "--output"]
    _set_cmd = ["xsel", "--clipboard", "--input"]


class WlClipboardBackend(_SubprocessClipboardBackend):
    name = "wl-clipboard"
    _tool = "wl-copy"

    @property
    def available(self) -> bool:
        return shutil.which("wl-copy") is not None

    def get_text(self) -> str | None:
        if shutil.which("wl-paste") is None:
            return None
        try:
            out = subprocess.run(
                ["wl-paste", "--no-newline"], capture_output=True, timeout=5, check=False
            )
            if out.returncode != 0:
                return None
            text = out.stdout.decode("utf-8", errors="replace")
            return text if text else None
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return None

    def set_text(self, text: str) -> bool:
        try:
            proc = subprocess.run(
                ["wl-copy"], input=text.encode("utf-8"), capture_output=True, timeout=5, check=False
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return False


class MacClipboardBackend(_SubprocessClipboardBackend):
    name = "pbcopy"
    _tool = "pbcopy"
    _get_cmd = ["pbpaste"]
    _set_cmd = ["pbcopy"]


class WindowsClipboardBackend(ClipboardBackend):
    """Windows clipboard via ``clip.exe`` (set) and PowerShell (get)."""

    name = "windows-native"

    @property
    def available(self) -> bool:
        return sys.platform.startswith("win")

    def get_text(self) -> str | None:
        if not self.available:
            return None
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if out.returncode != 0:
                return None
            text = out.stdout.decode("utf-8", errors="replace").rstrip("\r\n")
            return text if text else None
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return None

    def set_text(self, text: str) -> bool:
        if not self.available:
            return False
        try:
            proc = subprocess.run(
                ["clip"], input=text.encode("utf-16-le"), capture_output=True, timeout=5, check=False
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return False


class NullClipboardBackend(ClipboardBackend):
    """Last-resort backend that reports unavailable and no-ops."""

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def get_text(self) -> str | None:
        return None

    def set_text(self, text: str) -> bool:
        return False


_cached_backend: ClipboardBackend | None = None


def _candidate_backends() -> list[ClipboardBackend]:
    """Ordered candidates for the current platform (Qt first if running)."""

    candidates: list[ClipboardBackend] = [QtClipboardBackend()]
    if sys.platform.startswith("win"):
        candidates.append(WindowsClipboardBackend())
    elif sys.platform == "darwin":
        candidates.append(MacClipboardBackend())
    else:  # linux / other unix
        candidates.extend(
            [WlClipboardBackend(), XclipClipboardBackend(), XselClipboardBackend()]
        )
    return candidates


def get_clipboard_backend(force_refresh: bool = False) -> ClipboardBackend:
    """Return the best available clipboard backend (cached).

    Qt is preferred whenever a ``QApplication`` exists; otherwise the first
    available native tool wins. Falls back to :class:`NullClipboardBackend`.
    """

    global _cached_backend
    if _cached_backend is not None and not force_refresh:
        # Qt may have started after first resolution; re-check for an upgrade.
        qt = QtClipboardBackend()
        if qt.available and _cached_backend.name != "qt":
            _cached_backend = qt
        return _cached_backend

    for backend in _candidate_backends():
        if backend.available:
            _cached_backend = backend
            log.debug("Selected clipboard backend: %s", backend.name)
            return backend

    _cached_backend = NullClipboardBackend()
    log.warning("No clipboard backend available")
    return _cached_backend


def reset_clipboard_backend() -> None:
    """Clear the cached backend (used by tests)."""

    global _cached_backend
    _cached_backend = None
