# Assets

Screenshots here are **generated** (no display server required) by:

```bash
python scripts/capture_screenshots.py
```

It renders the real widgets under Qt's `offscreen` platform with sample data:

- `screenshot-main.png` — main window, idle
- `screenshot-listening.png` — main window, recording (level meter active)
- `screenshot-settings.png` — settings, Model tab
- `screenshot-shortcuts.png` — settings, Shortcuts tab (`QKeySequenceEdit`)
- `screenshot-diagnostics.png` — settings, platform diagnostics
- `screenshot-popup.png` — the status popup

Re-run the script after any UI change to refresh them. A short screen capture
(`demo.gif`) is still nice to add by hand — see
[../docs/demo-script.md](../docs/demo-script.md).
