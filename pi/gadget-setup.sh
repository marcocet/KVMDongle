#!/bin/bash
#
# gadget-setup.sh
#
# Brings up the composite USB gadget (HID keyboard + HID mouse + read-only
# CD-ROM-style mass storage) via configfs on the Pi's OTG port. Run as root
# by kvmdongle-gadget.service, strictly before kvmdongle-daemon.service
# starts (the daemon reads the device paths this script writes out).
#
# Safe to re-run: tears down any existing gadget of the same name first,
# so a crash mid-setup on a previous boot doesn't block this one.

set -e

GADGET_NAME=kvmdongle
GADGET_DIR=/sys/kernel/config/usb_gadget/$GADGET_NAME
ISOS_DIR=/srv/kvmdongle/isos
INFO_DIR=/run/kvmdongle
INFO_FILE=$INFO_DIR/gadget-info.json

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

echo "[gadget-setup] loading kernel modules..."
modprobe libcomposite

if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs none /sys/kernel/config
fi

if [ -d "$GADGET_DIR" ]; then
    echo "[gadget-setup] existing gadget found, tearing down first..."
    "$SCRIPT_DIR/gadget-teardown.sh" || true
fi

mkdir -p "$ISOS_DIR"

echo "[gadget-setup] creating gadget at $GADGET_DIR"
mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x1d6b > idVendor      # Linux Foundation
echo 0x0104 > idProduct     # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "kvmdongle0001"  > strings/0x409/serialnumber
echo "DIY"            > strings/0x409/manufacturer
echo "KVM Crash Cart" > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "HID+Storage" > configs/c.1/strings/0x409/configuration
echo 250  > configs/c.1/MaxPower
echo 0x80 > configs/c.1/bmAttributes

# --- HID keyboard: standard USB HID Appendix-B boot-keyboard report ---
# (modifier byte, reserved byte, 6 keycode bytes = 8 bytes total). The OUT
# endpoint is kept (no_out_endpoint=0) so the host can send LED reports
# (Caps/Num/Scroll Lock) back to us -- purely cosmetic if unused, but free.
mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol
echo 1 > functions/hid.usb0/subclass
echo 8 > functions/hid.usb0/report_length
echo 0 > functions/hid.usb0/no_out_endpoint
printf '\x05\x01\x09\x06\xA1\x01\x05\x07\x19\xE0\x29\xE7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xC0' \
    > functions/hid.usb0/report_desc

# --- HID mouse: 5 buttons + relative X/Y/wheel as signed bytes (4-byte report) ---
mkdir -p functions/hid.usb1
echo 2 > functions/hid.usb1/protocol
echo 1 > functions/hid.usb1/subclass
echo 4 > functions/hid.usb1/report_length
echo 1 > functions/hid.usb1/no_out_endpoint
printf '\x05\x01\x09\x02\xA1\x01\x09\x01\xA1\x00\x05\x09\x19\x01\x29\x05\x15\x00\x25\x01\x95\x05\x75\x01\x81\x02\x95\x01\x75\x03\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7F\x75\x08\x95\x03\x81\x06\xC0\xC0' \
    > functions/hid.usb1/report_desc

# --- Mass storage: single read-only CD-ROM-style LUN. Nothing mounted at
# boot; the daemon points lun.0/file at an ISO once the laptop asks for one. ---
mkdir -p functions/mass_storage.usb0
echo 1 > functions/mass_storage.usb0/lun.0/ro
echo 1 > functions/mass_storage.usb0/lun.0/cdrom
echo 1 > functions/mass_storage.usb0/lun.0/removable

# --- Bind functions to the config, in this exact order (determines
# /dev/hidgN assignment below, though we resolve it properly rather than
# assuming it) ---
ln -s functions/hid.usb0          configs/c.1/
ln -s functions/hid.usb1          configs/c.1/
ln -s functions/mass_storage.usb0 configs/c.1/

# --- Bind to the UDC LAST, only once every attribute above is set. Binding
# early risks the host (especially Windows) caching a garbage/empty HID
# descriptor, which can require a manual "Uninstall device" to recover from. ---
UDC_NAME=$(ls /sys/class/udc | head -n1)
if [ -z "$UDC_NAME" ]; then
    echo "[gadget-setup] ERROR: no UDC found -- is 'dtoverlay=dwc2,dr_mode=peripheral' set in config.txt?" >&2
    exit 1
fi
echo "$UDC_NAME" > UDC
echo "[gadget-setup] bound to UDC $UDC_NAME"

# --- Resolve the real /dev/hidgN nodes via each function's sysfs "dev"
# attribute rather than assuming hidg0=keyboard, hidg1=mouse. ---
resolve_devnode() {
    local majmin link
    majmin=$(cat "$1" 2>/dev/null) || return 1
    link="/sys/dev/char/$majmin"
    [ -e "$link" ] || return 1
    basename "$(readlink -f "$link")"
}

KBD_NODE=$(resolve_devnode "$GADGET_DIR/functions/hid.usb0/dev")
MOUSE_NODE=$(resolve_devnode "$GADGET_DIR/functions/hid.usb1/dev")

if [ -z "$KBD_NODE" ] || [ -z "$MOUSE_NODE" ]; then
    echo "[gadget-setup] ERROR: could not resolve /dev/hidgN device nodes" >&2
    exit 1
fi

# Give udev a moment to actually create the device nodes after binding.
for _ in $(seq 1 20); do
    [ -e "/dev/$KBD_NODE" ] && [ -e "/dev/$MOUSE_NODE" ] && break
    sleep 0.1
done

if [ ! -e "/dev/$KBD_NODE" ] || [ ! -e "/dev/$MOUSE_NODE" ]; then
    echo "[gadget-setup] ERROR: /dev/$KBD_NODE or /dev/$MOUSE_NODE never appeared" >&2
    exit 1
fi

mkdir -p "$INFO_DIR"
cat > "$INFO_FILE" <<EOF
{
  "hidg_keyboard": "/dev/$KBD_NODE",
  "hidg_mouse": "/dev/$MOUSE_NODE",
  "lun0_file_attr": "$GADGET_DIR/functions/mass_storage.usb0/lun.0/file",
  "isos_dir": "$ISOS_DIR"
}
EOF

echo "[gadget-setup] done. keyboard=/dev/$KBD_NODE mouse=/dev/$MOUSE_NODE"
