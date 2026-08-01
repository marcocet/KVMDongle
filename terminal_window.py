"""
terminal_window.py

Standalone helper process for client.py's "Terminal" feature: a real,
separate OS window (own taskbar entry, independently movable/resizable/
closable) running a pyte-based ANSI/VT100 renderer for the Pi's bash
shell. Spawned by client.py as a child process (see TerminalWindow in
client.py) rather than drawn as an overlay on top of the video window,
because pygame/SDL only supports one window per process -- a genuinely
separate window needs its own process.

NEVER run this file directly -- it expects its stdin/stdout connected to
pipes by client.py and does nothing useful stand-alone. It talks to
client.py using the exact same wire framing as the real laptop<->Pi
serial link (protocol.py's encode()/FrameParser):

    stdin  (client.py -> here):  SHELL_OUTPUT (Pi's shell output, relayed
                                  unchanged), SHELL_CLOSED (the Pi's bash
                                  session ended on its own, e.g. `exit`)
    stdout (here -> client.py):  SHELL_INPUT (keystrokes), SHELL_RESIZE
                                  (window resized), SHELL_CLOSE (user
                                  closed this window / pressed F12)

client.py relays these frame types unchanged between this pipe and the
real serial link -- this process never touches a serial port and knows
nothing about the Pi beyond the bytes client.py hands it.
"""

import os
import sys
import threading
import queue

import pygame
import pyte

import protocol

MARGIN = 16
MIN_COLS = 20
MIN_ROWS = 5

# ANSI 16-color palette (pyte's color names -> RGB). Note pyte names color 3
# (and its bright variant) "brown"/"brightbrown", not "yellow" -- confirmed
# directly against pyte rather than assumed, since xterm's historical naming
# is easy to get wrong from memory. Anything outside this table (256-color
# indices, truecolor) falls back to the default fg/bg -- covers ordinary
# shell usage (ls colors, colored prompts, systemctl status, etc) without
# taking on a much bigger color model.
ANSI_COLORS = {
    "black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121),
    "brown": (229, 229, 16), "blue": (36, 114, 200), "magenta": (188, 63, 188),
    "cyan": (17, 168, 205), "white": (229, 229, 229),
    "brightblack": (102, 102, 102), "brightred": (241, 76, 76),
    "brightgreen": (35, 209, 139), "brightbrown": (245, 245, 67),
    "brightblue": (59, 142, 234), "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219), "brightwhite": (255, 255, 255),
}
DEFAULT_FG = (220, 220, 220)
DEFAULT_BG = (12, 12, 12)

# Translates special keys into the byte sequences a real terminal (and thus
# bash/readline) expects -- xterm/VT100 conventions, not HID usage IDs
# (this is emulating normal local typing into a shell, not USB input).
KEY_SEQUENCES = {
    pygame.K_UP: b"\x1b[A",
    pygame.K_DOWN: b"\x1b[B",
    pygame.K_RIGHT: b"\x1b[C",
    pygame.K_LEFT: b"\x1b[D",
    pygame.K_HOME: b"\x1b[H",
    pygame.K_END: b"\x1b[F",
    pygame.K_DELETE: b"\x1b[3~",
    pygame.K_PAGEUP: b"\x1b[5~",
    pygame.K_PAGEDOWN: b"\x1b[6~",
    pygame.K_BACKSPACE: b"\x7f",
    pygame.K_TAB: b"\t",
    pygame.K_RETURN: b"\r",
    pygame.K_KP_ENTER: b"\r",
    pygame.K_ESCAPE: b"\x1b",
}


