"""
client.py

Displays the video feed from a USB capture device in a window, forwards
keyboard AND mouse input to a Raspberry Pi Zero W running pi/daemon.py
(which injects them as real USB keyboard/mouse input into the target
machine via its USB OTG port, and also exposes an ISO from its SD card
as read-only USB mass storage on request).

Keyboard input is always forwarded while the window has focus -- no
toggle needed, since the OS only delivers keyboard events to a focused
window anyway. Mouse input works like a touchscreen, not a captured
relative pointer: your real cursor is always visible and free, and
clicking/dragging in the video area moves the TARGET's cursor to the
corresponding position and clicks/drags there. Nothing is ever hidden
or grabbed, so there's no mode to toggle.

Requirements:
    pip install pygame opencv-python pyserial pyperclip pyte
    Windows only, optional: pip install pygrabber (real capture device
    names in the Video menu instead of just "Device N")

Usage:
    python client.py
    python client.py --serial-port COM5 --capture-index 1

    --serial-port    the COM/tty port for the Pi's control link
                      (Windows: e.g. COM5, Linux/Mac: e.g. /dev/ttyUSB0) --
                      omit to auto-select the first detected port
    --capture-index  the OpenCV device index for your capture card
                      (try 0, 1, 2... if unsure; the app also prints
                      available indices on startup) -- omit to auto-scan
    --baud           serial baud rate, must match pi/daemon.py (default 115200)
    --debug          print each key/mouse event to the terminal

Controls:
    - Click into the video window to give it focus, then type normally.
      Keystrokes go to the target machine whenever this window is
      focused -- no toggle needed.
    - Click or drag anywhere in the video area to move the target's
      cursor there and click/drag -- like a touchscreen. Left/middle/
      right buttons all work; scrolling forwards the wheel at the
      target's current cursor position. Nothing captures your real
      cursor, so it's always free to also use the menu bar below.
    - Use the menu bar at the top for macro combos, clipboard paste, and
      mounting ISOs from the Pi's SD card -- all menu-driven, no local
      hotkeys, so every keystroke while focused is always forwarded to
      the target with nothing intercepted first.
    - Use the Video menu to switch capture devices without restarting --
      lists detected indices, marks the active one, and has a Refresh
      item to re-scan (e.g. after plugging in another capture card). Shows
      a real device name where available (best-effort, OS-specific --
      free on Linux, needs `pip install pygrabber` on Windows, not
      implemented on macOS), else falls back to plain "Device N".
    - Use the Serial Port menu to switch which COM/tty port talks to the
      Pi without restarting -- handy if Windows reassigned the adapter to
      a different COM number after a reconnect. Shows a real description
      next to each port where the OS has one (e.g. "USB-SERIAL CH340"),
      fully portable via pyserial itself -- no extra dependency needed,
      unlike the Video menu's device names.
    - Use the Storage menu to list ISOs already on the Pi's SD card,
      mount one (exposed to the target as a read-only CD-ROM), or eject
      the current one. The Pi is the source of truth for what's
      available -- add ISOs by swapping the SD card or via the Pi's
      Wi-Fi upload page.
    - Use Terminal > Open Pi Shell to run bash commands directly on the
      Pi, over the same serial link -- opens in its own separate OS
      window (not an overlay on the video), rendered with real colors/
      cursor movement, like the shell half of an SSH session (requires
      `pip install pyte`). That window has its own keyboard focus, so it
      never steals input from the main KVM window or vice versa; press
      F12 or just close the window to end the session.
    - The Terminal menu also has Restart Daemon (one click), and Reboot
      Pi / Shutdown Pi (click once to arm -- the label changes to prompt
      a second click within ~4 seconds, or it just disarms itself). None
      of these get a "success" reply, since whatever would send one (the
      daemon process, or the whole machine) is exactly what's going away;
      watch the connection light instead.
    - Use Session > Quit in the menu bar, or the window's close button,
      to exit (there's no local keyboard shortcut for this, since every
      keystroke while focused is forwarded to the target).
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
import time

import cv2
import pygame
import serial
import serial.tools.list_ports

import protocol

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    import pyte
except ImportError:
    pyte = None

# Windows-only, optional: lets the Video menu show real device names
# (e.g. "Logitech C920") instead of just "Device N" -- OpenCV itself has
# no portable way to ask for this. See capture_device_names().
try:
    from pygrabber.dshow_graph import FilterGraph
except ImportError:
    FilterGraph = None

MENU_HEIGHT = 28
MIN_WINDOW_WIDTH = 320
MIN_WINDOW_HEIGHT = MENU_HEIGHT + 180

# --- Keymap: pygame key constant -> HID usage ID (USB HID Usage Page 0x07) --
# Unlike the old Arduino-based client, this maps *physical keys* (not typed
# characters), matching how real keyboards report over USB. Shift/Ctrl/Alt/
# GUI are ordinary keys in this table too (mapped to the HID modifier usage
# range 0xE0-0xE7) -- pygame delivers their own KEYDOWN/KEYUP events just
# like any other key, and the Pi daemon assembles the correct HID report
# (held keys + modifier bitmask) from whatever's currently held. This means
# Shift+A "just works" the same way it does on a real keyboard, with no
# ASCII-to-shifted-symbol translation needed on this end.
KEY_MAP = {
    pygame.K_LCTRL: protocol.USAGE_MOD_LEFT_CTRL,
    pygame.K_RCTRL: protocol.USAGE_MOD_RIGHT_CTRL,
    pygame.K_LSHIFT: protocol.USAGE_MOD_LEFT_SHIFT,
    pygame.K_RSHIFT: protocol.USAGE_MOD_RIGHT_SHIFT,
    pygame.K_LALT: protocol.USAGE_MOD_LEFT_ALT,
    pygame.K_RALT: protocol.USAGE_MOD_RIGHT_ALT,
    pygame.K_LGUI: protocol.USAGE_MOD_LEFT_GUI,
    pygame.K_RGUI: protocol.USAGE_MOD_RIGHT_GUI,
    pygame.K_RETURN: 0x28,
    pygame.K_KP_ENTER: 0x28,
    pygame.K_ESCAPE: 0x29,
    pygame.K_BACKSPACE: 0x2A,
    pygame.K_TAB: 0x2B,
    pygame.K_SPACE: 0x2C,
    pygame.K_MINUS: 0x2D,
    pygame.K_EQUALS: 0x2E,
    pygame.K_LEFTBRACKET: 0x2F,
    pygame.K_RIGHTBRACKET: 0x30,
    pygame.K_BACKSLASH: 0x31,
    pygame.K_SEMICOLON: 0x33,
    pygame.K_QUOTE: 0x34,
    pygame.K_BACKQUOTE: 0x35,
    pygame.K_COMMA: 0x36,
    pygame.K_PERIOD: 0x37,
    pygame.K_SLASH: 0x38,
    pygame.K_CAPSLOCK: 0x39,
    pygame.K_INSERT: 0x49,
    pygame.K_HOME: 0x4A,
    pygame.K_PAGEUP: 0x4B,
    pygame.K_DELETE: 0x4C,
    pygame.K_END: 0x4D,
    pygame.K_PAGEDOWN: 0x4E,
    pygame.K_RIGHT: 0x4F,
    pygame.K_LEFT: 0x50,
    pygame.K_DOWN: 0x51,
    pygame.K_UP: 0x52,
}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEY_MAP[getattr(pygame, f"K_{_ch}")] = 0x04 + _i
for _i in range(9):
    KEY_MAP[getattr(pygame, f"K_{_i + 1}")] = 0x1E + _i
KEY_MAP[pygame.K_0] = 0x27
for _i in range(12):
    KEY_MAP[getattr(pygame, f"K_F{_i + 1}")] = 0x3A + _i

_SPECIAL_NAMES = {v: k for k, v in {
    protocol.USAGE_MOD_LEFT_CTRL: "LEFT_CTRL", protocol.USAGE_MOD_RIGHT_CTRL: "RIGHT_CTRL",
    protocol.USAGE_MOD_LEFT_SHIFT: "LEFT_SHIFT", protocol.USAGE_MOD_RIGHT_SHIFT: "RIGHT_SHIFT",
    protocol.USAGE_MOD_LEFT_ALT: "LEFT_ALT", protocol.USAGE_MOD_RIGHT_ALT: "RIGHT_ALT",
    protocol.USAGE_MOD_LEFT_GUI: "LEFT_GUI", protocol.USAGE_MOD_RIGHT_GUI: "RIGHT_GUI",
    0x52: "UP", 0x51: "DOWN", 0x50: "LEFT", 0x4F: "RIGHT",
    0x2A: "BACKSPACE", 0x2B: "TAB", 0x28: "RETURN",
    0x29: "ESC", 0x49: "INSERT", 0x4C: "DELETE",
    0x4B: "PAGE_UP", 0x4E: "PAGE_DOWN",
    0x4A: "HOME", 0x4D: "END", 0x39: "CAPS_LOCK",
    **{0x3A + i: f"F{i + 1}" for i in range(12)},
}.items()}


def describe_usage(usage_id):
    """Human-readable label for a HID usage ID, for debug output."""
    if usage_id in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[usage_id]
    if 0x04 <= usage_id <= 0x1D:
        return f"'{chr(ord('a') + usage_id - 0x04)}'"
    return f"0x{usage_id:02X}"


# --- Printable-character -> (usage_id, needs_shift) map, for clipboard paste ---
CHAR_MAP = {
    ' ': (0x2C, False),
    '-': (0x2D, False), '_': (0x2D, True),
    '=': (0x2E, False), '+': (0x2E, True),
    '[': (0x2F, False), '{': (0x2F, True),
    ']': (0x30, False), '}': (0x30, True),
    '\\': (0x31, False), '|': (0x31, True),
    ';': (0x33, False), ':': (0x33, True),
    "'": (0x34, False), '"': (0x34, True),
    '`': (0x35, False), '~': (0x35, True),
    ',': (0x36, False), '<': (0x36, True),
    '.': (0x37, False), '>': (0x37, True),
    '/': (0x38, False), '?': (0x38, True),
    '0': (0x27, False), ')': (0x27, True),
    '1': (0x1E, False), '!': (0x1E, True),
    '2': (0x1F, False), '@': (0x1F, True),
    '3': (0x20, False), '#': (0x20, True),
    '4': (0x21, False), '$': (0x21, True),
    '5': (0x22, False), '%': (0x22, True),
    '6': (0x23, False), '^': (0x23, True),
    '7': (0x24, False), '&': (0x24, True),
    '8': (0x25, False), '*': (0x25, True),
    '9': (0x26, False), '(': (0x26, True),
}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHAR_MAP[_ch] = (0x04 + _i, False)
    CHAR_MAP[_ch.upper()] = (0x04 + _i, True)


def letter_usage(ch):
    return 0x04 + (ord(ch) - ord("a"))


# Macro combos -- ordered list of (label, usage_ids). Each press-sequence
# is sent in order, then released in reverse order. Menu-only (no local
# hotkeys) -- every keystroke while the window is focused is forwarded to
# the target, with nothing intercepted first.
MACROS = [
    ("Ctrl+Alt+Del", [protocol.USAGE_MOD_LEFT_CTRL, protocol.USAGE_MOD_LEFT_ALT, 0x4C]),
    ("Alt+Tab", [protocol.USAGE_MOD_LEFT_ALT, 0x2B]),
    ("Alt+F4", [protocol.USAGE_MOD_LEFT_ALT, 0x3D]),
    ("Win+R", [protocol.USAGE_MOD_LEFT_GUI, letter_usage("r")]),
    ("Win+D", [protocol.USAGE_MOD_LEFT_GUI, letter_usage("d")]),
]


class SerialLink:
    """Owns the serial connection. Writes happen directly from the caller's
    thread; a background thread owns all reads and pushes parsed
    (type, payload) frames onto a queue for the main loop to drain -- this
    is the new bidirectional half of the old Arduino link, needed for ISO
    listing/mount/eject replies.

    Windows can put an idle USB-serial adapter to sleep (USB selective
    suspend), and the first read/write after it wakes can fail with a
    stale WriteFile/ReadFile error that the port never recovers from on
    its own -- previously this killed the reader thread permanently and
    left every subsequent send silently failing, needing a full app
    restart to clear. Both read and write paths now reopen the port and
    retry once instead of giving up. A periodic keepalive PING (sent only
    when nothing else has gone out recently) also helps avoid the OS
    ever considering the port idle enough to suspend in the first place --
    relevant in practice around a target reboot, where there's naturally a
    lull in traffic while waiting for POST before you start typing again.

    _io_lock guards EVERY touch of self.ser -- reads, writes, and reopens
    alike -- and is reentrant (RLock) so _reopen() can be called from
    inside an already-locked _write()/_read_loop() without deadlocking. An
    earlier version only locked _reopen()'s own close+recreate sequence,
    which left a real race: the reader thread could be mid-read() on the
    old handle at the exact moment the write path closed it out from
    under it on reopen. On Windows, pyserial's overlapped-I/O structures
    get torn down by close(), and touching them from a read that was still
    in flight on another thread corrupted state badly enough to segfault
    the whole process, not just raise a catchable exception. The read
    loop also never blocks while holding the lock -- it only ever reads
    bytes it already confirmed are waiting via in_waiting, sleeping
    outside the lock otherwise -- so a reopen (which does need to block
    briefly) is never stuck waiting behind a long blocking read."""

    KEEPALIVE_INTERVAL_SECONDS = 2.0
    # How long without a single reply before the connection indicator
    # switches to "disconnected" -- comfortably more than one keepalive
    # round trip (which should normally take milliseconds), so a single
    # slow reply doesn't flap the indicator.
    CONNECTION_TIMEOUT_SECONDS = 5.0

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.ser = serial.Serial(port, baud, timeout=0.05)
        time.sleep(2)  # let the Pi's daemon finish starting after the port opens
        self.incoming = queue.Queue()
        self._parser = protocol.FrameParser()
        self._stop = threading.Event()
        self._io_lock = threading.RLock()
        self._last_write_time = time.monotonic()
        # 0.0 (not time.monotonic()) so the very first send_keepalive_if_idle()
        # call fires a ping immediately, rather than waiting out a full
        # KEEPALIVE_INTERVAL_SECONDS before the connection indicator can
        # possibly turn green.
        self._last_ping_time = 0.0
        # None until the first frame ever arrives from the Pi -- there's no
        # reply yet at startup, so is_connected() should say so honestly.
        self._last_receive_time = None
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _reopen(self):
        with self._io_lock:
            try:
                self.ser.close()
            except serial.SerialException:
                pass
            time.sleep(0.2)
            self.ser = serial.Serial(self.port, self.baud, timeout=0.05)

    def _read_loop(self):
        while not self._stop.is_set():
            with self._io_lock:
                try:
                    n = self.ser.in_waiting
                    data = self.ser.read(n) if n else b""
                except serial.SerialException as e:
                    print(f"[serial error] {e} -- reopening port")
                    try:
                        self._reopen()
                    except serial.SerialException as e2:
                        print(f"[serial error] reopen failed: {e2}")
                        time.sleep(1)  # avoid busy-looping if the port is truly gone
                    continue
            if not data:
                time.sleep(0.005)
                continue
            for frame in self._parser.feed(data):
                self._last_receive_time = time.monotonic()
                self.incoming.put(frame)

    def _write(self, frame_bytes):
        self._last_write_time = time.monotonic()
        with self._io_lock:
            try:
                self.ser.write(frame_bytes)
                return
            except serial.SerialException as e:
                print(f"[serial error] {e} -- reopening port")
            try:
                self._reopen()
                self.ser.write(frame_bytes)
            except serial.SerialException as e:
                print(f"[serial error] reopen/retry failed: {e}")

    def send_keepalive_if_idle(self):
        """Two independent reasons to send a PING here, checked separately
        rather than folded into one condition:

        - Anti-suspend (gated on time since the last WRITE of any kind):
          if nothing's gone out in a while, ping so Windows doesn't
          consider the port idle enough to selectively-suspend it.

        - Connection-indicator freshness (gated on time since the last
          PING WE sent, regardless of other write traffic): is_connected()
          only reflects the last time something was RECEIVED, but ordinary
          KEY_DOWN/KEY_UP/MOUSE_STATE/MOUSE_SCROLL frames never get a
          reply from the daemon. Gating this solely on write-idle time (as
          an earlier version did) meant continuous keyboard/mouse activity
          -- which is all one-way, write-only traffic -- kept
          _last_write_time constantly refreshed and so never triggered a
          ping, while never soliciting a reply either: the connection
          indicator would flip red under exactly the busiest, most
          obviously-still-connected traffic pattern. This must be judged
          against the last time WE pinged, not the last time anything was
          received -- gating on receive-idle instead would fire a new ping
          every single frame while genuinely waiting for a reply, instead
          of a clean one every KEEPALIVE_INTERVAL_SECONDS."""
        now = time.monotonic()
        idle_for_suspend = now - self._last_write_time > self.KEEPALIVE_INTERVAL_SECONDS
        overdue_for_indicator = now - self._last_ping_time > self.KEEPALIVE_INTERVAL_SECONDS
        if idle_for_suspend or overdue_for_indicator:
            self.send_ping()

    def is_connected(self):
        """True if the Pi has replied to *something* -- a PONG, an ISO
        list, whatever -- within the last CONNECTION_TIMEOUT_SECONDS. The
        periodic keepalive (see send_keepalive_if_idle) guarantees there's
        always something recent to judge this by, even during otherwise
        quiet stretches."""
        if self._last_receive_time is None:
            return False
        return time.monotonic() - self._last_receive_time < self.CONNECTION_TIMEOUT_SECONDS

    def send_ping(self):
        self._last_ping_time = time.monotonic()
        self._write(protocol.encode_ping())

    def send_key(self, down, usage_id):
        self._write(protocol.encode_key_event(down, usage_id))

    def send_mouse_state(self, x_frac, y_frac, buttons):
        self._write(protocol.encode_mouse_state(x_frac, y_frac, buttons))

    def send_mouse_scroll(self, amount):
        self._write(protocol.encode_mouse_scroll(amount))

    def send_list_isos(self):
        self._write(protocol.encode_list_isos())

    def send_mount_iso(self, name):
        self._write(protocol.encode_mount_iso(name))

    def send_eject_iso(self):
        self._write(protocol.encode_eject_iso())

    def send_ap_enable(self):
        self._write(protocol.encode_ap_enable())

    def send_ap_disable(self):
        self._write(protocol.encode_ap_disable())

    def send_ap_status_query(self):
        self._write(protocol.encode_ap_status_query())

    def send_restart_daemon(self):
        self._write(protocol.encode_restart_daemon())

    def send_reboot_pi(self):
        self._write(protocol.encode_reboot_pi())

    def send_shutdown_pi(self):
        self._write(protocol.encode_shutdown_pi())

    def send_shell_open(self):
        self._write(protocol.encode_shell_open())

    def send_shell_close(self):
        self._write(protocol.encode_shell_close())

    def send_shell_input(self, data):
        self._write(protocol.encode_shell_input(data))

    def send_shell_resize(self, rows, cols):
        self._write(protocol.encode_shell_resize(rows, cols))

    def close(self):
        self._stop.set()
        self._reader_thread.join(timeout=1)
        self.ser.close()


def find_capture_devices(max_index=5, skip_index=None):
    """Probes indices 0..max_index-1 by briefly opening each one. skip_index
    (the currently in-use device, if any) is reported as present without
    actually being reopened -- some backends won't let you open a device
    twice at once, and even if they would, doing so would risk disrupting
    the live stream just to confirm what we already know is working."""
    found = []
    for i in range(max_index):
        if i == skip_index:
            found.append(i)
            continue
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            found.append(i)
            cap.release()
    return found


def capture_device_names():
    """Best-effort {index: friendly name} mapping for capture devices --
    OpenCV has no portable API for this, so it's OS-specific and returns
    {} wherever it's not available (macOS always, Windows without
    `pip install pygrabber`, or if enumeration fails for any reason).
    Callers fall back to a plain "Device N" label in that case.

    Windows: pygrabber walks the same DirectShow device list client.py
    already opens devices through (cv2.CAP_DSHOW), so its indices line up
    with OpenCV's.

    Linux: V4L2 exposes a name file per device in sysfs, matching the
    /dev/videoN index OpenCV's V4L2 backend opens directly -- free, no
    extra dependency needed."""
    if sys.platform == "win32":
        if FilterGraph is None:
            return {}
        try:
            return dict(enumerate(FilterGraph().get_input_devices()))
        except Exception:
            return {}
    if sys.platform.startswith("linux"):
        names = {}
        v4l2_dir = "/sys/class/video4linux"
        for entry in os.listdir(v4l2_dir) if os.path.isdir(v4l2_dir) else []:
            try:
                index = int(entry.replace("video", ""))
                with open(f"{v4l2_dir}/{entry}/name") as f:
                    names[index] = f.read().strip()
            except (ValueError, OSError):
                continue
        return names
    return {}


def list_serial_ports():
    """Unlike capture devices, enumerating serial ports doesn't require
    opening them at all (just a registry/sysfs query), so there's no
    equivalent risk to sidestep for the currently-active one."""
    return [p.device for p in serial.tools.list_ports.comports()]


def serial_port_descriptions():
    """{device: description} for detected serial ports, e.g.
    "COM5" -> "USB-SERIAL CH340 (COM5)". Unlike capture devices, pyserial
    already exposes this portably on Windows/Linux/macOS via each OS's own
    device registry -- no OS-specific code needed here. "n/a" (pyserial's
    placeholder when a backend has nothing better) is treated as no
    description, same as an empty one, so callers can fall back to just
    the bare port path either way."""
    descriptions = {}
    for p in serial.tools.list_ports.comports():
        if p.description and p.description != "n/a":
            descriptions[p.device] = p.description
    return descriptions


def open_capture(index, width=None, height=None, fourcc=None):
    """Opens the capture device, preferring DirectShow on Windows over
    OpenCV's default backend (Media Foundation) -- DSHOW negotiates format
    with cheap UVC capture cards more predictably, and is closer to what
    tools like OBS use, which is why the same device can look fine in OBS
    but grainy/compressed here with the default backend. width/height/
    fourcc let you force a specific mode to match whatever OBS negotiated,
    since the "right" mode is device-specific and can't be guessed
    generically."""
    if sys.platform == "win32":
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(index)

    if fourcc:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc.upper()))
    if width:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    return cap


def describe_negotiated_mode(cap):
    """Human-readable summary of what a capture device actually negotiated
    -- handy for comparing against OBS's device properties when video
    quality looks off (see open_capture's docstring)."""
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    fourcc_str = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)) if fourcc else "?"
    return (f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} "
            f"'{fourcc_str}' @ {cap.get(cv2.CAP_PROP_FPS):.0f}fps")


