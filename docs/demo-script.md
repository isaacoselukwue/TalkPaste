# Demo & screenshots

This repo cannot capture real screenshots in CI, so the images referenced by the
README are **placeholders** for a maintainer to fill. Filenames are fixed so the
README links resolve once you drop the images in `assets/`.

## Screenshots to capture (save under `assets/`)

| File | What to show |
| --- | --- |
| `assets/screenshot-tray.png` | The tray icon + right-click menu (states visible) |
| `assets/screenshot-settings.png` | The Settings window, Model tab |
| `assets/screenshot-shortcuts.png` | The Shortcuts tab with `QKeySequenceEdit` |
| `assets/screenshot-popup.png` | The status popup mid-“Listening…” |
| `assets/screenshot-diagnostics.png` | `diagnose-platform` output in a terminal |

Recommended: PNG, ~1200px wide, light theme, cropped tight.

## Producing them

1. Install and launch the tray app:
   ```bash
   pip install -r requirements.txt
   python -m app.main
   ```
2. Trigger a dictation (hold Ctrl+Alt+Space) to reach the *Listening* and
   *Processing* states, and grab the popup.
3. Open **Settings…** from the tray menu; screenshot the Model and Shortcuts
   tabs.
4. In a terminal, capture the diagnostics text:
   ```bash
   python -m app.cli diagnose-platform -v
   ```

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
