"""Linux/X11 platform adapter.

Implements :class:`~app.platform.base.PlatformAdapter` for classic X11
sessions:

* **Hotkeys** are captured with ``pynput.keyboard`` (lazy import) on its own
  listener thread. The adapter tracks the state of the modifier keys plus the
  main key of the configured shortcut (parsed via
  :func:`app.platform.hotkeys.parse_hotkey`) and fires the appropriate
  :class:`~app.platform.base.HotkeyAction` / :class:`HotkeyEvent` transitions.
* **Text injection** is performed with ``xdotool`` (``xdotool key
  --clearmodifiers ctrl+v``) after placing the text on the clipboard. When
  ``xdotool`` is missing the adapter degrades to copy-only mode and reports it
  clearly.
* **Clipboard** access goes through :func:`app.services.clipboard.get_clipboard_backend`
  (xclip / xsel / wl-clipboard / Qt).

Importing this module must not require ``pynput`` to be installed; the import
is deferred until :meth:`LinuxX11Adapter.start_hotkeys` actually needs it.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.models import PlatformKind
from app.platform.base import (
    HotkeyAction,
    HotkeyCallback,
    HotkeyEvent,
    PasteResult,
    PlatformAdapter,
    PlatformCapabilities,
)

if TYPE_CHECKING:
    from app.models import Settings
    from app.platform.hotkeys import Hotkey

log = get_logger("platform.x11")


class HotkeyUnavailableError(RuntimeError):
    """Raised when global hotkeys are requested but cannot be provided.

    The message always includes remediation guidance (e.g. installing
    ``pynput`` or setting ``DISPLAY``).
    """


#: How long to wait for the ``xdotool`` subprocess before giving up.
_XDOTOOL_TIMEOUT_S = 5.0

#: Mapping from pynput :class:`~pynput.keyboard.Key` member names to the
#: canonical key tokens produced by :func:`app.platform.hotkeys.parse_hotkey`.
_SPECIAL_KEY_TOKENS = {
    "space": "space",
    "enter": "enter",
    "esc": "esc",
    "tab": "tab",
    "delete": "delete",
    "insert": "insert",
    "backspace": "backspace",
    "page_up": "pageup",
    "page_down": "pagedown",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
}


class LinuxX11Adapter(PlatformAdapter):
    """Platform adapter for X11 desktop sessions."""

    kind = PlatformKind.LINUX_X11

    def __init__(self, settings: Settings) -> None:
        """Initialise the adapter.

        Args:
            settings: The application settings tree (hotkeys, paste behaviour).
        """

        super().__init__(settings)
        self._listener: Any = None
        self._keyboard: Any = None

        # Parsed shortcut specs (populated in start_hotkeys).
        self._ptt: Hotkey | None = None
        self._toggle: Hotkey | None = None
        self._cancel: Hotkey | None = None

        # Live key state guarded by ``_lock``; mutated on the listener thread.
        self._lock = threading.Lock()
        self._pressed_mods: set[str] = set()
        self._ptt_main = False
        self._toggle_main = False
        self._cancel_main = False
        self._ptt_active = False
        self._toggle_active = False
        self._cancel_active = False


    def detect_capabilities(self) -> PlatformCapabilities:
        """Probe the X11 session and report available capabilities.

        Cheap and side-effect free: it only inspects environment variables and
        ``PATH`` (via :func:`shutil.which`) and checks whether ``pynput`` is
        importable without importing it.

        Returns:
            A populated :class:`PlatformCapabilities` snapshot.
        """

        from app.models import PasteMode

        caps = PlatformCapabilities(kind=self.kind)

        session = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
        display = (os.environ.get("DISPLAY") or "").strip()
        caps.session_type = session or ("x11" if display else "")
        caps.details["DISPLAY"] = display or "(unset)"
        caps.details["XDG_SESSION_TYPE"] = session or "(unset)"

        if not display:
            caps.add_note(
                "DISPLAY is not set — no X server is reachable. Global hotkeys "
                "and automatic paste will not work in this session."
            )
        if session == "wayland":
            caps.add_note(
                "Session type is 'wayland', not X11. The X11 adapter relies on "
                "xdotool/pynput which behave unpredictably under Wayland; use "
                "the Wayland adapter instead."
            )

        # Hotkeys (pynput). find_spec does not import the package.
        pynput_present = importlib.util.find_spec("pynput") is not None
        caps.details["pynput"] = "present" if pynput_present else "missing"
        if pynput_present and display:
            caps.hotkey_available = True
            caps.hotkey_method = "pynput global listener"
        else:
            caps.hotkey_available = False
            caps.hotkey_method = "none"
            if not pynput_present:
                caps.add_note(
                    "pynput is not installed — global hotkeys unavailable. "
                    "Install it with `pip install pynput`."
                )

        # Clipboard backend.
        try:
            from app.services.clipboard import get_clipboard_backend

            backend = get_clipboard_backend()
            caps.clipboard_available = backend.available
            caps.clipboard_method = backend.name
        except Exception as exc:  # pragma: no cover - defensive
            caps.clipboard_available = False
            caps.clipboard_method = "none"
            log.debug("Clipboard backend probe failed: %s", exc)

        # Paste method resolution.
        xdotool = shutil.which("xdotool")
        caps.details["xdotool"] = xdotool or "(missing)"

        if self.settings.paste.mode is PasteMode.COPY_ONLY:
            caps.paste_available = caps.clipboard_available
            caps.paste_method = "copy-only (configured)"
        elif xdotool is not None and display:
            caps.paste_available = True
            caps.paste_method = "xdotool ctrl+v"
        elif xdotool is not None and not display:
            caps.paste_available = False
            caps.paste_method = "none (no DISPLAY)"
        else:
            caps.paste_available = caps.clipboard_available
            caps.paste_method = "copy-only (xdotool missing)"
            caps.add_note(
                "xdotool not found — automatic paste unavailable, falling back "
                "to copy-only. Install with `sudo apt install xdotool`."
            )

        return caps


    def start_hotkeys(self, callback: HotkeyCallback) -> None:
        """Start the ``pynput`` listener on its own thread.

        Args:
            callback: Invoked as ``callback(action, event)`` on the listener
                thread whenever a configured shortcut transitions.

        Raises:
            HotkeyUnavailableError: If ``pynput`` cannot be imported (with an
                install hint) or if ``DISPLAY`` is unset.
        """

        if not (os.environ.get("DISPLAY") or "").strip():
            raise HotkeyUnavailableError(
                "DISPLAY is not set, so no X server is reachable for global "
                "hotkeys. Start TalkPaste from within an X11 graphical session."
            )

        try:
            from pynput import keyboard
        except Exception as exc:  # ImportError or backend init failure
            raise HotkeyUnavailableError(
                "Global hotkeys require the 'pynput' package, which could not "
                "be imported. Install it with `pip install pynput`."
            ) from exc

        from app.models import HotkeyMode
        from app.platform.hotkeys import HotkeyParseError, parse_hotkey

        def _parse(spec: str, label: str) -> Hotkey | None:
            try:
                return parse_hotkey(spec)
            except HotkeyParseError as exc:
                log.warning("Could not parse %s hotkey %r: %s", label, spec, exc)
                return None

        self._ptt = _parse(self.settings.hotkeys.push_to_talk, "push-to-talk")
        self._toggle = _parse(self.settings.hotkeys.hands_free_toggle, "toggle")
        self._cancel = _parse(self.settings.hotkeys.cancel, "cancel")

        # Restart cleanly if already running.
        if self._listener is not None:
            self.stop_hotkeys()

        self._callback = callback
        self._keyboard = keyboard
        with self._lock:
            self._pressed_mods.clear()
            self._ptt_main = self._toggle_main = self._cancel_main = False
            self._ptt_active = self._toggle_active = self._cancel_active = False

        listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        listener.daemon = True
        self._listener = listener
        listener.start()

        log.info(
            "X11 hotkey listener started (mode=%s, push_to_talk=%r, cancel=%r)",
            self.settings.hotkeys.mode.value,
            self.settings.hotkeys.push_to_talk,
            self.settings.hotkeys.cancel,
        )
        _ = HotkeyMode  # imported for clarity/documentation of supported modes

    def stop_hotkeys(self) -> None:
        """Stop the listener and reset key state. Idempotent."""

        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("Error stopping X11 hotkey listener: %s", exc)
            else:
                log.info("X11 hotkey listener stopped")
        with self._lock:
            self._pressed_mods.clear()
            self._ptt_main = self._toggle_main = self._cancel_main = False
            self._ptt_active = self._toggle_active = self._cancel_active = False


    def _on_press(self, key: Any) -> None:
        """Handle a pynput key-press event."""

        self._dispatch(key, is_press=True)

    def _on_release(self, key: Any) -> None:
        """Handle a pynput key-release event."""

        self._dispatch(key, is_press=False)

    def _dispatch(self, key: Any, is_press: bool) -> None:
        """Update key state and emit any resulting hotkey transitions.

        Runs on the pynput listener thread. State is mutated under ``_lock``;
        callbacks are invoked after the lock is released to avoid re-entrancy.
        """

        if key is None:
            return

        keyboard = self._keyboard
        if keyboard is None:  # pragma: no cover - listener without start
            return

        # Normalise modifier/layout variants where possible.
        canon = key
        listener = self._listener
        if listener is not None:
            try:
                canon = listener.canonical(key)
            except Exception:  # pragma: no cover - layout dependent
                canon = key

        mod = self._modifier_of(key, keyboard)
        token = self._key_token(canon, keyboard)

        events: list[tuple[HotkeyAction, HotkeyEvent]] = []

        from app.models import HotkeyMode

        with self._lock:
            if mod is not None:
                if is_press:
                    self._pressed_mods.add(mod)
                else:
                    self._pressed_mods.discard(mod)

            if self._ptt is not None and token is not None and token == self._ptt.key:
                self._ptt_main = is_press
            if self._toggle is not None and token is not None and token == self._toggle.key:
                self._toggle_main = is_press
            if self._cancel is not None and token is not None and token == self._cancel.key:
                self._cancel_main = is_press

            mode = self.settings.hotkeys.mode
            # Exact modifier match (not subset) so Ctrl+Alt+Space doesn't also
            # fire when Shift is held (that is the hands-free combo).
            pressed = self._canonical_mods()

            if mode is HotkeyMode.PUSH_TO_TALK and self._ptt is not None:
                active = self._ptt_main and self._ptt.modifiers == pressed
                if active and not self._ptt_active:
                    self._ptt_active = True
                    events.append((HotkeyAction.DICTATE, HotkeyEvent.PRESS))
                elif not active and self._ptt_active:
                    self._ptt_active = False
                    events.append((HotkeyAction.DICTATE, HotkeyEvent.RELEASE))

            if mode is HotkeyMode.HANDS_FREE_TOGGLE and self._toggle is not None:
                active = self._toggle_main and self._toggle.modifiers == pressed
                if active and not self._toggle_active:
                    self._toggle_active = True
                    events.append((HotkeyAction.TOGGLE, HotkeyEvent.TRIGGER))
                elif not active:
                    self._toggle_active = False

            if self._cancel is not None:
                c_active = self._cancel_main and self._cancel.modifiers == pressed
                if c_active and not self._cancel_active:
                    self._cancel_active = True
                    events.append((HotkeyAction.CANCEL, HotkeyEvent.TRIGGER))
                elif not c_active:
                    self._cancel_active = False

        for action, event in events:
            self._emit(action, event)

    def _emit(self, action: HotkeyAction, event: HotkeyEvent) -> None:
        """Invoke the registered callback, never letting errors kill the thread."""

        callback = self._callback
        if callback is None:
            return
        log.debug("Hotkey %s/%s", action.value, event.value)
        try:
            callback(action, event)
        except Exception as exc:  # pragma: no cover - user callback errors
            log.error("Hotkey callback raised for %s/%s: %s", action.value, event.value, exc)

    #: Canonical-token prefixes for concrete modifier key names.
    _MOD_PREFIX_TO_TOKEN = (
        ("ctrl", "ctrl"),
        ("alt", "alt"),
        ("shift", "shift"),
        ("cmd", "meta"),
        ("super", "meta"),
        ("meta", "meta"),
    )

    def _modifier_of(self, key: Any, keyboard: Any) -> str | None:
        """Return the *concrete* modifier key name (e.g. ``"ctrl_l"``) or None.

        Left/right variants are kept distinct so that releasing one while the
        other is still physically held does not clear the modifier prematurely
        (which would fire a spurious push-to-talk RELEASE). ``AltGr`` is
        deliberately not treated as ``Alt`` for combo matching.
        """

        Key = keyboard.Key
        if not isinstance(key, Key):
            return None
        name = getattr(key, "name", "")
        if name in ("alt_gr", "alt_r_gr"):
            return None
        if name.startswith(("ctrl", "alt", "shift", "cmd", "super", "meta")):
            return name or None
        return None

    def _canonical_mods(self) -> set[str]:
        """Collapse the concrete pressed-modifier names to canonical tokens."""

        tokens: set[str] = set()
        for name in self._pressed_mods:
            for prefix, token in self._MOD_PREFIX_TO_TOKEN:
                if name.startswith(prefix):
                    tokens.add(token)
                    break
        return tokens

    def _key_token(self, key: Any, keyboard: Any) -> str | None:
        """Return the canonical key token for a non-modifier ``key``.

        Mirrors the token vocabulary of
        :func:`app.platform.hotkeys.parse_hotkey` so equality comparisons are
        meaningful (single characters lower-cased, ``"space"``, ``"f5"``...).
        """

        Key = keyboard.Key
        KeyCode = keyboard.KeyCode

        if isinstance(key, KeyCode):
            char = key.char
            if char:
                return char.lower()
            return None

        if isinstance(key, Key):
            name = getattr(key, "name", "")
            if name in _SPECIAL_KEY_TOKENS:
                return _SPECIAL_KEY_TOKENS[name]
            if len(name) >= 2 and name[0] == "f" and name[1:].isdigit():
                return name
            return name or None

        return None


    def insert_text(self, text: str) -> PasteResult:
        """Insert ``text`` into the focused application.

        Sets the clipboard, then (unless copy-only) injects ``Ctrl+V`` via
        ``xdotool``. Falls back to copy-only when ``xdotool`` is missing or
        fails. Never raises for the expected "cannot inject" cases.

        Args:
            text: The final text to place into the focused app.

        Returns:
            A :class:`PasteResult` describing what happened.
        """

        from app.models import PasteMode

        paste = self.settings.paste

        # Capture the current clipboard so we can restore it afterwards.
        previous = self.get_clipboard() if paste.restore_clipboard else None

        copied = self.set_clipboard(text)
        if not copied:
            return PasteResult(
                copied=False,
                injected=False,
                method="none",
                error=(
                    "No clipboard backend available to copy the text. Install "
                    "xclip or xsel (e.g. `sudo apt install xclip`)."
                ),
            )

        if paste.mode is PasteMode.COPY_ONLY:
            log.info("Paste mode is copy-only; text copied, manual paste required.")
            return PasteResult(
                copied=True,
                injected=False,
                method="copy-only (configured)",
                needs_manual_paste=True,
            )

        xdotool = shutil.which("xdotool")
        if xdotool is None:
            log.warning("xdotool not found on PATH — using copy-only fallback.")
            return PasteResult(
                copied=True,
                injected=False,
                method="copy-only (xdotool missing)",
                needs_manual_paste=True,
                error=(
                    "xdotool not found — copy-only mode. Install with "
                    "`sudo apt install xdotool` for automatic paste."
                ),
            )

        # Let the clipboard settle before injecting the paste keystroke.
        if paste.paste_delay_ms > 0:
            time.sleep(paste.paste_delay_ms / 1000.0)

        try:
            proc = subprocess.run(
                [xdotool, "key", "--clearmodifiers", "ctrl+v"],
                capture_output=True,
                timeout=_XDOTOOL_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("xdotool timed out; text remains on clipboard.")
            return PasteResult(
                copied=True,
                injected=False,
                method="copy-only (xdotool timeout)",
                needs_manual_paste=True,
                error=(
                    "xdotool did not respond in time. Text copied — press "
                    "Ctrl+V to paste manually."
                ),
            )
        except OSError as exc:
            log.warning("xdotool could not be executed: %s", exc)
            return PasteResult(
                copied=True,
                injected=False,
                method="copy-only (xdotool error)",
                needs_manual_paste=True,
                error=(
                    f"Could not run xdotool ({exc}). Text copied — press Ctrl+V "
                    "to paste manually."
                ),
            )

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            log.warning("xdotool exited %d: %s", proc.returncode, stderr)
            return PasteResult(
                copied=True,
                injected=False,
                method="copy-only (xdotool failed)",
                needs_manual_paste=True,
                error=(
                    f"xdotool paste failed (exit {proc.returncode}"
                    + (f": {stderr}" if stderr else "")
                    + "). Text copied — press Ctrl+V to paste manually."
                ),
            )

        log.info("Injected paste via xdotool ctrl+v.")

        # Restore the previous clipboard contents after a short delay so the
        # target application has time to read what we pasted.
        if paste.restore_clipboard and previous is not None:
            self._schedule_clipboard_restore(previous, paste.restore_delay_ms)

        return PasteResult(
            copied=True,
            injected=True,
            method="xdotool ctrl+v",
        )

    def _schedule_clipboard_restore(self, previous: str, delay_ms: int) -> None:
        """Restore ``previous`` clipboard text after ``delay_ms`` milliseconds."""

        def _restore() -> None:
            try:
                if self.set_clipboard(previous):
                    log.debug("Restored previous clipboard contents.")
                else:
                    log.debug("Could not restore previous clipboard contents.")
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("Clipboard restore failed: %s", exc)

        timer = threading.Timer(max(delay_ms, 0) / 1000.0, _restore)
        timer.daemon = True
        timer.start()


    def get_clipboard(self) -> str | None:
        """Return current clipboard text (``None`` if empty/unavailable)."""

        try:
            from app.services.clipboard import get_clipboard_backend

            return get_clipboard_backend().get_text()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("get_clipboard failed: %s", exc)
            return None

    def set_clipboard(self, text: str) -> bool:
        """Set clipboard text; return whether it succeeded."""

        try:
            from app.services.clipboard import get_clipboard_backend

            return get_clipboard_backend().set_text(text)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("set_clipboard failed: %s", exc)
            return False
