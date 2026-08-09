#!/bin/bash
#
# build_macos.sh
#
# Builds the macOS standalone client: KVMDongle.app. Must be run ON
# macOS -- PyInstaller cannot cross-compile from another OS.
#
#   chmod +x packaging/build_macos.sh   # once
#   ./packaging/build_macos.sh
#
# Output: dist/KVMDongle.app
#
# NOT independently tested (built/verified on Windows only so far) --
# please report back if anything here needs adjusting for your machine.
# Two things worth knowing going in:
#
#   - Gatekeeper: an unsigned/unnotarized .app built this way will get a
#     "cannot be opened because the developer cannot be verified" warning
#     on first launch on someone else's Mac. Either right-click > Open
#     (bypasses it once) or, for real distribution, this needs an Apple
#     Developer ID to codesign + notarize -- out of scope for this script.
#   - The Video menu's device-name lookup (capture_device_names()) isn't
#     implemented on macOS at all yet (falls back to plain "Device N"),
#     unrelated to this build script -- see README.md.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

echo "[build] installing/upgrading build + runtime dependencies..."
python3 -m pip install --upgrade pip
pip3 install -r requirements.txt
pip3 install pyinstaller

echo "[build] running PyInstaller..."
pyinstaller packaging/client.spec --noconfirm --distpath dist --workpath build

echo "[build] done: dist/KVMDongle.app"
