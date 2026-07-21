"""Platform adapter interface, capability model and platform detection.

Three concrete adapters implement :class:`PlatformAdapter`:

* :class:`~app.platform.windows_adapter.WindowsAdapter` — Win32 low-level
  keyboard hook + ``SendInput``.
* :class:`~app.platform.linux_x11_adapter.LinuxX11Adapter` — ``pynput`` +
  ``xdotool``.
* :class:`~app.platform.linux_wayland_adapter.LinuxWaylandAdapter` — XDG
  Desktop Portal (Global Shortcuts / Remote Desktop), with copy-only fallback
  and optional ``ydotool``.

Each adapter is responsible for two capabilities that vary hugely by platform:

1. **Hotkey capture** — reporting press/release (and cancel/toggle) of the
   configured global shortcut via a callback.
2. **Text injection** — placing final text into the focused app, preferring a
   clipboard-preserving paste and gracefully degrading to copy-only.

The detection logic (:func:`detect_platform`) is pure and unit-testable via
the injectable ``env`` mapping.
"""

from __future__ import annotations

import abc
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from app.models import PlatformKind

if TYPE_CHECKING:
    from app.models import Settings


class HotkeyAction(str, Enum):
    """Logical dictation actions a hotkey maps to."""

    DICTATE = "dictate"   # push-to-talk key (has press + release)
    TOGGLE = "toggle"     # hands-free toggle
    CANCEL = "cancel"     # abort current capture


class HotkeyEvent(str, Enum):
    """Low-level key transitions reported by an adapter."""

    PRESS = "press"
    RELEASE = "release"
    TRIGGER = "trigger"   # a discrete activation (portal / toggle style)


#: Callback signature: ``(action, event) -> None``.
HotkeyCallback = Callable[[HotkeyAction, HotkeyEvent], None]


@dataclass
class PasteResult:
    """Outcome of an attempt to insert text into the focused app."""

    #: Text was placed on the clipboard successfully.
    copied: bool
    #: A paste keystroke was successfully injected (vs. copy-only fallback).
    injected: bool
    #: Human-readable method used, for logs/diagnostics.
    method: str = ""
    #: True when the user must paste manually (copy-only fallback path).
    needs_manual_paste: bool = False
    #: Populated on failure.
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.copied or self.injected


@dataclass
class PlatformCapabilities:
    """A snapshot of what the current platform/session can do.

    Rendered by ``diagnose-platform`` and the Wayland diagnostics panel.
    """

    kind: PlatformKind
    session_type: str = ""
    hotkey_available: bool = False
    hotkey_method: str = "none"
    paste_available: bool = False
    paste_method: str = "none"
    clipboard_available: bool = False
    clipboard_method: str = "none"
    #: Non-fatal advisories shown to the user (missing tools, permissions...).
    notes: list[str] = field(default_factory=list)
    #: Raw key/value details for verbose diagnostics.
    details: dict[str, str] = field(default_factory=dict)

    def add_note(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note)

    @property
    def is_fully_functional(self) -> bool:
        return self.hotkey_available and self.paste_available


class PlatformAdapter(abc.ABC):
    """Abstract base for all platform adapters."""

    kind: PlatformKind = PlatformKind.UNKNOWN

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._callback: HotkeyCallback | None = None


    @abc.abstractmethod
    def detect_capabilities(self) -> PlatformCapabilities:
        """Probe the environment and report what is available.

        Must be cheap and side-effect free (no global hooks installed) so it
        can be called from ``diagnose-platform`` and settings diagnostics.
        """


    @abc.abstractmethod
    def start_hotkeys(self, callback: HotkeyCallback) -> None:
        """Begin listening for the configured global shortcuts.

        The adapter invokes ``callback(action, event)`` on the appropriate
        thread; the controller is responsible for marshalling to whatever
        thread it needs. Must be safe to call once; call :meth:`stop_hotkeys`
        before starting again.
        """

    @abc.abstractmethod
    def stop_hotkeys(self) -> None:
        """Stop listening and remove any installed hooks. Idempotent."""


    @abc.abstractmethod
    def insert_text(self, text: str) -> PasteResult:
        """Insert ``text`` into the focused application.

        Honours :attr:`Settings.paste`: preferred behaviour saves the current
        clipboard, writes ``text``, injects paste, and restores the clipboard.
        When injection is unavailable it must fall back to copy-only and set
        :attr:`PasteResult.needs_manual_paste`. Must never raise for the
        expected "cannot inject" cases — report them in the result instead.
        """


    @abc.abstractmethod
    def get_clipboard(self) -> str | None:
        """Return current clipboard text (``None`` if empty/unavailable)."""

    @abc.abstractmethod
    def set_clipboard(self, text: str) -> bool:
        """Set clipboard text; return whether it succeeded."""


    def close(self) -> None:
        """Release all resources. Default stops hotkeys."""

        try:
            self.stop_hotkeys()
        except Exception:  # pragma: no cover - defensive
            pass


