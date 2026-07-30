#!/bin/bash
#
# install.sh
#
# One-shot setup for a blank Debian-based Pi install (DietPi or Raspberry
# Pi OS): enables USB gadget mode and frees up the GPIO UART, installs this
# repo's Pi-side files to /opt/kvmdongle, and enables the systemd services.
# Run this from a clone of the repo ON THE PI (e.g. after `git clone` or
# `scp -r` of this directory + protocol.py to the Pi), as root:
#
#   sudo ./install.sh
#
# A reboot is required afterwards for the boot-config changes (dwc2
# overlay, freed-up UART) to take effect -- the script will remind you.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root: sudo ./install.sh" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
INSTALL_DIR=/opt/kvmdongle

if [ -d /boot/firmware ]; then
    BOOT_DIR=/boot/firmware
else
    BOOT_DIR=/boot
fi
CONFIG_TXT="$BOOT_DIR/config.txt"
CMDLINE_TXT="$BOOT_DIR/cmdline.txt"

echo "[install] using boot dir: $BOOT_DIR"

# --- 1. Boot config: USB gadget mode + free up the good UART -------------
add_config_line() {
    local line=$1
    if ! grep -qxF "$line" "$CONFIG_TXT"; then
        echo "$line" >> "$CONFIG_TXT"
        echo "[install] added to config.txt: $line"
    fi
}

add_config_line "dtoverlay=dwc2,dr_mode=peripheral"
add_config_line "dtoverlay=disable-bt"
add_config_line "enable_uart=1"

# --- 2. Remove any serial console from cmdline.txt so nothing contends
# with the daemon for the UART. cmdline.txt is a single line; rewrite it
# with any console=serial0/ttyAMA0/ttyS0,<baud> token stripped. ---
if grep -qE 'console=(serial0|ttyAMA0|ttyS0)' "$CMDLINE_TXT"; then
    sed -i -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g' "$CMDLINE_TXT"
    echo "[install] removed serial console from cmdline.txt"
fi

# --- 3. Make sure nothing else can ever grab the UART -------------------
systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl mask serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl disable --now serial-getty@ttyS0.service 2>/dev/null || true
systemctl mask serial-getty@ttyS0.service 2>/dev/null || true
systemctl disable --now hciuart.service 2>/dev/null || true

# --- 4. Install dependencies via apt (avoids pip's "externally managed
# environment" restriction on recent Debian-based releases) ---
echo "[install] installing python3-serial..."
apt-get update -qq
apt-get install -y python3-serial

# --- 5. Copy files into place --------------------------------------------
mkdir -p "$INSTALL_DIR"
cp "$REPO_DIR/protocol.py" "$INSTALL_DIR/protocol.py"
cp "$SCRIPT_DIR/daemon.py" "$INSTALL_DIR/daemon.py"
cp "$SCRIPT_DIR/gadget-setup.sh" "$INSTALL_DIR/gadget-setup.sh"
cp "$SCRIPT_DIR/gadget-teardown.sh" "$INSTALL_DIR/gadget-teardown.sh"
chmod +x "$INSTALL_DIR/gadget-setup.sh" "$INSTALL_DIR/gadget-teardown.sh"

mkdir -p /srv/kvmdongle/isos

echo "[install] installed to $INSTALL_DIR"

# --- 6. Install and enable systemd services -------------------------------
cp "$SCRIPT_DIR/kvmdongle-gadget.service" /etc/systemd/system/kvmdongle-gadget.service
cp "$SCRIPT_DIR/kvmdongle-daemon.service" /etc/systemd/system/kvmdongle-daemon.service
systemctl daemon-reload
systemctl enable kvmdongle-gadget.service
systemctl enable kvmdongle-daemon.service

echo "[install] done."
echo "[install] a REBOOT is required for the dwc2/UART changes to take effect:"
echo "    sudo reboot"
echo "[install] after rebooting, check status with:"
echo "    systemctl status kvmdongle-gadget kvmdongle-daemon"
echo "    ls /dev/hidg*"
