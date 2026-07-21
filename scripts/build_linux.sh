#!/usr/bin/env bash
# Build the TalkPaste Linux app bundle with PyInstaller.
# Run on Linux (cross-compilation is not supported).
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
VENV=".venv-build"

echo "==> Creating build venv ($VENV)"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installing dependencies"
pip install --upgrade pip
pip install -r requirements.txt pyinstaller

echo "==> Running PyInstaller"
pyinstaller --noconfirm --clean packaging/talkpaste-linux.spec

echo "==> Done. Artefact: dist/talkpaste/talkpaste"
echo "    Package it for distribution, e.g.:"
echo "      tar -C dist -czf talkpaste-linux-x86_64.tar.gz talkpaste"
