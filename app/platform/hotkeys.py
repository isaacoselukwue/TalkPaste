"""Portable hotkey spec parsing shared by all platform adapters.

Hotkeys are stored as Qt ``QKeySequence`` portable strings such as
``"Ctrl+Alt+Space"`` so they round-trip cleanly through the
``QKeySequenceEdit`` widget in settings. Each adapter must translate that
spec into its native representation (Win32 virtual-key codes, X11 keysyms via
pynput, portal shortcut descriptions), and they all start from the same parsed
:class:`Hotkey`.

This module is pure and dependency-free so it is trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "altgr": "alt",
    "shift": "shift",
    "meta": "meta",
    "super": "meta",
    "win": "meta",
    "windows": "meta",
    "cmd": "meta",
    "command": "meta",
}

_KEY_ALIASES = {
    "esc": "esc",
    "escape": "esc",
    "return": "enter",
    "enter": "enter",
    "space": "space",
    "spacebar": "space",
    "spc": "space",
    "tab": "tab",
    "del": "delete",
    "delete": "delete",
    "ins": "insert",
    "insert": "insert",
    "backspace": "backspace",
    "bksp": "backspace",
    "pgup": "pageup",
    "pageup": "pageup",
    "pgdn": "pagedown",
    "pagedown": "pagedown",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
}


class HotkeyParseError(ValueError):
    """Raised when a hotkey spec cannot be parsed."""


@dataclass(frozen=True)
class Hotkey:
    """A parsed, normalised hotkey.

    ``key`` is the non-modifier key, normalised to lower case (e.g. ``"space"``,
    ``"a"``, ``"f5"``, ``"esc"``). Modifier flags are booleans. ``raw`` keeps
    the original spec for display/logging.
    """

    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    meta: bool = False
    key: str = ""
    #: Original spec string, kept for display/logging only — excluded from
    #: equality/hashing so two specs differing only in case/spacing compare equal.
    raw: str = field(default="", compare=False)

    @property
    def modifiers(self) -> frozenset[str]:
        mods = set()
        if self.ctrl:
            mods.add("ctrl")
        if self.alt:
            mods.add("alt")
        if self.shift:
            mods.add("shift")
        if self.meta:
            mods.add("meta")
        return frozenset(mods)

    @property
    def has_modifier(self) -> bool:
        return bool(self.modifiers)

    def normalized(self) -> str:
        """Return a canonical spec string (modifiers sorted, title-cased)."""

        order = [("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"), ("meta", "Meta")]
        parts = [label for flag, label in order if getattr(self, flag)]
        if self.key:
            parts.append(_display_key(self.key))
        return "+".join(parts)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.normalized()


def _display_key(key: str) -> str:
    if len(key) == 1:
        return key.upper()
    special = {
        "esc": "Esc",
        "enter": "Enter",
        "space": "Space",
        "tab": "Tab",
        "pageup": "PageUp",
        "pagedown": "PageDown",
    }
    return special.get(key, key.capitalize())


def parse_hotkey(spec: str) -> Hotkey:
    """Parse a hotkey spec like ``"Ctrl+Alt+Space"`` into a :class:`Hotkey`.

    Raises :class:`HotkeyParseError` if the spec is empty or has no main key.
    Whitespace and case are tolerated; ``+`` is the separator.
    """

    if not spec or not spec.strip():
        raise HotkeyParseError("Empty hotkey spec")

    raw = spec.strip()
    tokens = [t.strip() for t in raw.replace("-", "+").split("+") if t.strip()]
    if not tokens:
        raise HotkeyParseError(f"Could not parse hotkey: {spec!r}")

    ctrl = alt = shift = meta = False
    key = ""

    for token in tokens:
        low = token.lower()
        if low in _MODIFIER_ALIASES:
            canon = _MODIFIER_ALIASES[low]
            ctrl = ctrl or canon == "ctrl"
            alt = alt or canon == "alt"
            shift = shift or canon == "shift"
            meta = meta or canon == "meta"
        else:
            # Main key. Last non-modifier token wins if several are given.
            key = _normalize_key(low)

    if not key:
        raise HotkeyParseError(f"Hotkey {spec!r} has no main key")

    return Hotkey(ctrl=ctrl, alt=alt, shift=shift, meta=meta, key=key, raw=raw)


def _normalize_key(token: str) -> str:
    if token in _KEY_ALIASES:
        return _KEY_ALIASES[token]
    # Function keys f1..f24 stay as-is (lower case).
    if len(token) >= 2 and token[0] == "f" and token[1:].isdigit():
        return token
    return token
