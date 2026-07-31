"""
protocol.py

Shared wire protocol for the laptop <-> Raspberry Pi KVM/crash-cart control
link (the USB-TTL serial link, separate from the Pi's USB OTG port which
carries HID/mass-storage to the target machine).

This file must be identical on both ends -- copy it verbatim to the Pi
(see pi/install.sh) rather than maintaining two versions.

Frame format:
    [0xAA sync][type: 1 byte][len: 2 bytes LE][payload: len bytes][checksum: 1 byte]
    checksum = (type byte + len bytes + payload bytes) summed, & 0xFF

On a checksum mismatch the parser resyncs by discarding bytes until it
finds a sync byte that leads to a valid frame, rather than trusting a
possibly-corrupt length field.
"""

import struct

SYNC_BYTE = 0xAA

# Laptop -> Pi
KEY_DOWN = 0x01
KEY_UP = 0x02
MOUSE_STATE = 0x03
MOUSE_SCROLL = 0x06
LIST_ISOS = 0x10
MOUNT_ISO = 0x11
EJECT_ISO = 0x12
PING = 0x13
AP_ENABLE = 0x14
AP_DISABLE = 0x15
AP_STATUS_QUERY = 0x16

# Pi -> Laptop
ERROR = 0x81
ISO_LIST = 0x82
ISO_MOUNTED = 0x83
ISO_EJECTED = 0x84
PONG = 0x85
AP_STATUS = 0x86

# Matches USB HID usage-page-0x07 button bit positions used in
# MOUSE_STATE's buttons byte.
MOUSE_LEFT = 0x01
MOUSE_RIGHT = 0x02
MOUSE_MIDDLE = 0x04

# The mouse is an ABSOLUTE pointer (touchscreen-style, not a relative
# capture) -- MOUSE_STATE's x/y are each in [0, MOUSE_ABSOLUTE_MAX], a
# fraction of the way across the target's whole screen, matching the
# gadget's HID report descriptor (pi/gadget-setup.sh, Logical Maximum
# 0x7FFF -- the same convention QEMU's usb-tablet device uses). The
# target's own OS maps that fraction onto its actual screen resolution,
# whatever that is -- the client never needs to know the target's real
# resolution, only where a click landed within the displayed video frame.
#
# MOUSE_STATE bundles position AND buttons into ONE frame/report rather
# than sending them separately (position, then a distinct button-down):
# a real HID absolute pointer reports a complete snapshot -- position and
# button state together -- in a single sample, not as two independently-
# timed events. Splitting them into two writes was a real, confirmed bug:
# a plain click (no cursor movement needed) sends a position report
# identical to the one already in effect immediately followed by a
# button-only report, and something in that gap (host-side duplicate-
# report handling, endpoint queue timing, or both) made the click
# unreliable specifically when the position didn't change -- exactly
# reproducible by clicking at the same spot repeatedly. Dragging was
# unaffected because each motion sample already changed the position.
# Sending one atomic report removes the gap entirely.
MOUSE_ABSOLUTE_MAX = 0x7FFF

# HID keyboard usage IDs for the modifier keys (Ctrl/Shift/Alt/GUI x L/R).
# These occupy usage IDs 0xE0-0xE7 on the real HID keyboard usage page,
# which is also how they're reported: as bits in the modifier byte of the
# 8-byte boot-keyboard report, not as entries in the 6-key array. Sending
# them as ordinary KEY_DOWN/KEY_UP events (like any other key) and letting
# the receiving end sort modifiers-vs-regular-keys by this range keeps the
# wire format uniform.
USAGE_MOD_LEFT_CTRL = 0xE0
USAGE_MOD_LEFT_SHIFT = 0xE1
USAGE_MOD_LEFT_ALT = 0xE2
USAGE_MOD_LEFT_GUI = 0xE3
USAGE_MOD_RIGHT_CTRL = 0xE4
USAGE_MOD_RIGHT_SHIFT = 0xE5
USAGE_MOD_RIGHT_ALT = 0xE6
USAGE_MOD_RIGHT_GUI = 0xE7


def is_modifier_usage(usage_id):
    return USAGE_MOD_LEFT_CTRL <= usage_id <= USAGE_MOD_RIGHT_GUI


def modifier_bit(usage_id):
    """Bit position within the HID report's modifier byte for this usage ID."""
    return 1 << (usage_id - USAGE_MOD_LEFT_CTRL)


def _checksum(type_byte, len_bytes, payload):
    return (type_byte + len_bytes[0] + len_bytes[1] + sum(payload)) & 0xFF


def encode(frame_type, payload=b""):
    """Build a complete wire frame for the given type/payload."""
    payload = bytes(payload)
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for a single frame")
    len_bytes = struct.pack("<H", len(payload))
    checksum = _checksum(frame_type, len_bytes, payload)
    return bytes([SYNC_BYTE, frame_type]) + len_bytes + payload + bytes([checksum])


