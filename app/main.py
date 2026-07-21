"""GUI entry point — launches the system-tray dictation app.

Kept separate from :mod:`app.cli` so the CLI never imports PySide6. If Qt is
not installed we fail with a clear, actionable message rather than a raw
``ImportError`` traceback.
"""

from __future__ import annotations

import sys

from app import APP_NAME
from app.config import get_paths, load_settings
from app.logging_setup import configure_logging, get_logger

log = get_logger("main")


def main(argv: list[str] | None = None) -> int:
    """Launch the tray application. Returns a process exit code."""

    paths = get_paths().ensure()
    settings = load_settings(paths)
    configure_logging(level=settings.general.log_level, log_dir=paths.logs_dir, force=True)

    try:
        from app.ui.tray_app import run_tray
    except ImportError as exc:  # pragma: no cover - depends on Qt presence
        log.error("PySide6 is not available: %s", exc)
        sys.stderr.write(
            f"{APP_NAME}: the graphical tray app requires PySide6.\n"
            "Install it with:  pip install PySide6\n"
            "Or use the headless CLI:  python -m app.cli run-headless\n"
        )
        return 1

    log.info("Launching %s tray app", APP_NAME)
    return run_tray(settings=settings, paths=paths, argv=argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
