#!/bin/bash
#
# gadget-teardown.sh
#
# Cleanly tears down the composite gadget in the order configfs requires:
# unbind the UDC first (drops the device from the host), then remove the
# config symlinks, then the functions, then the now-empty directories.
# Safe to run when nothing is set up -- every step is best-effort.

GADGET_NAME=kvmdongle
GADGET_DIR=/sys/kernel/config/usb_gadget/$GADGET_NAME

if [ ! -d "$GADGET_DIR" ]; then
    exit 0
fi

cd "$GADGET_DIR" || exit 0

echo "" > UDC 2>/dev/null || true

rm -f configs/c.1/hid.usb0 2>/dev/null || true
rm -f configs/c.1/hid.usb1 2>/dev/null || true
rm -f configs/c.1/mass_storage.usb0 2>/dev/null || true

rmdir functions/hid.usb0 2>/dev/null || true
rmdir functions/hid.usb1 2>/dev/null || true
rmdir functions/mass_storage.usb0 2>/dev/null || true

rmdir configs/c.1/strings/0x409 2>/dev/null || true
rmdir configs/c.1 2>/dev/null || true
rmdir strings/0x409 2>/dev/null || true

cd /
rmdir "$GADGET_DIR" 2>/dev/null || true

rm -f /run/kvmdongle/gadget-info.json 2>/dev/null || true

echo "[gadget-teardown] done"
