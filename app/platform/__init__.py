"""Platform adapters: global-hotkey capture and text injection.

This layer is deliberately isolated behind :class:`~app.platform.base.PlatformAdapter`
so that native helpers (Rust/C++, ydotool, portals) can replace parts later
without touching the services or UI layers.

Use :func:`create_platform_adapter` to obtain the correct adapter for the
current OS / session.
"""

from __future__ import annotations

from app.platform.base import (
    HotkeyAction,
    HotkeyEvent,
    PasteResult,
    PlatformAdapter,
    PlatformCapabilities,
    create_platform_adapter,
    detect_platform,
)

__all__ = [
    "HotkeyAction",
    "HotkeyEvent",
    "PasteResult",
    "PlatformAdapter",
    "PlatformCapabilities",
    "create_platform_adapter",
    "detect_platform",
]
