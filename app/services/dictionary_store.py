"""Per-user custom dictionary (spoken form → written form) persistence.

The dictionary is a flat JSON object mapping a spoken word or phrase to its
desired written output, e.g.::

    {
      "github": "GitHub",
      "javascript": "JavaScript",
      "my email": "isaac@example.com"
    }

Matching is case-insensitive and whole-word (handled by the formatter); the
store itself is a thin, atomic JSON persistence layer. :class:`MappingStore`
is the shared base reused by :mod:`app.services.snippets_store`.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.logging_setup import get_logger

log = get_logger("dictionary")


class MappingStore:
    """A persistent, case-insensitive ``str -> str`` mapping backed by JSON."""

    #: Overridden by subclasses for clearer logs.
    label = "mapping"

    def __init__(self, path: Path, autoload: bool = True) -> None:
        self.path = Path(path)
        self._entries: dict[str, str] = {}
        self._loaded = False
        if autoload:
            self.load()


    def load(self) -> dict[str, str]:
        """(Re)load entries from disk. Missing/invalid files yield ``{}``."""

        if not self.path.exists():
            self._entries = {}
            self._loaded = True
            return self._entries
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to read %s from %s (%s); starting empty",
                      self.label, self.path, exc)
            self._entries = {}
            self._loaded = True
            return self._entries
        if not isinstance(data, dict):
            log.error("%s file %s is not a JSON object; ignoring", self.label, self.path)
            data = {}
        self._entries = {str(k): str(v) for k, v in data.items()}
        self._loaded = True
        log.debug("Loaded %d %s entries from %s", len(self._entries), self.label, self.path)
        return self._entries

    def save(self) -> Path:
        """Atomically write entries to disk and return the path."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._entries, indent=2, ensure_ascii=False, sort_keys=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        log.debug("Saved %d %s entries to %s", len(self._entries), self.label, self.path)
        return self.path


    @property
    def entries(self) -> dict[str, str]:
        """Return a copy of the current entries."""

        return dict(self._entries)

    def as_mapping(self) -> dict[str, str]:
        """Alias for :attr:`entries` (used by the formatter)."""

        return dict(self._entries)

    def get(self, key: str) -> str | None:
        return self._find(key)

    def add(self, key: str, value: str, *, save: bool = True) -> None:
        """Add or update an entry (case-insensitive key). Optionally persist."""

        key = key.strip()
        if not key:
            raise ValueError("Key must be non-empty")
        # Replace any existing case-insensitive variant to avoid duplicates.
        existing = self._match_key(key)
        if existing is not None and existing != key:
            del self._entries[existing]
        self._entries[key] = value
        if save:
            self.save()

    def remove(self, key: str, *, save: bool = True) -> bool:
        """Remove an entry by (case-insensitive) key. Returns whether removed."""

        existing = self._match_key(key)
        if existing is None:
            return False
        del self._entries[existing]
        if save:
            self.save()
        return True

    def clear(self, *, save: bool = True) -> None:
        self._entries = {}
        if save:
            self.save()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return self._match_key(key) is not None


    def _match_key(self, key: str) -> str | None:
        low = key.strip().lower()
        for existing in self._entries:
            if existing.lower() == low:
                return existing
        return None

    def _find(self, key: str) -> str | None:
        existing = self._match_key(key)
        return self._entries[existing] if existing is not None else None


class DictionaryStore(MappingStore):
    """Custom dictionary: corrects spellings, expands acronyms, fixes casing."""

    label = "dictionary"
