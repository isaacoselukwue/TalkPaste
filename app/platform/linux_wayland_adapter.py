"""Wayland platform adapter built on the XDG Desktop Portal.

Wayland deliberately denies applications the ability to grab global keys or
synthesise input into other windows, so everything here is *best-effort* with
graceful, explicit fallback. Nothing requires root and nothing fails silently:
when a capability is unavailable we say exactly which one and why, and we point
the user at a concrete remediation.

Capabilities, in order of preference:

* **Hotkeys** — the ``org.freedesktop.portal.GlobalShortcuts`` interface over
  D-Bus (via the optional ``dbus-next`` package). The portal's
  ``Activated`` / ``Deactivated`` signals are mapped to
  ``DICTATE`` ``PRESS`` / ``RELEASE``. When the portal is missing, denied, or
  ``dbus-next`` is not installed, hotkeys are unavailable and the user is told
  to bind a system shortcut that runs ``talkpaste dictate-toggle`` instead.
* **Paste** — first the ``org.freedesktop.portal.RemoteDesktop`` interface
  (inject ``Ctrl+V`` via keysym events), then — only when the user has opted in
  with ``paste.allow_ydotool`` — the external ``ydotool``, and finally
  copy-only with :attr:`PasteResult.needs_manual_paste` set.
* **Clipboard** — delegated to :func:`app.services.clipboard.get_clipboard_backend`
  which prefers ``wl-clipboard`` on Wayland.

``dbus-next`` is an optional dependency and is imported lazily; importing this
module never requires it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.models import PasteMode, PlatformKind
from app.platform.base import (
    HotkeyAction,
    HotkeyEvent,
    PasteResult,
    PlatformAdapter,
    PlatformCapabilities,
)
from app.platform.hotkeys import Hotkey, parse_hotkey

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import Settings
    from app.platform.base import HotkeyCallback

log = get_logger("wayland")


# Portal constants

_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_GLOBAL_SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
_REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"
_REQUEST_IFACE = "org.freedesktop.portal.Request"

#: Stable identifier for our single push-to-talk shortcut.
_SHORTCUT_ID = "talkpaste_dictate"

#: Generous timeouts: portals may show a one-time confirmation dialog.
_PORTAL_HANDSHAKE_TIMEOUT = 20.0
_PORTAL_REQUEST_TIMEOUT = 15.0
_RD_START_TIMEOUT = 20.0
_RD_INJECT_TIMEOUT = 5.0

#: X11 keysyms used for the RemoteDesktop ``Ctrl+V`` injection.
_CTRL_KEYSYM = 0xFFE3   # Control_L
_V_KEYSYM = 0x0076      # lower-case "v"

#: Linux input event codes for ``ydotool`` (KEY_LEFTCTRL / KEY_V).
_YDOTOOL_CTRL = "29"
_YDOTOOL_V = "47"

#: Minimal introspection for the transient ``Request`` object a portal call
#: returns. We subscribe to its ``Response`` signal to learn the result.
_REQUEST_INTROSPECTION = """<node>
  <interface name="org.freedesktop.portal.Request">
    <method name="Close"/>
    <signal name="Response">
      <arg type="u" name="response"/>
      <arg type="a{sv}" name="results"/>
    </signal>
  </interface>
