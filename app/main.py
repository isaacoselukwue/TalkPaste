"""GUI entry point — launches the system-tray dictation app.

Kept separate from :mod:`app.cli` so the CLI never imports PySide6. If Qt is
not installed we fail with a clear, actionable message rather than a raw
``ImportError`` traceback.
"""

from __future__ import annotations

import sys

from app import APP_NAME, __version__
from app.config import get_paths, load_settings
from app.logging_setup import configure_logging, get_logger

log = get_logger("main")

# (module, critical?). sounddevice needs system PortAudio on Linux, so it is
# only critical on Windows, where the DLL is bundled.
_SELFCHECK_IMPORTS = [
    ("PySide6.QtWidgets", True),
    ("numpy", True),
    ("faster_whisper", True),
    ("ctranslate2", True),
    ("onnxruntime", True),
    ("sounddevice", sys.platform == "win32"),
]


def _selfcheck() -> int:
    """Import the heavy bundled deps and exit non-zero if a critical one fails."""

    sys.stderr.write(f"{APP_NAME} {__version__} self-check ({sys.platform})\n")
    ok = True
    for module, critical in _SELFCHECK_IMPORTS:
        try:
            __import__(module)
            sys.stderr.write(f"  [ok]   {module}\n")
        except BaseException as exc:  # noqa: BLE001 - report every failure kind
            tag = "FAIL" if critical else "warn"
            sys.stderr.write(f"  [{tag}] {module}: {exc}\n")
            if critical:
                ok = False
    sys.stderr.write("self-check: PASS\n" if ok else "self-check: FAIL\n")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """Launch the tray application. Returns a process exit code."""

    args = sys.argv[1:] if argv is None else argv
    if "--selfcheck" in args:
        return _selfcheck()

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
