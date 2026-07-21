"""Deterministic spoken-command parser.

This runs *before* any LLM logic and turns spoken commands embedded in a
transcript into their literal effect. It is intentionally rule-based and fully
deterministic so behaviour is predictable and testable.

Supported commands (case-insensitive, punctuation-tolerant):

* line breaks — ``new line``, ``new paragraph``, ``press enter``, ``tab``
* punctuation — ``comma``, ``period`` / ``full stop``, ``question mark``,
  ``exclamation mark``/``point``, ``colon``, ``semicolon``, ``hyphen``/``dash``
* grouping — ``open/close quote``, ``open/close bracket`` (round),
  ``open/close square bracket``
* lists — ``bullet list``/``bullet point``, ``numbered list``
* editing — ``scratch that`` (delete last sentence),
  ``undo last phrase``/``undo that`` (delete last phrase)
* code casing — ``snake case``, ``camel case`` (always on); ``kebab case``,
  ``constant case``, ``pascal case`` (developer mode only)
* developer helpers — ``command flag`` / ``cli flag`` (prefix ``--``)

The parser works on Whisper output, which already contains capitalisation and
some punctuation; command matching strips surrounding punctuation and case so
it is robust to that. Normal (non-command) words are emitted verbatim so the
model's own punctuation survives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from app.logging_setup import get_logger
from app.models import FormattingSettings

log = get_logger("commands")

# A "word" token, optionally carrying attached punctuation from the model.
_TOKEN_RE = re.compile(r"\S+")
# Characters stripped from a token to compute its comparison form.
_STRIP_CHARS = ".,!?;:\"'()[]{}"


class _Kind(str, Enum):
    WORD = "word"
    PUNCT = "punct"      # attaches to previous token, no leading space
    OPEN = "open"        # opening quote/bracket, suppresses following space
    NEWLINE = "newline"  # hard break; suppresses surrounding spaces
    MARKER = "marker"    # list prefix / tab; suppresses following space


_SENTENCE_ENDERS = (".", "!", "?")


@dataclass
class _Piece:
    text: str
    kind: _Kind


class _Builder:
    """Accumulates output pieces with correct spacing and edit support."""

    def __init__(self) -> None:
        self._pieces: list[_Piece] = []


    @property
    def empty(self) -> bool:
        return not self._pieces

    def _last_kind(self) -> _Kind | None:
        return self._pieces[-1].kind if self._pieces else None

    def _at_line_start(self) -> bool:
        last = self._last_kind()
        return last is None or last in (_Kind.NEWLINE, _Kind.MARKER)

    def _leading_space(self) -> str:
        last = self._last_kind()
        if last is None or last in (_Kind.OPEN, _Kind.NEWLINE, _Kind.MARKER):
            return ""
        return " "


    def add_word(self, word: str) -> None:
        if not word:
            return
        self._pieces.append(_Piece(self._leading_space() + word, _Kind.WORD))

    def add_punct(self, punct: str) -> None:
        # Attaches directly to the previous token with no leading space.
        self._pieces.append(_Piece(punct, _Kind.PUNCT))

    def add_open(self, opener: str) -> None:
        self._pieces.append(_Piece(self._leading_space() + opener, _Kind.OPEN))

    def add_newline(self, text: str = "\n") -> None:
        self._pieces.append(_Piece(text, _Kind.NEWLINE))

    def add_tab(self) -> None:
        self._pieces.append(_Piece("\t", _Kind.MARKER))

    def add_marker(self, marker: str, *, ensure_line_start: bool = True) -> None:
        if ensure_line_start and not self._at_line_start():
            self.add_newline()
        self._pieces.append(_Piece(marker, _Kind.MARKER))

    def undo_last_phrase(self) -> None:
        """Remove the trailing run of words (the last spoken phrase)."""

        removed = False
        while self._pieces and self._pieces[-1].kind is _Kind.WORD:
            self._pieces.pop()
            removed = True
        if not removed and self._pieces:
            # Top was a boundary token (punct/open/marker/newline): drop one.
            self._pieces.pop()

    def scratch_that(self) -> None:
        """Remove back to (and keeping) the previous sentence boundary."""

        while self._pieces:
            top = self._pieces[-1]
            if top.kind is _Kind.NEWLINE:
                break
            if top.kind is _Kind.PUNCT and top.text.strip().endswith(_SENTENCE_ENDERS):
                break
            self._pieces.pop()

    def render(self) -> str:
        return "".join(p.text for p in self._pieces)


# Command definitions


class _CaseMode(str, Enum):
    NONE = "none"
    SNAKE = "snake"
    CAMEL = "camel"
    KEBAB = "kebab"
    CONSTANT = "constant"
    PASCAL = "pascal"
    FLAG = "flag"


@dataclass(frozen=True)
class _Command:
    """A recognised multi-word command phrase and how to apply it."""

    phrase: tuple[str, ...]
    action: str            # dispatch key
    arg: str = ""          # optional payload (e.g. the punctuation char)
    dev_only: bool = False

    @property
    def length(self) -> int:
        return len(self.phrase)


def _build_command_table() -> list[_Command]:
    cmds: list[_Command] = [
        # Line breaks (longest phrases first is handled by sorting later).
        _Command(("new", "paragraph"), "newline", "\n\n"),
        _Command(("new", "line"), "newline", "\n"),
        _Command(("press", "enter"), "newline", "\n"),
        _Command(("next", "line"), "newline", "\n"),
        _Command(("tab",), "tab"),
        _Command(("tab", "key"), "tab"),
        # Punctuation.
        _Command(("full", "stop"), "punct", "."),
        _Command(("period",), "punct", "."),
        _Command(("comma",), "punct", ","),
        _Command(("question", "mark"), "punct", "?"),
        _Command(("exclamation", "mark"), "punct", "!"),
        _Command(("exclamation", "point"), "punct", "!"),
        _Command(("semicolon",), "punct", ";"),
        _Command(("semi", "colon"), "punct", ";"),
        _Command(("colon",), "punct", ":"),
        _Command(("hyphen",), "punct", "-"),
        _Command(("dash",), "punct", "-"),
        _Command(("ellipsis",), "punct", "…"),
        # Grouping.
        _Command(("open", "quote"), "open", '"'),
        _Command(("open", "quotes"), "open", '"'),
        _Command(("close", "quote"), "punct", '"'),
        _Command(("close", "quotes"), "punct", '"'),
        _Command(("open", "square", "bracket"), "open", "["),
        _Command(("close", "square", "bracket"), "punct", "]"),
        _Command(("open", "bracket"), "open", "("),
        _Command(("close", "bracket"), "punct", ")"),
        _Command(("open", "paren"), "open", "("),
        _Command(("close", "paren"), "punct", ")"),
        _Command(("open", "parenthesis"), "open", "("),
        _Command(("close", "parenthesis"), "punct", ")"),
        # Lists.
        _Command(("bullet", "list"), "bullet"),
        _Command(("bullet", "point"), "bullet"),
        _Command(("new", "bullet"), "bullet"),
        _Command(("numbered", "list"), "numbered"),
        _Command(("number", "list"), "numbered"),
        _Command(("next", "item"), "numbered"),
        # Editing.
        _Command(("scratch", "that"), "scratch"),
        _Command(("undo", "last", "phrase"), "undo"),
        _Command(("undo", "that"), "undo"),
        # Casing (core).
        _Command(("snake", "case"), "case", _CaseMode.SNAKE.value),
        _Command(("camel", "case"), "case", _CaseMode.CAMEL.value),
        # Casing (developer mode).
        _Command(("kebab", "case"), "case", _CaseMode.KEBAB.value, dev_only=True),
        _Command(("constant", "case"), "case", _CaseMode.CONSTANT.value, dev_only=True),
        _Command(("screaming", "snake", "case"), "case", _CaseMode.CONSTANT.value, dev_only=True),
        _Command(("pascal", "case"), "case", _CaseMode.PASCAL.value, dev_only=True),
        _Command(("command", "flag"), "case", _CaseMode.FLAG.value, dev_only=True),
        _Command(("cli", "flag"), "case", _CaseMode.FLAG.value, dev_only=True),
    ]
    # Longest phrases must be tried first so "open square bracket" wins over
    # "open bracket"-style prefixes.
    cmds.sort(key=lambda c: c.length, reverse=True)
    return cmds


_COMMAND_TABLE = _build_command_table()
_MAX_PHRASE_LEN = max(c.length for c in _COMMAND_TABLE)


@dataclass
class _Token:
    raw: str      # original token (keeps model punctuation/case)
    cmp: str      # lowercased, punctuation-stripped comparison form


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(0)
        cmp = raw.lower().strip(_STRIP_CHARS)
        tokens.append(_Token(raw=raw, cmp=cmp))
    return tokens


# Case conversion helpers


def _clean_words(words: list[str]) -> list[str]:
    """Lower-case and strip punctuation from words destined for an identifier."""

    cleaned: list[str] = []
    for w in words:
        stripped = re.sub(r"[^0-9A-Za-z]+", "", w)
        if stripped:
            cleaned.append(stripped.lower())
    return cleaned


def convert_case(words: list[str], mode: _CaseMode) -> str:
    """Convert a list of spoken words into a cased identifier."""

    parts = _clean_words(words)
    if not parts:
        return ""
    if mode is _CaseMode.SNAKE:
        return "_".join(parts)
    if mode is _CaseMode.KEBAB:
        return "-".join(parts)
    if mode is _CaseMode.CONSTANT:
        return "_".join(p.upper() for p in parts)
    if mode is _CaseMode.CAMEL:
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    if mode is _CaseMode.PASCAL:
        return "".join(p.capitalize() for p in parts)
    if mode is _CaseMode.FLAG:
        return "--" + "-".join(parts)
    return " ".join(parts)


class CommandParser:
    """Parses spoken commands out of a transcript into literal text."""

    def __init__(self, settings: FormattingSettings | None = None) -> None:
        self.settings = settings or FormattingSettings()

    def apply(self, text: str) -> str:
        """Return ``text`` with all recognised spoken commands applied."""

        if not text or not text.strip():
            return text

        tokens = _tokenize(text)
        builder = _Builder()
        numbered_counter = 0
        case_mode = _CaseMode.NONE
        case_buffer: list[str] = []

        def flush_case() -> None:
            nonlocal case_mode, case_buffer
            if case_mode is not _CaseMode.NONE and case_buffer:
                identifier = convert_case(case_buffer, case_mode)
                if identifier:
                    builder.add_word(identifier)
            case_mode = _CaseMode.NONE
            case_buffer = []

        i = 0
        n = len(tokens)
        while i < n:
            cmd, span = self._match_command(tokens, i)
            if cmd is not None:
                # A command ends any pending case-conversion run.
                if cmd.action != "case":
                    flush_case()

                if cmd.action == "newline":
                    builder.add_newline(cmd.arg)
                elif cmd.action == "tab":
                    builder.add_tab()
                elif cmd.action == "punct":
                    if cmd.arg in ('"',) and self._is_open_quote(builder):
                        builder.add_open('"')
                    else:
                        builder.add_punct(cmd.arg)
                elif cmd.action == "open":
                    builder.add_open(cmd.arg)
                elif cmd.action == "bullet":
                    builder.add_marker("- ")
                elif cmd.action == "numbered":
                    numbered_counter += 1
                    builder.add_marker(f"{numbered_counter}. ")
                elif cmd.action == "scratch":
                    builder.scratch_that()
                elif cmd.action == "undo":
                    builder.undo_last_phrase()
                elif cmd.action == "case":
                    flush_case()
                    case_mode = _CaseMode(cmd.arg)
                i += span
                continue

            # Not a command: a plain word.
            token = tokens[i]
            if case_mode is not _CaseMode.NONE:
                case_buffer.append(token.raw)
            else:
                builder.add_word(token.raw)
            i += 1

        flush_case()
        return builder.render()


    def _match_command(
        self, tokens: list[_Token], start: int
    ) -> tuple[_Command | None, int]:
        """Return the longest command matching at ``start`` and its span."""

        max_span = min(_MAX_PHRASE_LEN, len(tokens) - start)
        window = [tokens[start + k].cmp for k in range(max_span)]
        for cmd in _COMMAND_TABLE:
            if cmd.dev_only and not self.settings.developer_mode:
                continue
            length = cmd.length
            if length > max_span:
                continue
            if tuple(window[:length]) == cmd.phrase:
                return cmd, length
        return None, 0

    @staticmethod
    def _is_open_quote(builder: _Builder) -> bool:
        """Heuristic: an even count of quote chars so far means the next is an
        opening quote."""

        rendered = builder.render()
        return rendered.count('"') % 2 == 0
