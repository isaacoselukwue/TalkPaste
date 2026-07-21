# Packaging TalkPaste

TalkPaste ships as a small application **shell**. Models are never bundled — they
are downloaded/located at runtime under the per-user data directory
(`talkpaste config-path` shows exactly where). This keeps the executable small
and lets users pick model sizes to suit their hardware.

## Build per platform (no cross-compilation)

PyInstaller cannot cross-compile. Build each artefact on its target OS.

### Linux (Ubuntu 22.04+)

```bash
sudo apt install xdotool wl-clipboard   # runtime helpers (paste/clipboard)
./scripts/build_linux.sh
# -> dist/talkpaste/talkpaste
tar -C dist -czf talkpaste-linux-x86_64.tar.gz talkpaste
```

### Windows 10/11

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# -> dist\talkpaste\talkpaste.exe
Compress-Archive -Path dist\talkpaste\* -DestinationPath talkpaste-windows-x64.zip
```

## What the spec files do

- `talkpaste-linux.spec` / `talkpaste-windows.spec` build the windowed GUI
  entry point (`app/main.py`) with `console=False`.
- Optional heavy backends (`llama_cpp` for rewrite) are excluded from the base
  build; users install them on demand when enabling those features.
- The CLI is available inside the bundle too, but the primary artefact is the
  tray GUI. For a headless-only deployment, ship the source + `pip install`.

## Store / distribution notes

Build per OS as above and publish via **GitHub Releases** — the tag-driven
workflow in [../.github/workflows/release.yml](../.github/workflows/release.yml)
does this automatically. Microsoft Store (MSIX) and Snap/Flathub are possible
but require developer accounts and manual packaging.