</node>"""


# Typed errors


class PortalError(RuntimeError):
    """Base class for XDG Desktop Portal failures."""


class HotkeyPortalError(PortalError):
    """Raised when global hotkeys cannot be established via the portal."""


class PortalPasteError(PortalError):
    """Raised internally when the RemoteDesktop paste path is unavailable."""


# Small helpers


def _new_token() -> str:
    """Return a fresh, D-Bus-object-path-safe token for portal handles."""

    return "talkpaste_" + uuid.uuid4().hex


def _portal_trigger(hotkey: Hotkey) -> str:
    """Build a portal ``preferred_trigger`` hint from a parsed hotkey.

    The GlobalShortcuts portal treats this only as a *suggestion*; the
    compositor owns the final binding and may present its own picker. The
    syntax follows the XDG shortcuts convention (``CTRL+ALT+space``).
    """

    parts: list[str] = []
    if hotkey.ctrl:
        parts.append("CTRL")
    if hotkey.alt:
        parts.append("ALT")
    if hotkey.shift:
        parts.append("SHIFT")
    if hotkey.meta:
        parts.append("LOGO")
    if hotkey.key:
        parts.append(hotkey.key)
    return "+".join(parts)


def _dbus_next_available() -> bool:
    """Return whether the optional ``dbus-next`` package is importable.

    Uses :func:`importlib.util.find_spec` so detection never actually imports
    the package (keeping :meth:`detect_capabilities` cheap and side-effect
    free).
    """

    try:
        import importlib.util

        return importlib.util.find_spec("dbus_next") is not None
    except Exception:  # pragma: no cover - defensive
        return False


def _run_coro_blocking(coro_factory: Callable[[], Any], timeout: float) -> Any:
    """Run ``coro_factory()`` to completion on a throwaway event loop thread.

    Running on a dedicated thread keeps us safe regardless of whether the
    caller already has a running asyncio loop (e.g. under Qt). Raises
    :class:`TimeoutError` if the work does not finish within ``timeout`` and
    re-raises any exception the coroutine produced.
    """

    box: dict[str, Any] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box["value"] = loop.run_until_complete(
                asyncio.wait_for(coro_factory(), timeout)
            )
        except BaseException as exc:  # noqa: BLE001 - report back to caller
            box["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:  # pragma: no cover - defensive
                pass

    thread = threading.Thread(target=_runner, name="portal-probe", daemon=True)
    thread.start()
    thread.join(timeout + 1.0)
    if thread.is_alive():
        raise TimeoutError("D-Bus operation timed out")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


async def _portal_request(
    bus: Any,
    loop: asyncio.AbstractEventLoop,
    method: Callable[[dict], Any],
    handle_token: str,
    options: dict,
) -> dict:
    """Invoke an async portal ``method`` and await its ``Response`` signal.

    Portal methods return immediately with a ``Request`` object path; the real
    result arrives later via that object's ``Response`` signal. We precompute
    the request path from ``handle_token`` (portals honour the caller's token),
    subscribe first, then call the method.

    Raises:
        PortalError: If the portal denies/cancels the request or it times out.
    """

    from dbus_next import introspection as intr

    sender_part = str(bus.unique_name)[1:].replace(".", "_")
    request_path = (
        "/org/freedesktop/portal/desktop/request/" f"{sender_part}/{handle_token}"
    )
    node = intr.Node.parse(_REQUEST_INTROSPECTION)
    req_proxy = bus.get_proxy_object(_PORTAL_BUS, request_path, node)
    req_iface = req_proxy.get_interface(_REQUEST_IFACE)

    fut: asyncio.Future = loop.create_future()

    def _on_response(response: int, results: dict) -> None:
        if not fut.done():
            fut.set_result((response, results))

    req_iface.on_response(_on_response)
    try:
        await method(options)
        response, results = await asyncio.wait_for(fut, _PORTAL_REQUEST_TIMEOUT)
    finally:
        try:
            req_iface.off_response(_on_response)
        except Exception:  # pragma: no cover - defensive
            pass

    if response != 0:
        # 1 = user cancelled, 2 = ended by other means.
        raise PortalError(
            f"Portal request was denied or cancelled (response code {response})."
        )
    return results


def _unwrap_variant(value: Any) -> Any:
    """Return the underlying value of a ``dbus_next.Variant`` (or passthrough)."""

    return value.value if hasattr(value, "value") else value


# GlobalShortcuts listener (dedicated asyncio thread)


class _GlobalShortcutsListener:
    """Binds a single portal global shortcut and forwards press/release.

    A dedicated thread owns an asyncio loop: it performs the CreateSession /
    BindShortcuts handshake, then runs forever delivering ``Activated`` /
    ``Deactivated`` signals to ``callback`` until :meth:`stop` is called.
    """

    def __init__(
        self,
        hotkey: Hotkey,
        shortcut_id: str,
        callback: Callable[[HotkeyAction, HotkeyEvent], None],
    ) -> None:
        self._hotkey = hotkey
        self._shortcut_id = shortcut_id
        self._callback = callback
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus: Any = None
        self._session_handle: str | None = None
        self._ready = threading.Event()
        self._start_error: str | None = None


    def start(self, timeout: float = _PORTAL_HANDSHAKE_TIMEOUT) -> None:
        """Start the listener thread and block until bound (or it fails)."""

        self._thread = threading.Thread(
            target=self._run, name="portal-shortcuts", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise HotkeyPortalError(
                "Timed out waiting for the GlobalShortcuts portal. Your "
                "compositor may not implement it, or the confirmation dialog "
                "was dismissed. Bind a system shortcut to run "
                "`talkpaste dictate-toggle` instead."
            )
        if self._start_error is not None:
            err = self._start_error
            self.stop()
            raise HotkeyPortalError(err)

    def stop(self) -> None:
        """Stop the loop and join the thread. Idempotent and thread-safe."""

        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # pragma: no cover - loop already gone
                pass
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._handshake())
        except Exception as exc:  # noqa: BLE001 - reported via start_error
            self._start_error = str(exc)
            self._ready.set()
            self._safe_disconnect()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            self._safe_disconnect()
            loop.close()

    async def _handshake(self) -> None:
        from dbus_next import Variant
        from dbus_next.aio import MessageBus

        assert self._loop is not None
        self._bus = await MessageBus().connect()
        node = await self._bus.introspect(_PORTAL_BUS, _PORTAL_PATH)
        if _GLOBAL_SHORTCUTS_IFACE not in {i.name for i in node.interfaces}:
            raise HotkeyPortalError(
                "The GlobalShortcuts portal interface is not available on this "
                "desktop. Bind a system shortcut to run "
                "`talkpaste dictate-toggle` instead."
            )
        proxy = self._bus.get_proxy_object(_PORTAL_BUS, _PORTAL_PATH, node)
        gs = proxy.get_interface(_GLOBAL_SHORTCUTS_IFACE)

        # Subscribe before binding so we never miss activations.
        gs.on_activated(self._on_activated)
        gs.on_deactivated(self._on_deactivated)

        token = _new_token()
        session_token = _new_token()
        results = await _portal_request(
            self._bus,
            self._loop,
            lambda opts: gs.call_create_session(opts),
            token,
            {
                "handle_token": Variant("s", token),
                "session_handle_token": Variant("s", session_token),
            },
        )
        self._session_handle = _unwrap_variant(results.get("session_handle"))
        if not self._session_handle:
            raise HotkeyPortalError("The portal did not return a session handle.")

        bind_token = _new_token()
        trigger = _portal_trigger(self._hotkey)
        shortcuts = [
            (
                self._shortcut_id,
                {
                    "description": Variant("s", "TalkPaste push-to-talk dictation"),
                    "preferred_trigger": Variant("s", trigger),
                },
            )
        ]
        await _portal_request(
            self._bus,
            self._loop,
            lambda opts: gs.call_bind_shortcuts(
                self._session_handle, shortcuts, "", opts
            ),
            bind_token,
            {"handle_token": Variant("s", bind_token)},
        )
        log.info(
            "GlobalShortcuts portal bound %r (preferred trigger %r)",
            self._shortcut_id,
            trigger,
        )


    def _on_activated(
        self, session_handle: str, shortcut_id: str, timestamp: int, options: dict
    ) -> None:
        if shortcut_id == self._shortcut_id:
            self._emit(HotkeyAction.DICTATE, HotkeyEvent.PRESS)

    def _on_deactivated(
        self, session_handle: str, shortcut_id: str, timestamp: int, options: dict
    ) -> None:
        if shortcut_id == self._shortcut_id:
            self._emit(HotkeyAction.DICTATE, HotkeyEvent.RELEASE)

    def _emit(self, action: HotkeyAction, event: HotkeyEvent) -> None:
        try:
            self._callback(action, event)
        except Exception as exc:  # noqa: BLE001 - never let callbacks kill loop
            log.error("Hotkey callback raised: %s", exc)

    def _safe_disconnect(self) -> None:
        try:
            if self._bus is not None:
                self._bus.disconnect()
        except Exception:  # pragma: no cover - defensive
            pass


# RemoteDesktop keyboard-injection session (dedicated asyncio thread)


class _RemoteDesktopSession:
    """A persistent RemoteDesktop portal session used to inject ``Ctrl+V``.

    Creating the session prompts the user once (via the compositor); the
    session is then reused for every subsequent paste so we do not re-prompt.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus: Any = None
        self._rd_iface: Any = None
        self._session_handle: str | None = None
        self._ready = threading.Event()
        self._start_error: str | None = None

    def start(self, timeout: float = _RD_START_TIMEOUT) -> None:
        """Start the session thread and block until ready (or it fails)."""

        self._thread = threading.Thread(
            target=self._run, name="portal-remotedesktop", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise PortalPasteError(
                "Timed out starting the RemoteDesktop portal (the permission "
                "prompt may have been dismissed)."
            )
        if self._start_error is not None:
            err = self._start_error
            self.stop()
            raise PortalPasteError(err)

    def type_ctrl_v(self, timeout: float = _RD_INJECT_TIMEOUT) -> None:
        """Inject a ``Ctrl+V`` keystroke via the portal. Raises on failure."""

        loop = self._loop
        if loop is None or self._rd_iface is None or self._session_handle is None:
            raise PortalPasteError("RemoteDesktop session is not started.")
        future = asyncio.run_coroutine_threadsafe(self._inject_ctrl_v(), loop)
        future.result(timeout)

    def stop(self) -> None:
        """Stop the loop and join the thread. Idempotent and thread-safe."""

        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # pragma: no cover - loop already gone
                pass
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._handshake())
        except Exception as exc:  # noqa: BLE001 - reported via start_error
            self._start_error = str(exc)
            self._ready.set()
            self._safe_disconnect()
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            self._safe_disconnect()
            loop.close()

    async def _handshake(self) -> None:
        from dbus_next import Variant
        from dbus_next.aio import MessageBus

        assert self._loop is not None
        self._bus = await MessageBus().connect()
        node = await self._bus.introspect(_PORTAL_BUS, _PORTAL_PATH)
        if _REMOTE_DESKTOP_IFACE not in {i.name for i in node.interfaces}:
            raise PortalPasteError(
                "The RemoteDesktop portal interface is not available."
            )
        proxy = self._bus.get_proxy_object(_PORTAL_BUS, _PORTAL_PATH, node)
        rd = proxy.get_interface(_REMOTE_DESKTOP_IFACE)
        self._rd_iface = rd

        token = _new_token()
        session_token = _new_token()
        results = await _portal_request(
            self._bus,
            self._loop,
            lambda opts: rd.call_create_session(opts),
            token,
            {
                "handle_token": Variant("s", token),
                "session_handle_token": Variant("s", session_token),
            },
        )
        self._session_handle = _unwrap_variant(results.get("session_handle"))
        if not self._session_handle:
            raise PortalPasteError(
                "The portal did not return a RemoteDesktop session handle."
            )

        # SelectDevices: 1 == KEYBOARD in the portal device-type bitmask.
        select_token = _new_token()
        await _portal_request(
            self._bus,
            self._loop,
            lambda opts: rd.call_select_devices(self._session_handle, opts),
            select_token,
            {
                "handle_token": Variant("s", select_token),
                "types": Variant("u", 1),
            },
        )

        start_token = _new_token()
        await _portal_request(
            self._bus,
            self._loop,
            lambda opts: rd.call_start(self._session_handle, "", opts),
            start_token,
            {"handle_token": Variant("s", start_token)},
        )
        log.info("RemoteDesktop portal session started for keyboard injection")

    async def _inject_ctrl_v(self) -> None:
        rd = self._rd_iface
        sh = self._session_handle
        empty: dict = {}
        # keysym state: 1 == pressed, 0 == released.
        await rd.call_notify_keyboard_keysym(sh, empty, _CTRL_KEYSYM, 1)
        await rd.call_notify_keyboard_keysym(sh, empty, _V_KEYSYM, 1)
        await asyncio.sleep(0.01)
        await rd.call_notify_keyboard_keysym(sh, empty, _V_KEYSYM, 0)
        await rd.call_notify_keyboard_keysym(sh, empty, _CTRL_KEYSYM, 0)

    def _safe_disconnect(self) -> None:
        try:
            if self._bus is not None:
                self._bus.disconnect()
        except Exception:  # pragma: no cover - defensive
            pass


# Adapter


class LinuxWaylandAdapter(PlatformAdapter):
    """Wayland adapter: XDG portals with copy-only and manual-shortcut fallback."""

    kind = PlatformKind.LINUX_WAYLAND

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._listener: _GlobalShortcutsListener | None = None
        self._rd_session: _RemoteDesktopSession | None = None
        #: Once RemoteDesktop paste has failed we stop re-prompting this run.
        self._rd_unavailable = False
        self._rd_note: str | None = None


    def detect_capabilities(self) -> PlatformCapabilities:
        """Probe portals/tools cheaply and explain every unavailable capability.

        No global hooks are installed and no permission dialogs are triggered:
        the portal is only *introspected* (interface presence), which does not
        prompt the user.
        """

        env = os.environ
        session_type = (env.get("XDG_SESSION_TYPE") or "").strip().lower() or "wayland"
        wayland_display = (env.get("WAYLAND_DISPLAY") or "").strip()

        caps = PlatformCapabilities(kind=self.kind, session_type=session_type)
        caps.details["wayland_display"] = wayland_display or "(unset)"

        if session_type != "wayland":
            caps.add_note(
                f"XDG_SESSION_TYPE is {session_type!r}, not 'wayland'; "
                "the X11 adapter may be a better match for this session."
            )
        if not wayland_display:
            caps.add_note(
                "WAYLAND_DISPLAY is not set; this may not be a Wayland session."
            )

        # Clipboard (delegated backend, prefers wl-clipboard on Wayland).
        backend = self._clipboard_backend()
        caps.clipboard_available = backend.available
        caps.clipboard_method = backend.name
        if not backend.available:
            caps.add_note(
                "No clipboard tool found. Install wl-clipboard "
                "(`sudo apt install wl-clipboard`)."
            )

        # Portal availability.
        dbus_present = _dbus_next_available()
        caps.details["dbus_next"] = "installed" if dbus_present else "missing"

        interfaces: set | None = None
        probe_err: str | None = None
        if dbus_present:
            interfaces, probe_err = self._probe_portal_interfaces()
        has_global_shortcuts = bool(interfaces) and _GLOBAL_SHORTCUTS_IFACE in interfaces
        has_remote_desktop = bool(interfaces) and _REMOTE_DESKTOP_IFACE in interfaces
        caps.details["portal_global_shortcuts"] = (
            "present" if has_global_shortcuts else "absent"
        )
        caps.details["portal_remote_desktop"] = (
            "present" if has_remote_desktop else "absent"
        )
        if interfaces:
            caps.details["portal_interfaces"] = ", ".join(sorted(interfaces))

        # Hotkeys.
        if not dbus_present:
            caps.hotkey_available = False
            caps.hotkey_method = "none"
            caps.add_note(
                "Global hotkeys need the XDG portal via the Python package "
                "'dbus-next' (`pip install dbus-next`). Alternatively, bind a "
                "system shortcut in your desktop settings to run "
                "`talkpaste dictate-toggle`."
            )
        elif has_global_shortcuts:
            caps.hotkey_available = True
            caps.hotkey_method = "XDG portal GlobalShortcuts"
            caps.add_note(
                "Global hotkeys use the GlobalShortcuts portal; your compositor "
                "will ask you to confirm the shortcut the first time."
            )
        else:
            caps.hotkey_available = False
            caps.hotkey_method = "none"
            reason = probe_err or "the compositor does not implement GlobalShortcuts"
            caps.add_note(
                f"Global hotkeys unavailable ({reason}). Bind a system shortcut "
                "in your desktop settings to run `talkpaste dictate-toggle`."
            )

        # Paste.
        ydotool_present = shutil.which("ydotool") is not None
        caps.details["ydotool"] = "present" if ydotool_present else "absent"
        if dbus_present and has_remote_desktop:
            caps.paste_available = True
            caps.paste_method = "XDG portal RemoteDesktop (Ctrl+V)"
            caps.add_note(
                "Automatic paste uses the RemoteDesktop portal; your compositor "
                "will prompt for permission the first time."
            )
        elif self.settings.paste.allow_ydotool and ydotool_present:
            caps.paste_available = True
            caps.paste_method = "ydotool (Ctrl+V)"
            caps.add_note(
                "Automatic paste via ydotool requires the ydotoold daemon to be "
                "running with access to /dev/uinput."
            )
        else:
            caps.paste_available = False
            caps.paste_method = "copy-only"
            if not has_remote_desktop:
                caps.add_note(
                    "Automatic paste unavailable: the RemoteDesktop portal is "
                    "not present. Text will be copied — press Ctrl+V to paste."
                )
            if not ydotool_present:
                caps.add_note(
                    "Optional fallback 'ydotool' is not installed; install it "
                    "and set paste.allow_ydotool for automatic paste."
                )
            elif not self.settings.paste.allow_ydotool:
                caps.add_note(
                    "ydotool is installed but disabled; set paste.allow_ydotool "
                    "to use it for automatic paste."
                )

        return caps

    def _probe_portal_interfaces(
        self, timeout: float = 1.5
    ) -> tuple[set | None, str | None]:
        """Introspect the portal object and return its interface names.

        Returns ``(interfaces, None)`` on success or ``(set(), reason)`` when
        the session bus / portal is unreachable. Bounded and side-effect free.
        """

        async def _introspect() -> set:
            from dbus_next.aio import MessageBus

            bus = await MessageBus().connect()
            try:
                node = await bus.introspect(_PORTAL_BUS, _PORTAL_PATH)
                return {iface.name for iface in node.interfaces}
            finally:
                try:
                    bus.disconnect()
                except Exception:  # pragma: no cover - defensive
                    pass

        try:
            return _run_coro_blocking(_introspect, timeout), None
        except Exception as exc:  # noqa: BLE001 - absence is not fatal
            log.debug("Portal introspection failed: %s", exc)
            return set(), f"portal not reachable: {exc}"


    def start_hotkeys(self, callback: HotkeyCallback) -> None:
        """Bind the push-to-talk shortcut via the GlobalShortcuts portal.

        Raises:
            HotkeyPortalError: If ``dbus-next`` is missing, the portal is
                unavailable/denied, or the configured hotkey is invalid. The
                message always includes a remediation (bind a system shortcut
                to run ``talkpaste dictate-toggle``).
        """

        self._callback = callback
        if self._listener is not None:
            log.debug("Hotkey listener already running; restarting it")
            self.stop_hotkeys()

        try:
            hotkey = parse_hotkey(self.settings.hotkeys.push_to_talk)
        except Exception as exc:
            raise HotkeyPortalError(
                f"Invalid push-to-talk hotkey "
                f"{self.settings.hotkeys.push_to_talk!r}: {exc}"
            ) from exc

        if not _dbus_next_available():
            raise HotkeyPortalError(
                "Global hotkeys need the XDG Desktop Portal via the Python "
                "package 'dbus-next', which is not installed. Install it with "
                "`pip install dbus-next`, or bind a system shortcut in your "
                "desktop settings to run `talkpaste dictate-toggle`."
            )

        listener = _GlobalShortcutsListener(hotkey, _SHORTCUT_ID, self._dispatch)
        listener.start()
        self._listener = listener
        log.info("Wayland global shortcuts active via the XDG GlobalShortcuts portal")

    def stop_hotkeys(self) -> None:
        """Stop the portal listener. Idempotent."""

        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception as exc:  # noqa: BLE001 - defensive
                log.debug("Error stopping hotkey listener: %s", exc)

    def _dispatch(self, action: HotkeyAction, event: HotkeyEvent) -> None:
        callback = self._callback
        if callback is None:
            return
        callback(action, event)


    def insert_text(self, text: str) -> PasteResult:
        """Copy ``text`` and paste it, degrading gracefully to copy-only.

        Never raises for the expected "cannot inject" cases — the outcome is
        always reported in the returned :class:`PasteResult`.
        """

        if not text:
            return PasteResult(
                copied=False,
                injected=False,
                method="none",
                error="Nothing to paste (empty text).",
            )

        backend = self._clipboard_backend()

        # Preserve the current clipboard only when we intend to paste+restore.
        want_restore = (
            self.settings.paste.restore_clipboard
            and self.settings.paste.mode == PasteMode.PASTE
        )
        previous: str | None = None
        if want_restore:
            try:
                previous = backend.get_text()
            except Exception:  # noqa: BLE001 - restore is best-effort
                previous = None

        if not backend.set_text(text):
            return PasteResult(
                copied=False,
                injected=False,
                method=backend.name,
                error=(
                    "Could not write to the clipboard. Install wl-clipboard "
                    "(`sudo apt install wl-clipboard`)."
                ),
            )

        if self.settings.paste.mode == PasteMode.COPY_ONLY:
            log.info("Copy-only mode: text copied to clipboard (%s)", backend.name)
            return PasteResult(
                copied=True,
                injected=False,
                method=f"{backend.name} (copy-only)",
                needs_manual_paste=True,
                error="Copy-only mode — text copied, press Ctrl+V to paste.",
            )

        # PASTE mode: give the clipboard a moment to settle, then inject.
        self._sleep_ms(self.settings.paste.paste_delay_ms)
        injected, method, note = self._try_inject()
        if injected:
            log.info("Paste injected via %s", method)
            if want_restore and previous is not None:
                self._sleep_ms(self.settings.paste.restore_delay_ms)
                try:
                    backend.set_text(previous)
                except Exception:  # noqa: BLE001 - best-effort restore
                    pass
            return PasteResult(copied=True, injected=True, method=method)

        log.info("Automatic paste unavailable (%s); copied only", note or "no method")
        return PasteResult(
            copied=True,
            injected=False,
            method="copy-only",
            needs_manual_paste=True,
            error=(
                note
                or "Wayland: automatic paste unavailable — text copied, "
                "press Ctrl+V to paste."
            ),
        )

    def _try_inject(self) -> tuple[bool, str, str | None]:
        """Attempt injection: RemoteDesktop portal, then opt-in ydotool.

        Returns ``(injected, method, note)`` — ``note`` explains the fallback
        when injection was not performed.
        """

        notes: list[str] = []

        ok, err = self._try_remote_desktop_paste()
        if ok:
            return True, "portal RemoteDesktop Ctrl+V", None
        if err:
            notes.append(err)

        if self.settings.paste.allow_ydotool:
            ok, err = self._try_ydotool_paste()
            if ok:
                return True, "ydotool Ctrl+V", None
            if err:
                notes.append(err)
        else:
            notes.append(
                "ydotool disabled (set paste.allow_ydotool to opt in)"
            )

        combined = "; ".join(notes) if notes else None
        message = (
            f"Wayland: automatic paste unavailable ({combined}) — text copied, "
            "press Ctrl+V to paste."
            if combined
            else None
        )
        return False, "copy-only", message

    def _try_remote_desktop_paste(self) -> tuple[bool, str | None]:
        """Inject Ctrl+V via the RemoteDesktop portal (reusing its session)."""

        if self._rd_unavailable:
            return False, self._rd_note

        if not _dbus_next_available():
            self._rd_unavailable = True
            self._rd_note = "RemoteDesktop portal unavailable (dbus-next not installed)"
            return False, self._rd_note

        try:
            if self._rd_session is None:
                session = _RemoteDesktopSession()
                session.start()
                self._rd_session = session
            self._rd_session.type_ctrl_v()
            return True, None
        except Exception as exc:  # noqa: BLE001 - fall back cleanly
            # Do not keep re-prompting the user for the rest of this run.
            self._rd_unavailable = True
            self._rd_note = f"RemoteDesktop portal unavailable ({exc})"
            if self._rd_session is not None:
                try:
                    self._rd_session.stop()
                except Exception:  # pragma: no cover - defensive
                    pass
                self._rd_session = None
            log.info("RemoteDesktop paste not used: %s", exc)
            return False, self._rd_note

    def _try_ydotool_paste(self) -> tuple[bool, str | None]:
        """Inject Ctrl+V via the external ``ydotool`` (opt-in, advanced)."""

        import subprocess

        if shutil.which("ydotool") is None:
            return (
                False,
                "ydotool enabled but not found on PATH (install 'ydotool' and "
                "run the ydotoold daemon)",
            )
        try:
            proc = subprocess.run(
                [
                    "ydotool",
                    "key",
                    f"{_YDOTOOL_CTRL}:1",
                    f"{_YDOTOOL_V}:1",
                    f"{_YDOTOOL_V}:0",
                    f"{_YDOTOOL_CTRL}:0",
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                return True, None
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            return (
                False,
                "ydotool failed (is ydotoold running?): "
                f"{stderr or 'exit code ' + str(proc.returncode)}",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"ydotool error: {exc}"


    def get_clipboard(self) -> str | None:
        try:
            return self._clipboard_backend().get_text()
        except Exception as exc:  # noqa: BLE001 - never raise for clipboard reads
            log.debug("Clipboard read failed: %s", exc)
            return None

    def set_clipboard(self, text: str) -> bool:
        try:
            return self._clipboard_backend().set_text(text)
        except Exception as exc:  # noqa: BLE001 - report failure as False
            log.debug("Clipboard write failed: %s", exc)
            return False


    def close(self) -> None:
        """Release the portal listener and any RemoteDesktop session."""

        self.stop_hotkeys()
        session = self._rd_session
        self._rd_session = None
        if session is not None:
            try:
                session.stop()
            except Exception as exc:  # noqa: BLE001 - defensive
                log.debug("Error stopping RemoteDesktop session: %s", exc)


    @staticmethod
    def _clipboard_backend() -> Any:
        from app.services.clipboard import get_clipboard_backend

        return get_clipboard_backend()

    @staticmethod
    def _sleep_ms(milliseconds: int) -> None:
        if milliseconds and milliseconds > 0:
            time.sleep(milliseconds / 1000.0)
