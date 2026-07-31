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
    at the requested ISO, replying with ISO_LIST/ISO_MOUNTED/ISO_EJECTED/
    ERROR as appropriate
  - handles AP_ENABLE / AP_DISABLE / AP_STATUS_QUERY by shelling out to
    wifi-ap-toggle.sh (phase 2, only present if install-webui.sh has been
    run), replying with the resulting AP_STATUS or an ERROR.
  - handles SHELL_OPEN / SHELL_CLOSE / SHELL_INPUT / SHELL_RESIZE by
    running an actual bash session in a real PTY (see ShellSession),
    streaming its output back as SHELL_OUTPUT frames -- a genuine shell,
    not a canned command runner, so the client can render it like a
    normal terminal (ls colors, tab completion, nano, top, all of it).

Single-threaded for everything except the AP commands: the Pi Zero W is
single-core, and HID writes/directory scans/LUN swaps are fast enough
that a plain read-parse-handle loop introduces no meaningful latency. The
AP commands are the one exception, and they run in a background thread,
not inline -- subprocess.run(timeout=...) only kills the *direct* child
on timeout, not any grandchild it may have spawned (nmcli, hostapd,
etc.), so if one of those is left holding the captured stdout/stderr
pipes open, the read blocks forever regardless of the nominal timeout.
Running it inline would freeze keyboard/mouse (and everything else) along
with it until the daemon was restarted -- which is exactly the bug this
was fixed after hitting. WifiApController.set_enabled() also now kills
the whole process group on timeout, not just the immediate child, so a
hang gets cleaned up instead of merely being isolated from the main loop.

The /dev/hidg* writes themselves are also non-blocking (O_NONBLOCK), for
the same reason: write() to a HID gadget character device can block if
the USB host isn't promptly polling/consuming that endpoint's queue. A
modern OS's HID stack does this reliably, but a BIOS/UEFI's minimal USB
stack may not (confirmed: mouse input froze the whole daemon, keyboard
included, specifically while the target was in its BIOS, and was fine
once it booted into Windows) -- with a blocking fd, that single stalled
write would freeze the entire single-threaded loop exactly like the AP
commands used to. A dropped report under those conditions just means a
skipped mouse update, not a frozen daemon.
"""

import json
import logging
import os
import signal
import struct
import subprocess
import sys
import threading
import time

import serial

# POSIX-only, needed only by ShellSession -- imported defensively (rather
# than left to fail at the top of the file) so daemon.py still imports
# cleanly in non-Linux dev/test environments, where these simply aren't
# available. Real Pi deployments are always Linux, so this is never
# actually None there. Module-level (not a deferred import inside
# ShellSession's methods) specifically so tests can monkeypatch e.g.
# `daemon.pty` the same way they already do `daemon.subprocess`.
try:
    import fcntl
    import pty
    import termios
except ImportError:
    fcntl = None
    pty = None
    termios = None

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
    def __init__(self, fd, path):
        # fd is opened once, for the daemon's whole lifetime, with
        # buffering=0 (raw, unbuffered I/O) -- opening the HID gadget
        # character device is not cheap (the kernel function driver does
        # real setup work on every open()), and re-opening it for every
        # single report was adding significant, very noticeable latency to
        # mouse movement in particular, since a fast mouse flick can emit
        # several reports per event. path is kept only for logging.
        self.fd = fd
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
        _write_hid_report(self.fd, self.path, report)

    def close(self):
        try:
            self.fd.close()
        except OSError:
            pass


class HidMouse:
    """Absolute pointer (touchscreen-style), not a relative-motion mouse --
    x/y are positions in [0, protocol.MOUSE_ABSOLUTE_MAX], matching the
    gadget's HID report descriptor (pi/gadget-setup.sh). The client
    computes where a click landed as a fraction of its own displayed video
    frame, bundles it with the current button bitmask, and sends both
    together as one MOUSE_STATE frame -- set_state() writes ONE atomic
    report reflecting both. Position and buttons used to be split across
    two separate frames/writes (a move, then a distinct button-down); that
    was a real, confirmed bug -- unreliable clicks specifically when the
    position didn't need to change (a real HID absolute pointer always
    reports a complete position+button snapshot per sample, never splits
    them), while dragging worked fine since every motion sample already
    changed the position. See protocol.py's MOUSE_STATE comment."""

    def __init__(self, fd, path):
        self.fd = fd
        self.path = path
        self.buttons = 0
        # Default to center so a stray button event before the first
        # position update (shouldn't happen given the client always sends
        # position before a click) doesn't pin the cursor to a corner.
        self.x = protocol.MOUSE_ABSOLUTE_MAX // 2
        self.y = protocol.MOUSE_ABSOLUTE_MAX // 2

    def set_state(self, x, y, buttons):
        self.x = x
        self.y = y
        self.buttons = buttons
        self._write_report()

    def scroll(self, amount):
        self._write_report(wheel=amount)

    def release_all_buttons(self):
        self.buttons = 0
        self._write_report()

    def _write_report(self, wheel=0):
        report = struct.pack("<BHHb", self.buttons & 0xFF, self.x, self.y, wheel)
        _write_hid_report(self.fd, self.path, report)

    def close(self):
        try:
            self.fd.close()
        except OSError:
            pass


