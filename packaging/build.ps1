# Build HassAmbilights.exe (one file, no console). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Invoke-Step {
    param([scriptblock]$Command, [string]$What)
    & $Command
    # Windows PowerShell 5.1 ignores $ErrorActionPreference for native commands,
    # so without this a failed build still printed the green success line
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

Invoke-Step { python -m pip install --upgrade pip } "pip upgrade"
Invoke-Step { python -m pip install -r requirements.txt -r requirements-gui.txt pyinstaller } "dependency install"

# icon, splash and Windows version resource are generated from __version__
Invoke-Step { python packaging/make_assets.py } "icon and version resource"
Invoke-Step { python packaging/make_splash.py } "splash image"

Invoke-Step { python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name HassAmbilights `
  --icon packaging/icon.ico `
  --splash packaging/splash.png `
  --version-file packaging/version_info.txt `
  --collect-submodules uvicorn `
  --collect-submodules pystray `
  --collect-submodules webview `
  --hidden-import pystray._win32 `
  --exclude-module matplotlib `
  hass_ambilights.py } "PyInstaller"

if (-not (Test-Path "dist/HassAmbilights.exe")) { throw "dist/HassAmbilights.exe was not produced" }

Write-Host ""
Write-Host "Built dist\HassAmbilights.exe" -ForegroundColor Green
Write-Host "Unsigned, so Windows SmartScreen will warn on first run."
