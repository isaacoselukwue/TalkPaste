"""TalkPaste command-line interface (the CLI-first milestone).

Run ``python -m app.cli --help`` for the full command list. The CLI is fully
functional without any GUI code — importing this module never imports PySide6.

Commands
--------
* ``transcribe FILE``       — transcribe a WAV file and print the transcript
* ``record-once``           — record from the mic for N seconds and transcribe
* ``list-audio-devices``    — enumerate input devices
* ``diagnose-platform``     — report platform + hotkey/paste capabilities
* ``run-headless``          — run the full dictation loop without a tray
* ``dictate-*``             — send control commands to a running instance
* ``config-path`` / ``version``
"""

from __future__ import annotations

import io
import json as _json
import signal
import sys
import threading
import time
from pathlib import Path

import typer

from app import APP_NAME, __version__
from app.config import get_paths, load_settings, save_settings
from app.logging_setup import configure_logging, get_logger
from app.models import AppState, ModelProfile, Settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=f"{APP_NAME} — fully-local push-to-talk dictation.",
)

log = get_logger("cli")


def _bootstrap(log_level: str | None = None) -> tuple[Settings, object]:
    """Load settings, resolve paths and configure logging. Returns (settings, paths)."""

    paths = get_paths().ensure()
    settings = load_settings(paths)
    level = log_level or settings.general.log_level
    configure_logging(level=level, log_dir=paths.logs_dir, force=True)
    return settings, paths


def _apply_profile(settings: Settings, profile: str | None) -> None:
    if not profile:
        return
    try:
        settings.asr.profile = ModelProfile(profile.lower())
    except ValueError as exc:
        valid = ", ".join(p.value for p in ModelProfile)
        raise typer.BadParameter(
            f"Unknown profile {profile!r}. Choose from: {valid}"
        ) from exc


def _control_socket_path() -> Path:
    return get_paths().data_dir / "control.sock"


