"""Transcript history persistence (append-only JSONL).

Each dictation produces one :class:`HistoryEntry`, appended as a single JSON
line to ``history.jsonl``. JSONL keeps writes cheap (append, no rewrite) and
survives partial writes gracefully — a corrupt final line is skipped on read.

The store prunes to ``max_entries`` (newest kept) on write when a limit is set.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.logging_setup import get_logger

log = get_logger("history")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class HistoryEntry:
    """A single transcript history record."""

    text: str
    raw_text: str = ""
    timestamp: str = field(default_factory=_now_iso)
    duration: float = 0.0
    inference_seconds: float = 0.0
    backend: str = ""
    model: str = ""
    rewritten: bool = False
    paste_method: str = ""

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> HistoryEntry:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class HistoryStore:
    """Append-only transcript history."""

    def __init__(self, path: Path, max_entries: int = 500) -> None:
        self.path = Path(path)
        self.max_entries = max_entries

    def add(self, entry: HistoryEntry) -> None:
        """Append ``entry`` and prune to ``max_entries`` if a limit is set."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        log.debug("Recorded transcript (%d words) to %s", entry.word_count, self.path)
        if self.max_entries and self.max_entries > 0:
            self._prune()

    def all(self) -> list[HistoryEntry]:
        """Return all entries oldest-first, skipping any corrupt lines."""

        if not self.path.exists():
            return []
        entries: list[HistoryEntry] = []
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(HistoryEntry.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError) as exc:
                        log.warning("Skipping corrupt history line %d: %s", lineno, exc)
        except OSError as exc:
            log.error("Could not read history from %s: %s", self.path, exc)
        return entries

    def recent(self, limit: int = 20) -> list[HistoryEntry]:
        """Return up to ``limit`` most-recent entries, newest first."""

        entries = self.all()
        entries.reverse()
        return entries[:limit] if limit > 0 else entries

    def count(self) -> int:
        return len(self.all())

    def clear(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError as exc:  # pragma: no cover
                log.error("Could not clear history %s: %s", self.path, exc)

    def _prune(self) -> None:
        entries = self.all()
        if len(entries) <= self.max_entries:
            return
        keep = entries[-self.max_entries :]
        tmp = self.path.with_suffix(".jsonl.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for entry in keep:
                    fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            tmp.replace(self.path)
            log.debug("Pruned history to %d entries", len(keep))
        except OSError as exc:  # pragma: no cover
            log.error("Could not prune history: %s", exc)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