HID_WRITE_RETRIES = 5
HID_WRITE_RETRY_DELAY_SECONDS = 0.002


def _write_hid_report(fd, path, report_bytes):
    """The fd is O_NONBLOCK (see _open_hidg_nonblock) so a host that isn't
    polling at all can't block the daemon forever -- but that same
    non-blocking mode means two reports written back-to-back faster than
    the gadget endpoint's small queue can drain (exactly what a plain
    click does: an absolute-position report immediately followed by a
    button-down report, with no gap between them) can hit a transient
    EAGAIN even though the host is polling completely normally. Retrying a
    few times with a tiny delay covers that normal case -- confirmed:
    without this, a quick click's position report would land but the
    button-down report right behind it would occasionally get dropped
    silently, moving the target's cursor without ever clicking, while
    dragging (whose motion reports naturally have gaps between them) was
    unaffected. Still bounded (~10ms worst case) so a genuinely
    non-polling host (e.g. a BIOS/UEFI screen) can't freeze the daemon."""
    for attempt in range(HID_WRITE_RETRIES):
        try:
            fd.write(report_bytes)
            return
        except BlockingIOError:
            if attempt == HID_WRITE_RETRIES - 1:
                log.warning("dropped HID report to %s: endpoint still busy after %d retries",
                            path, HID_WRITE_RETRIES)
                return
            time.sleep(HID_WRITE_RETRY_DELAY_SECONDS)
        except OSError as e:
            log.warning("failed to write HID report to %s: %s", path, e)
            return


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


