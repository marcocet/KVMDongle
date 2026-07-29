"""
crash_cart.py

Displays the video feed from a USB capture device in a window, forwards
keyboard AND mouse input to an Arduino running arduino_hid_bridge.ino
(which injects them as real USB keyboard/mouse input into the target
machine).

Keyboard input is always forwarded while the window has focus -- no
toggle needed, since the OS only delivers keyboard events to a focused
window anyway. Mouse input is only forwarded while capture mode is on
(toggle with F9), so you can use your real cursor to click the on-screen
menu bar the rest of the time.

Requirements:
    pip install pygame opencv-python pyserial pyperclip

Usage:
    python crash_cart.py --serial-port COM5 --capture-index 1

    --serial-port    the COM/tty port for the Arduino control link
                      (Windows: e.g. COM5, Linux/Mac: e.g. /dev/ttyUSB0)
    --capture-index  the OpenCV device index for your capture card
                      (try 0, 1, 2... if unsure; the app also prints
                      available indices on startup)
    --baud           serial baud rate, must match the Arduino sketch
                      (default 115200)
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
    - Use the menu bar at the top for common macro combos and clipboard
      paste, or use their hotkeys directly:
        F11             Paste clipboard text onto the target
        Ctrl+Shift+F1   Ctrl+Alt+Del
        Ctrl+Shift+F2   Alt+Tab
        Ctrl+Shift+F3   Alt+F4
        Ctrl+Shift+F4   Win+R
        Ctrl+Shift+F5   Win+D
    - Use Session > Quit in the menu bar, or the window's close button,
      to exit (there's no local keyboard shortcut for this, since every
      keystroke while focused is forwarded to the target).
"""

import argparse
import struct
import sys
import time

import cv2
import pygame
import serial

try:
    import pyperclip
except ImportError:
    pyperclip = None

SYNC_BYTE = 0xAA
EVT_KEYDOWN = 0x01
EVT_KEYUP = 0x02
EVT_MOUSE_MOVE = 0x03
EVT_MOUSE_DOWN = 0x04
EVT_MOUSE_UP = 0x05
EVT_MOUSE_SCROLL = 0x06

# Matches Arduino Mouse.h button constants
MOUSE_LEFT = 1
MOUSE_RIGHT = 2
MOUSE_MIDDLE = 4

# Arduino Keyboard.h special key constants
KEY_LEFT_CTRL = 0x80
KEY_LEFT_SHIFT = 0x81
KEY_LEFT_ALT = 0x82
KEY_LEFT_GUI = 0x83
KEY_RIGHT_CTRL = 0x84
KEY_RIGHT_SHIFT = 0x85
KEY_RIGHT_ALT = 0x86
KEY_RIGHT_GUI = 0x87
KEY_UP_ARROW = 0xDA
KEY_DOWN_ARROW = 0xD9
KEY_LEFT_ARROW = 0xD8
KEY_RIGHT_ARROW = 0xD7
KEY_BACKSPACE = 0xB2
KEY_TAB = 0xB3
KEY_RETURN = 0xB0
KEY_ESC = 0xB1
KEY_INSERT = 0xD1
KEY_DELETE = 0xD4
KEY_PAGE_UP = 0xD3
KEY_PAGE_DOWN = 0xD6
KEY_HOME = 0xD2
KEY_END = 0xD5
KEY_CAPS_LOCK = 0xC1
KEY_F1 = 0xC2
# F1..F12 are sequential from 0xC2

MENU_HEIGHT = 28