# Platform detection (pure / testable)


def detect_platform_kind(
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> PlatformKind:
    """Classify the current platform/session.

    Parameters are injectable for testing. ``platform_name`` mirrors
    :data:`sys.platform` (``"win32"``, ``"linux"``, ``"darwin"``).
    """

    env = os.environ if env is None else env
    platform_name = sys.platform if platform_name is None else platform_name

    if platform_name.startswith("win"):
        return PlatformKind.WINDOWS
    if platform_name == "darwin":
        return PlatformKind.MACOS
    if platform_name.startswith("linux"):
        session = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
        wayland_display = env.get("WAYLAND_DISPLAY", "").strip()
        x11_display = env.get("DISPLAY", "").strip()

        if session == "wayland" or (not session and wayland_display):
            return PlatformKind.LINUX_WAYLAND
        if session == "x11" or (not session and x11_display):
            return PlatformKind.LINUX_X11
        # Wayland compositors always set WAYLAND_DISPLAY; prefer it when both.
        if wayland_display:
            return PlatformKind.LINUX_WAYLAND
        if x11_display:
            return PlatformKind.LINUX_X11
        return PlatformKind.UNKNOWN
    return PlatformKind.UNKNOWN


def detect_platform(
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> PlatformKind:
    """Alias kept for readability at call sites."""

    return detect_platform_kind(env=env, platform_name=platform_name)


def create_platform_adapter(
    settings: Settings,
    kind: PlatformKind | None = None,
) -> PlatformAdapter:
    """Instantiate the correct adapter for ``kind`` (auto-detected if None).

    Concrete adapters are imported lazily so importing this module never pulls
    in ``pynput``/``dbus``/``ctypes`` machinery that may be unavailable.
    Unsupported platforms (macOS/unknown) return a copy-only
    :class:`NullAdapter` rather than raising, satisfying graceful degradation.
    """

    kind = kind or detect_platform_kind()

    if kind is PlatformKind.WINDOWS:
        from app.platform.windows_adapter import WindowsAdapter

        return WindowsAdapter(settings)
    if kind is PlatformKind.LINUX_X11:
        from app.platform.linux_x11_adapter import LinuxX11Adapter

        return LinuxX11Adapter(settings)
    if kind is PlatformKind.LINUX_WAYLAND:
        from app.platform.linux_wayland_adapter import LinuxWaylandAdapter

        return LinuxWaylandAdapter(settings)
    return NullAdapter(settings, kind)


class NullAdapter(PlatformAdapter):
    """Fallback adapter for unsupported platforms (macOS / unknown).

    Hotkeys are unavailable; text can still be copied to the clipboard via Qt
    if a Qt application is running, otherwise it is a no-op. This keeps the app
    importable and the CLI usable everywhere.
    """

    def __init__(self, settings: Settings, kind: PlatformKind = PlatformKind.UNKNOWN) -> None:
        super().__init__(settings)
        self.kind = kind

    def detect_capabilities(self) -> PlatformCapabilities:
        caps = PlatformCapabilities(kind=self.kind, session_type=self.kind.value)
        caps.add_note(
            "This platform is not fully supported. Global hotkeys and paste "
            "injection are unavailable; copy-only mode may work."
        )
        # Try Qt clipboard opportunistically.
        try:
            from app.services.clipboard import get_clipboard_backend

            backend = get_clipboard_backend()
            caps.clipboard_available = backend.available
            caps.clipboard_method = backend.name
        except Exception:  # pragma: no cover
            caps.clipboard_available = False
        return caps

    def start_hotkeys(self, callback: HotkeyCallback) -> None:
        self._callback = callback  # no hooks available

    def stop_hotkeys(self) -> None:
        self._callback = None

    def insert_text(self, text: str) -> PasteResult:
        copied = self.set_clipboard(text)
        return PasteResult(
            copied=copied,
            injected=False,
            method="clipboard-only",
            needs_manual_paste=True,
            error=None if copied else "No clipboard backend available",
        )

    def get_clipboard(self) -> str | None:
        try:
            from app.services.clipboard import get_clipboard_backend

            return get_clipboard_backend().get_text()
        except Exception:  # pragma: no cover
            return None

    def set_clipboard(self, text: str) -> bool:
        try:
            from app.services.clipboard import get_clipboard_backend

            return get_clipboard_backend().set_text(text)
        except Exception:  # pragma: no cover
            return False
