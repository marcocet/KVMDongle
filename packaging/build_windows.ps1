# build_windows.ps1
#
# Builds the Windows standalone client: KVMDongle.exe (plus a folder
# of its dependencies alongside it -- see client.spec's docstring for why
# this is --onedir, not a single-file exe). Run from anywhere; paths are
# resolved relative to this script's own location.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Output: dist\KVMDongle\KVMDongle.exe

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

Write-Host "[build] done: dist\KVMDongle\KVMDongle.exe"
Write-Host "[build] the whole 'dist\KVMDongle' folder is the app -- copy/zip it as a unit, not just the .exe"
