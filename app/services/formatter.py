"""Deterministic text post-processing pipeline.

Applies, in order:

1. filler-word removal (``um``, ``uh`` ...)
2. spoken-command parsing (:mod:`app.services.commands`)
3. snippet expansion
4. custom dictionary replacements
5. British-English spelling conversion (preferred output dialect)
6. whitespace normalisation + sentence capitalisation

Every step is individually toggleable via :class:`~app.models.FormattingSettings`
and exposed as a method so it can be unit-tested in isolation. The pipeline is
pure: same input + settings always yields the same output.

Note on ordering: the blueprint lists "normalise capitalisation" early, but
correct sentence casing depends on where the command parser places full stops
and line breaks, so capitalisation is applied as the final step. Whitespace is
also finalised last for the same reason.
"""

from __future__ import annotations

import re

from app.logging_setup import get_logger
from app.models import FormattingSettings
from app.services.commands import CommandParser

log = get_logger("formatter")

#: Conservative default filler list: safe interjections only. Riskier fillers
#: ("you know", "like", "sort of") are opt-in via ``extra_fillers`` so we never
#: destroy meaning by default.
DEFAULT_FILLERS: tuple[str, ...] = (
    "um", "umm", "ummm", "uhm", "uh", "uhh", "uhhh",
    "er", "err", "erm", "ah", "ahh", "hmm", "hmmm", "mm", "mmm", "mhm",
)

_WORD_STRIP = ".,!?;:\"'()[]{}"

#: American-to-British spelling map (case-preserving). Curated to avoid
#: ambiguous words (e.g. "program", "practice") that change meaning by dialect.
BRITISH_SPELLINGS: dict[str, str] = {
    "color": "colour", "colors": "colours", "colored": "coloured", "coloring": "colouring",
    "favorite": "favourite", "favorites": "favourites", "favor": "favour", "favors": "favours",
    "honor": "honour", "honored": "honoured", "flavor": "flavour", "flavors": "flavours",
    "neighbor": "neighbour", "neighbors": "neighbours", "behavior": "behaviour",
    "behaviors": "behaviours", "labor": "labour", "humor": "humour", "rumor": "rumour",
    "harbor": "harbour", "odor": "odour", "vapor": "vapour", "savior": "saviour",
    "organize": "organise", "organized": "organised", "organizes": "organises",
    "organizing": "organising", "organization": "organisation", "organizations": "organisations",
    "realize": "realise", "realized": "realised", "realizes": "realises", "realizing": "realising",
    "recognize": "recognise", "recognized": "recognised", "recognizing": "recognising",
    "apologize": "apologise", "apologized": "apologised", "analyze": "analyse",
    "analyzed": "analysed", "analyzing": "analysing", "paralyze": "paralyse",
    "customize": "customise", "customized": "customised", "prioritize": "prioritise",
    "optimize": "optimise", "optimized": "optimised", "optimizing": "optimising",
    "summarize": "summarise", "emphasize": "emphasise", "minimize": "minimise",
    "maximize": "maximise", "categorize": "categorise", "capitalize": "capitalise",
    "center": "centre", "centered": "centred", "centers": "centres",
    "theater": "theatre", "meter": "metre", "meters": "metres", "liter": "litre",
    "fiber": "fibre", "fibers": "fibres", "caliber": "calibre",
    "defense": "defence", "offense": "offence", "license": "licence", "pretense": "pretence",
    "catalog": "catalogue", "dialog": "dialogue", "analog": "analogue",
    "gray": "grey", "grey": "grey", "mold": "mould", "plow": "plough",
    "traveled": "travelled", "traveling": "travelling", "traveler": "traveller",
    "canceled": "cancelled", "canceling": "cancelling", "modeling": "modelling",
    "labeled": "labelled", "labeling": "labelling", "fueled": "fuelled",
    "aluminum": "aluminium", "artifact": "artefact", "artifacts": "artefacts",
}


