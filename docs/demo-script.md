# Demo & screenshots

## Screenshots (generated)

The UI screenshots in `assets/` are produced from the real widgets — no display
server needed — by:

```bash
python scripts/capture_screenshots.py
```

It renders under Qt's `offscreen` platform with sample data and writes
`screenshot-main.png`, `screenshot-listening.png`, `screenshot-settings.png`,
`screenshot-shortcuts.png`, `screenshot-diagnostics.png` and
`screenshot-popup.png`. Re-run it after any UI change. To capture on a real
desktop instead, launch `python -m app.main` and use your OS screenshot tool.

## Short screen capture (optional)

A 20–30s clip is the most convincing demo:

1. Record your screen (OBS, or `wf-recorder`/`peek` on Linux, Xbox Game Bar on
   Windows).
2. Script: open a text editor → hold the shortcut → say *“hello comma this is
   TalkPaste running fully offline period new line pretty neat exclamation
   mark”* → release → watch it type `Hello, this is TalkPaste running fully
   offline.` / `Pretty neat!`.
3. Export to `assets/demo.gif` (or `.mp4`) and embed it at the top of the
   README.

## Terminal cast (optional)

For a lightweight, copy-pasteable demo, record an
[asciinema](https://asciinema.org/) cast of the CLI:

```bash
asciinema rec assets/cli-demo.cast
# then run: transcribe, diagnose-platform, config-path
```
