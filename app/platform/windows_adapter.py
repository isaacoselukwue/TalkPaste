"""Windows platform adapter — Win32 low-level keyboard hook + ``SendInput``.

This adapter implements global hotkeys and clipboard-preserving paste on
Windows using nothing but :mod:`ctypes`/:mod:`ctypes.wintypes` (no ``pynput``).
All Win32 machinery is imported/accessed lazily so importing this module works
on any platform (including Linux CI, where :data:`ctypes.windll` does not
exist). The Windows-only methods raise a clear, actionable error if they are
ever invoked on a non-Windows box.

Design
------
* **Hotkeys** — a ``WH_KEYBOARD_LL`` hook is installed via
  :func:`SetWindowsHookExW` on a dedicated thread that runs a classic message
  loop (:func:`GetMessageW`/:func:`TranslateMessage`/:func:`DispatchMessageW`).
  The hook callback tracks which virtual keys are down and fires the mapped
  :class:`~app.platform.base.HotkeyCallback` on press/release of the configured
  push-to-talk combo, on activation of the hands-free toggle, and on the cancel
  key.
* **Paste** — :meth:`WindowsAdapter.insert_text` saves the clipboard, writes the
  new text, synthesises ``Ctrl+V`` via :func:`SendInput`, waits, and restores
  the previous clipboard, honouring :class:`~app.models.PasteSettings`. When
  injection fails (e.g. the focused window is elevated and User Interface
  Privilege Isolation blocks input), it degrades to copy-only and returns a
  :class:`~app.platform.base.PasteResult` with ``needs_manual_paste=True`` and a
  helpful message — it never raises for that expected case.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.models import PasteMode, PlatformKind
from app.platform.base import (
    HotkeyAction,
    HotkeyCallback,
    HotkeyEvent,
    PasteResult,
    PlatformAdapter,
    PlatformCapabilities,
)
from app.platform.hotkeys import Hotkey, HotkeyParseError, parse_hotkey

if TYPE_CHECKING:
    from app.models import Settings

log = get_logger("platform.windows")


# Win32 constants (kept module-level; they are just integers, safe on Linux)

_WH_KEYBOARD_LL = 13
_HC_ACTION = 0

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_WM_QUIT = 0x0012

_KEYEVENTF_KEYUP = 0x0002

_INPUT_KEYBOARD = 1

_VK_CONTROL = 0x11
_VK_V = 0x56

#: ``GetLastError`` value returned when input injection is blocked by UIPI
#: (the target window belongs to a higher-integrity / elevated process).
_ERROR_ACCESS_DENIED = 5

#: Virtual-key codes that count as a given logical modifier being held. The
#: low-level hook reports the specific left/right codes; the generic codes are
#: included defensively.
_MODIFIER_VKS = {
    "ctrl": (0x11, 0xA2, 0xA3),   # VK_CONTROL, VK_LCONTROL, VK_RCONTROL
    "alt": (0x12, 0xA4, 0xA5),    # VK_MENU, VK_LMENU, VK_RMENU
    "shift": (0x10, 0xA0, 0xA1),  # VK_SHIFT, VK_LSHIFT, VK_RSHIFT
    "meta": (0x5B, 0x5C),         # VK_LWIN, VK_RWIN
}

#: Named non-modifier keys -> virtual-key code. Single letters/digits and
#: function keys are handled programmatically in :func:`_key_to_vk`.
_NAMED_VKS = {
    "space": 0x20,
    "esc": 0x1B,
    "enter": 0x0D,
    "tab": 0x09,
    "delete": 0x2E,
    "insert": 0x2D,
    "backspace": 0x08,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
}


class WindowsAdapterError(RuntimeError):
    """Raised when a Windows-only operation is attempted off Windows."""


def _key_to_vk(key: str) -> int | None:
    """Map a normalised hotkey key name to a Win32 virtual-key code.

    Args:
        key: A normalised key name from :func:`~app.platform.hotkeys.parse_hotkey`
            (e.g. ``"space"``, ``"a"``, ``"5"``, ``"f7"``).

    Returns:
        The virtual-key code, or ``None`` if the key is not recognised.
    """

    if not key:
        return None
    key = key.lower()
    if key in _NAMED_VKS:
        return _NAMED_VKS[key]
    if len(key) == 1:
        ch = key
        if "a" <= ch <= "z":
            return ord(ch.upper())
        if "0" <= ch <= "9":
            return ord(ch)
    if len(key) >= 2 and key[0] == "f" and key[1:].isdigit():
        num = int(key[1:])
        if 1 <= num <= 24:
            return 0x70 + (num - 1)  # VK_F1 == 0x70
    return None


class _ArmedHotkey:
    """A parsed hotkey mapped to VK codes plus its live edge-detection state."""

    def __init__(self, action: HotkeyAction, hotkey: Hotkey, vk: int) -> None:
        self.action = action
        self.hotkey = hotkey
        self.vk = vk
        #: Whether the full combo was satisfied at the last evaluation.
        self.active = False


class WindowsAdapter(PlatformAdapter):
    """Windows implementation of :class:`~app.platform.base.PlatformAdapter`."""

    kind = PlatformKind.WINDOWS

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._hook_thread: threading.Thread | None = None
        self._hook_thread_id: int = 0
        self._hook_handle = None
        #: Keep a strong reference to the ctypes callback so it is not GC'd
        #: while the OS still holds the hook (a crash otherwise).
        self._hook_proc = None
        self._ready = threading.Event()
        self._stopping = False
        #: Set of virtual-key codes currently held down.
        self._down: set = set()
        self._armed: list = []


    def detect_capabilities(self) -> PlatformCapabilities:
        """Report Windows capabilities cheaply and without side effects.

        Returns:
            A populated :class:`~app.platform.base.PlatformCapabilities`.
        """

        caps = PlatformCapabilities(kind=PlatformKind.WINDOWS, session_type="windows")

        if not self._is_windows():
            caps.add_note(
                "WindowsAdapter is running on a non-Windows platform; global "
                "hotkeys and paste injection are unavailable here."
            )
        else:
            caps.hotkey_available = True
            caps.hotkey_method = "win32 low-level keyboard hook"
            caps.paste_available = self.settings.paste.mode is PasteMode.PASTE
            caps.paste_method = (
                "SendInput Ctrl+V"
                if self.settings.paste.mode is PasteMode.PASTE
                else "copy-only (paste mode disabled in settings)"
            )
            caps.add_note(
                "Injecting into an elevated app (Run as administrator) requires "
                "running TalkPaste as administrator; otherwise text is copied "
                "and must be pasted manually with Ctrl+V."
            )
            caps.details["push_to_talk"] = self.settings.hotkeys.push_to_talk
            caps.details["cancel"] = self.settings.hotkeys.cancel

        # Clipboard is provided by the shared backend on every platform.
        try:
            from app.services.clipboard import get_clipboard_backend

            backend = get_clipboard_backend()
            caps.clipboard_available = backend.available
            caps.clipboard_method = backend.name
        except Exception as exc:  # pragma: no cover - defensive
            caps.clipboard_available = False
            caps.add_note(f"Clipboard backend unavailable: {exc}")

        return caps


    def start_hotkeys(self, callback: HotkeyCallback) -> None:
        """Install the low-level keyboard hook on a dedicated message thread.

        Args:
            callback: Invoked as ``callback(action, event)`` from the hook
                thread when a configured hotkey transition is detected.

        Raises:
            WindowsAdapterError: If called on a non-Windows platform.
        """

        self._require_windows("start_hotkeys")

        if self._hook_thread is not None and self._hook_thread.is_alive():
            log.warning("start_hotkeys called while already running; ignoring. "
                        "Call stop_hotkeys() first.")
            return

        self._callback = callback
        self._armed = self._build_armed_hotkeys()
        if not self._armed:
            log.warning("No hotkeys could be parsed; the keyboard hook will run "
                        "but fire no events.")

        self._down = set()
        self._stopping = False
        self._ready.clear()
        self._hook_thread = threading.Thread(
            target=self._run_message_loop,
            name="talkpaste-win-hotkeys",
            daemon=True,
        )
        self._hook_thread.start()

        # Wait briefly for the hook to install so callers know it is live.
        # ``_ready`` is set on both success and every failure path, so we must
        # also check that the hook handle actually exists before claiming success.
        if not self._ready.wait(timeout=5.0):
            log.error("Keyboard hook thread did not become ready within 5s; "
                      "global hotkeys are inactive.")
            self._hook_thread = None
        elif self._hook_handle is None:
            log.error("Keyboard hook failed to install; global hotkeys are "
                      "inactive. Use the tray menu or bind a system shortcut to "
                      "`talkpaste dictate-toggle` instead.")
            self._hook_thread = None
        else:
            log.info("Windows keyboard hook installed (%d hotkeys armed).",
                     len(self._armed))

    def stop_hotkeys(self) -> None:
        """Unhook and stop the message loop. Idempotent."""

        thread = self._hook_thread
        if thread is None:
            return

        self._stopping = True
        thread_id = self._hook_thread_id
        if thread_id and self._is_windows():
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW.argtypes = [
                    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
                ]
                user32.PostThreadMessageW.restype = wintypes.BOOL
                # Wake the GetMessage loop so it can exit cleanly.
                user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("PostThreadMessage(WM_QUIT) failed: %s", exc)

        thread.join(timeout=5.0)
        if thread.is_alive():  # pragma: no cover - defensive
            log.warning("Keyboard hook thread did not stop within 5s.")
        else:
            log.info("Windows keyboard hook removed.")

        self._hook_thread = None
        self._hook_thread_id = 0
        self._hook_handle = None
        self._hook_proc = None
        self._callback = None
        self._ready.clear()

    def _build_armed_hotkeys(self) -> list:
        """Parse the configured hotkeys and map them to VK codes.

        Returns:
            A list of :class:`_ArmedHotkey`; unparseable/unmappable specs are
            skipped with a warning rather than raising.
        """

        specs = [
            (HotkeyAction.DICTATE, self.settings.hotkeys.push_to_talk),
            (HotkeyAction.TOGGLE, self.settings.hotkeys.hands_free_toggle),
            (HotkeyAction.CANCEL, self.settings.hotkeys.cancel),
        ]
        armed: list = []
        for action, spec in specs:
            if not spec:
                continue
            try:
                hotkey = parse_hotkey(spec)
            except HotkeyParseError as exc:
                log.warning("Ignoring unparseable %s hotkey %r: %s",
                            action.value, spec, exc)
                continue
            vk = _key_to_vk(hotkey.key)
            if vk is None:
                log.warning("Ignoring %s hotkey %r: key %r has no known "
                            "virtual-key code.", action.value, spec, hotkey.key)
                continue
            armed.append(_ArmedHotkey(action, hotkey, vk))
            log.debug("Armed %s hotkey %s (vk=0x%02X, mods=%s)",
                      action.value, hotkey.normalized(), vk,
                      sorted(hotkey.modifiers))
        return armed

    def _run_message_loop(self) -> None:
        """Thread body: install the hook and pump the message queue."""

        import ctypes
        from ctypes import wintypes

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception as exc:  # pragma: no cover - Windows only
            log.error("Could not load Win32 libraries for hotkeys: %s", exc)
            self._ready.set()
            return

        ULONG_PTR = wintypes.WPARAM

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )

        # Signatures (critical on 64-bit: default restype c_int truncates
        # pointer-sized handles and corrupts CallNextHookEx).
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
        ]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        user32.GetMessageW.restype = ctypes.c_int
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        def _proc(nCode: int, wParam: int, lParam: int) -> int:
            if nCode == _HC_ACTION:
                try:
                    kb = ctypes.cast(
                        lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)
                    ).contents
                    self._handle_key_event(int(kb.vkCode), int(wParam))
                except Exception as exc:  # pragma: no cover - never break input
                    log.debug("Hotkey hook callback error: %s", exc)
            return user32.CallNextHookEx(self._hook_handle, nCode, wParam, lParam)

        hook_proc = HOOKPROC(_proc)
        self._hook_proc = hook_proc  # keep alive

        hmod = kernel32.GetModuleHandleW(None)
        handle = user32.SetWindowsHookExW(_WH_KEYBOARD_LL, hook_proc, hmod, 0)
        if not handle:
            err = ctypes.get_last_error()
            log.error("SetWindowsHookExW failed (GetLastError=%d).", err)
            self._ready.set()
            return

        self._hook_handle = handle
        self._hook_thread_id = int(kernel32.GetCurrentThreadId())
        self._ready.set()
        log.debug("Keyboard hook thread ready (tid=%d).", self._hook_thread_id)

        try:
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:  # WM_QUIT
                    break
                if ret == -1:  # error
                    err = ctypes.get_last_error()
                    log.error("GetMessageW failed (GetLastError=%d); "
                              "stopping hook loop.", err)
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            try:
                user32.UnhookWindowsHookEx(handle)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("UnhookWindowsHookEx failed: %s", exc)

    def _handle_key_event(self, vk: int, wparam: int) -> None:
        """Update pressed-key state and fire callbacks on hotkey transitions.

        Runs in the hook thread. Kept fast and exception-safe.

        Args:
            vk: The virtual-key code from the hook struct.
            wparam: The window message (``WM_KEYDOWN`` etc.) identifying the
                transition direction.
        """

        is_down = wparam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
        is_up = wparam in (_WM_KEYUP, _WM_SYSKEYUP)
        if not (is_down or is_up):
            return

        if is_down:
            self._down.add(vk)
        else:
            self._down.discard(vk)

        mods_now = {
            name: any(v in self._down for v in vks)
            for name, vks in _MODIFIER_VKS.items()
        }

        for armed in self._armed:
            hk = armed.hotkey
            combo = (
                armed.vk in self._down
                and mods_now["ctrl"] == hk.ctrl
                and mods_now["alt"] == hk.alt
                and mods_now["shift"] == hk.shift
                and mods_now["meta"] == hk.meta
            )
            if combo and not armed.active:
                armed.active = True
                self._fire_press(armed.action)
            elif not combo and armed.active:
                armed.active = False
                self._fire_release(armed.action)

    def _fire_press(self, action: HotkeyAction) -> None:
        """Dispatch the rising-edge event for a hotkey action."""

        cb = self._callback
        if cb is None:
            return
        if action is HotkeyAction.DICTATE:
            cb(HotkeyAction.DICTATE, HotkeyEvent.PRESS)
        else:
            # Toggle and cancel are discrete activations.
            cb(action, HotkeyEvent.TRIGGER)

    def _fire_release(self, action: HotkeyAction) -> None:
        """Dispatch the falling-edge event for a hotkey action."""

        cb = self._callback
        if cb is None:
            return
        if action is HotkeyAction.DICTATE:
            cb(HotkeyAction.DICTATE, HotkeyEvent.RELEASE)
        # Toggle/cancel have no release semantics.


    def insert_text(self, text: str) -> PasteResult:
        """Insert ``text`` into the focused window per paste settings.

        Copy-only mode simply sets the clipboard. Paste mode saves the current
        clipboard, sets the text, injects ``Ctrl+V`` via ``SendInput``, waits,
        and restores the previous clipboard. Injection failures (e.g. an
        elevated target) degrade to copy-only with ``needs_manual_paste=True``.

        Args:
            text: The final text to place into the focused application.

        Returns:
            A :class:`~app.platform.base.PasteResult` describing the outcome.
        """

        paste = self.settings.paste

        if paste.mode is PasteMode.COPY_ONLY:
            copied = self.set_clipboard(text)
            return PasteResult(
                copied=copied,
                injected=False,
                method="copy-only",
                needs_manual_paste=True,
                error=None if copied else "No clipboard backend available.",
            )

        self._require_windows("insert_text")

        previous: str | None = None
        if paste.restore_clipboard:
            try:
                previous = self.get_clipboard()
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("Could not read clipboard for restore: %s", exc)
                previous = None

        copied = self.set_clipboard(text)
        if not copied:
            return PasteResult(
                copied=False,
                injected=False,
                method="SendInput Ctrl+V",
                needs_manual_paste=True,
                error="Failed to set the clipboard; cannot paste.",
            )

        if paste.paste_delay_ms > 0:
            time.sleep(paste.paste_delay_ms / 1000.0)

        injected, err = self._send_ctrl_v()
        if not injected:
            log.warning("Paste injection failed: %s", err)
            return PasteResult(
                copied=True,
                injected=False,
                method="SendInput Ctrl+V",
                needs_manual_paste=True,
                error=err,
            )

        if paste.restore_clipboard:
            if paste.restore_delay_ms > 0:
                time.sleep(paste.restore_delay_ms / 1000.0)
            try:
                self.set_clipboard(previous if previous is not None else "")
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("Could not restore clipboard: %s", exc)

        log.info("Injected text via SendInput Ctrl+V (%d chars).", len(text))
        return PasteResult(copied=True, injected=True, method="SendInput Ctrl+V")

    def _send_ctrl_v(self) -> tuple[bool, str | None]:
        """Synthesise a ``Ctrl+V`` keystroke with ``SendInput``.

        Returns:
            ``(injected, error)`` — ``injected`` is ``True`` on success; on
            failure ``error`` carries a user-facing remediation message.
        """

        import ctypes
        from ctypes import wintypes

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except Exception as exc:  # pragma: no cover - Windows only
            return False, f"Could not load user32 for SendInput: {exc}"

        ULONG_PTR = wintypes.WPARAM

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        user32.SendInput.argtypes = [
            wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int
        ]
        user32.SendInput.restype = wintypes.UINT

        def _key(vk: int, up: bool) -> INPUT:
            inp = INPUT()
            inp.type = _INPUT_KEYBOARD
            inp.u.ki = KEYBDINPUT(
                wVk=vk,
                wScan=0,
                dwFlags=_KEYEVENTF_KEYUP if up else 0,
                time=0,
                dwExtraInfo=0,
            )
            return inp

        events = (INPUT * 4)(
            _key(_VK_CONTROL, False),
            _key(_VK_V, False),
            _key(_VK_V, True),
            _key(_VK_CONTROL, True),
        )

        ctypes.set_last_error(0)
        sent = user32.SendInput(4, events, ctypes.sizeof(INPUT))
        if sent != 4:
            err = ctypes.get_last_error()
            if err == _ERROR_ACCESS_DENIED:
                message = (
                    "Target app is elevated/Run as administrator; cannot "
                    "inject. Text copied — paste manually with Ctrl+V, or run "
                    "TalkPaste as administrator."
                )
            else:
                message = (
                    "SendInput could not inject the paste keystroke "
                    f"(inserted {sent}/4 events, GetLastError={err}). "
                    "Text copied — paste manually with Ctrl+V."
                )
            return False, message

        return True, None


    def get_clipboard(self) -> str | None:
        """Return the current clipboard text via the shared backend."""

        try:
            from app.services.clipboard import get_clipboard_backend

            return get_clipboard_backend().get_text()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("get_clipboard failed: %s", exc)
            return None

    def set_clipboard(self, text: str) -> bool:
        """Set the clipboard text via the shared backend."""

        try:
            from app.services.clipboard import get_clipboard_backend

            return get_clipboard_backend().set_text(text)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("set_clipboard failed: %s", exc)
            return False


    @staticmethod
    def _is_windows() -> bool:
        """Whether the Win32 API is available in this process."""

        import ctypes

        return hasattr(ctypes, "windll")

    def _require_windows(self, operation: str) -> None:
        """Raise a clear error if a Windows-only operation runs off Windows.

        Args:
            operation: Name of the operation being attempted, for the message.

        Raises:
            WindowsAdapterError: When not running on Windows.
        """

        if not self._is_windows():
            raise WindowsAdapterError(
                f"WindowsAdapter.{operation} requires Windows (Win32 API), but "
                "this process is not running on Windows. Use the Linux X11/"
                "Wayland adapter on Linux, or run TalkPaste on Windows."
            )