class WifiApController:
    """Shells out to wifi-ap-toggle.sh (phase 2, only present if
    install-webui.sh has been run on this Pi) to switch the ISO-upload
    Wi-Fi AP on/off, and asks systemd whether hostapd is currently active
    to report status."""

    TOGGLE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wifi-ap-toggle.sh")
    TOGGLE_TIMEOUT_SECONDS = 30

    def is_installed(self):
        return os.path.isfile(self.TOGGLE_SCRIPT)

    def is_enabled(self):
        try:
            result = subprocess.run(["systemctl", "is-active", "--quiet", "hostapd"], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def set_enabled(self, enabled):
        """Runs wifi-ap-toggle.sh on/off. Raises RuntimeError with a short
        message on failure (script missing, non-zero exit, or timeout).

        Deliberately does NOT use subprocess.run(timeout=...): that only
        kills the *direct* child (bash) on timeout, not any grandchild it
        spawned (nmcli, hostapd, ...); if one of those is left holding the
        captured stdout/stderr pipes open, the read blocks forever no
        matter what timeout was requested. start_new_session=True puts the
        whole script in its own process group so a timeout can kill that
        entire group, not just bash."""
        if not self.is_installed():
            raise RuntimeError("Wi-Fi AP not installed (run install-webui.sh on the Pi)")

        mode = "on" if enabled else "off"
        proc = subprocess.Popen(
            [self.TOGGLE_SCRIPT, mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.TOGGLE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()  # reap now that the whole group is dead
            raise RuntimeError("timed out switching Wi-Fi AP mode (killed hung process group)")

        if proc.returncode != 0:
            message = (stderr or stdout or "unknown error").strip()
            raise RuntimeError(message[:200])


class ShellSession:
    """A real bash session in a real PTY -- like the shell half of an SSH
    connection, just tunneled over the same UART link as everything else
    instead of the network. Deliberately not a restricted/sandboxed
    command runner: there's no separate trust boundary to enforce here,
    since this is a single point-to-point wire the laptop already has to
    be physically wired to in order to send anything at all.

    One session at a time (matching the physical reality of this project
    -- only one client can be on the other end of the wire anyway).
    Opening while already open, or closing while already closed, are both
    safe no-ops, so the client doesn't need to track exact state to avoid
    double-open/double-close races.

    The output-reading thread is what actually detects the shell process
    exiting (a plain blocking os.read() on the PTY master returns EOF, or
    the read itself raises, once the child is gone) -- so close() just
    signals the child to exit and lets that same thread notice and clean
    up, whether the exit was requested by the client or the user just
    typed `exit` themselves. That keeps there being exactly one cleanup
    path instead of two slightly-different ones."""

    READ_CHUNK_SIZE = 4096

    def __init__(self, send_output, on_closed):
        self.send_output = send_output  # callback(bytes)
        self.on_closed = on_closed  # callback() -- called once, when the shell exits
        self.master_fd = None
        self.pid = None
        self._thread = None

    def is_open(self):
        return self.pid is not None

    def open(self):
        if pty is None:
            raise RuntimeError("pty module not available (daemon.py must run on Linux)")
        if self.is_open():
            return
        pid, fd = pty.fork()
        if pid == 0:
            # Child: replace this process image with a login-ish shell.
            # Falls back to sh if bash isn't installed for some reason.
            try:
                os.execvp("bash", ["bash"])
            except OSError:
                os.execvp("sh", ["sh"])
            os._exit(1)  # only reached if both execvp calls fail
        self.pid = pid
        self.master_fd = fd
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def resize(self, rows, cols):
        if self.master_fd is None or fcntl is None:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def write(self, data):
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def close(self):
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _read_loop(self):
        fd = self.master_fd
        while True:
            try:
                data = os.read(fd, self.READ_CHUNK_SIZE)
            except OSError:
                break
            if not data:
                break
            self.send_output(data)
        self._cleanup()
        self.on_closed()

    def _cleanup(self):
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.pid is not None:
            pid, self.pid = self.pid, None
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def _open_hidg_nonblock(path):
    """O_NONBLOCK so a write() that can't complete immediately (host not
    polling the endpoint) raises BlockingIOError instead of blocking --
    see the module docstring. _write_hid_report already catches OSError
    (BlockingIOError is a subclass), so no other code needs to change."""
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    return os.fdopen(fd, "wb", buffering=0)


class Daemon:
    def __init__(self):
        info = load_gadget_info()
        # buffering=0: raw, unbuffered binary I/O -- every .write() call is
        # an immediate syscall, with no Python-level buffer that could hold
        # a report back until it happens to fill up.
        keyboard_fd = _open_hidg_nonblock(info["hidg_keyboard"])
        mouse_fd = _open_hidg_nonblock(info["hidg_mouse"])
        self.keyboard = HidKeyboard(keyboard_fd, info["hidg_keyboard"])
        self.mouse = HidMouse(mouse_fd, info["hidg_mouse"])
        self.storage = StorageController(info["lun0_file_attr"], info["isos_dir"])
        self.wifi_ap = WifiApController()
        self.shell = ShellSession(
            send_output=lambda data: self._send(protocol.encode_shell_output(data)),
            on_closed=lambda: self._send(protocol.encode_shell_closed()),
        )
        self.parser = protocol.FrameParser()
        self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1, exclusive=True)
        # AP commands run on background threads (see module docstring), so
        # replies can now arrive from either the main loop or one of those
        # threads -- serialize access to the one shared serial handle.
        self._send_lock = threading.Lock()
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
        self.shell.close()
        self.keyboard.close()
        self.mouse.close()
        self.ser.close()

    def _send_startup_state(self):
        if self.storage.current_iso:
            self._send(protocol.encode_iso_mounted(self.storage.current_iso))
        else:
            self._send(protocol.encode_iso_ejected())

    def _send(self, frame_bytes):
        with self._send_lock:
            try:
                self.ser.write(frame_bytes)
            except serial.SerialException as e:
                log.warning("serial write failed: %s", e)

    def _run_ap_async(self, frame_type, action):
        """Runs an AP-related action (enable/disable/status-check) on a
        background thread, then replies. These shell out to
        wifi-ap-toggle.sh/systemctl, which can legitimately take a few
        seconds -- or, if something upstream hangs, far longer than its
        nominal timeout (see WifiApController.set_enabled and the module
        docstring). Never run this inline on the main loop: keyboard/mouse
        would freeze along with it for however long that turns out to be."""
        def worker():
            try:
                action()
                self._send(protocol.encode_ap_status(self.wifi_ap.is_enabled()))
            except Exception as e:
                self._send(protocol.encode_error(frame_type, str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _handle_frame(self, frame_type, payload):
        p = protocol
        try:
            if frame_type == p.KEY_DOWN:
                self.keyboard.key_down(payload[0])
            elif frame_type == p.KEY_UP:
                self.keyboard.key_up(payload[0])
            elif frame_type == p.MOUSE_STATE:
                x, y, buttons = p.decode_mouse_state(payload)
                self.mouse.set_state(x, y, buttons)
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
            elif frame_type == p.AP_ENABLE:
                self._run_ap_async(frame_type, lambda: self.wifi_ap.set_enabled(True))
            elif frame_type == p.AP_DISABLE:
                self._run_ap_async(frame_type, lambda: self.wifi_ap.set_enabled(False))
            elif frame_type == p.AP_STATUS_QUERY:
                self._run_ap_async(frame_type, lambda: None)
            elif frame_type == p.SHELL_OPEN:
                self.shell.open()
            elif frame_type == p.SHELL_CLOSE:
                self.shell.close()
            elif frame_type == p.SHELL_INPUT:
                self.shell.write(payload)
            elif frame_type == p.SHELL_RESIZE:
                rows, cols = p.decode_shell_resize(payload)
                self.shell.resize(rows, cols)
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
