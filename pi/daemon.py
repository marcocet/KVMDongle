"""
daemon.py

Runs on the Raspberry Pi as root (via kvmdongle-daemon.service, after
kvmdongle-gadget.service has brought up the USB gadget). Reads framed
commands off the GPIO UART control link from the laptop and:

  - translates KEY_DOWN/KEY_UP/MOUSE_* events into USB HID reports written
    to /dev/hidg0 (keyboard) and /dev/hidg1 (mouse), whose paths are read
    from /run/kvmdongle/gadget-info.json rather than hardcoded
  - handles LIST_ISOS / MOUNT_ISO / EJECT_ISO by scanning the ISOs
    directory and pointing the mass-storage function's LUN backing file
    at the requested ISO, replying with ACK/ERROR/ISO_LIST/etc.

Single-threaded: the Pi Zero W is single-core, and every operation here
(HID writes, directory scans, LUN swaps) is fast enough that a plain
read-parse-handle loop introduces no meaningful latency.
"""

import json
import logging
import os
import signal
import struct
import sys
import time

import serial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import protocol

GADGET_INFO_PATH = "/run/kvmdongle/gadget-info.json"
SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 460800
MAX_HELD_KEYS = 6
EJECT_SETTLE_SECONDS = 0.3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("kvmdongle-daemon")


def load_gadget_info(retries=50, delay=0.1):
    """Read the device paths written by gadget-setup.sh. Retries briefly in
    case of a startup race, even though systemd unit ordering should make
    this unnecessary."""
    last_err = None
    for _ in range(retries):
        try:
            with open(GADGET_INFO_PATH) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(delay)
    raise RuntimeError(f"could not read {GADGET_INFO_PATH}: {last_err}")


class HidKeyboard:
    def __init__(self, path):
        self.path = path
        self.modifiers = 0
        self.held_keys = []  # ordered list of up to MAX_HELD_KEYS usage IDs

    def key_down(self, usage_id):
        if protocol.is_modifier_usage(usage_id):
            self.modifiers |= protocol.modifier_bit(usage_id)
        elif usage_id not in self.held_keys:
            if len(self.held_keys) < MAX_HELD_KEYS:
                self.held_keys.append(usage_id)
            else:
                log.debug("dropping key 0x%02X: 6 keys already held", usage_id)
        self._write_report()

    def key_up(self, usage_id):
        if protocol.is_modifier_usage(usage_id):
            self.modifiers &= ~protocol.modifier_bit(usage_id)
        elif usage_id in self.held_keys:
            self.held_keys.remove(usage_id)
        self._write_report()

    def release_all(self):
        self.modifiers = 0
        self.held_keys = []
        self._write_report()

    def _write_report(self):
        keys = self.held_keys + [0] * (MAX_HELD_KEYS - len(self.held_keys))
        report = bytes([self.modifiers & 0xFF, 0] + keys[:MAX_HELD_KEYS])
        _write_hid_report(self.path, report)


class HidMouse:
    def __init__(self, path):
        self.path = path
        self.buttons = 0

    def button_down(self, mask):
        self.buttons |= mask
        self._write_report(0, 0, 0)

    def button_up(self, mask):
        self.buttons &= ~mask
        self._write_report(0, 0, 0)

    def move(self, dx, dy):
        # HID report deltas are signed bytes (-127..127); split a wider
        # move into as many reports as needed.
        while dx != 0 or dy != 0:
            step_dx = max(-127, min(127, dx))
            step_dy = max(-127, min(127, dy))
            dx -= step_dx
            dy -= step_dy
            self._write_report(step_dx, step_dy, 0)

    def scroll(self, amount):
        self._write_report(0, 0, amount)

    def release_all_buttons(self):
        self.buttons = 0
        self._write_report(0, 0, 0)

    def _write_report(self, dx, dy, wheel):
        report = struct.pack("<Bbbb", self.buttons & 0xFF, dx, dy, wheel)
        _write_hid_report(self.path, report)


def _write_hid_report(path, report_bytes):
    try:
        with open(path, "wb") as f:
            f.write(report_bytes)
    except OSError as e:
        log.warning("failed to write HID report to %s: %s", path, e)


