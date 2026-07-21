# How TalkPaste works

## Architecture

TalkPaste is split into three layers with strict boundaries:

- **`app/services/`** holds the platform-agnostic logic: audio capture, ASR
  backends, the deterministic text pipeline, optional rewrite, persistence
  stores and the orchestrating controller.
- **`app/platform/`** contains the three isolated platform adapters (Windows,
  X11, Wayland) behind a single `PlatformAdapter` interface. This is the only
  layer that touches native input/injection, so it can be replaced (e.g. with a
  Rust/C++ helper) without disturbing the rest.
- **`app/ui/`** provides the PySide6 tray app, settings window and status popup.
  The CLI never imports this layer, so the headless path has no Qt dependency.

A small spine underpins everything: `models.py` (the typed settings tree and
enums), `config.py` (per-user paths + load/save), `logging_setup.py`,
`asr_base.py` (the ASR interface) and `platform/base.py` (the adapter interface
and platform detection).

## The runtime pipeline

1. **Trigger.** The platform adapter reports a hotkey press (push-to-talk) or a
   toggle. On Wayland where global hotkeys may be unavailable, a system shortcut
   bound to `talkpaste dictate-toggle` drives the same path over a local socket.
2. **Capture.** `AudioEngine` opens a callback-driven `sounddevice` input stream
   at 16 kHz mono float32 into a bounded ring buffer, updating an RMS level for
   the UI meter.
3. **Finalise.** On release/toggle-off, capture stops and the buffer is handed
   to a worker thread, so the UI/event loop is never blocked.
4. **Transcribe.** The configured `ASRBackend` (faster-whisper by default) runs
   with Silero VAD (`vad_filter=True`), CPU int8, and the profile's model. The
   model is lazy-loaded on first use.
5. **Deterministic post-processing** (`formatter.py` + `commands.py`), in order:
   filler removal → spoken commands (punctuation, line breaks, lists, editing,
   casing) → snippet expansion → dictionary replacement → British-English
   spelling → whitespace + sentence capitalisation.
6. **Optional rewrite** (`rewrite.py`). If enabled and a GGUF model is present,
   a local llama.cpp model does grammar/punctuation-only cleanup, bounded by a
   hard timeout; on timeout/failure the deterministic text is used unchanged.
7. **Insert.** The adapter saves the clipboard, writes the text, injects paste,
   and restores the clipboard (all configurable). If injection is unavailable it
   degrades to copy-only and asks the user to paste manually.
8. **Persist.** The transcript is appended to `history.jsonl`; structured logs
   go to `logs/`.

## Threading model

- Hotkey callbacks arrive on the adapter's own thread.
- Recording start/stop is cheap and handled inline.
- Transcription + formatting + rewrite + injection run on a dedicated worker
  thread. In the GUI, controller state callbacks are marshalled onto the Qt
  thread via a queued signal.

## ASR & VAD

`ASRBackend` is a small interface (`load` / `is_loaded` / `transcribe` /
`unload`). `FasterWhisperBackend` is the default; `WhisperCppBackend` is an
optional low-RAM path via `pywhispercpp` bindings or a `whisper.cpp` binary.
Audio is always mono float32; non-16 kHz input is resampled. VAD parameters are
backend-neutral so another VAD can be swapped in.

## Text pipeline details

The command parser is fully deterministic and runs before any LLM. It tolerates
Whisper's own punctuation/capitalisation (command matching strips surrounding
punctuation and case) and emits plain words verbatim. Casing commands such as
`snake case` buffer the following words until the next command or sentence
boundary. Dictionary and snippet replacements are case-insensitive, whole-word,
longest-match-first.

## Persistence

All state lives under one per-user directory (`platformdirs`, overridable with
`TALKPASTE_DATA_DIR`): `settings.json`, `dictionary.json`, `snippets.json`,
`history.jsonl`, `logs/`, `models/`. Writes are atomic where it matters. Models
are never bundled into the executable.
