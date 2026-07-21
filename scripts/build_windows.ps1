# Build the TalkPaste Windows app with PyInstaller.
# Run on Windows in PowerShell (cross-compilation is not supported):
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")

$venv = ".venv-build"
Write-Host "==> Creating build venv ($venv)"
python -m venv $venv
& "$venv\Scripts\Activate.ps1"

Write-Host "==> Installing dependencies"
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller

Write-Host "==> Running PyInstaller"
pyinstaller --noconfirm --clean packaging\talkpaste-windows.spec

Write-Host "==> Done. Artefact: dist\talkpaste\talkpaste.exe"
Write-Host "    Zip it for distribution, e.g.:"
Write-Host "      Compress-Archive -Path dist\talkpaste\* -DestinationPath talkpaste-windows-x64.zip"