class StorageController:
    def __init__(self, lun_file_attr, isos_dir):
        self.lun_file_attr = lun_file_attr
        self.isos_dir = isos_dir
        self.current_iso = self._read_current_iso()

    def _read_current_iso(self):
        try:
            with open(self.lun_file_attr) as f:
                value = f.read().strip()
        except OSError:
            return None
        return os.path.basename(value) if value else None

    def list_isos(self):
        try:
            names = sorted(
                name for name in os.listdir(self.isos_dir)
                if name.lower().endswith(".iso")
            )
        except OSError as e:
            log.warning("could not list isos dir %s: %s", self.isos_dir, e)
            names = []
        return names, self.current_iso

    def mount(self, name):
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise ValueError("invalid filename")
        path = os.path.join(self.isos_dir, name)
        if not os.path.isfile(path):
            raise ValueError(f"no such file: {name}")

        # Eject first, then insert, with a short settle delay so the host's
        # removable-media poll reliably observes both the "no medium" and
        # the "new medium" transitions rather than possibly missing one.
        self._write_lun("\n")
        time.sleep(EJECT_SETTLE_SECONDS)
        self._write_lun(path + "\n")
        self.current_iso = name

    def eject(self):
        self._write_lun("\n")
        self.current_iso = None

    def _write_lun(self, text):
        # A zero-byte write can be a no-op for some sysfs/configfs store
        # callbacks, so eject writes a bare newline rather than "".
        with open(self.lun_file_attr, "w") as f:
            f.write(text)


class Daemon:
    def __init__(self):
        info = load_gadget_info()
        self.keyboard = HidKeyboard(info["hidg_keyboard"])
        self.mouse = HidMouse(info["hidg_mouse"])
        self.storage = StorageController(info["lun0_file_attr"], info["isos_dir"])
        self.parser = protocol.FrameParser()
        self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1, exclusive=True)
        self.running = True

    def run(self):
        log.info("daemon ready, keyboard=%s mouse=%s isos_dir=%s",
                  self.keyboard.path, self.mouse.path, self.storage.isos_dir)
        self._send_startup_state()

        while self.running:
            n = self.ser.in_waiting
            data = self.ser.read(n if n else 1)
            if not data:
                continue
            for frame_type, payload in self.parser.feed(data):
                self._handle_frame(frame_type, payload)

    def stop(self, *_args):
        self.running = False

    def shutdown(self):
        log.info("shutting down: releasing keys/buttons")
        try:
            self.keyboard.release_all()
            self.mouse.release_all_buttons()
        except Exception:
            pass
        self.ser.close()

    def _send_startup_state(self):
        if self.storage.current_iso:
            self._send(protocol.encode_iso_mounted(self.storage.current_iso))
        else:
            self._send(protocol.encode_iso_ejected())

    def _send(self, frame_bytes):
        try:
            self.ser.write(frame_bytes)
        except serial.SerialException as e:
            log.warning("serial write failed: %s", e)

    def _handle_frame(self, frame_type, payload):
        p = protocol
        try:
            if frame_type == p.KEY_DOWN:
                self.keyboard.key_down(payload[0])
            elif frame_type == p.KEY_UP:
                self.keyboard.key_up(payload[0])
            elif frame_type == p.MOUSE_MOVE:
                dx, dy = p.decode_mouse_move(payload)
                self.mouse.move(dx, dy)
            elif frame_type == p.MOUSE_DOWN:
                self.mouse.button_down(payload[0])
            elif frame_type == p.MOUSE_UP:
                self.mouse.button_up(payload[0])
            elif frame_type == p.MOUSE_SCROLL:
                self.mouse.scroll(struct.unpack("b", payload)[0])
            elif frame_type == p.LIST_ISOS:
                names, current = self.storage.list_isos()
                self._send(p.encode_iso_list(names, current))
            elif frame_type == p.MOUNT_ISO:
                name = p.decode_mount_iso(payload)
                try:
                    self.storage.mount(name)
                    self._send(p.encode_iso_mounted(name))
                except ValueError as e:
                    self._send(p.encode_error(frame_type, str(e)))
            elif frame_type == p.EJECT_ISO:
                self.storage.eject()
                self._send(p.encode_iso_ejected())
            elif frame_type == p.PING:
                self._send(p.encode_pong())
            else:
                log.debug("unknown frame type 0x%02X", frame_type)
        except Exception as e:
            log.exception("error handling frame type 0x%02X", frame_type)
            self._send(p.encode_error(frame_type, str(e)))


def main():
    daemon = Daemon()
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    try:
        daemon.run()
    finally:
        daemon.shutdown()


if __name__ == "__main__":
    main()