def fit(window_width, window_height, char_w, char_h):
    """How many character columns/rows fit in the given window size,
    leaving room for the margin on every side."""
    available_w = window_width - 2 * MARGIN
    available_h = window_height - 2 * MARGIN
    cols = max(MIN_COLS, available_w // char_w)
    rows = max(MIN_ROWS, available_h // char_h)
    return cols, rows


def translate_key(event):
    """Returns the bytes to send to the shell for this KEYDOWN event. Uses
    event.unicode (the actual typed character) rather than an HID usage-ID
    mapping -- this is emulating normal local terminal typing (readline,
    bash), not USB HID input, so the actual typed character is what's
    wanted."""
    ctrl = bool(event.mod & pygame.KMOD_CTRL)
    if ctrl and pygame.K_a <= event.key <= pygame.K_z:
        return bytes([event.key - pygame.K_a + 1])  # Ctrl+A=0x01 .. Ctrl+Z=0x1A
    if event.key in KEY_SEQUENCES:
        return KEY_SEQUENCES[event.key]
    if event.unicode:
        return event.unicode.encode("utf-8", errors="ignore")
    return b""


def draw(surface, vt_screen, cols, rows, char_w, char_h, font, window_width, window_height):
    x0 = max(MARGIN, (window_width - cols * char_w) // 2)
    y0 = max(MARGIN, (window_height - rows * char_h) // 2)

    surface.fill(DEFAULT_BG)

    for row in range(rows):
        line = vt_screen.buffer[row]
        for col in range(cols):
            char = line[col]
            cx, cy = x0 + col * char_w, y0 + row * char_h
            if char.bg != "default":
                bg = ANSI_COLORS.get(char.bg)
                if bg is not None:
                    pygame.draw.rect(surface, bg, (cx, cy, char_w, char_h))
            if char.data and char.data != " ":
                fg = ANSI_COLORS.get(char.fg, DEFAULT_FG)
                glyph = font.render(char.data, True, fg)
                surface.blit(glyph, (cx, cy))

    cursor = vt_screen.cursor
    if not cursor.hidden and 0 <= cursor.x < cols and 0 <= cursor.y < rows:
        cx, cy = x0 + cursor.x * char_w, y0 + cursor.y * char_h
        pygame.draw.rect(surface, (200, 200, 200), (cx, cy, char_w, char_h), 1)


def draw_closed_banner(surface, font, window_width, window_height):
    """Drawn on top once the Pi's bash session has ended, so this looks
    like a real terminal emulator noticing its process exited rather than
    the window just silently going stale."""
    text = font.render("[session closed -- press any key or close this window]", True, (0, 0, 0))
    bar_h = text.get_height() + 8
    pygame.draw.rect(surface, (230, 200, 60), (0, window_height - bar_h, window_width, bar_h))
    surface.blit(text, (8, window_height - bar_h + 4))


def _read_from_parent(incoming):
    """Runs in a background thread: parses SHELL_* frames off stdin (raw
    wire bytes relayed by client.py) and pushes (type, payload) tuples for
    the main loop to drain. Pushes a None sentinel when client.py's end of
    the pipe closes -- e.g. the whole app quit without an explicit close,
    which should still end this window rather than leaving it orphaned."""
    parser = protocol.FrameParser()
    stdin_fd = sys.stdin.fileno()
    while True:
        try:
            chunk = os.read(stdin_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        for item in parser.feed(chunk):
            incoming.put(item)
    incoming.put(None)


def _send_frame(wire_bytes):
    try:
        sys.stdout.buffer.write(wire_bytes)
        sys.stdout.buffer.flush()
        return True
    except OSError:
        return False


def main():
    pygame.init()
    pygame.display.set_caption("Pi Shell")

    font = pygame.font.SysFont("consolas,couriernew,monospace", 16)
    char_w, char_h = font.size("M")
    cols, rows = 80, 24
    min_window_w = MIN_COLS * char_w + 2 * MARGIN
    min_window_h = MIN_ROWS * char_h + 2 * MARGIN
    window_w = cols * char_w + 2 * MARGIN
    window_h = rows * char_h + 2 * MARGIN
    screen = pygame.display.set_mode((window_w, window_h), pygame.RESIZABLE)

    vt_screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(vt_screen)

    incoming = queue.Queue()
    threading.Thread(target=_read_from_parent, args=(incoming,), daemon=True).start()
    _send_frame(protocol.encode_shell_resize(rows, cols))

    running = True
    session_closed = False
    clock = pygame.time.Clock()

    while running:
        while True:
            try:
                item = incoming.get_nowait()
            except queue.Empty:
                break
            if item is None:
                running = False
                break
            frame_type, payload = item
            if frame_type == protocol.SHELL_OUTPUT:
                stream.feed(payload.decode("utf-8", errors="replace"))
            elif frame_type == protocol.SHELL_CLOSED:
                session_closed = True

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.VIDEORESIZE:
                new_w = max(min_window_w, event.w)
                new_h = max(min_window_h, event.h)
                screen = pygame.display.set_mode((new_w, new_h), pygame.RESIZABLE)
                new_cols, new_rows = fit(new_w, new_h, char_w, char_h)
                if (new_cols, new_rows) != (cols, rows):
                    cols, rows = new_cols, new_rows
                    vt_screen.resize(rows, cols)
                    if not _send_frame(protocol.encode_shell_resize(rows, cols)):
                        running = False
            elif event.type == pygame.KEYDOWN:
                if session_closed or event.key == pygame.K_F12:
                    running = False
                    continue
                data = translate_key(event)
                if data and not _send_frame(protocol.encode_shell_input(data)):
                    running = False

        draw(screen, vt_screen, cols, rows, char_w, char_h, font, screen.get_width(), screen.get_height())
        if session_closed:
            draw_closed_banner(screen, font, screen.get_width(), screen.get_height())
        pygame.display.flip()
        clock.tick(60)

    if not session_closed:
        _send_frame(protocol.encode_shell_close())
    pygame.quit()


if __name__ == "__main__":
    main()
