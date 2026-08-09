#!/bin/bash
#
# build_linux.sh
#
# Builds the Linux standalone client as a single self-extracting
# KVMDongle-Linux.run installer/launcher. Must be run ON Linux --
# PyInstaller cannot cross-compile from another OS.
#
# Requires makeself (not a pip package -- a separate system tool):
#   Debian/Ubuntu: sudo apt install makeself
#   or: https://github.com/megastep/makeself
#
#   chmod +x packaging/build_linux.sh   # once
#   ./packaging/build_linux.sh
#
# Output: dist/KVMDongle-Linux.run
#
# NOT independently tested (built/verified on Windows only so far) --
# please report back if anything here needs adjusting for your distro.
# PyInstaller itself only produces a plain folder on Linux (there's no
# native ".run" concept the way macOS has .app bundles); makeself is what
# wraps that folder into the single self-extracting file you asked for.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

MAKESELF_BIN="$(command -v makeself.sh || command -v makeself || true)"
if [ -z "$MAKESELF_BIN" ]; then
    echo "[build] ERROR: makeself not found on PATH. Install it first:" >&2
    echo "    Debian/Ubuntu: sudo apt install makeself" >&2
    echo "    or download from https://github.com/megastep/makeself" >&2
    exit 1
fi

echo "[build] installing/upgrading build + runtime dependencies..."
python3 -m pip install --upgrade pip
pip3 install -r requirements.txt
pip3 install pyinstaller

echo "[build] running PyInstaller..."
pyinstaller packaging/client.spec --noconfirm --distpath dist --workpath build

APP_DIR="dist/KVMDongle"
STAGING="dist/kvmdongle-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
cp -r "$APP_DIR"/. "$STAGING/"

# makeself's startup script runs with the extracted archive as its
# working directory, so this can assume the exe sits right next to it --
# but resolves its own location explicitly anyway rather than relying on
# that, in case a future makeself version changes that behavior.
cat > "$STAGING/run.sh" <<'EOF'
#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/KVMDongle" "$@"
EOF
chmod +x "$STAGING/run.sh"

OUTPUT="dist/KVMDongle-Linux.run"
echo "[build] packaging with makeself..."
"$MAKESELF_BIN" --gzip "$STAGING" "$OUTPUT" "KVMDongle" ./run.sh

rm -rf "$STAGING"

echo "[build] done: $OUTPUT"
echo "[build] run it with: chmod +x '$OUTPUT' && './$OUTPUT'"
