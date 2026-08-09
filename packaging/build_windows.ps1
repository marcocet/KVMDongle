# build_windows.ps1
#
# Builds the Windows standalone client as a single-file KVMDongle.exe --
# nothing else needed alongside it. See client.spec's docstring for the
# trade-off this accepts: opening the Pi Shell or Debug Log window
# relaunches this same exe as a subprocess, and a single-file build has
# to re-extract its whole bundled payload from scratch on every one of
# those relaunches, not just the first launch. Run from anywhere; paths
# are resolved relative to this script's own location.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Output: dist\KVMDongle.exe

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

Set-Location $RepoRoot

Write-Host "[build] installing/upgrading build + runtime dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

Write-Host "[build] running PyInstaller..."
pyinstaller packaging\client.spec --noconfirm --distpath dist --workpath build

Write-Host "[build] done: dist\KVMDongle.exe"
