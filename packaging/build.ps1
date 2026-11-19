# Build HassAmbilights.exe (one file, no console) and the installer. Run from the
# repo root:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Needs Inno Setup 6 for the installer step: https://jrsoftware.org/isdl.php
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

$version = (python packaging/make_assets.py --version).Trim()
if (-not $version) { throw "could not read __version__ from hass_ambilights.py" }

# The installer is the thing the auto-updater downloads, so it is built from the
# same exe and carries the same version.
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 not found at $iscc - install it from jrsoftware.org" }
Invoke-Step { & $iscc "/DAppVersion=$version" "packaging\hass-ambilights.iss" } "Inno Setup"

$setup = "dist/HassAmbilights-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "$setup was not produced" }

# The updater refuses a download it cannot verify, so the release needs this file.
$sums = foreach ($file in @("dist/HassAmbilights.exe", $setup)) {
    $hash = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
    "$hash  $(Split-Path $file -Leaf)"
}
$sums | Set-Content -Encoding ascii "dist/SHA256SUMS.txt"

Write-Host ""
Write-Host "Built:" -ForegroundColor Green
Write-Host "  dist\HassAmbilights.exe"
Write-Host "  $setup"
Write-Host "  dist\SHA256SUMS.txt"
Write-Host "Unsigned, so Windows SmartScreen will warn on first run."
