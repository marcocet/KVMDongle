#!/bin/bash
#
# wifi-ap-toggle.sh on|off
#
# The Pi Zero W has a single Wi-Fi radio: AP mode and normal Wi-Fi-client
# mode are not reliably concurrent on this chip, so this is a deliberate
# switch, not something left running alongside your home network. Turn it
# on when you want to upload ISOs from a phone/laptop, off when you're done.

set -e

MODE=$1
IFACE=wlan0
AP_IP=192.168.50.1/24
INSTALL_DIR=/opt/kvmdongle
STATE_FILE=/run/kvmdongle/wifi-ap-prev-mode

if [ "$MODE" != "on" ] && [ "$MODE" != "off" ]; then
    echo "usage: $0 on|off" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root" >&2
    exit 1
fi

# Detect how wlan0 is normally managed, since this varies by distro:
# NetworkManager (Raspberry Pi OS Bookworm), dhcpcd (older Raspberry Pi OS),
# or plain ifupdown (DietPi's default -- no dhcpcd/NetworkManager daemon at
# all, just /etc/network/interfaces brought up by networking.service).
network_manager_kind() {
    if systemctl is-active --quiet NetworkManager 2>/dev/null; then
        echo nm
    elif systemctl is-active --quiet dhcpcd 2>/dev/null; then
        echo dhcpcd
    else
        echo ifupdown
    fi
}

if [ "$MODE" = "on" ]; then
    echo "[wifi-ap] switching $IFACE to AP mode..."

    # Record which networking stack was in charge BEFORE we touch anything,
    # so "off" restores the right one -- re-detecting live state at "off"
    # time would be wrong, since by then we've deliberately stopped/unmanaged
    # whichever one this was.
    KIND=$(network_manager_kind)
    mkdir -p "$(dirname "$STATE_FILE")"
    echo "$KIND" > "$STATE_FILE"

    case "$KIND" in
        nm) nmcli device set "$IFACE" managed no || true ;;
        dhcpcd) systemctl stop dhcpcd 2>/dev/null || true ;;
        ifupdown) ifdown "$IFACE" 2>/dev/null || true ;;
    esac

    rfkill unblock wifi || true
    ip link set "$IFACE" down
    ip addr flush dev "$IFACE"
    ip addr add "$AP_IP" dev "$IFACE"
    ip link set "$IFACE" up

    cp "$INSTALL_DIR/hostapd.conf" /etc/hostapd/hostapd.conf
    cp "$INSTALL_DIR/dnsmasq.conf" /etc/dnsmasq.d/kvmdongle.conf

    systemctl unmask hostapd 2>/dev/null || true
    systemctl restart hostapd
    systemctl restart dnsmasq
    systemctl start kvmdongle-webui

    echo "[wifi-ap] AP is up. Join the Wi-Fi network from hostapd.conf, then browse to:"
    echo "    http://${AP_IP%/*}:8080"
else
    echo "[wifi-ap] switching $IFACE back to normal client mode..."
    systemctl stop kvmdongle-webui 2>/dev/null || true
    systemctl stop hostapd 2>/dev/null || true
    systemctl stop dnsmasq 2>/dev/null || true

    ip addr flush dev "$IFACE"

    # Restore whatever was managing wlan0 before "on" was run, if we know
    # it; otherwise fall back to a fresh (best-effort) detection.
    if [ -f "$STATE_FILE" ]; then
        KIND=$(cat "$STATE_FILE")
    else
        KIND=$(network_manager_kind)
    fi
    case "$KIND" in
        nm) nmcli device set "$IFACE" managed yes || true ;;
        dhcpcd) systemctl start dhcpcd 2>/dev/null || true ;;
        ifupdown) ifup "$IFACE" 2>/dev/null || true ;;
    esac
    rm -f "$STATE_FILE"

    echo "[wifi-ap] back to normal Wi-Fi client mode."
fi
