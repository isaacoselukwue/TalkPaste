"""Tiny local control channel over a Unix domain socket.

Purpose: on Wayland (and anywhere global hotkeys are unavailable) the user can
bind a *system* shortcut to run ``talkpaste dictate-toggle``; that command
connects here and toggles the already-running headless/tray instance. It is
also handy for scripting and testing.

The protocol is deliberately trivial: the client connects, sends a single
newline-terminated command, reads a single newline-terminated reply, and
disconnects. Supported commands: ``toggle``, ``begin``, ``end``, ``cancel``,
``status``, ``ping``, ``quit``.

Only implemented on platforms with ``AF_UNIX`` (Linux/macOS). On Windows the
native keyboard hook is used instead, so the control channel is not required;
:func:`is_supported` returns ``False`` there and the server is a no-op.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from pathlib import Path

from app.logging_setup import get_logger

log = get_logger("ipc")

CommandHandler = Callable[[str], str]

VALID_COMMANDS = ("toggle", "begin", "end", "cancel", "status", "ping", "quit")


def is_supported() -> bool:
    """Whether Unix-domain-socket IPC is available on this platform."""

    return hasattr(socket, "AF_UNIX")


class ControlServer:
    """Accepts control commands and dispatches them to a handler."""

    def __init__(self, socket_path: str | Path, handler: CommandHandler) -> None:
        self.socket_path = Path(socket_path)
        self.handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> bool:
        """Start listening. Returns False (with a log) if unsupported/failed."""

        if not is_supported():
            log.info("Control socket unsupported on this platform; skipping")
            return False
        try:
            self._cleanup_stale()
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(self.socket_path))
            self._sock.listen(4)
            self._sock.settimeout(0.5)
        except OSError as exc:
            log.warning("Could not open control socket %s: %s", self.socket_path, exc)
            self._sock = None
            return False
        self._running = True
        self._thread = threading.Thread(target=self._serve, name="ipc-server", daemon=True)
        self._thread.start()
        log.info("Control socket listening at %s", self.socket_path)
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:  # pragma: no cover
                pass
            self._sock = None
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:  # pragma: no cover
            pass

    def _serve(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:  # socket closed
                break
            with conn:
                try:
                    conn.settimeout(2.0)
                    data = conn.recv(1024).decode("utf-8", errors="replace").strip()
                    command = data.splitlines()[0].strip().lower() if data else ""
                    reply = self._dispatch(command)
                    conn.sendall((reply + "\n").encode("utf-8"))
                except OSError as exc:  # pragma: no cover
                    log.debug("Control connection error: %s", exc)

    def _dispatch(self, command: str) -> str:
        if not command:
            return "error: empty command"
        if command not in VALID_COMMANDS:
            return f"error: unknown command {command!r}"
        try:
            return self.handler(command)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Control handler failed for %r", command)
            return f"error: {exc}"

    def _cleanup_stale(self) -> None:
        """Remove a stale socket file left by a crashed prior instance."""

        if not self.socket_path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.3)
            probe.connect(str(self.socket_path))
        except OSError:
            # Nobody is listening — safe to remove.
            try:
                self.socket_path.unlink()
            except OSError:  # pragma: no cover
                pass
        else:
            raise OSError(f"Another instance is already listening at {self.socket_path}")
        finally:
            probe.close()


def send_command(socket_path: str | Path, command: str, timeout: float = 2.0) -> str | None:
    """Send one command to a running :class:`ControlServer`.

    Returns the reply string, or ``None`` if no server is reachable.
    """

    if not is_supported():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall((command.strip() + "\n").encode("utf-8"))
        return sock.recv(1024).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    finally:
        sock.close()
