# Build HassAmbilights.exe (one file, no console). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-gui.txt pyinstaller

# icon + Windows version resource are generated from __version__, not committed
python packaging/make_assets.py

python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name HassAmbilights `
  --icon packaging/icon.ico `
  --version-file packaging/version_info.txt `
  --collect-submodules uvicorn `
  --collect-submodules pystray `
  --collect-submodules webview `
  --hidden-import pystray._win32 `
  --exclude-module tkinter `
  --exclude-module matplotlib `
  hass_ambilights.py

Write-Host ""
Write-Host "Built dist\HassAmbilights.exe" -ForegroundColor Green
Write-Host "Unsigned, so Windows SmartScreen will warn on first run."
