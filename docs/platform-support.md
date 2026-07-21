# Platform support

Run `python -m app.cli diagnose-platform -v` at any time to see exactly what is
available in your current session.

## Capability matrix

| Capability | Windows 10/11 | Linux / X11 | Linux / Wayland |
| --- | --- | --- | --- |
| Global hotkeys | Win32 `WH_KEYBOARD_LL` low-level hook | `pynput` global listener | XDG portal *GlobalShortcuts* (if the compositor supports it) |
| Paste injection | `SendInput` Ctrl+V | `xdotool key ctrl+v` | XDG portal *RemoteDesktop*, or opt-in `ydotool`; else copy-only |
| Clipboard | native / Qt | `xclip` / `xsel` / Qt | `wl-clipboard` / Qt |
| Copy-only fallback | ✅ | ✅ | ✅ (default when injection unavailable) |

## Windows

- Native `ctypes` Win32 — no `pynput`. A low-level keyboard hook runs on a
  dedicated message-loop thread so both key press and release are observed
  (needed for push-to-talk).
- Paste is synthesised with `SendInput`. Injecting into an **elevated** target
  (an app running as administrator) fails with access-denied; TalkPaste detects
  this, degrades to copy-only, and tells you to run it as administrator if you
  need injection there.
- Default shortcuts avoid F12: Ctrl+Alt+Space (hold), Ctrl+Alt+Shift+Space
  (toggle), Esc (cancel).

## Linux / X11

- Hotkeys via `pynput`; requires `DISPLAY` to be set.
- Paste via `xdotool`. If `xdotool` is missing, TalkPaste falls back to
  copy-only and logs the exact reason. Install: `sudo apt install xdotool`.
- Clipboard needs `xclip` or `xsel` (`sudo apt install xclip`). Without a
  clipboard tool even copy-only cannot place text — install one.

## Linux / Wayland

Wayland deliberately restricts global input for security, so this is
**best-effort with graceful, explicit fallback** — it never fails silently and
never requires root. See [wayland-notes.md](wayland-notes.md) for the full
story. In short:

- Hotkeys: try the *GlobalShortcuts* portal; if unavailable, bind a system
  shortcut to `talkpaste dictate-toggle`.
- Paste: try the *RemoteDesktop* portal; then opt-in `ydotool`; else copy-only.
- Clipboard: `wl-clipboard` (`sudo apt install wl-clipboard`).

## macOS / other

Not a target platform. TalkPaste still imports and the CLI runs; a `NullAdapter`
provides copy-only behaviour where possible so nothing crashes.