# Map pygame key constants -> Arduino key codes (non-printable / special keys)
SPECIAL_KEYS = {
    pygame.K_LCTRL: KEY_LEFT_CTRL,
    pygame.K_RCTRL: KEY_RIGHT_CTRL,
    pygame.K_LSHIFT: KEY_LEFT_SHIFT,
    pygame.K_RSHIFT: KEY_RIGHT_SHIFT,
    pygame.K_LALT: KEY_LEFT_ALT,
    pygame.K_RALT: KEY_RIGHT_ALT,
    pygame.K_LGUI: KEY_LEFT_GUI,
    pygame.K_RGUI: KEY_RIGHT_GUI,
    pygame.K_UP: KEY_UP_ARROW,
    pygame.K_DOWN: KEY_DOWN_ARROW,
    pygame.K_LEFT: KEY_LEFT_ARROW,
    pygame.K_RIGHT: KEY_RIGHT_ARROW,
    pygame.K_BACKSPACE: KEY_BACKSPACE,
    pygame.K_TAB: KEY_TAB,
    pygame.K_RETURN: KEY_RETURN,
    pygame.K_KP_ENTER: KEY_RETURN,
    pygame.K_ESCAPE: KEY_ESC,
    pygame.K_INSERT: KEY_INSERT,
    pygame.K_DELETE: KEY_DELETE,
    pygame.K_PAGEUP: KEY_PAGE_UP,
    pygame.K_PAGEDOWN: KEY_PAGE_DOWN,
    pygame.K_HOME: KEY_HOME,
    pygame.K_END: KEY_END,
    pygame.K_CAPSLOCK: KEY_CAPS_LOCK,
}
for i in range(12):
    SPECIAL_KEYS[getattr(pygame, f"K_F{i + 1}")] = KEY_F1 + i

# Macro combos -- ordered list of (label, codes, hotkey_description).
# Each press-sequence is sent in order, then released in reverse order.
# Available both from the menu bar and via Ctrl+Shift+F1..F5, which
# avoids colliding with plain F-keys you might need for a BIOS/POST screen.
MACROS = [
    ("Ctrl+Alt+Del", [KEY_LEFT_CTRL, KEY_LEFT_ALT, KEY_DELETE], "Ctrl+Shift+F1"),
    ("Alt+Tab", [KEY_LEFT_ALT, KEY_TAB], "Ctrl+Shift+F2"),
    ("Alt+F4", [KEY_LEFT_ALT, KEY_F1 + 3], "Ctrl+Shift+F3"),
    ("Win+R", [KEY_LEFT_GUI, ord("r")], "Ctrl+Shift+F4"),
    ("Win+D", [KEY_LEFT_GUI, ord("d")], "Ctrl+Shift+F5"),
]
MACRO_HOTKEYS = {
    pygame.K_F1: MACROS[0],
    pygame.K_F2: MACROS[1],
    pygame.K_F3: MACROS[2],
    pygame.K_F4: MACROS[3],
    pygame.K_F5: MACROS[4],
}

_SPECIAL_NAMES = {v: k for k, v in {
    KEY_LEFT_CTRL: "LEFT_CTRL", KEY_RIGHT_CTRL: "RIGHT_CTRL",
    KEY_LEFT_SHIFT: "LEFT_SHIFT", KEY_RIGHT_SHIFT: "RIGHT_SHIFT",
    KEY_LEFT_ALT: "LEFT_ALT", KEY_RIGHT_ALT: "RIGHT_ALT",
    KEY_LEFT_GUI: "LEFT_GUI", KEY_RIGHT_GUI: "RIGHT_GUI",
    KEY_UP_ARROW: "UP", KEY_DOWN_ARROW: "DOWN",
    KEY_LEFT_ARROW: "LEFT", KEY_RIGHT_ARROW: "RIGHT",
    KEY_BACKSPACE: "BACKSPACE", KEY_TAB: "TAB", KEY_RETURN: "RETURN",
    KEY_ESC: "ESC", KEY_INSERT: "INSERT", KEY_DELETE: "DELETE",
    KEY_PAGE_UP: "PAGE_UP", KEY_PAGE_DOWN: "PAGE_DOWN",
    KEY_HOME: "HOME", KEY_END: "END", KEY_CAPS_LOCK: "CAPS_LOCK",
    **{KEY_F1 + i: f"F{i + 1}" for i in range(12)},
}.items()}


