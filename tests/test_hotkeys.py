"""Tests for the portable hotkey spec parser."""

from __future__ import annotations

import pytest

from app.platform.hotkeys import HotkeyParseError, parse_hotkey


def test_parse_simple_combo():
    hk = parse_hotkey("Ctrl+Alt+Space")
    assert hk.ctrl and hk.alt and hk.key == "space"
    assert not hk.shift and not hk.meta
    assert hk.modifiers == frozenset({"ctrl", "alt"})


def test_parse_with_shift_and_meta():
    hk = parse_hotkey("Ctrl+Alt+Shift+Space")
    assert hk.ctrl and hk.alt and hk.shift and hk.key == "space"
    hk2 = parse_hotkey("Super+L")
    assert hk2.meta and hk2.key == "l"


def test_modifier_aliases():
    assert parse_hotkey("Control+C").ctrl
    assert parse_hotkey("Win+D").meta
    assert parse_hotkey("Cmd+Q").meta
    assert parse_hotkey("Option+A").alt


def test_key_aliases():
    assert parse_hotkey("Escape").key == "esc"
    assert parse_hotkey("Esc").key == "esc"
    assert parse_hotkey("Return").key == "enter"
    assert parse_hotkey("PgUp").key == "pageup"


def test_function_keys():
    assert parse_hotkey("F5").key == "f5"
    assert parse_hotkey("Ctrl+F12").key == "f12"


def test_case_insensitive():
    assert parse_hotkey("ctrl+alt+space") == parse_hotkey("CTRL+ALT+SPACE")


def test_normalized_roundtrip():
    hk = parse_hotkey("alt+ctrl+space")
    assert hk.normalized() == "Ctrl+Alt+Space"
    assert parse_hotkey("shift+a").normalized() == "Shift+A"


def test_no_modifier():
    hk = parse_hotkey("Esc")
    assert not hk.has_modifier
    assert hk.key == "esc"


def test_empty_raises():
    with pytest.raises(HotkeyParseError):
        parse_hotkey("")
    with pytest.raises(HotkeyParseError):
        parse_hotkey("   ")


def test_modifier_only_raises():
    with pytest.raises(HotkeyParseError):
        parse_hotkey("Ctrl+Alt")


def test_hotkey_is_hashable():
    combos = {parse_hotkey("Ctrl+Space"), parse_hotkey("Ctrl+Space")}
    assert len(combos) == 1
