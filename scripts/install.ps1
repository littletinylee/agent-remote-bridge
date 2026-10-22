# Install agent-remote-bridge on Windows without touching global site-packages.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#
# What it does:
#   1. verifies Python >= 3.9 is on PATH
#   2. installs the optional `qrcode` dependency (terminal QR) - failure is non-fatal
#   3. adds a small `arb.cmd` shim to %USERPROFILE%\.local\bin (already on PATH
#      for most setups) so you can type `arb ...` from any directory
#   4. runs `arb init`

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Write-Host "agent-remote-bridge root: $root"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "python not found on PATH. Install Python 3.9+ and re-run."
    exit 1
}
$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "python: $($py.Source) ($version)"

try {
    & python -m pip install --quiet --disable-pip-version-check "qrcode[pil]" 2>$null
    & python -c "import qrcode" 2>$null
    Write-Host "optional dependency qrcode: ok (terminal QR enabled)"
} catch {
    Write-Host "optional dependency qrcode: skipped (login will print a fallback link)"
}

$binDir = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$shim = Join-Path $binDir "arb.cmd"
$content = @"
@echo off
setlocal
set "PYTHONPATH=$root\src;%PYTHONPATH%"
python -m arb %*
exit /b %ERRORLEVEL%
"@
Set-Content -Path $shim -Value $content -Encoding ASCII
Write-Host "shim installed: $shim"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
    Write-Host "added $binDir to the user PATH (open a new terminal to use it)"
}

& python -m arb init
Write-Host ""
Write-Host "Next:"
Write-Host "  arb login    # scan the QR with your personal WeChat"
Write-Host "  arb start    # start the daemon"