def describe_code(code):
    """Human-readable label for a key code, for debug output."""
    if code in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[code]
    if 32 <= code < 127:
        return f"'{chr(code)}'"
    return "unknown"


class SerialLink:
    def __init__(self, port, baud):
        self.ser = serial.Serial(port, baud, timeout=0)
        time.sleep(2)  # let the board reset after the port opens

    def _write(self, frame_bytes):
        try:
            self.ser.write(bytes(frame_bytes))
        except serial.SerialException as e:
            print(f"[serial error] {e}")

    def send_key(self, event_type, code):
        self._write([SYNC_BYTE, event_type, code & 0xFF])

    def send_mouse_move(self, dx, dy):
        dx = max(-127, min(127, int(dx)))
        dy = max(-127, min(127, int(dy)))
        self._write([SYNC_BYTE, EVT_MOUSE_MOVE, struct.pack("b", dx)[0], struct.pack("b", dy)[0]])

    def send_mouse_button(self, event_type, button):
        self._write([SYNC_BYTE, event_type, button & 0xFF])

    def send_mouse_scroll(self, amount):
        amount = max(-127, min(127, int(amount)))
        self._write([SYNC_BYTE, EVT_MOUSE_SCROLL, struct.pack("b", amount)[0]])

    def close(self):
        self.ser.close()


def find_capture_devices(max_index=5):
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            found.append(i)
            cap.release()
    return found


def keycode_for_event(event):
    """Return the Arduino key code to send for this pygame KEYDOWN event,
    or None if we don't have a mapping for it."""
    if event.key in SPECIAL_KEYS:
        return SPECIAL_KEYS[event.key]
    if event.unicode and 32 <= ord(event.unicode) < 127:
        return ord(event.unicode)
    return None


def send_macro(link, codes, debug, label):
    """Press a sequence of codes in order, then release in reverse order,
    with a short delay between each step so the target reliably registers
    every key in the combo."""
    if debug:
        print(f"[MACRO] sending {label}")
    for code in codes:
        link.send_key(EVT_KEYDOWN, code)
        time.sleep(0.03)
    for code in reversed(codes):
        link.send_key(EVT_KEYUP, code)
        time.sleep(0.03)


def send_clipboard_text(link, debug):
    """Read the local clipboard and type it out to the target, char by
    char. Runs synchronously, so the video feed will pause briefly for
    long text."""
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
            code = KEY_RETURN
        elif ch == "\t":
            code = KEY_TAB
        elif 32 <= ord(ch) < 127:
            code = ord(ch)
        else:
            if debug:
                print(f"[paste] skipping unsupported character {ch!r}")
            continue

        link.send_key(EVT_KEYDOWN, code)
        time.sleep(0.008)
        link.send_key(EVT_KEYUP, code)
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


class MenuBar:
    """A minimal top-of-window menu bar with click-to-open dropdowns.
    Only usable while mouse capture is off (i.e. your real cursor is
    visible and free), since mouse position is meaningless once capture
    mode takes over for driving the target."""

    def __init__(self, width, on_macro, on_paste, on_toggle_mouse, on_quit, get_mouse_state):
        self.width = width
        self.font = pygame.font.SysFont(None, 20)
        self.bg = (40, 40, 40)
        self.fg = (230, 230, 230)
        self.highlight = (70, 70, 70)
        self.border = (90, 90, 90)
        self.open_index = None

        self.menus = [
            ("Macros", [(label, hotkey, lambda codes=codes, label=label: on_macro(codes, label))
                        for (label, codes, hotkey) in MACROS]),
            ("Clipboard", [("Paste Clipboard", "F11", on_paste)]),
            ("Mouse", [("Toggle Mouse Capture", "F9", on_toggle_mouse)]),
            ("Session", [("Quit", "", on_quit)]),
        ]
        self.get_mouse_state = get_mouse_state

        # Precompute top-level button rects
        self.top_rects = []
        x = 8
        for label, _items in self.menus:
            w = self.font.size(label)[0] + 20
            self.top_rects.append(pygame.Rect(x, 0, w, MENU_HEIGHT))
            x += w

    def draw(self, screen):
        screen.fill(self.bg, pygame.Rect(0, 0, self.width, MENU_HEIGHT))
        pygame.draw.line(screen, self.border, (0, MENU_HEIGHT - 1), (self.width, MENU_HEIGHT - 1))

        for i, (label, _items) in enumerate(self.menus):
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
        _label, items = self.menus[index]
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
        _label, items = self.menus[index]
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
            _label, items = self.menus[self.open_index]
            for rect, (_lbl, _hk, fn) in zip(rects, items):
                if rect.collidepoint(x, y):
                    fn()
                    self.open_index = None
                    return True

        for i, rect in enumerate(self.top_rects):
            if rect.collidepoint(x, y):
                self.open_index = None if self.open_index == i else i
                return True

        # Clicked elsewhere: close any open menu, consume the click only
        # if a menu was actually open (so a plain click in the video area
        # doesn't get silently swallowed).
        was_open = self.open_index is not None
        self.open_index = None
        return was_open


