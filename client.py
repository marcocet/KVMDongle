"""
client.py

Displays the video feed from a USB capture device in a window, forwards
keyboard AND mouse input to a Raspberry Pi Zero W running pi/daemon.py
(which injects them as real USB keyboard/mouse input into the target
machine via its USB OTG port, and also exposes an ISO from its SD card
as read-only USB mass storage on request).

Keyboard input is always forwarded while the window has focus -- no
toggle needed, since the OS only delivers keyboard events to a focused
window anyway. Mouse input is only forwarded while capture mode is on
(toggle with F9), so you can use your real cursor to click the on-screen
menu bar the rest of the time.

Requirements:
    pip install pygame opencv-python pyserial pyperclip

Usage:
    python client.py --serial-port COM5 --capture-index 1

    --serial-port    the COM/tty port for the Pi's control link
                      (Windows: e.g. COM5, Linux/Mac: e.g. /dev/ttyUSB0)
    --capture-index  the OpenCV device index for your capture card
                      (try 0, 1, 2... if unsure; the app also prints
                      available indices on startup)
    --baud           serial baud rate, must match pi/daemon.py (default 460800)
    --debug          print each key/mouse event to the terminal

Controls:
    - Click into the video window to give it focus, then type normally.
      Keystrokes go to the target machine whenever this window is
      focused -- no toggle needed.
    - Press F9 to toggle mouse capture on/off (shown in the title bar
      and in the menu bar). While ON, your cursor is hidden and mouse
      movement/clicks/scroll are sent to the target as relative input.
      While OFF (the default), your real cursor is visible and free to
      use -- including clicking the menu bar below.
    - Use the menu bar at the top for common macro combos, clipboard
      paste, and mounting ISOs from the Pi's SD card, or use their
      hotkeys directly:
        F11             Paste clipboard text onto the target
        Ctrl+Shift+F1   Ctrl+Alt+Del
        Ctrl+Shift+F2   Alt+Tab
        Ctrl+Shift+F3   Alt+F4
        Ctrl+Shift+F4   Win+R
        Ctrl+Shift+F5   Win+D
    - Use the Storage menu to list ISOs already on the Pi's SD card,
      mount one (exposed to the target as a read-only CD-ROM), or eject
      the current one. The Pi is the source of truth for what's
      available -- add ISOs by swapping the SD card or via the Pi's
      Wi-Fi upload page.
    - Use Session > Quit in the menu bar, or the window's close button,
      to exit (there's no local keyboard shortcut for this, since every
      keystroke while focused is forwarded to the target).
"""

import argparse
import queue
import sys
import threading
import time

import cv2
import pygame
import serial

import protocol

try:
    import pyperclip
except ImportError:
    pyperclip = None

# Matches protocol.MOUSE_LEFT/RIGHT/MIDDLE
MOUSE_LEFT = protocol.MOUSE_LEFT
MOUSE_RIGHT = protocol.MOUSE_RIGHT
MOUSE_MIDDLE = protocol.MOUSE_MIDDLE

MENU_HEIGHT = 28

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


# Macro combos -- ordered list of (label, usage_ids, hotkey_description).
# Each press-sequence is sent in order, then released in reverse order.
# Available both from the menu bar and via Ctrl+Shift+F1..F5, which
# avoids colliding with plain F-keys you might need for a BIOS/POST screen.
MACROS = [
    ("Ctrl+Alt+Del", [protocol.USAGE_MOD_LEFT_CTRL, protocol.USAGE_MOD_LEFT_ALT, 0x4C], "Ctrl+Shift+F1"),
    ("Alt+Tab", [protocol.USAGE_MOD_LEFT_ALT, 0x2B], "Ctrl+Shift+F2"),
    ("Alt+F4", [protocol.USAGE_MOD_LEFT_ALT, 0x3D], "Ctrl+Shift+F3"),
    ("Win+R", [protocol.USAGE_MOD_LEFT_GUI, letter_usage("r")], "Ctrl+Shift+F4"),
    ("Win+D", [protocol.USAGE_MOD_LEFT_GUI, letter_usage("d")], "Ctrl+Shift+F5"),
]
MACRO_HOTKEYS = {
    pygame.K_F1: MACROS[0],
    pygame.K_F2: MACROS[1],
    pygame.K_F3: MACROS[2],
    pygame.K_F4: MACROS[3],
    pygame.K_F5: MACROS[4],
}


