# PyInstaller spec — Linux build of the TalkPaste tray app.
# Build on the target OS (no cross-compilation):
#   pyinstaller packaging/talkpaste-linux.spec
# Output: dist/talkpaste  (single-folder, windowed). Models are NOT bundled —
# they live in the per-user data directory and are downloaded/located at runtime.

# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None
project_root = os.path.abspath(os.getcwd())

a = Analysis(
    [os.path.join(project_root, "app", "main.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=[
        "app.platform.linux_x11_adapter",
        "app.platform.linux_wayland_adapter",
        "app.services.asr_faster_whisper",
        "app.services.asr_whisper_cpp",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep the shell lean: exclude optional heavy backends the base app does
    # not require. Users install them separately when enabling those features.
    excludes=["llama_cpp", "tkinter", "matplotlib", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="talkpaste",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # windowed GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="talkpaste",
)