class VideoStream:
    """Runs cap.read() in a background thread so a slow or variable-latency
    capture device never delays keyboard/mouse handling in the main loop.
    Previously cap.read() was called directly in the same loop that polls
    pygame input, so any hiccup in frame capture (common with USB capture
    dongles) added directly to mouse/keyboard latency. The main loop now
    just grabs whatever the latest decoded frame is and moves on."""

    def __init__(self, cap):
        self.cap = cap
        self._lock = threading.Lock()
        self._latest_frame = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        while not self._stop.is_set():
            ret, frame = self.cap.read()
            if ret:
                with self._lock:
                    self._latest_frame = frame

    def get_latest_frame(self):
        with self._lock:
            return self._latest_frame

    def release(self):
        self._stop.set()
        self._thread.join(timeout=1)
        self.cap.release()


def usage_for_event(event):
    """Return the HID usage ID to send for this pygame KEYDOWN event,
    or None if we don't have a mapping for it."""
    return KEY_MAP.get(event.key)


def send_macro(link, usage_ids, debug, label):
    """Press a sequence of keys in order, then release in reverse order,
    with a short delay between each step so the target reliably registers
    every key in the combo."""
    if debug:
        print(f"[MACRO] sending {label}")
    for usage_id in usage_ids:
        link.send_key(True, usage_id)
        time.sleep(0.03)
    for usage_id in reversed(usage_ids):
        link.send_key(False, usage_id)
        time.sleep(0.03)