class FrameParser:
    """Incremental frame parser fed raw bytes as they arrive off the wire.

    Usage: create one instance per serial connection, call feed(data) each
    time bytes are read; it returns a list of zero or more (type, payload)
    tuples for whatever complete, checksum-valid frames were found.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf.extend(data)
        frames = []
        while True:
            frame = self._try_extract()
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _try_extract(self):
        buf = self._buf
        while True:
            while buf and buf[0] != SYNC_BYTE:
                del buf[0]
            if len(buf) < 4:
                return None  # need at least sync + type + len(2)
            frame_type = buf[1]
            length = buf[2] | (buf[3] << 8)
            total_len = 4 + length + 1  # header + payload + checksum
            if len(buf) < total_len:
                return None  # wait for more bytes
            payload = bytes(buf[4:4 + length])
            checksum = buf[4 + length]
            expected = _checksum(frame_type, bytes(buf[2:4]), payload)
            if checksum != expected:
                # Corrupt frame (or we locked onto a stray 0xAA that isn't
                # really a sync byte) -- drop it and keep scanning.
                del buf[0]
                continue
            del buf[0:total_len]
            return (frame_type, payload)


# --- Convenience encoders -------------------------------------------------

def encode_key_event(down, usage_id):
    return encode(KEY_DOWN if down else KEY_UP, bytes([usage_id & 0xFF]))


def encode_mouse_state(x_frac, y_frac, buttons):
    """x_frac/y_frac: 0.0-1.0 fraction of the way across the target's
    screen. buttons: bitmask of MOUSE_LEFT/RIGHT/MIDDLE currently held.
    Position is clamped and scaled to the HID report's logical range here,
    so the Pi daemon can pass the resulting ints straight into the report
    with no further conversion. See the MOUSE_STATE comment above for why
    position and buttons travel together in one frame."""
    x = round(max(0.0, min(1.0, x_frac)) * MOUSE_ABSOLUTE_MAX)
    y = round(max(0.0, min(1.0, y_frac)) * MOUSE_ABSOLUTE_MAX)
    return encode(MOUSE_STATE, struct.pack("<HHB", x, y, buttons & 0xFF))


def decode_mouse_state(payload):
    """Returns (x, y, buttons); x/y already in [0, MOUSE_ABSOLUTE_MAX]."""
    return struct.unpack("<HHB", payload)


def encode_mouse_scroll(amount):
    amount = max(-127, min(127, int(amount)))
    return encode(MOUSE_SCROLL, struct.pack("b", amount))


def encode_list_isos():
    return encode(LIST_ISOS)


def encode_mount_iso(filename):
    return encode(MOUNT_ISO, filename.encode("utf-8"))


def decode_mount_iso(payload):
    return payload.decode("utf-8")


def encode_eject_iso():
    return encode(EJECT_ISO)


def encode_ap_enable():
    return encode(AP_ENABLE)


def encode_ap_disable():
    return encode(AP_DISABLE)


def encode_ap_status_query():
    return encode(AP_STATUS_QUERY)


def encode_ap_status(enabled):
    return encode(AP_STATUS, bytes([1 if enabled else 0]))


def decode_ap_status(payload):
    return bool(payload[0])


def encode_ping():
    return encode(PING)


def encode_pong():
    return encode(PONG)


def encode_error(failed_type, message):
    return encode(ERROR, bytes([failed_type & 0xFF]) + message.encode("utf-8"))


def decode_error(payload):
    """Returns (failed_type, message)."""
    return payload[0], payload[1:].decode("utf-8", errors="replace")


def encode_iso_list(names, current=None):
    """names: list of str filenames. current: currently-mounted filename, or None."""
    if len(names) > 0xFF:
        raise ValueError("too many ISOs to list in a single frame")
    payload = bytearray([len(names)])
    for name in names:
        name_bytes = name.encode("utf-8")[:255]
        payload.append(len(name_bytes))
        payload.extend(name_bytes)
        payload.append(1 if name == current else 0)
    return encode(ISO_LIST, bytes(payload))


def decode_iso_list(payload):
    """Returns (names, current_name_or_None)."""
    count = payload[0]
    offset = 1
    names = []
    current = None
    for _ in range(count):
        name_len = payload[offset]
        offset += 1
        name = payload[offset:offset + name_len].decode("utf-8")
        offset += name_len
        is_current = payload[offset]
        offset += 1
        names.append(name)
        if is_current:
            current = name
    return names, current


def encode_iso_mounted(filename):
    return encode(ISO_MOUNTED, filename.encode("utf-8"))


def decode_iso_mounted(payload):
    return payload.decode("utf-8")


def encode_iso_ejected():
    return encode(ISO_EJECTED)
