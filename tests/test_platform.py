"""Tests for platform detection and the platform-adapter factory."""

from __future__ import annotations

from app.models import PlatformKind, Settings
from app.platform.base import (
    NullAdapter,
    PasteResult,
    create_platform_adapter,
    detect_platform_kind,
)


def test_detect_windows():
    assert detect_platform_kind({}, "win32") is PlatformKind.WINDOWS


def test_detect_macos():
    assert detect_platform_kind({}, "darwin") is PlatformKind.MACOS


def test_detect_x11_from_session():
    env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
    assert detect_platform_kind(env, "linux") is PlatformKind.LINUX_X11


def test_detect_wayland_from_session():
    env = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
    assert detect_platform_kind(env, "linux") is PlatformKind.LINUX_WAYLAND


def test_detect_wayland_without_session_var():
    # No XDG_SESSION_TYPE but WAYLAND_DISPLAY present.
    env = {"WAYLAND_DISPLAY": "wayland-0"}
    assert detect_platform_kind(env, "linux") is PlatformKind.LINUX_WAYLAND


def test_detect_x11_without_session_var():
    env = {"DISPLAY": ":0"}
    assert detect_platform_kind(env, "linux") is PlatformKind.LINUX_X11


def test_detect_wayland_preferred_when_both_present():
    env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    assert detect_platform_kind(env, "linux") is PlatformKind.LINUX_WAYLAND


def test_detect_unknown_linux():
    assert detect_platform_kind({}, "linux") is PlatformKind.UNKNOWN


def test_factory_unknown_returns_null_adapter():
    adapter = create_platform_adapter(Settings(), PlatformKind.MACOS)
    assert isinstance(adapter, NullAdapter)
    caps = adapter.detect_capabilities()
    assert not caps.hotkey_available
    assert caps.notes  # explains the limitation


def test_null_adapter_insert_is_copy_only():
    adapter = NullAdapter(Settings(), PlatformKind.UNKNOWN)
    result = adapter.insert_text("hello")
    assert isinstance(result, PasteResult)
    assert not result.injected
    # copied depends on a clipboard backend being present; either way it must
    # request a manual paste and never raise.
    assert result.needs_manual_paste


def test_capabilities_add_note_dedupes():
    from app.platform.base import PlatformCapabilities

    caps = PlatformCapabilities(kind=PlatformKind.LINUX_X11)
    caps.add_note("hi")
    caps.add_note("hi")
    assert caps.notes == ["hi"]