class SerialLink:
    """Owns the serial connection. Writes happen directly from the caller's
    thread; a background thread owns all reads and pushes parsed
    (type, payload) frames onto a queue for the main loop to drain -- this
    is the new bidirectional half of the old Arduino link, needed for ISO
    listing/mount/eject replies."""

    def __init__(self, port, baud):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        time.sleep(2)  # let the Pi's daemon finish starting after the port opens
        self.incoming = queue.Queue()
        self._parser = protocol.FrameParser()
        self._stop = threading.Event()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                n = self.ser.in_waiting
                data = self.ser.read(n if n else 1)
            except serial.SerialException as e:
                print(f"[serial error] {e}")
                return
            if not data:
                continue
            for frame in self._parser.feed(data):
                self.incoming.put(frame)

    def _write(self, frame_bytes):
        try:
            self.ser.write(frame_bytes)
        except serial.SerialException as e:
            print(f"[serial error] {e}")

    def send_key(self, down, usage_id):
        self._write(protocol.encode_key_event(down, usage_id))

    def send_mouse_move(self, dx, dy):
        self._write(protocol.encode_mouse_move(dx, dy))

    def send_mouse_button(self, down, button_mask):
        self._write(protocol.encode_mouse_button(down, button_mask))

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

    def close(self):
        self._stop.set()
        self._reader_thread.join(timeout=1)
        self.ser.close()


def find_capture_devices(max_index=5):
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            found.append(i)
            cap.release()
    return found


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


def set_mouse_capture(enabled):
    """Hide the local cursor and switch to relative mouse mode while
    capture is on, so movement generates continuous deltas instead of
    stopping dead at the edge of the window."""
    if hasattr(pygame.mouse, "set_relative_mode"):
        pygame.mouse.set_relative_mode(enabled)
    else:
        # Fallback for older pygame versions without relative mode support.
        pygame.mouse.set_visible(not enabled)
        pygame.event.set_grab(enabled)
    pygame.mouse.get_rel()  # discard any accumulated delta from the switch


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