def send_clipboard_text(link, debug):
    """Read the local clipboard and type it out to the target, char by
    char, explicitly pressing/releasing Shift for uppercase/shifted
    characters -- the target sees exactly the same key sequence a real
    keyboard would send. Runs synchronously, so the video feed will pause
    briefly for long text."""
    if pyperclip is None:
        print("[paste] pyperclip is not installed -- run: pip install pyperclip")
        return

    try:
        text = pyperclip.paste()
    except Exception as e:
        print(f"[paste] could not read clipboard: {e}")
        return

    if not text:
        print("[paste] clipboard is empty")
        return

    if debug:
        print(f"[paste] typing {len(text)} characters from clipboard")

    for ch in text:
        if ch == "\n":
            usage_id, shift = 0x28, False
        elif ch == "\t":
            usage_id, shift = 0x2B, False
        elif ch in CHAR_MAP:
            usage_id, shift = CHAR_MAP[ch]
        else:
            if debug:
                print(f"[paste] skipping unsupported character {ch!r}")
            continue

        if shift:
            link.send_key(True, protocol.USAGE_MOD_LEFT_SHIFT)
        link.send_key(True, usage_id)
        time.sleep(0.008)
        link.send_key(False, usage_id)
        if shift:
            link.send_key(False, protocol.USAGE_MOD_LEFT_SHIFT)
        time.sleep(0.008)


