# Changelog

All notable changes to TalkPaste are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-07-21

### Fixed
- Windows packaging: bundle the runtime library assets PyInstaller's import
  scan does not collect (faster-whisper's Silero VAD `silero_vad_v6.onnx`,
  sounddevice's PortAudio DLL, and the onnxruntime and CTranslate2 native
  libraries). Fixes the missing-ONNX crash and no-audio or VAD failures in the
  packaged app. UPX is disabled on Windows to avoid corrupting Qt and
  onnxruntime DLLs.
- Windows CLI: route console output through plain UTF-8 writes so `typer`
  status messages no longer raise `OSError: [WinError 6]` on consoles where
  `WriteConsoleW` fails (the VS Code terminal, mintty, redirected handles).

## [0.1.0] - 2026-07-21

### Added
- Initial release of TalkPaste, a fully-local, cross-platform push-to-talk
  dictation app for Windows 10/11 and Ubuntu 22.04+.
- CLI-first milestone: `transcribe`, `record-once`, `list-audio-devices`,
  `diagnose-platform`, `run-headless`, plus `dictate-toggle`/`dictate-cancel`,
  `config-path`, `init-config` and `version`.
- Pluggable ASR backend interface with a `faster-whisper` default (CPU int8)
  and an optional `whisper.cpp` low-RAM backend.
- Model profiles: fast (`tiny.en`), balanced (`base.en`), accurate
  (`small.en`), plus multilingual and custom modes. Models are lazy-loaded and
  never bundled.
- Deterministic text pipeline: filler removal, spoken punctuation/formatting
  commands, snippet expansion, custom dictionary, developer casing helpers and
  British-English spelling.
- Optional local LLM rewrite mode via `llama-cpp-python` (off by default,
  grammar/punctuation cleanup only, with a hard timeout).
- Three isolated platform adapters: Windows (Win32 low-level hook + SendInput),
  Linux/X11 (pynput + xdotool) and Linux/Wayland (XDG portals, ydotool opt-in,
  copy-only fallback).
- System-tray app with clear states, a full settings window
  (`QKeySequenceEdit` shortcuts, model/profile/language, paste behaviour, audio
  device, dictionary/snippet editors, Wayland diagnostics) and a status popup.
- Transcript history, structured logging and atomic JSON persistence under a
  per-user data directory.
- PyInstaller packaging specs and GitHub Actions CI/release workflows.

[Unreleased]: https://github.com/isaacoselukwue/TalkPaste
[0.1.1]: https://github.com/isaacoselukwue/TalkPaste/releases/tag/v0.1.1
[0.1.0]: https://github.com/isaacoselukwue/TalkPaste/releases/tag/v0.1.0