def main():
    parser = argparse.ArgumentParser(description="Laptop crash cart: video + keyboard/mouse injection")
    parser.add_argument("--serial-port", required=True, help="COM port / tty for the Arduino")
    parser.add_argument("--capture-index", type=int, default=None, help="OpenCV capture device index")
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

    running = True

    def do_macro(codes, label):
        send_macro(link, codes, args.debug, label)

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

    menu = MenuBar(w, do_macro, do_paste, do_toggle_mouse, do_quit, lambda: mouse_capture)

    def set_title():
        state = "ON" if mouse_capture else "OFF"
        pygame.display.set_caption(f"Crash Cart - keyboard always live - Mouse Capture [{state}] (F9)")

    set_title()

    # Track which pygame keys are currently "down" and what code was sent,
    # so KEYUP releases the correct code even without unicode info.
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
                    label, codes, _hotkey = MACRO_HOTKEYS[event.key]
                    send_macro(link, codes, args.debug, label)
                    continue

                # Keyboard is always forwarded while this window has focus --
                # the OS only delivers KEYDOWN/KEYUP events here when it does.
                code = keycode_for_event(event)
                if code is not None:
                    active_keys[event.key] = code
                    link.send_key(EVT_KEYDOWN, code)
                    if args.debug:
                        print(f"[KEYDOWN] code=0x{code:02X} ({describe_code(code)})")
                elif args.debug:
                    print(f"[KEYDOWN] unmapped pygame key={pygame.key.name(event.key)!r}, no code sent")

            elif event.type == pygame.KEYUP:
                code = active_keys.pop(event.key, None)
                if code is not None:
                    link.send_key(EVT_KEYUP, code)
                    if args.debug:
                        print(f"[KEYUP]   code=0x{code:02X} ({describe_code(code)})")

            elif event.type == pygame.MOUSEBUTTONDOWN:
                if not mouse_capture:
                    if event.button == 1:
                        menu.handle_click(event.pos)
                    continue
                button = {1: MOUSE_LEFT, 2: MOUSE_MIDDLE, 3: MOUSE_RIGHT}.get(event.button)
                if button is not None:
                    link.send_mouse_button(EVT_MOUSE_DOWN, button)
                    if args.debug:
                        print(f"[MOUSE DOWN] button={button}")

            elif event.type == pygame.MOUSEBUTTONUP:
                if not mouse_capture:
                    continue
                button = {1: MOUSE_LEFT, 2: MOUSE_MIDDLE, 3: MOUSE_RIGHT}.get(event.button)
                if button is not None:
                    link.send_mouse_button(EVT_MOUSE_UP, button)
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
    for code in active_keys.values():
        link.send_key(EVT_KEYUP, code)

    set_mouse_capture(False)
    cap.release()
    link.close()
    pygame.quit()


if __name__ == "__main__":
    main()