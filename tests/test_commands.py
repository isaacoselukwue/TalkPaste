"""Unit tests for the spoken-command parser."""

from __future__ import annotations

import pytest

from app.models import FormattingSettings
from app.services.commands import CommandParser, _CaseMode, convert_case


@pytest.fixture()
def parser() -> CommandParser:
    return CommandParser(FormattingSettings())


@pytest.mark.parametrize(
    "text, expected",
    [
        ("hello comma world", "hello, world"),
        ("this is a test period", "this is a test."),
        ("wait full stop done", "wait. done"),
        ("really question mark", "really?"),
        ("wow exclamation mark", "wow!"),
        ("wow exclamation point", "wow!"),
        ("one semicolon two", "one; two"),
        ("label colon value", "label: value"),
    ],
)
def test_punctuation(parser, text, expected):
    assert parser.apply(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("line one new line line two", "line one\nline two"),
        ("para one new paragraph para two", "para one\n\npara two"),
        ("say this press enter next", "say this\nnext"),
    ],
)
def test_line_breaks(parser, text, expected):
    assert parser.apply(text) == expected


def test_tab_inserts_tab_char(parser):
    assert parser.apply("before tab after") == "before\tafter"


def test_quotes_and_brackets(parser):
    assert parser.apply("open quote hello close quote") == '"hello"'
    assert parser.apply("open bracket note close bracket") == "(note)"
    assert parser.apply("open square bracket x close square bracket") == "[x]"


def test_open_close_bracket_ordering():
    # "open square bracket" (3 words) must beat "open bracket" (2 words).
    p = CommandParser(FormattingSettings())
    assert p.apply("open square bracket") == "["


def test_bullet_list(parser):
    assert parser.apply("bullet list milk bullet list eggs") == "- milk\n- eggs"


def test_numbered_list_increments(parser):
    assert parser.apply("numbered list first numbered list second") == "1. first\n2. second"


def test_scratch_that_removes_last_sentence(parser):
    assert parser.apply("first sentence period second stuff scratch that") == "first sentence."


def test_undo_last_phrase(parser):
    # No boundary -> the whole run is one phrase and is removed.
    assert parser.apply("hello world undo last phrase") == ""
    # With a boundary, only the trailing phrase goes.
    assert parser.apply("keep this comma remove this undo last phrase") == "keep this,"


def test_snake_and_camel_case(parser):
    assert parser.apply("snake case my new variable") == "my_new_variable"
    assert parser.apply("camel case my new variable") == "myNewVariable"


def test_case_mode_flushes_at_next_command(parser):
    # A comma ends the case run.
    assert parser.apply("snake case hello world comma done") == "hello_world, done"


def test_dev_only_commands_gated():
    off = CommandParser(FormattingSettings(developer_mode=False))
    # Without developer mode "kebab" and "case" are plain words.
    assert off.apply("kebab case hello world") == "kebab case hello world"

    on = CommandParser(FormattingSettings(developer_mode=True))
    assert on.apply("kebab case hello world") == "hello-world"
    assert on.apply("constant case max size") == "MAX_SIZE"
    assert on.apply("pascal case my type") == "MyType"
    assert on.apply("cli flag dry run") == "--dry-run"


def test_punctuation_is_tolerant_of_model_punctuation(parser):
    # Whisper may already attach punctuation; command matching strips it.
    assert parser.apply("hello, comma. world") == "hello,, world"  # explicit comma still applied


def test_empty_input(parser):
    assert parser.apply("") == ""
    assert parser.apply("   ") == "   "


def test_convert_case_helpers():
    words = ["My", "New", "Var"]
    assert convert_case(words, _CaseMode.SNAKE) == "my_new_var"
    assert convert_case(words, _CaseMode.CAMEL) == "myNewVar"
    assert convert_case(words, _CaseMode.PASCAL) == "MyNewVar"
    assert convert_case(words, _CaseMode.CONSTANT) == "MY_NEW_VAR"
    assert convert_case(words, _CaseMode.KEBAB) == "my-new-var"
    assert convert_case([], _CaseMode.SNAKE) == ""
