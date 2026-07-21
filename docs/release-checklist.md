# Release checklist

## 1. Pre-flight

- [ ] `./scripts/run_tests.sh` is green on your machine.
- [ ] `ruff check .` and `mypy app` are clean.
- [ ] `CHANGELOG.md` updated; move items out of *Unreleased* into the version.
- [ ] Bump the version in `app/__init__.py` and `pyproject.toml` (keep them in
      sync).
- [ ] `python -m app.cli diagnose-platform` sanity-checked on each target OS.

## 2. Build per platform (no cross-compilation)

Build on each target OS — see [../packaging/README.md](../packaging/README.md).

- [ ] **Linux:** `./scripts/build_linux.sh` → `dist/talkpaste/`
- [ ] **Windows:** `powershell -File scripts\build_windows.ps1` → `dist\talkpaste\`
- [ ] Smoke-test each built binary: launch the tray, run one dictation
      (or copy-only), open Settings.

## 3. Tag & publish (GitHub)

CI (`.github/workflows/release.yml`) builds and attaches artefacts on a tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

- [ ] The Release workflow produced `talkpaste-linux-x86_64.tar.gz` and
      `talkpaste-windows-x64.zip` and attached them to the GitHub Release.
- [ ] Release notes reviewed (auto-generated notes + a short summary).

## 4. App-store distribution (optional)

These require developer accounts and are done manually; TalkPaste's build output
is the starting point.

### Microsoft Store (Windows)

- Register a [Partner Center](https://partner.microsoft.com/) developer account
  (one-time fee).
- Wrap the PyInstaller output as an MSIX (e.g. with the MSIX Packaging Tool or
  `msix` tooling), declaring the app as a desktop app.
- Note: a low-level keyboard hook and input injection may require justification
  during Store certification; a sideloaded MSIX or direct GitHub download avoids
  Store review.

### Snap Store / Flathub (Linux)

- **Snap:** author a `snapcraft.yaml` wrapping the app; note that global input
  and portals interact with confinement — classic confinement or the
  appropriate interfaces (`x11`, `wayland`, portal access) are needed. Publish
  via a [Snapcraft](https://snapcraft.io/) developer account.
- **Flatpak/Flathub:** author a manifest; rely on the XDG portals TalkPaste
  already targets for Wayland. Submit to [Flathub](https://flathub.org/).

For most users the **GitHub Release** download is the simplest channel and is
fully automated here.

## 5. Post-release

- [ ] Verify the download links in `README.md` resolve.
- [ ] Open a new *Unreleased* section in `CHANGELOG.md`.
