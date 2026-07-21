#!/usr/bin/env python3
"""Backward-compatible convenience launcher.

Usage:

    python main.py                      # launch the tray app (GUI)
    python main.py --transcribe a.wav   # transcribe a file and print it
    python main.py --help               # full CLI help

Any arguments other than the ``--transcribe FILE`` shortcut are forwarded to
the full Typer CLI (:mod:`app.cli`), so ``python main.py diagnose-platform``
works too.
"""

from __future__ import annotations

import sys


def _run() -> int:
    argv = sys.argv[1:]

    # Backward-compatible shortcut: `python main.py --transcribe FILE`.
    if argv and argv[0] in ("--transcribe", "-t"):
        if len(argv) < 2:
            sys.stderr.write("usage: python main.py --transcribe FILE.wav\n")
            return 2
        from app.cli import main as cli_main

        cli_main(["transcribe", *argv[1:]])
        return 0

    # No arguments -> launch the GUI tray app.
    if not argv:
        from app.main import main as gui_main

        return gui_main()

    # Otherwise, forward everything to the Typer CLI.
    from app.cli import main as cli_main

    cli_main(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
