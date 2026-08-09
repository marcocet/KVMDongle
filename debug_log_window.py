"""
debug_log_window.py

Standalone helper process for client.py's "Debug Log" feature: a real,
separate OS window that displays everything client.py prints to its own
stdout/stderr. Spawned by client.py as a child process (see
DebugLogWindow in client.py), for the same reason as the Pi-shell
terminal -- pygame/SDL only supports one window per process. Much
simpler than that one, though: purely one-directional (client.py sends
lines, this process never sends anything back), no interactivity beyond
scrolling.

This exists because a packaged, windowed standalone build (a Windows EXE
built without a console, a macOS .app bundle, ...) has no visible
terminal at all -- every print() client.py makes would otherwise go
nowhere anyone could ever see, including the errors you'd most want to
see, since there's no console window for the OS to attach in the first
place.

NEVER run this file directly -- it expects its stdin connected to a pipe
by client.py and does nothing useful stand-alone. Reads newline-delimited
UTF-8 text lines from stdin and renders them in a scrolling monospace
view; exits when that pipe closes (client.py exited).
"""

import os
import queue
import sys
import threading

import pygame

MARGIN = 8
MAX_LINES = 5000
MIN_WINDOW_WIDTH = 300
MIN_WINDOW_HEIGHT = 150


def line_color(line):
    """Cheap, tag-based color-coding so errors/warnings jump out while
    scanning a long log -- matches this project's own [tag]-prefixed
    print() convention and daemon.py's [LEVEL] logging format closely
    enough to catch both without needing to parse either precisely."""
    lower = line.lower()
    if "error" in lower or "traceback" in lower or "exception" in lower:
        return (240, 90, 90)
    if "warning" in lower:
        return (230, 190, 90)
    return (220, 220, 220)


def _read_lines(incoming):
    """Runs in a background thread: parses newline-delimited text off
    stdin (raw bytes relayed by client.py) and pushes complete lines for
    the main loop to drain. Pushes a None sentinel on EOF (client.py
    exited or closed the pipe), so this window closes itself rather than
    lingering as an orphan with nothing left feeding it."""
    stdin_fd = sys.stdin.fileno()
    buf = b""
    while True:
        try:
            chunk = os.read(stdin_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            raw_line, buf = buf.split(b"\n", 1)
            incoming.put(raw_line.decode("utf-8", errors="replace"))
    incoming.put(None)


def main():
    pygame.init()
    pygame.display.set_caption("Debug Log")
    font = pygame.font.SysFont("consolas,couriernew,monospace", 14)
    char_h = font.get_height()

    window_w, window_h = 900, 500
    screen = pygame.display.set_mode((window_w, window_h), pygame.RESIZABLE)

    lines = []
    # Lines scrolled up from the bottom; 0 means pinned to the bottom
    # (auto-scroll as new lines arrive), matching how a real console/tail
    # behaves -- only stops following once the user deliberately scrolls
    # up to read something older.
    scroll = 0

    incoming = queue.Queue()
    threading.Thread(target=_read_lines, args=(incoming,), daemon=True).start()

    clock = pygame.time.Clock()
    running = True
    while running:
        while True:
            try:
                item = incoming.get_nowait()
            except queue.Empty:
                break
            if item is None:
                running = False
                break
            lines.append(item)
            if len(lines) > MAX_LINES:
                del lines[:len(lines) - MAX_LINES]

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.VIDEORESIZE:
                window_w = max(MIN_WINDOW_WIDTH, event.w)
                window_h = max(MIN_WINDOW_HEIGHT, event.h)
                screen = pygame.display.set_mode((window_w, window_h), pygame.RESIZABLE)
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
            elif event.type == pygame.MOUSEWHEEL:
                visible_rows = window_h // char_h
                max_scroll = max(0, len(lines) - visible_rows)
                scroll = min(max_scroll, max(0, scroll + event.y))

        screen.fill((12, 12, 12))
        visible_rows = window_h // char_h
        start = max(0, len(lines) - visible_rows - scroll)
        end = start + visible_rows
        for row, text in enumerate(lines[start:end]):
            surf = font.render(text, True, line_color(text))
            screen.blit(surf, (MARGIN, row * char_h))
        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    main()
