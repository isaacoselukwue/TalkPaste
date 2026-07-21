"""Tests for the local control socket (Unix-domain-socket IPC)."""

from __future__ import annotations

import time

import pytest

from app.services.ipc import ControlServer, is_supported, send_command

pytestmark = pytest.mark.skipif(not is_supported(), reason="AF_UNIX not available")


def test_roundtrip(tmp_path):
    received = []

    def handler(command: str) -> str:
        received.append(command)
        return f"ok:{command}"

    sock = tmp_path / "control.sock"
    server = ControlServer(sock, handler)
    assert server.start() is True
    try:
        # Give the accept loop a moment to spin up.
        time.sleep(0.1)
        reply = send_command(sock, "toggle")
        assert reply == "ok:toggle"
        assert received == ["toggle"]
    finally:
        server.stop()
    assert not sock.exists()


def test_unknown_command_rejected(tmp_path):
    server = ControlServer(tmp_path / "c.sock", lambda c: "handled")
    server.start()
    try:
        time.sleep(0.1)
        reply = send_command(tmp_path / "c.sock", "explode")
        assert reply is not None and reply.startswith("error")
    finally:
        server.stop()


def test_send_to_missing_server_returns_none(tmp_path):
    assert send_command(tmp_path / "nope.sock", "toggle") is None


def test_command_normalised_to_lowercase(tmp_path):
    seen = []
    server = ControlServer(tmp_path / "c.sock", lambda c: seen.append(c) or "ok")
    server.start()
    try:
        time.sleep(0.1)
        send_command(tmp_path / "c.sock", "TOGGLE")
        assert seen == ["toggle"]
    finally:
        server.stop()