def map_click_to_target(pos, video_rect):
    """Maps a raw window-pixel click position to a (x_frac, y_frac) pair in
    [0, 1] representing where it landed within the displayed video content,
    clamping into video_rect first so a click that lands in a letterbox/
    pillarbox bar still maps to the nearest edge instead of being ignored.
    Returns None if there's no valid video area to map into (e.g. window
    still initializing)."""
    if video_rect.width <= 0 or video_rect.height <= 0:
        return None
    x = min(max(pos[0], video_rect.left), video_rect.right - 1)
    y = min(max(pos[1], video_rect.top), video_rect.bottom - 1)
    x_frac = (x - video_rect.left) / video_rect.width
    y_frac = (y - video_rect.top) / video_rect.height
    return x_frac, y_frac


class VideoState:
    """Tracks which local capture device is active and which others were
    last seen -- purely local/synchronous (no round trip to the Pi), so
    unlike Storage/Network below the device list is scanned once when the
    menu is opened rather than needing an explicit reply to arrive."""

    def __init__(self, current_index):
        self.current_index = current_index
        self.available = [current_index]
        self.names = {}  # {index: friendly name}, best-effort -- see capture_device_names()
        self.error = None


class PortState:
    """Tracks which serial port is active and which others were last seen
    -- same pattern as VideoState (local/synchronous scan, no round trip
    to the Pi needed to list them)."""

    def __init__(self, current_port):
        self.current_port = current_port
        self.available = [current_port]
        self.descriptions = {}  # {device: description}, best-effort -- see serial_port_descriptions()
        self.error = None


class StorageState:
    """Tracks what the client currently believes about the Pi's ISO
    library, updated as replies arrive from the reader thread's queue."""

    def __init__(self):
        self.isos = []
        self.current = None
        self.loading = False
        self.busy = None  # name being mounted, "__eject__", or None
        self.error = None


class NetworkState:
    """Tracks what the client currently believes about the Pi's ISO-upload
    Wi-Fi AP (phase 2, optional -- only meaningful if install-webui.sh has
    been run on the Pi). ap_enabled is None until the first status reply
    arrives, since the daemon doesn't push this unsolicited at startup the
    way it does for the mounted ISO."""

    def __init__(self):
        self.ap_enabled = None  # None = unknown, else True/False
        self.busy = None  # "enabling", "disabling", or None
        self.error = None


class ArmedAction:
    """A confirm-to-execute guard for menu items with real physical
    consequences (Reboot/Shutdown Pi) that the rest of this app's
    one-click menu items (Eject, Mount, AP toggle) don't have -- a
    Pi Zero W has no remote power button, so an accidental Shutdown click
    means someone has to walk over and physically power-cycle it.

    The first click arms it (the menu item's own label changes to prompt
    a second click); a second click within ARM_TIMEOUT_SECONDS actually
    fires. The arming click's handler returns True to tell MenuBar to keep
    the dropdown open (see handle_click) rather than closing it like every
    other one-click item here -- otherwise the freshly-relabeled item
    would vanish the instant it appears, forcing the user to notice the
    label changed, reopen the menu, and find it again just to confirm. The
    short timeout alone is what disarms it if no second click follows."""

    ARM_TIMEOUT_SECONDS = 4.0

    def __init__(self):
        self._armed_at = None

    def is_armed(self):
        return self._armed_at is not None and (time.monotonic() - self._armed_at) < self.ARM_TIMEOUT_SECONDS

    def arm(self):
        self._armed_at = time.monotonic()

    def disarm(self):
        self._armed_at = None


