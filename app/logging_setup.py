"""Centralised logging configuration for TalkPaste.

Logging goes to two places:

* the console (stderr), respecting the configured level, and
* a rotating file under the per-user logs directory, always at DEBUG so we
  have detail available for troubleshooting after the fact.

:func:`configure_logging` is idempotent and safe to call from both the CLI and
the tray app; it will not attach duplicate handlers if called twice.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_FILE_FORMAT = (
    "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] "
    "%(filename)s:%(lineno)d: %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    level: str | int = "INFO",
    log_dir: Path | None = None,
    *,
    force: bool = False,
) -> logging.Logger:
    """Configure the root logger and return the ``talkpaste`` logger.

    Parameters
    ----------
    level:
        Console log level as a name (``"DEBUG"``) or numeric level.
    log_dir:
        Directory for the rotating ``talkpaste.log`` file. When ``None`` the
        file handler is skipped (useful for tests).
    force:
        Reconfigure even if logging was already configured (e.g. after the
        user changes the log level in settings).
    """

    global _CONFIGURED

    root = logging.getLogger()

    if _CONFIGURED and not force:
        return logging.getLogger("talkpaste")

    # Clear any handlers we previously installed so re-configuration is clean.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    numeric_level = _coerce_level(level)
    # Root stays at the most verbose of the two sinks so the file can capture
    # DEBUG while the console shows only ``level``.
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(numeric_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "talkpaste.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _DATE_FORMAT))
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            root.warning("Could not open log file in %s: %s", log_dir, exc)

    # Tame chatty third-party loggers.
    for noisy in ("numba", "faster_whisper", "urllib3", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    logger = logging.getLogger("talkpaste")
    logger.debug("Logging configured (console level=%s)", logging.getLevelName(numeric_level))
    return logger


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``talkpaste`` namespace."""

    if name == "talkpaste" or name.startswith("talkpaste."):
        return logging.getLogger(name)
    return logging.getLogger(f"talkpaste.{name}")
