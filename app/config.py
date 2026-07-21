"""Per-user paths and settings load/save.

All mutable user state lives under a single platform-appropriate data
directory resolved via :mod:`platformdirs`:

* ``settings.json``   — the :class:`~app.models.Settings` tree
* ``dictionary.json`` — custom word/phrase replacements
* ``snippets.json``   — expandable snippets
* ``history.jsonl``   — transcript history (one JSON object per line)
* ``logs/``           — rotating log files
* ``models/``         — downloaded ASR / rewrite model files (never bundled)

The directory can be overridden with the ``TALKPASTE_DATA_DIR`` environment
variable, which is handy for tests and portable installs.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import platformdirs

from app import APP_AUTHOR, APP_SLUG
from app.logging_setup import get_logger
from app.models import Settings

log = get_logger("config")

_ENV_DATA_DIR = "TALKPASTE_DATA_DIR"


@dataclass(frozen=True)
class Paths:
    """Resolved locations for all persisted state."""

    data_dir: Path
    config_file: Path
    dictionary_file: Path
    snippets_file: Path
    history_file: Path
    logs_dir: Path
    models_dir: Path

    def ensure(self) -> Paths:
        """Create the directories that must exist. Returns self for chaining."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        return self


def get_data_dir() -> Path:
    """Resolve the root data directory (env override wins)."""

    override = os.environ.get(_ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_data_dir(APP_SLUG, APP_AUTHOR))


def get_paths(data_dir: Path | None = None) -> Paths:
    """Return the :class:`Paths` bundle, optionally under an explicit root."""

    root = data_dir or get_data_dir()
    return Paths(
        data_dir=root,
        config_file=root / "settings.json",
        dictionary_file=root / "dictionary.json",
        snippets_file=root / "snippets.json",
        history_file=root / "history.jsonl",
        logs_dir=root / "logs",
        models_dir=root / "models",
    )


def load_settings(paths: Paths | None = None) -> Settings:
    """Load settings from disk, returning defaults if absent or unreadable."""

    paths = paths or get_paths()
    path = paths.config_file
    if not path.exists():
        log.info("No settings file at %s; using defaults", path)
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to read settings from %s (%s); using defaults", path, exc)
        return Settings()
    settings = Settings.from_dict(data)
    log.debug("Loaded settings from %s", path)
    return settings


def save_settings(settings: Settings, paths: Paths | None = None) -> Path:
    """Atomically write settings to disk and return the file path."""

    paths = paths or get_paths()
    paths.ensure()
    _atomic_write_json(paths.config_file, settings.to_dict())
    log.debug("Saved settings to %s", paths.config_file)
    return paths.config_file


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON to ``path`` atomically (temp file + os.replace)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:  # pragma: no cover
                pass
