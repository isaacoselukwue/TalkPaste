# Wayland notes

Wayland intentionally prevents apps from grabbing global keyboard input or
synthesising keystrokes into other windows. TalkPaste therefore treats Wayland
as **best-effort** and always degrades gracefully with a clear explanation —
never a silent failure, never a demand for root.

## Global hotkeys

TalkPaste first probes the **XDG Desktop Portal `GlobalShortcuts`** interface
over D-Bus. If the compositor implements it and you grant the binding,
push-to-talk maps to the portal's `Activated` / `Deactivated` signals.

Support varies by compositor (KDE Plasma has it; some GNOME versions do not). If
it is unavailable or denied, TalkPaste says so and you can use the reliable
fallback:

1. Start a running instance: `talkpaste run-headless` (or launch the tray app).
2. In your desktop's keyboard settings, bind a custom global shortcut to run:
   `talkpaste dictate-toggle`
3. Press it once to start dictation, again to stop. `talkpaste dictate-cancel`
   aborts.

This talks to the running instance over a local Unix socket in the data
directory, so no special privileges are needed.

## Paste injection

In `PASTE` mode TalkPaste tries, in order:

1. **XDG Portal `RemoteDesktop`** keyboard control — creates a session (you may
   be prompted once) and injects Ctrl+V. The session is cached for the run.
2. **`ydotool`** — only if you explicitly enable it (Settings → Paste → “Allow
   ydotool”, or `paste.allow_ydotool`). It is never a silent default and
   typically needs `ydotoold` running.
3. **Copy-only** — the text is placed on the clipboard and you press Ctrl+V. The
   status popup/notification tells you when this happens.

## Clipboard

Uses `wl-clipboard` (`wl-copy` / `wl-paste`). Install with
`sudo apt install wl-clipboard`. Qt's clipboard is used when the tray app is
running.

## Diagnosing

```bash
python -m app.cli diagnose-platform -v
```

reports which portal interfaces were found, whether `ydotool`/`wl-clipboard`
are present, and exactly which capability is unavailable and why.