class MenuBar:
    """A minimal top-of-window menu bar with click-to-open dropdowns.
    Real mouse input always reaches it directly (nothing is ever captured
    or hidden) -- clicks are routed here purely by Y position, whenever
    they land within the menu bar strip.

    Each menu's items may be a static list of (label, hotkey, fn), or a
    zero-arg callable returning a fresh list -- used by the Storage menu,
    whose contents depend on the Pi's latest reply rather than being fixed
    at startup like Macros/Clipboard/Session."""

    CONNECTED_COLOR = (60, 200, 60)
    DISCONNECTED_COLOR = (200, 60, 60)

    def __init__(self, width, menus, get_connected=None):
        self.width = width
        self.font = pygame.font.SysFont(None, 20)
        self.bg = (40, 40, 40)
        self.fg = (230, 230, 230)
        self.highlight = (70, 70, 70)
        self.border = (90, 90, 90)
        self.open_index = None
        # Zero-arg callable returning True/False for the connection-status
        # light, right-aligned in the bar. Optional (None skips drawing it
        # entirely) so tests/other callers don't need to supply one.
        self.get_connected = get_connected

        # menus: list of (label, items_or_callable, on_open_or_None)
        self.menus = menus

        # Precompute top-level button rects (labels are static even though
        # some menus' dropdown contents are dynamic).
        self.top_rects = []
        x = 8
        for label, _items, _on_open in self.menus:
            w = self.font.size(label)[0] + 20
            self.top_rects.append(pygame.Rect(x, 0, w, MENU_HEIGHT))
            x += w

    def _items(self, index):
        entry = self.menus[index][1]
        return entry() if callable(entry) else entry

    def resize(self, width):
        # Only the background fill depends on the total width -- top_rects
        # are left-anchored and stay valid as the window grows/shrinks.
        self.width = width

    def _connection_color(self):
        connected = self.get_connected is not None and self.get_connected()
        return self.CONNECTED_COLOR if connected else self.DISCONNECTED_COLOR

    def draw(self, screen):
        screen.fill(self.bg, pygame.Rect(0, 0, self.width, MENU_HEIGHT))
        pygame.draw.line(screen, self.border, (0, MENU_HEIGHT - 1), (self.width, MENU_HEIGHT - 1))

        for i, (label, _items, _on_open) in enumerate(self.menus):
            rect = self.top_rects[i]
            if self.open_index == i:
                pygame.draw.rect(screen, self.highlight, rect)
            text = self.font.render(label, True, self.fg)
            screen.blit(text, (rect.x + 10, rect.y + (MENU_HEIGHT - text.get_height()) // 2))

        # Pi connection status light, right-aligned: green while the link
        # has heard from the Pi recently, red otherwise (including at
        # startup, before the first reply has ever arrived).
        if self.get_connected is not None:
            radius = 6
            cx = self.width - radius - 10
            cy = MENU_HEIGHT // 2
            pygame.draw.circle(screen, self._connection_color(), (cx, cy), radius)

        if self.open_index is not None:
            self._draw_dropdown(screen, self.open_index)

    def _dropdown_rects(self, index):
        items = self._items(index)
        top = self.top_rects[index]
        item_w = max(self.font.size(f"{lbl}   {hk}")[0] + 24 for lbl, hk, _fn in items)
        item_w = max(item_w, top.width)
        rects = []
        y = MENU_HEIGHT
        for _lbl, _hk, _fn in items:
            rects.append(pygame.Rect(top.x, y, item_w, MENU_HEIGHT))
            y += MENU_HEIGHT
        return rects

    def _draw_dropdown(self, screen, index):
        items = self._items(index)
        rects = self._dropdown_rects(index)
        for rect, (lbl, hk, _fn) in zip(rects, items):
            pygame.draw.rect(screen, self.bg, rect)
            pygame.draw.rect(screen, self.border, rect, 1)
            text = self.font.render(lbl, True, self.fg)
            screen.blit(text, (rect.x + 10, rect.y + (MENU_HEIGHT - text.get_height()) // 2))
            if hk:
                hk_text = self.font.render(hk, True, (160, 160, 160))
                screen.blit(hk_text, (rect.right - hk_text.get_width() - 10,
                                       rect.y + (MENU_HEIGHT - hk_text.get_height()) // 2))

    def handle_click(self, pos):
        """Returns True if the click was consumed by the menu bar (either
        opened/closed a menu or triggered an action)."""
        x, y = pos

        if self.open_index is not None:
            rects = self._dropdown_rects(self.open_index)
            items = self._items(self.open_index)
            for rect, (_lbl, _hk, fn) in zip(rects, items):
                if rect.collidepoint(x, y):
                    # fn() returning a truthy value means "keep the dropdown
                    # open" -- used by ArmedAction-guarded items (Reboot/
                    # Shutdown Pi) so the arming click leaves the item
                    # sitting right there under the cursor with its new
                    # "click again to confirm" label, instead of forcing the
                    # user to notice the label changed AFTER the dropdown
                    # already closed and reopen the menu to find it again.
                    # Every other item here returns None (falsy), so this
                    # doesn't change any existing item's close-on-click
                    # behavior.
                    keep_open = fn()
                    if not keep_open:
                        self.open_index = None
                    return True

        for i, rect in enumerate(self.top_rects):
            if rect.collidepoint(x, y):
                opening = self.open_index != i
                self.open_index = None if self.open_index == i else i
                if opening and self.menus[i][2] is not None:
                    self.menus[i][2]()
                return True

        # Clicked elsewhere: close any open menu, consume the click only
        # if a menu was actually open (so a plain click in the video area
        # doesn't get silently swallowed).
        was_open = self.open_index is not None
        self.open_index = None
        return was_open


class TerminalWindow:
    """Controller for the Pi-shell terminal, rendered in a genuine,
    separate OS window rather than as an overlay on top of the video --
    pygame/SDL only supports one window per process, so a real second
    window means a second process (terminal_window.py, spawned here).

    This class does no rendering and no ANSI/VT100 interpretation itself
    -- it's a pure relay. It talks to the child process over stdin/stdout
    using the exact same wire framing as the real laptop<->Pi serial link
    (protocol.py's encode()/FrameParser), so SHELL_* frames arriving from
    the Pi are simply re-written unchanged onto the child's stdin, and
    SHELL_* frames the child writes to its stdout are simply forwarded
    unchanged onto the real link -- see terminal_window.py's docstring
    for the exact frame directions.

    Trust model is the same as protocol.py's SHELL_* comment: nothing new
    to guard here either, it's just another local pipe to a process this
    same user account spawned."""

    SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terminal_window.py")

    def __init__(self, link):
        self.link = link
        self.is_open = False
        self.proc = None
        self._parser = None

    def open(self):
        if self.is_open:
            return
        if pyte is None:
            print("[terminal] pyte is not installed -- run: pip install pyte")
            return
        try:
            self.proc = subprocess.Popen(
                [sys.executable, self.SCRIPT_PATH],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
        except OSError as e:
            print(f"[terminal] could not launch terminal window: {e}")
            return
        self._parser = protocol.FrameParser()
        self.is_open = True
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.link.send_shell_open()

    def _read_loop(self):
        """Runs in a background thread for the lifetime of the child
        process: relays SHELL_INPUT/SHELL_RESIZE/SHELL_CLOSE frames from
        its stdout straight to the real Pi link. Ends when the child's
        stdout hits EOF -- whether from a graceful close (see close()/
        notify_closed()) or the window being closed/crashing on its own
        -- and either way makes sure the Pi's bash session doesn't linger
        orphaned if the window vanished without an explicit SHELL_CLOSE."""
        stdout_fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(stdout_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            for frame_type, payload in self._parser.feed(chunk):
                if frame_type == protocol.SHELL_INPUT:
                    self.link.send_shell_input(payload)
                elif frame_type == protocol.SHELL_RESIZE:
                    rows, cols = protocol.decode_shell_resize(payload)
                    self.link.send_shell_resize(rows, cols)
                elif frame_type == protocol.SHELL_CLOSE:
                    self.link.send_shell_close()
        self.is_open = False
        self.link.send_shell_close()

    def feed(self, payload):
        """A SHELL_OUTPUT frame arrived from the Pi -- relay it to the
        window unchanged."""
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write(protocol.encode_shell_output(payload))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def notify_closed(self):
        """The Pi's bash session ended on its own (e.g. the user typed
        `exit`) -- tell the window so it can show that and let the user
        close it in their own time, same as a real terminal emulator
        noticing its child process exited.

        Deliberately does NOT wait for or terminate the child here, unlike
        close() -- this was a real bug: forcibly killing the window ~1s
        after telling it the session closed defeated the entire point of
        showing the banner and letting the user close it themselves. The
        child process is left to run independently; _read_loop's own EOF
        handling (already idempotent) cleans up once it eventually exits
        on its own, whenever the user actually closes that window."""
        if not self.is_open:
            return
        self.is_open = False
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.write(protocol.encode_shell_closed())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        self.proc = None  # not ours to track/wait on anymore -- let it linger

    def close(self):
        """The user asked (Terminal menu) to end the session right now --
        unlike notify_closed(), this really does end the window, since the
        user explicitly asked for it to close rather than the Pi's shell
        ending unprompted."""
        if not self.is_open:
            return
        self.link.send_shell_close()
        self.is_open = False
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write(protocol.encode_shell_closed())
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
            self.proc = None


def _fix_windows_dpi_scaling():
    """Without this, Windows treats the process as DPI-unaware and scales
    the whole window via bitmap stretching to match the display's scale
    factor (125%/150%/etc, common on laptop screens) -- which blurs
    everything, and the blur gets more visually obvious the more fine
    detail (small text, UI chrome) is in the source, i.e. exactly "grainy
    and unreadable at higher resolutions." DPI-aware apps like OBS declare
    this and never get stretched.

    This MUST go through SDL's own mechanism (the SDL_WINDOWS_DPI_AWARENESS
    env var, read once at pygame.init() time), not a raw call to Windows'
    SetProcessDpiAwareness(). A raw ctypes call was tried first and caused
    a worse bug: it changes how Windows reports scaling to the process
    without SDL's knowledge, so SDL's own idea of the window's coordinate
    space stopped matching Windows' actual (scaled) coordinates -- every
    mouse event's position came out scaled up relative to the window's
    real pixel size. Since map_click_to_target() clamps into the video
    content rect, an out-of-range scaled-up position landed every single
    click at the rect's bottom-right corner, which happens to be exactly
    where Windows' "Show Desktop" sliver lives -- so every click toggled
    minimize/restore-all instead of clicking where the video actually
    showed. Must be set before pygame.init()."""
    if sys.platform != "win32":
        return
    os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")


def main():
    _fix_windows_dpi_scaling()

    parser = argparse.ArgumentParser(description="Laptop crash cart: video + keyboard/mouse/storage bridge")
    parser.add_argument("--serial-port", default=None,
                         help="COM port / tty for the Pi's control link -- omit to auto-select "
                              "the first detected serial port")
    parser.add_argument("--capture-index", type=int, default=None, help="OpenCV capture device index")
    parser.add_argument("--capture-width", type=int, default=1920,
                         help="request this capture width (default 1920 -- without an explicit "
                              "width/height request, some capture cards silently fall back to a "
                              "lower-detail mode despite reporting the same nominal frame size, "
                              "which looks grainy/blurry). Pass 0 to not request a specific width.")
    parser.add_argument("--capture-height", type=int, default=1080,
                         help="request this capture height (default 1080). Pass 0 to not request "
                              "a specific height.")
    parser.add_argument("--capture-fourcc", default=None,
                         help="force a capture pixel format/codec, e.g. MJPG or YUY2 -- some capture "
                              "cards fall back to a more heavily compressed mode at higher resolutions "
                              "unless a specific format is requested")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--debug", action="store_true", help="print each key/mouse event to the terminal")
    args = parser.parse_args()

    capture_index = args.capture_index
    if capture_index is None:
        print("Scanning for capture devices...")
        devices = find_capture_devices()
        if not devices:
            print("No capture devices found. Specify one with --capture-index.")
            sys.exit(1)
        print(f"Found devices at indices: {devices}. Using {devices[0]}.")
        capture_index = devices[0]

    print(f"Opening capture device index {capture_index}...")
    cap = open_capture(capture_index, args.capture_width, args.capture_height, args.capture_fourcc)
    if not cap.isOpened():
        print(f"Could not open capture device at index {capture_index}.")
        sys.exit(1)

    print(f"Negotiated capture mode: {describe_negotiated_mode(cap)} -- compare against OBS's "
          f"device properties for this same capture card if video quality looks off")

    serial_port = args.serial_port
    if serial_port is None:
        print("Scanning for serial ports...")
        ports = list_serial_ports()
        if not ports:
            print("No serial ports found. Specify one with --serial-port.")
            sys.exit(1)
        print(f"Found ports: {ports}. Using {ports[0]}.")
        serial_port = ports[0]

    print(f"Opening serial link on {serial_port} @ {args.baud} baud...")
    try:
        link = SerialLink(serial_port, args.baud)
    except serial.SerialException as e:
        print(f"Could not open serial port: {e}")
        sys.exit(1)

    pygame.init()
    video = VideoStream(cap)
    frame = None
    print("Waiting for the first frame from the capture device...")
    wait_start = time.monotonic()
    while frame is None:
        frame = video.get_latest_frame()
        if time.monotonic() - wait_start > 5:
            print("Could not read a frame from the capture device.")
            sys.exit(1)
        time.sleep(0.05)
    h, w = frame.shape[:2]
    screen = pygame.display.set_mode((w, h + MENU_HEIGHT), pygame.RESIZABLE)

    # Where the video is actually drawn on screen right now (accounting for
    # letterboxing/pillarboxing), updated every render pass -- used to map
    # a click's raw window position to a fraction of the way across the
    # target's screen. Seeded from the startup frame size before the first
    # render pass runs.
    video_rect = pygame.Rect(0, MENU_HEIGHT, w, h)
    # Bitmask of buttons currently held that we told the target to press --
    # tracks only presses that started inside the video area, so a release
    # is still forwarded no matter where the cursor ends up by the time it
    # happens (dragging out over the menu bar, say), avoiding a stuck
    # button on the target. A press that started on the menu bar itself
    # never sets any of these bits.
    held_target_buttons = 0

    video_state = VideoState(capture_index)
    port_state = PortState(serial_port)
    storage = StorageState()
    network = NetworkState()
    terminal = TerminalWindow(link)
    reboot_arm = ArmedAction()
    shutdown_arm = ArmedAction()

    running = True

    def do_macro(usage_ids, label):
        send_macro(link, usage_ids, args.debug, label)

    def do_paste():
        send_clipboard_text(link, args.debug)

    def do_quit():
        nonlocal running
        running = False

    def do_toggle_terminal():
        if terminal.is_open:
            terminal.close()
        else:
            terminal.open()

    def do_restart_daemon():
        print("[power] restarting the Pi's daemon")
        link.send_restart_daemon()

    def do_reboot():
        if reboot_arm.is_armed():
            reboot_arm.disarm()
            print("[power] rebooting the Pi")
            link.send_reboot_pi()
            return False  # confirmed and fired -- close the dropdown, as usual
        shutdown_arm.disarm()
        reboot_arm.arm()
        return True  # just armed -- keep the dropdown open for the confirm click

    def do_shutdown():
        if shutdown_arm.is_armed():
            shutdown_arm.disarm()
            print("[power] shutting down the Pi")
            link.send_shutdown_pi()
            return False
        reboot_arm.disarm()
        shutdown_arm.arm()
        return True

    def terminal_menu_items():
        label = "Close Pi Shell" if terminal.is_open else "Open Pi Shell"
        items = [(label, "", do_toggle_terminal)]
        items.append(("Restart Daemon", "", do_restart_daemon))
        reboot_label = "Reboot Pi (click again to confirm)" if reboot_arm.is_armed() else "Reboot Pi"
        items.append((reboot_label, "", do_reboot))
        shutdown_label = "Shutdown Pi (click again to confirm)" if shutdown_arm.is_armed() else "Shutdown Pi"
        items.append((shutdown_label, "", do_shutdown))
        return items

    def do_refresh_video_devices():
        video_state.error = None
        video_state.available = find_capture_devices(skip_index=video_state.current_index)
        # Computed once here (not from video_menu_items() itself, which
        # _draw_dropdown() calls every single frame the dropdown is open) --
        # on Windows this spins up a DirectShow FilterGraph, not something
        # to redo 60 times a second just to draw a label.
        video_state.names = capture_device_names()

    def do_switch_video(new_index):
        nonlocal video
        if new_index == video_state.current_index:
            return
        print(f"Switching to capture device index {new_index}...")
        new_cap = open_capture(new_index, args.capture_width, args.capture_height, args.capture_fourcc)
        if not new_cap.isOpened():
            video_state.error = f"Could not open device {new_index}"
            return
        old_video = video
        video = VideoStream(new_cap)
        old_video.release()
        video_state.current_index = new_index
        video_state.error = None
        print(f"Switched to capture device {new_index}: {describe_negotiated_mode(new_cap)}")

    def video_menu_items():
        items = []
        if video_state.error:
            items.append((f"Error: {video_state.error}", "", lambda: None))
        if not video_state.available:
            items.append(("(no devices found)", "", lambda: None))
        else:
            for idx in sorted(video_state.available):
                marker = "* " if idx == video_state.current_index else "  "
                name = video_state.names.get(idx)
                label = f"{marker}Device {idx}" + (f" - {name}" if name else "")
                items.append((label, "", lambda idx=idx: do_switch_video(idx)))
        items.append(("Refresh", "", do_refresh_video_devices))
        return items

    def do_refresh_serial_ports():
        port_state.error = None
        port_state.available = list_serial_ports()
        if port_state.current_port not in port_state.available:
            port_state.available.append(port_state.current_port)
        port_state.descriptions = serial_port_descriptions()

    def do_switch_serial_port(new_port):
        nonlocal link
        if new_port == port_state.current_port:
            return
        print(f"Switching to serial port {new_port}...")
        try:
            # SerialLink's constructor sleeps ~2s (letting the Pi's daemon
            # settle after the port opens) -- a deliberate, bounded pause
            # for a rare, user-initiated action, same trade-off already
            # accepted for ISO mount/eject.
            new_link = SerialLink(new_port, args.baud)
        except serial.SerialException as e:
            port_state.error = f"Could not open {new_port}: {e}"
            return
        old_link = link
        link = new_link
        # TerminalWindow holds its own reference to the link it relays
        # shell I/O through -- keep it pointed at whichever one is live,
        # same reasoning as the MenuBar connection-indicator lambda above.
        terminal.link = new_link
        old_link.close()
        port_state.current_port = new_port
        port_state.error = None
        print(f"Switched to serial port {new_port}")

    def port_menu_items():
        items = []
        if port_state.error:
            items.append((f"Error: {port_state.error}", "", lambda: None))
        if not port_state.available:
            items.append(("(no ports found)", "", lambda: None))
        else:
            for dev in port_state.available:
                marker = "* " if dev == port_state.current_port else "  "
                desc = port_state.descriptions.get(dev)
                label = f"{marker}{dev}" + (f" - {desc}" if desc else "")
                items.append((label, "", lambda dev=dev: do_switch_serial_port(dev)))
        items.append(("Refresh", "", do_refresh_serial_ports))
        return items

    def do_refresh_isos():
        storage.loading = True
        storage.error = None
        link.send_list_isos()

    def do_mount(name):
        if storage.busy is not None:
            return
        storage.busy = name
        storage.error = None
        link.send_mount_iso(name)

    def do_eject():
        if storage.busy is not None:
            return
        storage.busy = "__eject__"
        storage.error = None
        link.send_eject_iso()

    def storage_menu_items():
        items = []
        if storage.error:
            items.append((f"Error: {storage.error}", "", lambda: None))
        elif storage.loading:
            items.append(("Loading...", "", lambda: None))
        elif not storage.isos:
            items.append(("(none found)", "", lambda: None))
        else:
            for name in storage.isos:
                marker = "* " if name == storage.current else "  "
                label = f"{marker}{name}"
                if storage.busy == name:
                    label += " (mounting...)"
                items.append((label, "", lambda name=name: do_mount(name)))
        items.append(("Refresh", "", do_refresh_isos))
        eject_label = "Eject (ejecting...)" if storage.busy == "__eject__" else "Eject"
        items.append((eject_label, "", do_eject))
        return items

    def do_query_ap_status():
        network.error = None
        link.send_ap_status_query()

    def do_enable_ap():
        if network.busy is not None:
            return
        network.busy = "enabling"
        network.error = None
        link.send_ap_enable()

    def do_disable_ap():
        if network.busy is not None:
            return
        network.busy = "disabling"
        network.error = None
        link.send_ap_disable()

    def network_menu_items():
        items = []
        if network.error:
            items.append((f"Error: {network.error}", "", lambda: None))
        if network.busy == "enabling":
            items.append(("Enabling AP...", "", lambda: None))
        elif network.busy == "disabling":
            items.append(("Disabling AP...", "", lambda: None))
        else:
            state = {None: "unknown", True: "ON", False: "OFF"}[network.ap_enabled]
            items.append((f"AP status: {state}", "", do_query_ap_status))
            items.append(("Enable Wi-Fi AP (for ISO uploads)", "", do_enable_ap))
            items.append(("Disable Wi-Fi AP", "", do_disable_ap))
        return items

    menus = [
        ("Macros", [(label, "", lambda usage_ids=usage_ids, label=label: do_macro(usage_ids, label))
                    for (label, usage_ids) in MACROS], None),
        ("Clipboard", [("Paste Clipboard", "", do_paste)], None),
        ("Video", video_menu_items, do_refresh_video_devices),
        ("Serial Port", port_menu_items, do_refresh_serial_ports),
        ("Storage", storage_menu_items, do_refresh_isos),
        ("Network", network_menu_items, do_query_ap_status),
        ("Session", [("Quit", "", do_quit)], None),
        ("Debug", terminal_menu_items, None)
    ]
    # A plain "link.is_connected" here would snapshot a bound method on
    # whichever SerialLink object is current *right now* -- once
    # do_switch_serial_port() can reassign `link` to a whole new object,
    # that snapshot would silently keep reporting the OLD (possibly now
    # closed) link forever. The lambda re-reads `link` fresh on every call.
    menu = MenuBar(w, menus, lambda: link.is_connected())

    pygame.display.set_caption("Crash Cart")

    def handle_incoming(frame_type, payload):
        p = protocol
        if frame_type == p.ISO_LIST:
            names, current = p.decode_iso_list(payload)
            storage.isos = names
            storage.current = current
            storage.loading = False
            storage.error = None
        elif frame_type == p.ISO_MOUNTED:
            name = p.decode_iso_mounted(payload)
            storage.current = name
            storage.busy = None
            storage.error = None
            if name not in storage.isos:
                storage.isos.append(name)
        elif frame_type == p.ISO_EJECTED:
            storage.current = None
            storage.busy = None
            storage.error = None
        elif frame_type == p.AP_STATUS:
            network.ap_enabled = p.decode_ap_status(payload)
            network.busy = None
            network.error = None
        elif frame_type == p.ERROR:
            failed_type, message = p.decode_error(payload)
            if failed_type in (p.LIST_ISOS, p.MOUNT_ISO, p.EJECT_ISO):
                storage.busy = None
                storage.loading = False
                storage.error = message
            elif failed_type in (p.AP_ENABLE, p.AP_DISABLE, p.AP_STATUS_QUERY):
                network.busy = None
                network.error = message
            print(f"[pi error] {message}")
        elif frame_type == p.SHELL_OUTPUT:
            terminal.feed(payload)
        elif frame_type == p.SHELL_CLOSED:
            terminal.notify_closed()
        if args.debug and frame_type not in (p.ISO_LIST, p.ISO_MOUNTED, p.ISO_EJECTED, p.AP_STATUS, p.ERROR,
                                              p.SHELL_OUTPUT, p.SHELL_CLOSED):
            print(f"[from pi] type=0x{frame_type:02X} payload={payload!r}")

    # Track which pygame keys are currently "down" and what usage ID was
    # sent, so KEYUP releases the correct code.
    active_keys = {}

    clock = pygame.time.Clock()

    while running:
        # Grabs whatever frame the background capture thread most recently
        # decoded, rather than blocking here on the capture device itself --
        # a slow/variable capture device would otherwise stall this same
        # loop iteration's pygame.event.get() below, adding directly to
        # keyboard/mouse latency.
        frame = video.get_latest_frame()
        # Clear every frame so resizing to an aspect ratio that doesn't match
        # the video leaves clean letterbox/pillarbox bars, not stale pixels.
        screen.fill((0, 0, 0))
        if frame is not None:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame_h, frame_w = frame.shape[:2]
            surf = pygame.image.frombuffer(frame.tobytes(), (frame_w, frame_h), "RGB")

            video_area_w = screen.get_width()
            video_area_h = screen.get_height() - MENU_HEIGHT
            if video_area_w > 0 and video_area_h > 0:
                # Scale to fit the available area while preserving the
                # video's own aspect ratio -- never the window's.
                scale = min(video_area_w / frame_w, video_area_h / frame_h)
                disp_w = max(1, round(frame_w * scale))
                disp_h = max(1, round(frame_h * scale))
                if (disp_w, disp_h) != (frame_w, frame_h):
                    surf = pygame.transform.smoothscale(surf, (disp_w, disp_h))
                offset_x = (video_area_w - disp_w) // 2
                offset_y = MENU_HEIGHT + (video_area_h - disp_h) // 2
                screen.blit(surf, (offset_x, offset_y))
                video_rect = pygame.Rect(offset_x, offset_y, disp_w, disp_h)
        menu.draw(screen)
        pygame.display.flip()

        link.send_keepalive_if_idle()

        while not link.incoming.empty():
            frame_type, payload = link.incoming.get_nowait()
            handle_incoming(frame_type, payload)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.VIDEORESIZE:
                new_w = max(MIN_WINDOW_WIDTH, event.w)
                new_h = max(MIN_WINDOW_HEIGHT, event.h)
                screen = pygame.display.set_mode((new_w, new_h), pygame.RESIZABLE)
                menu.resize(new_w)

            elif event.type == pygame.KEYDOWN:
                # Keyboard is always forwarded while this window has focus --
                # the OS only delivers KEYDOWN/KEYUP events here when it does.
                usage_id = usage_for_event(event)
                if usage_id is not None:
                    active_keys[event.key] = usage_id
                    link.send_key(True, usage_id)
                    if args.debug:
                        print(f"[KEYDOWN] usage=0x{usage_id:02X} ({describe_usage(usage_id)})")
                elif args.debug:
                    print(f"[KEYDOWN] unmapped pygame key={pygame.key.name(event.key)!r}, no usage sent")

            elif event.type == pygame.KEYUP:
                usage_id = active_keys.pop(event.key, None)
                if usage_id is not None:
                    link.send_key(False, usage_id)
                    if args.debug:
                        print(f"[KEYUP]   usage=0x{usage_id:02X} ({describe_usage(usage_id)})")

            elif event.type == pygame.MOUSEBUTTONDOWN:
                # Try the menu bar first for left-clicks -- it knows its own
                # extent, which isn't just the top strip: an OPEN dropdown's
                # items are drawn below it, reaching down into what would
                # otherwise be video-area territory. Routing purely by
                # "y < MENU_HEIGHT" (the old check here) missed that
                # entirely, so clicking a dropdown item sent the click
                # through to the target underneath the menu instead of
                # actually activating the item.
                if event.button == 1 and menu.handle_click(event.pos):
                    continue
                if event.pos[1] < MENU_HEIGHT or menu.open_index is not None:
                    # Our own UI space (the bar itself, or an open dropdown
                    # for a non-left click) -- never forward to the target.
                    continue
                button = {1: protocol.MOUSE_LEFT, 2: protocol.MOUSE_MIDDLE, 3: protocol.MOUSE_RIGHT}.get(event.button)
                if button is not None:
                    frac = map_click_to_target(event.pos, video_rect)
                    if frac is not None:
                        # Position and buttons travel together in ONE
                        # frame/report -- touchscreen-style, not a relative
                        # capture. Sending them as two separate writes (a
                        # move, then a distinct button-down) was a real,
                        # confirmed bug: unreliable clicks specifically
                        # when the position didn't need to change, since
                        # nothing about a real HID absolute pointer's
                        # design ever splits a position+button sample into
                        # two independently-timed reports.
                        held_target_buttons |= button
                        link.send_mouse_state(*frac, held_target_buttons)
                        if args.debug:
                            print(f"[MOUSE DOWN] button={button} at ({frac[0]:.3f}, {frac[1]:.3f})")

            elif event.type == pygame.MOUSEBUTTONUP:
                button = {1: protocol.MOUSE_LEFT, 2: protocol.MOUSE_MIDDLE, 3: protocol.MOUSE_RIGHT}.get(event.button)
                # Only forward if WE sent the matching down (started in the
                # video area) -- and forward it regardless of where the
                # cursor is now, so dragging off into the menu bar before
                # releasing can't leave a button stuck down on the target.
                if button is not None and (held_target_buttons & button):
                    held_target_buttons &= ~button
                    frac = map_click_to_target(event.pos, video_rect)
                    if frac is not None:
                        link.send_mouse_state(*frac, held_target_buttons)
                        if args.debug:
                            print(f"[MOUSE UP]   button={button}")

            elif event.type == pygame.MOUSEMOTION:
                # No hover on a touchscreen -- only forward motion while
                # dragging (a button we sent down is still held).
                if held_target_buttons:
                    frac = map_click_to_target(event.pos, video_rect)
                    if frac is not None:
                        link.send_mouse_state(*frac, held_target_buttons)
                        if args.debug:
                            print(f"[MOUSE DRAG] at ({frac[0]:.3f}, {frac[1]:.3f})")

            elif event.type == pygame.MOUSEWHEEL:
                pos = pygame.mouse.get_pos()
                if pos[1] >= MENU_HEIGHT:
                    frac = map_click_to_target(pos, video_rect)
                    if frac is not None:
                        link.send_mouse_state(*frac, held_target_buttons)
                    link.send_mouse_scroll(event.y)
                    if args.debug:
                        print(f"[MOUSE SCROLL] amount={event.y}")

        clock.tick(60)

    # release any keys still held down before exiting
    for usage_id in active_keys.values():
        link.send_key(False, usage_id)

    # Tell the Pi to end the shell session and close the terminal window's
    # process too -- otherwise a still-open bash process (and an orphaned
    # window) would sit there indefinitely after we exit.
    if terminal.is_open:
        terminal.close()

    video.release()
    link.close()
    pygame.quit()


if __name__ == "__main__":
    main()