class Formatter:
    """Deterministic transcript cleaner and command applier."""

    def __init__(
        self,
        settings: FormattingSettings | None = None,
        dictionary: dict[str, str] | None = None,
        snippets: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings or FormattingSettings()
        self.dictionary = dictionary or {}
        self.snippets = snippets or {}
        self._command_parser = CommandParser(self.settings)
        self._filler_phrases = self._build_filler_phrases()


    def format(self, text: str) -> str:
        """Run the full deterministic pipeline over ``text``."""

        if text is None:
            return ""
        original = text

        if self.settings.remove_fillers:
            text = self.remove_fillers(text)
        if self.settings.enable_commands:
            text = self._command_parser.apply(text)
        if self.settings.enable_snippets and self.snippets:
            text = self.apply_snippets(text)
        if self.settings.enable_dictionary and self.dictionary:
            text = self.apply_dictionary(text)
        if self.settings.british_english:
            text = self.to_british(text)
        text = self.normalize(text)

        log.debug("Formatted %d chars -> %d chars", len(original), len(text))
        return text


    def remove_fillers(self, text: str) -> str:
        """Drop filler words/phrases, preserving all other tokens."""

        if not self._filler_phrases:
            return text
        tokens = text.split()
        cmp = [t.lower().strip(_WORD_STRIP) for t in tokens]
        keep: list[str] = []
        i = 0
        n = len(tokens)
        max_len = max(len(p) for p in self._filler_phrases)
        while i < n:
            matched = 0
            for length in range(min(max_len, n - i), 0, -1):
                phrase = tuple(cmp[i : i + length])
                if phrase in self._filler_phrases:
                    matched = length
                    break
            if matched:
                i += matched
            else:
                keep.append(tokens[i])
                i += 1
        return " ".join(keep)

    def apply_snippets(self, text: str) -> str:
        """Expand snippet triggers into their full text."""

        return _apply_replacements(text, self.snippets)

    def apply_dictionary(self, text: str) -> str:
        """Apply custom dictionary word/phrase replacements."""

        return _apply_replacements(text, self.dictionary)

    def to_british(self, text: str) -> str:
        """Convert common American spellings to British, preserving case."""

        def repl(match: re.Match[str]) -> str:
            word = match.group(0)
            replacement = BRITISH_SPELLINGS.get(word.lower())
            if replacement is None:
                return word
            return _match_case(word, replacement)

        return _WORD_BOUNDARY_RE.sub(repl, text)

    def normalize(self, text: str) -> str:
        """Normalise whitespace and apply sentence capitalisation."""

        if self.settings.normalize_whitespace:
            text = re.sub(r"[ ]{2,}", " ", text)
            text = re.sub(r"\s+([,.;:!?])", r"\1", text)
            text = re.sub(r"[ \t]+(\n)", r"\1", text)
            text = re.sub(r"\n{3,}", "\n\n", text)

        if self.settings.auto_capitalize:
            text = _capitalize_sentences(text)
            text = re.sub(r"\bi\b", "I", text)
            text = re.sub(r"\bi'", "I'", text)

        if self.settings.trim_trailing_space:
            text = text.strip()

        return text


    def _build_filler_phrases(self) -> frozenset[tuple[str, ...]]:
        phrases: set[tuple[str, ...]] = set()
        for entry in list(DEFAULT_FILLERS) + list(self.settings.extra_fillers or []):
            words = tuple(entry.lower().split())
            if words:
                phrases.add(words)
        return frozenset(phrases)


_WORD_BOUNDARY_RE = re.compile(r"[A-Za-z]+")


def _match_case(source: str, replacement: str) -> str:
    """Return ``replacement`` cased to match ``source``."""

    if source.isupper():
        return replacement.upper()
    if source[:1].isupper():
        return replacement.capitalize()
    return replacement


def _capitalize_sentences(text: str) -> str:
    """Capitalise the first letter of the text and after ``. ! ? \\n``."""

    out: list[str] = []
    capitalize_next = True
    openers = "\"'([{-*"  # things that may precede a sentence's first letter
    for ch in text:
        if ch.isalpha():
            out.append(ch.upper() if capitalize_next else ch)
            capitalize_next = False
        else:
            out.append(ch)
            if ch in ".!?\n":
                capitalize_next = True
            elif ch.isspace() or ch in openers:
                # Keep any pending capitalisation pending.
                pass
            else:
                capitalize_next = False
    return "".join(out)


def _apply_replacements(text: str, mapping: dict[str, str]) -> str:
    """Case-insensitive whole-word/phrase replacement using ``mapping``."""

    if not mapping:
        return text
    # Longest keys first so multi-word triggers win over their prefixes.
    keys = sorted(mapping, key=len, reverse=True)
    lookup = {k.lower(): v for k, v in mapping.items()}
    pattern = re.compile(
        r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)",
        re.IGNORECASE,
    )

    def repl(match: re.Match[str]) -> str:
        return lookup.get(match.group(0).lower(), match.group(0))

    return pattern.sub(repl, text)
