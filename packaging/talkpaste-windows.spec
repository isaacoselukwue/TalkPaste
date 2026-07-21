# PyInstaller spec — Windows build of the TalkPaste tray app.
# Build on Windows (no cross-compilation):
#   pyinstaller packaging\talkpaste-windows.spec
# Output: dist\talkpaste\talkpaste.exe (windowed, no console). Models are NOT
# bundled — they live in %LOCALAPPDATA%\TalkPaste\models and load at runtime.

# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None
project_root = os.path.abspath(os.getcwd())

datas = collect_data_files("faster_whisper")
datas += collect_data_files("_sounddevice_data")

binaries = collect_dynamic_libs("onnxruntime")
binaries += collect_dynamic_libs("ctranslate2")

a = Analysis(
    [os.path.join(project_root, "app", "main.py")],
    pathex=[project_root],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "app.platform.windows_adapter",
        "app.services.asr_faster_whisper",
        "app.services.asr_whisper_cpp",
        "onnxruntime",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    upx=False,
    console=False,  # no console window for the GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="packaging/talkpaste.ico",  # add an icon when available
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="talkpaste",
)
