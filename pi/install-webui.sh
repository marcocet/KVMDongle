#!/bin/bash
#
# install-webui.sh
#
# Optional phase-2 setup: Wi-Fi AP + ISO upload page. Run this after
# install.sh and a reboot, once the core keyboard/mouse/storage bridge is
# confirmed working.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root: sudo ./install-webui.sh" >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=/opt/kvmdongle

echo "[install-webui] installing hostapd, dnsmasq, python3-flask..."
apt-get update -qq
apt-get install -y hostapd dnsmasq python3-flask

# hostapd/dnsmasq/webui are only started on demand by wifi-ap-toggle.sh,
# never automatically at boot -- the AP is a deliberate, occasional mode.
systemctl unmask hostapd 2>/dev/null || true
systemctl disable hostapd 2>/dev/null || true
systemctl disable dnsmasq 2>/dev/null || true

cp "$SCRIPT_DIR/webui.py" "$INSTALL_DIR/webui.py"
cp "$SCRIPT_DIR/hostapd.conf" "$INSTALL_DIR/hostapd.conf"
cp "$SCRIPT_DIR/dnsmasq.conf" "$INSTALL_DIR/dnsmasq.conf"
cp "$SCRIPT_DIR/wifi-ap-toggle.sh" "$INSTALL_DIR/wifi-ap-toggle.sh"
chmod +x "$INSTALL_DIR/wifi-ap-toggle.sh"

cp "$SCRIPT_DIR/kvmdongle-webui.service" /etc/systemd/system/kvmdongle-webui.service
systemctl daemon-reload
systemctl disable kvmdongle-webui 2>/dev/null || true

echo "[install-webui] done."
echo "[install-webui] edit $INSTALL_DIR/hostapd.conf to set your own SSID/passphrase, then:"
echo "    sudo $INSTALL_DIR/wifi-ap-toggle.sh on"
echo "    sudo $INSTALL_DIR/wifi-ap-toggle.sh off"