class MenuBar:
    """A minimal top-of-window menu bar with click-to-open dropdowns.
    Only usable while mouse capture is off (i.e. your real cursor is
    visible and free), since mouse position is meaningless once capture
    mode takes over for driving the target.

    Each menu's items may be a static list of (label, hotkey, fn), or a
    zero-arg callable returning a fresh list -- used by the Storage menu,
    whose contents depend on the Pi's latest reply rather than being fixed
    at startup like Macros/Clipboard/Mouse/Session."""

    def __init__(self, width, menus, get_mouse_state):
        self.width = width
        self.font = pygame.font.SysFont(None, 20)
        self.bg = (40, 40, 40)
        self.fg = (230, 230, 230)
        self.highlight = (70, 70, 70)
        self.border = (90, 90, 90)
        self.open_index = None

        # menus: list of (label, items_or_callable, on_open_or_None)
        self.menus = menus
        self.get_mouse_state = get_mouse_state

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

    def draw(self, screen):
        screen.fill(self.bg, pygame.Rect(0, 0, self.width, MENU_HEIGHT))
        pygame.draw.line(screen, self.border, (0, MENU_HEIGHT - 1), (self.width, MENU_HEIGHT - 1))

        for i, (label, _items, _on_open) in enumerate(self.menus):
            rect = self.top_rects[i]
            if self.open_index == i:
                pygame.draw.rect(screen, self.highlight, rect)
            text = self.font.render(label, True, self.fg)
            screen.blit(text, (rect.x + 10, rect.y + (MENU_HEIGHT - text.get_height()) // 2))

        # Mouse capture state indicator, right-aligned
        state_text = f"Mouse Capture: {'ON' if self.get_mouse_state() else 'OFF'}"
        rendered = self.font.render(state_text, True, self.fg)
        screen.blit(rendered, (self.width - rendered.get_width() - 10,
                                (MENU_HEIGHT - rendered.get_height()) // 2))

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
                    fn()
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


def main():
    parser = argparse.ArgumentParser(description="Laptop crash cart: video + keyboard/mouse/storage bridge")
    parser.add_argument("--serial-port", required=True, help="COM port / tty for the Pi's control link")
    parser.add_argument("--capture-index", type=int, default=None, help="OpenCV capture device index")
    parser.add_argument("--baud", type=int, default=460800)
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
    cap = cv2.VideoCapture(capture_index)
    if not cap.isOpened():
        print(f"Could not open capture device at index {capture_index}.")
        sys.exit(1)

    print(f"Opening serial link on {args.serial_port} @ {args.baud} baud...")
    try:
        link = SerialLink(args.serial_port, args.baud)
    except serial.SerialException as e:
        print(f"Could not open serial port: {e}")
        sys.exit(1)

    pygame.init()
    ret, frame = cap.read()
    if not ret:
        print("Could not read a frame from the capture device.")
        sys.exit(1)
    h, w = frame.shape[:2]
    screen = pygame.display.set_mode((w, h + MENU_HEIGHT))

    mouse_capture = False
    set_mouse_capture(False)
    storage = StorageState()
    network = NetworkState()

    running = True

    def do_macro(usage_ids, label):
        send_macro(link, usage_ids, args.debug, label)

    def do_paste():
        send_clipboard_text(link, args.debug)

    def do_toggle_mouse():
        nonlocal mouse_capture
        mouse_capture = not mouse_capture
        set_mouse_capture(mouse_capture)
        set_title()

    def do_quit():
        nonlocal running
        running = False

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
        ("Macros", [(label, hotkey, lambda usage_ids=usage_ids, label=label: do_macro(usage_ids, label))
                    for (label, usage_ids, hotkey) in MACROS], None),
        ("Clipboard", [("Paste Clipboard", "F11", do_paste)], None),
        ("Mouse", [("Toggle Mouse Capture", "F9", do_toggle_mouse)], None),
        ("Storage", storage_menu_items, do_refresh_isos),
        ("Network", network_menu_items, do_query_ap_status),
        ("Session", [("Quit", "", do_quit)], None),
    ]
    menu = MenuBar(w, menus, lambda: mouse_capture)

    def set_title():
        state = "ON" if mouse_capture else "OFF"
        pygame.display.set_caption(f"Crash Cart - keyboard always live - Mouse Capture [{state}] (F9)")

    set_title()

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
        if args.debug and frame_type not in (p.ISO_LIST, p.ISO_MOUNTED, p.ISO_EJECTED, p.AP_STATUS, p.ERROR):
            print(f"[from pi] type=0x{frame_type:02X} payload={payload!r}")

    # Track which pygame keys are currently "down" and what usage ID was
    # sent, so KEYUP releases the correct code.
    active_keys = {}

    clock = pygame.time.Clock()

    while running:
        ret, frame = cap.read()
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            surf = pygame.image.frombuffer(frame.tobytes(), (frame.shape[1], frame.shape[0]), "RGB")
            screen.blit(surf, (0, MENU_HEIGHT))
        menu.draw(screen)
        pygame.display.flip()

        while not link.incoming.empty():
            frame_type, payload = link.incoming.get_nowait()
            handle_incoming(frame_type, payload)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_F9:
                    do_toggle_mouse()
                    continue

                if event.key == pygame.K_F11:
                    do_paste()
                    continue

                if (event.mod & pygame.KMOD_CTRL) and (event.mod & pygame.KMOD_SHIFT) and event.key in MACRO_HOTKEYS:
                    label, usage_ids, _hotkey = MACRO_HOTKEYS[event.key]
                    send_macro(link, usage_ids, args.debug, label)
                    continue

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
                if not mouse_capture:
                    if event.button == 1:
                        menu.handle_click(event.pos)
                    continue
                button = {1: MOUSE_LEFT, 2: MOUSE_MIDDLE, 3: MOUSE_RIGHT}.get(event.button)
                if button is not None:
                    link.send_mouse_button(True, button)
                    if args.debug:
                        print(f"[MOUSE DOWN] button={button}")

            elif event.type == pygame.MOUSEBUTTONUP:
                if not mouse_capture:
                    continue
                button = {1: MOUSE_LEFT, 2: MOUSE_MIDDLE, 3: MOUSE_RIGHT}.get(event.button)
                if button is not None:
                    link.send_mouse_button(False, button)
                    if args.debug:
                        print(f"[MOUSE UP]   button={button}")

            elif event.type == pygame.MOUSEMOTION:
                if not mouse_capture:
                    continue
                dx, dy = event.rel
                if dx or dy:
                    link.send_mouse_move(dx, dy)
                    if args.debug:
                        print(f"[MOUSE MOVE] dx={dx} dy={dy}")

            elif event.type == pygame.MOUSEWHEEL:
                if not mouse_capture:
                    continue
                link.send_mouse_scroll(event.y)
                if args.debug:
                    print(f"[MOUSE SCROLL] amount={event.y}")

        clock.tick(60)

    # release any keys still held down before exiting
    for usage_id in active_keys.values():
        link.send_key(False, usage_id)

    set_mouse_capture(False)
    cap.release()
    link.close()
    pygame.quit()


if __name__ == "__main__":
    main()