@app.command()
def transcribe(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                help="Path to a WAV file to transcribe."),
    raw: bool = typer.Option(False, "--raw", help="Print the raw transcript before formatting."),
    no_format: bool = typer.Option(False, "--no-format", help="Skip deterministic formatting."),
    profile: str | None = typer.Option(None, "--profile", help="fast | balanced | accurate."),
    as_json: bool = typer.Option(False, "--json", help="Emit a JSON object with metadata."),
) -> None:
    """Transcribe a WAV file locally and print the transcript."""

    settings, _ = _bootstrap()
    _apply_profile(settings, profile)
    if no_format:
        # Truly raw output: disable every post-processing step.
        fmt = settings.formatting
        fmt.enable_commands = False
        fmt.remove_fillers = False
        fmt.enable_snippets = False
        fmt.enable_dictionary = False
        fmt.british_english = False
        fmt.auto_capitalize = False
        fmt.normalize_whitespace = False
        fmt.trim_trailing_space = False

    from app.services.controller import DictationController

    controller = DictationController(settings)
    typer.echo(f"Transcribing {file} ...", err=True)
    result = controller.transcribe_file(file)

    if result.error:
        typer.secho(f"Error: {result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if as_json:
        t = result.transcription
        payload = {
            "text": result.final_text,
            "raw_text": result.raw_text,
            "language": t.language if t else None,
            "duration": t.duration if t else None,
            "inference_seconds": t.inference_seconds if t else None,
            "backend": t.backend if t else None,
            "model": t.model if t else None,
            "rewritten": result.rewritten,
        }
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if raw:
        typer.secho("--- raw ---", fg=typer.colors.BRIGHT_BLACK, err=True)
        typer.echo(result.raw_text)
        typer.secho("--- formatted ---", fg=typer.colors.BRIGHT_BLACK, err=True)
    typer.echo(result.final_text)


@app.command("record-once")
def record_once(
    seconds: float = typer.Option(5.0, "--seconds", "-s", min=0.5, help="Seconds to record."),
    insert: bool = typer.Option(False, "--insert", help="Insert the result into the focused app."),
    profile: str | None = typer.Option(None, "--profile"),
) -> None:
    """Record from the microphone for N seconds, transcribe, and print."""

    settings, _ = _bootstrap()
    _apply_profile(settings, profile)

    from app.services.audio_engine import AudioEngine, AudioError
    from app.services.controller import DictationController

    controller = DictationController(settings)
    engine = AudioEngine(settings.audio)
    typer.secho(f"Recording for {seconds:.1f}s ... speak now.", fg=typer.colors.CYAN, err=True)
    try:
        audio = engine.record_for(seconds)
    except AudioError as exc:
        typer.secho(f"Audio error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        engine.close()

    typer.secho("Transcribing ...", err=True)
    result = controller.process_audio(audio, sample_rate=settings.audio.sample_rate)
    if result.error:
        typer.secho(f"Error: {result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo(result.final_text)
    if insert and result.final_text.strip():
        paste = controller._insert_text(result.final_text)  # noqa: SLF001 - intentional reuse
        if paste.needs_manual_paste:
            typer.secho(f"Copied to clipboard ({paste.method}); paste manually.",
                        fg=typer.colors.YELLOW, err=True)
        elif paste.injected:
            typer.secho("Inserted into focused app.", fg=typer.colors.GREEN, err=True)


@app.command("list-audio-devices")
def list_audio_devices() -> None:
    """List available microphone input devices."""

    _bootstrap()
    from app.services.audio_engine import AudioError
    from app.services.audio_engine import list_audio_devices as _list

    try:
        devices = _list()
    except AudioError as exc:
        typer.secho(f"Audio error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if not devices:
        typer.secho("No input devices found.", fg=typer.colors.YELLOW)
        return

    typer.secho("Input devices:", bold=True)
    for d in devices:
        default = "  [default]" if d.is_default else ""
        typer.echo(
            f"  [{d.index:>2}] {d.name}  "
            f"({d.max_input_channels}ch, {int(d.default_samplerate)} Hz){default}"
        )


@app.command("diagnose-platform")
def diagnose_platform(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show raw detail key/values."),
) -> None:
    """Report the platform and which hotkey/paste paths are available."""

    settings, _ = _bootstrap()
    from app.platform.base import create_platform_adapter, detect_platform_kind

    kind = detect_platform_kind()
    typer.secho(f"{APP_NAME} platform diagnosis", bold=True)
    typer.echo(f"  Detected platform : {kind.value}")

    adapter = create_platform_adapter(settings, kind)
    caps = adapter.detect_capabilities()

    def yn(value: bool) -> str:
        return (typer.style("yes", fg=typer.colors.GREEN) if value
                else typer.style("no", fg=typer.colors.RED))

    typer.echo(f"  Session type      : {caps.session_type or 'n/a'}")
    typer.echo(f"  Global hotkeys    : {yn(caps.hotkey_available)}  ({caps.hotkey_method})")
    typer.echo(f"  Paste injection   : {yn(caps.paste_available)}  ({caps.paste_method})")
    typer.echo(f"  Clipboard         : {yn(caps.clipboard_available)}  ({caps.clipboard_method})")

    if caps.notes:
        typer.secho("  Notes:", bold=True)
        for note in caps.notes:
            typer.echo(f"    • {note}")

    if verbose and caps.details:
        typer.secho("  Details:", bold=True)
        for key, value in caps.details.items():
            typer.echo(f"    {key} = {value}")

    typer.echo(f"  Config directory  : {get_paths().data_dir}")
    if not caps.is_fully_functional:
        typer.secho(
            "  → Some capabilities are limited on this platform/session; see notes.",
            fg=typer.colors.YELLOW,
        )


@app.command("run-headless")
def run_headless(
    preload: bool = typer.Option(True, "--preload/--no-preload",
                                 help="Warm the ASR model at startup."),
) -> None:
    """Run the full dictation loop without a GUI (Ctrl+C to quit)."""

    settings, paths = _bootstrap()
    from app.services.controller import DictationController
    from app.services.ipc import ControlServer

    stop_event = threading.Event()

    def on_state(state: AppState, message: str) -> None:
        colour = {
            AppState.LISTENING: typer.colors.CYAN,
            AppState.PROCESSING: typer.colors.BLUE,
            AppState.READY: typer.colors.GREEN,
            AppState.ERROR: typer.colors.RED,
        }.get(state, typer.colors.WHITE)
        typer.secho(f"[{state.value:>10}] {message}", fg=colour, err=True)

    controller = DictationController(settings, paths=paths, on_state=on_state)
    controller.start()
    if preload:
        controller.preload_model()

    # Control socket for `dictate-toggle` (Wayland/manual-shortcut fallback).
    def handle(command: str) -> str:
        if command in ("toggle",):
            controller.toggle_dictation()
        elif command == "begin":
            controller.begin_dictation()
        elif command == "end":
            controller.end_dictation()
        elif command == "cancel":
            controller.cancel()
        elif command == "quit":
            stop_event.set()
        elif command in ("status", "ping"):
            pass
        return f"ok {controller.state.value}"

    control = ControlServer(_control_socket_path(), handle)
    control.start()

    hk = settings.hotkeys
    typer.secho(f"{APP_NAME} running headless.", fg=typer.colors.GREEN, bold=True, err=True)
    typer.secho(
        f"  push-to-talk: {hk.push_to_talk}   toggle: {hk.hands_free_toggle}   "
        f"cancel: {hk.cancel}",
        err=True,
    )
    typer.secho("  (If hotkeys are unavailable, bind a system shortcut to "
                "`talkpaste dictate-toggle`.)", fg=typer.colors.BRIGHT_BLACK, err=True)
    typer.secho("  Press Ctrl+C to quit.", fg=typer.colors.BRIGHT_BLACK, err=True)

    def _sigint(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sigint)
    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        typer.secho("\nShutting down ...", err=True)
        control.stop()
        controller.close()


def _dictate_control(command: str) -> None:
    from app.services.ipc import is_supported, send_command

    if not is_supported():
        typer.secho("Control socket not supported on this platform.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)
    reply = send_command(_control_socket_path(), command)
    if reply is None:
        typer.secho(
            "No running TalkPaste instance found. Start one with `talkpaste run-headless` "
            "(or launch the tray app).",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=4)
    typer.echo(reply)


@app.command("dictate-toggle")
def dictate_toggle() -> None:
    """Toggle dictation on a running instance (bind to a system shortcut)."""

    _bootstrap()
    _dictate_control("toggle")


@app.command("dictate-cancel")
def dictate_cancel() -> None:
    """Cancel an in-progress dictation on a running instance."""

    _bootstrap()
    _dictate_control("cancel")


@app.command("config-path")
def config_path() -> None:
    """Print the resolved data/config directory and key file locations."""

    paths = get_paths()
    typer.echo(f"data_dir    : {paths.data_dir}")
    typer.echo(f"settings    : {paths.config_file}")
    typer.echo(f"dictionary  : {paths.dictionary_file}")
    typer.echo(f"snippets    : {paths.snippets_file}")
    typer.echo(f"history     : {paths.history_file}")
    typer.echo(f"logs        : {paths.logs_dir}")
    typer.echo(f"models      : {paths.models_dir}")


@app.command("init-config")
def init_config() -> None:
    """Write a default settings.json (and empty dictionary/snippets) if absent."""

    paths = get_paths().ensure()
    settings = load_settings(paths)
    save_settings(settings, paths)
    for f in (paths.dictionary_file, paths.snippets_file):
        if not f.exists():
            f.write_text("{}\n", encoding="utf-8")
    typer.secho(f"Wrote configuration under {paths.data_dir}", fg=typer.colors.GREEN)


@app.command()
def history(
    last: int = typer.Option(10, "--last", "-n", help="Number of recent entries to show."),
    full: bool = typer.Option(False, "--full", help="Print full text (not a preview)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the entries as JSON."),
    path: bool = typer.Option(False, "--path", help="Just print the history file path."),
) -> None:
    """Show recent transcript history."""

    settings, paths = _bootstrap()
    if path:
        typer.echo(str(paths.history_file))
        return

    from app.services.history_store import HistoryStore

    store = HistoryStore(paths.history_file, settings.history.max_entries)
    entries = store.recent(last)
    if not entries:
        typer.secho("No transcripts recorded yet.", fg=typer.colors.YELLOW)
        return

    if as_json:
        typer.echo(_json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False))
        return

    for entry in entries:
        header = f"{entry.timestamp}  ·  {entry.backend}/{entry.model}  ·  {entry.word_count} words"
        typer.secho(header, fg=typer.colors.BRIGHT_BLACK)
        if full:
            typer.echo(entry.text)
        else:
            preview = " ".join(entry.text.split())
            typer.echo(preview[:200] + ("..." if len(preview) > 200 else ""))
        typer.echo("")


@app.command()
def version() -> None:
    """Print the TalkPaste version."""

    typer.echo(f"{APP_NAME} {__version__}")


class _PlainConsoleWriter(io.TextIOBase):
    """A text stream that forwards writes but reports no console file handle."""

    def __init__(self, wrapped: io.TextIOBase) -> None:
        self._wrapped = wrapped

    def write(self, text: str) -> int:
        self._wrapped.write(text)
        self._wrapped.flush()
        return len(text)

    def flush(self) -> None:
        try:
            self._wrapped.flush()
        except OSError:
            pass

    def isatty(self) -> bool:
        return False


def _harden_windows_console() -> None:
    """Route console output through plain UTF-8 writes on Windows.

    Some Windows consoles (the VS Code terminal, mintty, redirected handles)
    accept Python's own writes but fail Click/Colorama's ``WriteConsoleW`` with
    ``OSError: [WinError 6]``. Presenting streams with no console file handle
    makes Click fall back to the plain writes that work everywhere.
    """

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        wrapped = io.TextIOWrapper(
            buffer, encoding="utf-8", errors="replace", line_buffering=True
        )
        setattr(sys, name, _PlainConsoleWriter(wrapped))


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point."""

    _harden_windows_console()
    app(args=argv)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
